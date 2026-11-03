"""topo_manager JSON 持久化層（store.py，設計 §持久化、FR-5、Phase 2 Hardening）。

本模組把 Allocator 的 session ledger 以單一 JSON 檔持久化，供斷電/重啟後
還原既有分配狀態（FR-5.6）。只用標準函式庫（json / tempfile / os / signal /
fcntl / pathlib / contextlib），不引入任何新相依。

設計重點：
  - ledger 以 ``version`` 欄位標記 schema 版本（``SCHEMA_VERSION``），序列化後
    即 JSON 頂層 ``version`` key；load 時先讀頂層 ``version`` 與
    ``SCHEMA_VERSION`` 常數比對，再做 schema 驗證，讓格式演進可明確拒絕。
  - ledger 頂層另帶 ``next_seq``（序號高水位，可選），確保序號只增不減、
    釋放最高序號後重啟不重用（#11）。
  - save 採「同目錄 tempfile + fsync + os.replace + 父目錄 fsync」的原子且
    durable 寫入；首次建立狀態目錄時逐層對新目錄的父 fsync（#3）。
  - 跨行程併發以 ``fcntl.flock`` 對同目錄的 lock 檔取排他鎖（#1），搭配
    ``signal.alarm`` 逾時保護；``locked()`` context manager 供 allocator 編排
    「取鎖 → 鎖內 reload → 決策 → save → 釋鎖」的跨行程交易。
  - 所有讀寫面 I/O 失敗（含權限、路徑、磁碟滿、唯讀、非 UTF-8）一律轉成
    ``SessionStoreError``（含檔案路徑與原因，保留 ``__cause__``）。

已知限制（NOTES）：
  - ledger 與實際拓樸的一致性由 allocator 還原層負責；本層只保證 ledger 檔
    本身格式正確、I/O 失敗被妥善包裝。
  - ``locked(timeout)`` 的 ``timeout`` 以 ``signal.alarm`` 實作，僅支援整數秒
    （截斷小數、不支援次秒級），且 SIGALRM 僅主執行緒可用，故併發須多行程。

歷史（HISTORY）：
  - 0.2.0  FEAT-005 初版，建立 SCHEMA_VERSION / Ledger 模型 /
           JsonSessionStore（load 版本與 schema 檢查、save 原子寫入）。
  - 0.3.0  Phase 2 Hardening：SCHEMA_VERSION 1→2；Ledger 加 next_seq；
           load() 包 I/O 例外並回 LoadedLedger NamedTuple；save(sessions,
           next_seq) 加必填 next_seq、改以 _ensure_dir_durable 建目錄+逐層
           fsync、包 I/O 例外、temp cleanup 不先 exists()；新增 locked()
           跨行程檔案鎖、load_raw()、_ensure_dir_durable()、_open_lock_fd()、
           _fsync_dir()。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Union,
)

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from topo_manager.errors import SessionStoreError

if TYPE_CHECKING:
    from topo_manager.allocator import Session

# ledger schema 版本；序列化後即 JSON 頂層 ``version`` key（非
# ``schema_version``），值為本常數。load 時嚴格比對頂層 ``version`` key 與本
# 常數，不符即拒絕（FR-5、#15）。
SCHEMA_VERSION = 2

# 預設狀態檔路徑（相對於當前工作目錄，須列入 .gitignore）。
_DEFAULT_STATE_PATH = ".topo-state/allocations.json"


class LoadedLedger(NamedTuple):
    """``JsonSessionStore.load()`` 的回傳容器（#11）。

    欄位：
      - next_seq：ledger 頂層序號高水位（可能為 None，表舊/缺欄位帳本，
        由 allocator 退回「active 最大序號 + 1」推導）。
      - sessions：還原的 session 清單。
    """

    next_seq: Optional[int]
    sessions: List["Session"]


class Ledger(BaseModel):
    """持久化 ledger 的頂層容器模型（設計 §持久化）。

    欄位：
      - version：ledger schema 版本（int），須等於 ``SCHEMA_VERSION``；
        序列化後即 JSON 頂層 ``version`` key。
      - next_seq：session 序號高水位（Optional[int]，``>= 1``）；缺值（None）
        時由 allocator 退回「active 最大序號 + 1」推導（#11）。
      - sessions：session 清單；型別以字串前置參照 ``allocator.Session``，
        避免與 allocator 互相 import 造成循環相依，load 時再
        ``model_rebuild()`` 解析。

    以 ``extra='forbid'`` 拒絕未知頂層欄位，確保格式嚴謹（例如 v1 殘留的
    ``access_ports`` 會被擋下）。
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    next_seq: Optional[int] = Field(default=None, ge=1)
    sessions: List["Session"]


class JsonSessionStore:
    """以單一 JSON 檔持久化 session ledger 的儲存層（設計 §持久化）。

    狀態封裝於實例屬性（不使用全域可變變數）：
      - ``self._path``：ledger 檔的 ``pathlib.Path``。
    """

    class _LockTimeout(Exception):
        """flock 取鎖逾時的內部訊號（僅 locked() 內使用，不對外匯出）。"""

    def __init__(self, path: Union[str, Path] = _DEFAULT_STATE_PATH) -> None:
        """建立儲存層並記錄 ledger 檔路徑（設計 §持久化）。

        參數：
          - path：ledger 檔路徑（str 或 Path）；預設
            ``.topo-state/allocations.json``（相對於當前工作目錄）。
        """
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """回傳 ledger 檔的 ``pathlib.Path``（供呼叫端顯示實際狀態檔位置）。"""
        return self._path

    # ------------------------------------------------------------------
    # durability 輔助（目錄 fsync / 建目錄）
    # ------------------------------------------------------------------

    def _fsync_dir(self, dir_path: Path) -> None:
        """對目錄本身 fsync，使其目錄項落盤（#3）。

        以 ``os.open(..., O_DIRECTORY)`` 取得目錄 fd，``os.fsync`` 後
        ``try/finally`` 關閉 fd。``OSError`` 由呼叫端包成 ``SessionStoreError``。
        """
        dir_fd = os.open(str(dir_path), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _ensure_dir_durable(self, dir_path: Path) -> None:
        """確保 dir_path（及其所有新建祖先）存在且其目錄項已 durable（FR-H3.2）。

        冪等：dir_path 已存在時 missing 為空、不建任何目錄、不做任何 fsync，
        直接返回（第二次以後的呼叫即走此快路徑）。

        失敗：mkdir 或任一層 fsync 的 OSError 由呼叫端的 save()/locked() 包成
        SessionStoreError（FR-H3.3）；本函式不自行吞例外。
        """
        # mkdir 前，由下而上收集「本次會新建」的祖先鏈（尚不存在者）。
        missing: List[Path] = []
        node = dir_path
        while not node.exists():
            missing.append(node)
            if node.parent == node:          # 抵達根（/ 或 .），停止
                break
            node = node.parent
        if not missing:                      # 目錄已存在 → 冪等 no-op
            return
        dir_path.mkdir(parents=True, exist_ok=True)
        # 對每個新建目錄的「父目錄」各 fsync 一次，使新目錄項 durable；
        # 由上而下（reversed）確保先讓上層目錄項落盤。
        for created in reversed(missing):
            self._fsync_dir(created.parent)

    def _open_lock_fd(self, lock_path: str) -> int:
        """開啟（必要時建立）lock 檔並回傳 fd；父目錄缺失時以
        _ensure_dir_durable 建之（含 durable fsync），確保無論哪一步先建
        目錄，新目錄項都 durable（#1）。
        """
        try:
            return os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except FileNotFoundError:
            # 父目錄尚不存在：durable 建之（含逐層 fsync）再開 lock 檔。
            self._ensure_dir_durable(self._path.parent)
            return os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)

    # ------------------------------------------------------------------
    # 跨行程檔案鎖（#1）
    # ------------------------------------------------------------------

    @contextmanager
    def locked(self, timeout: int = 10) -> Iterator[None]:
        """取得 ledger 的跨行程排他鎖，離開時釋放（#1）。

        以 ``fcntl.flock(fd, LOCK_EX)`` 阻塞取鎖，並用 ``signal.SIGALRM`` 設
        逾時（預設 10 秒）包住阻塞呼叫，逾時轉 ``SessionStoreError``。取鎖
        成功後、臨界區內呼叫 ``_ensure_dir_durable(parent)``（冪等），使「建
        目錄 + fsync」落在 flock 序列化範圍內。

        ``timeout`` 為整數秒（``signal.alarm`` 限制，僅接受整數並截斷小數，
        不支援次秒級粒度）；傳入非整數會被 ``signal.alarm`` 靜默截斷。本專案
        所有呼叫一律用整數（預設 10 秒）。

        例外語意（N#5）：``except OSError`` 僅攔「取鎖本身」的 OSError
        （``_open_lock_fd``/``flock``）；臨界區（``yield`` 之後、呼叫端於鎖內
        執行的程式碼）拋出的例外**原樣穿透**不被攔截，僅 ``finally`` 負責釋鎖
        （還原 handler、關 fd）。前置不變量：``save()`` 已就地把所有 I/O
        OSError 包成 ``SessionStoreError``，故臨界區內的 save 失敗以
        ``SessionStoreError`` 形式穿透，不會被此處的 ``except OSError`` 誤攔。
        """
        lock_path = str(self._path) + ".lock"
        try:
            fd = self._open_lock_fd(lock_path)
        except OSError as exc:
            raise SessionStoreError(
                "取鎖失敗（{0}）：{1}".format(lock_path, exc)
            ) from exc

        def _on_alarm(signum, frame):
            raise JsonSessionStore._LockTimeout()

        prev_handler = signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(timeout)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except JsonSessionStore._LockTimeout as exc:
                raise SessionStoreError(
                    "取鎖逾時（{0} 秒）：{1}".format(timeout, lock_path)
                ) from exc
            except OSError as exc:
                raise SessionStoreError(
                    "取鎖失敗（{0}）：{1}".format(lock_path, exc)
                ) from exc
            signal.alarm(0)                 # 取鎖成功，立即取消計時器
            # 取鎖後、臨界區內再 durable 建目錄一次（冪等 no-op 若已建）。
            self._ensure_dir_durable(self._path.parent)
            yield
        finally:
            signal.alarm(0)                             # 保險：確保計時器清除
            signal.signal(signal.SIGALRM, prev_handler)  # 還原原 handler
            os.close(fd)                                # 關 fd 即釋放 flock

    # ------------------------------------------------------------------
    # 載入
    # ------------------------------------------------------------------

    def load(self) -> "LoadedLedger":
        """讀取並還原 ledger，回傳 ``LoadedLedger``（設計 §持久化、FR-5.6）。

        讀取 JSON 頂層 ``version`` key 與 ``SCHEMA_VERSION`` 常數比對；讀檔
        I/O 錯與非 UTF-8 一律轉 ``SessionStoreError``（保留 chaining）。

        回傳：
          - ``LoadedLedger(next_seq, sessions)``；檔案不存在時回
            ``LoadedLedger(next_seq=None, sessions=[])``。

        例外：
          - SessionStoreError：讀檔 I/O 失敗、非 UTF-8、非合法 JSON、頂層型別
            錯、版本不支援、或 schema 驗證失敗（訊息含檔案路徑與原因）。
        """
        # 延後 import 以打破 store ↔ allocator 的循環相依，並解析 Ledger 的
        # 前置參照。
        from topo_manager.allocator import Session  # noqa: F401

        Ledger.model_rebuild()

        if not self._path.exists():
            return LoadedLedger(next_seq=None, sessions=[])

        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SessionStoreError(
                "ledger 檔非合法 UTF-8（{0}）：{1}".format(self._path, exc)
            ) from exc
        except OSError as exc:
            raise SessionStoreError(
                "無法讀取 ledger 檔（{0}）：{1}".format(self._path, exc)
            ) from exc

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SessionStoreError(
                "ledger 檔非合法 JSON（{0}）：{1}（line {2}, column {3}）".format(
                    self._path, exc.msg, exc.lineno, exc.colno
                )
            ) from exc

        # 先檢查頂層型別與版本，再做完整 schema 驗證（版本不符要能明確拒絕）。
        if not isinstance(data, dict):
            raise SessionStoreError(
                "ledger 檔頂層須為 JSON object（{0}）：實得 {1}".format(
                    self._path, type(data).__name__
                )
            )

        version = data.get("version")
        if version != SCHEMA_VERSION:
            raise SessionStoreError(
                "ledger 版本不支援（{0}）：實得 {1}，本程式僅支援 {2}".format(
                    self._path, version, SCHEMA_VERSION
                )
            )

        try:
            ledger = Ledger.model_validate(data)
        except ValidationError as exc:
            # M#2：version 正確但 schema 驗證失敗，最常見是殘留舊欄位（如
            # access_ports 被 extra='forbid' 擋下）；補泛用修復指引。
            if version == SCHEMA_VERSION:
                raise SessionStoreError(
                    "ledger schema 驗證失敗（{0}）：{1}；疑似舊格式殘留，"
                    "請以 'list --raw --state <狀態檔>' 檢視並移除".format(
                        self._path, exc
                    )
                ) from exc
            raise SessionStoreError(
                "ledger schema 驗證失敗（{0}）：{1}".format(self._path, exc)
            ) from exc

        return LoadedLedger(
            next_seq=ledger.next_seq, sessions=list(ledger.sessions)
        )

    def load_raw(self) -> dict:
        """原始讀取 ledger JSON，不做版本/schema/拓樸檢查（#12）。

        供 ``list --raw`` 在帳本版本不符/不一致或拓樸損壞時仍能唯讀檢視原始
        內容。只處理「檔案不存在→回空結構」與「非法 JSON / 讀檔 I/O 失敗 /
        非 UTF-8 →``SessionStoreError``」。

        回傳：
          - 原始 dict；檔案不存在時回
            ``{"version": None, "next_seq": None, "sessions": []}``。

        例外：
          - SessionStoreError：讀檔 I/O 失敗、非 UTF-8、或非合法 JSON（含路徑
            與修復指引，保留 chaining）。
        """
        if not self._path.exists():
            return {"version": None, "next_seq": None, "sessions": []}

        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SessionStoreError(
                "ledger 檔非合法 UTF-8（{0}）：{1}；"
                "請以 'list --raw --state <狀態檔>' 檢視".format(self._path, exc)
            ) from exc
        except OSError as exc:
            raise SessionStoreError(
                "無法讀取 ledger 檔（{0}）：{1}；"
                "請以 'list --raw --state <狀態檔>' 檢視".format(self._path, exc)
            ) from exc

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SessionStoreError(
                "ledger 檔非合法 JSON（{0}）：{1}（line {2}, column {3}）；"
                "請以 'list --raw --state <狀態檔>' 檢視原始帳本後修正".format(
                    self._path, exc.msg, exc.lineno, exc.colno
                )
            ) from exc

    # ------------------------------------------------------------------
    # 寫入
    # ------------------------------------------------------------------

    def save(self, sessions: Iterable["Session"], next_seq: int) -> None:
        """原子且 durable 寫入 session ledger（設計 §持久化、#3/#11）。

        以「同目錄 tempfile + flush + fsync + os.replace + 父目錄 fsync」確保
        原子性與斷電安全：寫入過程中斷電時，目標檔不是舊內容就是新完整內容，
        不會半截損毀；``os.replace`` 後對父目錄 fsync 使換名落盤。首次建立
        狀態目錄時由 ``_ensure_dir_durable`` 逐層對新目錄的父 fsync（FR-H3.2）。

        參數：
          - sessions：要寫入的 session 可迭代集合。
          - next_seq：序號高水位（必填位置參數），寫入 ledger 頂層
            ``next_seq``；漏傳會在呼叫處立即 ``TypeError``（#11）。

        例外：
          - SessionStoreError：寫入任一步（建目錄/mkstemp/write/fsync/replace/
            目錄 fsync）的 OSError 一律包成本例外（保留 chaining），失敗前已
            清殘留 temp 檔。
        """
        # 延後 import 以打破循環相依並解析 Ledger 前置參照。
        from topo_manager.allocator import Session  # noqa: F401

        Ledger.model_rebuild()

        ledger = Ledger(
            version=SCHEMA_VERSION, next_seq=next_seq, sessions=list(sessions)
        )
        payload = json.dumps(
            ledger.model_dump(), ensure_ascii=False, indent=2
        )

        tmp_path: Optional[Path] = None
        try:
            self._ensure_dir_durable(self._path.parent)
            # 在目標檔同一目錄建立 temp 檔，確保 os.replace 為同檔案系統內的
            # 原子換名。
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=".allocations-",
                suffix=".tmp",
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp_path), str(self._path))
            self._fsync_dir(self._path.parent)
            tmp_path = None                  # replace 成功，temp 已消失
        except OSError as exc:
            raise SessionStoreError(
                "寫入 ledger 失敗（{0}）：{1}".format(self._path, exc)
            ) from exc
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()        # 不先 exists()，直接嘗試刪
                except OSError:
                    pass                     # cleanup 盡力而為，不掩蓋主例外
