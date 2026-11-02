"""topo_manager JSON 持久化層（store.py，設計 §持久化、FR-5）。

本模組把 Allocator 的 session ledger 以單一 JSON 檔持久化，供斷電/重啟後
還原既有分配狀態（FR-5.6）。只用標準函式庫（json / tempfile / os /
pathlib），不引入任何新相依。

設計重點：
  - ledger 以 ``version`` 欄位標記 schema 版本（``SCHEMA_VERSION``），load
    時先比對版本再做 schema 驗證，讓未來格式演進可明確拒絕舊/新檔。
  - save 採「同目錄 tempfile + fsync + os.replace」的原子寫入，確保寫入
    過程中斷電不會留下半截損毀的 ledger（舊檔完好或新檔完整，二擇一）。
  - 所有讀取面失敗一律轉成 ``SessionStoreError``（含檔案路徑與原因）。

已知限制（NOTES）：
  - ledger 與實際拓樸的一致性由使用者負責：本層不驗證 ledger 內的 port /
    path 是否仍符合當前拓樸 YAML，只保證 ledger 檔本身格式正確。
  - 跨行程一致性假設所有操作使用同一工作目錄（預設相對路徑），或一致地
    以 ``--state`` 明確指定同一狀態檔；本層不做檔案鎖。

歷史（HISTORY）：
  - 0.2.0  FEAT-005 初版，建立 SCHEMA_VERSION / Ledger 模型 /
           JsonSessionStore（load 版本與 schema 檢查、save 原子寫入）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Union

from pydantic import BaseModel, ConfigDict, ValidationError

from topo_manager.errors import SessionStoreError

if TYPE_CHECKING:
    from topo_manager.allocator import Session

# ledger schema 版本；load 時嚴格比對，不符即拒絕（FR-5）。
SCHEMA_VERSION = 1

# 預設狀態檔路徑（相對於當前工作目錄，須列入 .gitignore）。
_DEFAULT_STATE_PATH = ".topo-state/allocations.json"


class Ledger(BaseModel):
    """持久化 ledger 的頂層容器模型（設計 §持久化）。

    欄位：
      - version：ledger schema 版本（int），須等於 ``SCHEMA_VERSION``。
      - sessions：session 清單；型別以字串前置參照 ``allocator.Session``，
        避免與 allocator 互相 import 造成循環相依，load 時再
        ``model_rebuild()`` 解析。

    以 ``extra='forbid'`` 拒絕未知頂層欄位，確保格式嚴謹。
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    sessions: List["Session"]


class JsonSessionStore:
    """以單一 JSON 檔持久化 session ledger 的儲存層（設計 §持久化）。

    狀態封裝於實例屬性（不使用全域可變變數）：
      - ``self._path``：ledger 檔的 ``pathlib.Path``。
    """

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

    def load(self) -> List["Session"]:
        """讀取並還原 ledger，回傳 session 清單（設計 §持久化、FR-5.6）。

        回傳：
          - ledger 內的 session 清單；檔案不存在時回傳空 list。

        例外：
          - SessionStoreError：檔案非合法 JSON、版本欄位缺漏/不支援、或
            schema 驗證失敗（訊息含檔案路徑與原因）。
        """
        # 延後 import 以打破 store ↔ allocator 的循環相依，並解析 Ledger 的
        # 前置參照。
        from topo_manager.allocator import Session

        Ledger.model_rebuild()

        if not self._path.exists():
            return []

        raw_text = self._path.read_text(encoding="utf-8")

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
            raise SessionStoreError(
                "ledger schema 驗證失敗（{0}）：{1}".format(self._path, exc)
            ) from exc

        return list(ledger.sessions)

    def save(self, sessions: Iterable["Session"]) -> None:
        """原子寫入 session ledger（設計 §持久化）。

        以「同目錄 tempfile + flush + fsync + os.replace」確保原子性：寫入
        過程中斷電時，目標檔不是舊內容就是新完整內容，不會半截損毀。

        參數：
          - sessions：要寫入的 session 可迭代集合。

        例外：
          - OSError 等底層寫入錯誤會原樣往外拋（已先清掉殘留 temp 檔）。
        """
        # 延後 import 以打破循環相依並解析 Ledger 前置參照。
        from topo_manager.allocator import Session  # noqa: F401

        Ledger.model_rebuild()

        ledger = Ledger(version=SCHEMA_VERSION, sessions=list(sessions))
        payload = json.dumps(
            ledger.model_dump(), ensure_ascii=False, indent=2
        )

        self._path.parent.mkdir(parents=True, exist_ok=True)

        # 在目標檔同一目錄建立 temp 檔，確保 os.replace 為同檔案系統內的
        # 原子換名。
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=".allocations-",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp_path), str(self._path))
        finally:
            # os.replace 成功後 tmp_path 已不存在；此處只清理失敗殘留。
            if tmp_path.exists():
                tmp_path.unlink()
