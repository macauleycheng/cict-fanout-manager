"""topo_manager 互聯帳本持久化（interconnect/store.py，互聯設計 §4）。

``IcSessionStore`` 子類化 ``JsonSessionStore``，重用其原子寫入
（``_write_atomic``）、跨行程鎖（``locked``）與 ``load_raw``；帳本另開檔
（預設 ``.topo-state/interconnect.json``，version=1、kind="interconnect"），
與打流側 version-2 帳本完全隔離：
  - ``IcSessionStore.load`` 遇 version != 1 或 kind 不符一律丟
    ``SessionStoreError``（含誤指向打流側帳本）。
  - 互聯帳本餵給既有 ``JsonSessionStore`` 會因 version != 2 被明確拒絕。
  - ``device_locked()``：序列化所有會寫設備動作的設備鎖（``.device.lock``，
    預設逾時 120 秒）；鎖序固定「設備鎖（外）→ 帳本鎖（內）」。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import json
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Union

from pydantic import ValidationError

from topo_manager.errors import SessionStoreError
from topo_manager.interconnect.models import (
    IC_LEDGER_KIND,
    IC_SCHEMA_VERSION,
    IcLedger,
    IcSession,
)
from topo_manager.store import JsonSessionStore

# 互聯帳本預設路徑（相對於工作目錄；.topo-state/ 已在 .gitignore）。
_DEFAULT_IC_STATE_PATH = ".topo-state/interconnect.json"
# 設備鎖後綴與預設逾時秒數。
_DEVICE_LOCK_SUFFIX = ".device.lock"
_DEVICE_LOCK_TIMEOUT = 120


class IcLoaded(NamedTuple):
    """``IcSessionStore.load()`` 回傳容器：序號高水位（可為 None）與 session 清單。"""

    next_seq: Optional[int]
    sessions: List[IcSession]


class IcSessionStore(JsonSessionStore):
    """互聯帳本儲存層（子類化 JsonSessionStore）。"""

    def __init__(self, path: Union[str, Path] = _DEFAULT_IC_STATE_PATH) -> None:
        """記錄互聯帳本檔路徑（預設 ``.topo-state/interconnect.json``）。"""
        super().__init__(path)

    def device_locked(self, timeout: int = _DEVICE_LOCK_TIMEOUT) -> AbstractContextManager:
        """回傳設備鎖 context manager（``<path>.device.lock``，預設 120 秒）。"""
        return self.locked(timeout, lock_suffix=_DEVICE_LOCK_SUFFIX)

    def load(self) -> IcLoaded:  # type: ignore[override]
        """讀取並驗證互聯帳本；檔案不存在回空容器。

        例外：
          - SessionStoreError：I/O 失敗、非 UTF-8、非合法 JSON、頂層非 object、
            version/kind 不符（提示這不是互聯帳本）、schema 驗證失敗。
        """
        if not self._path.exists():
            return IcLoaded(next_seq=None, sessions=[])
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SessionStoreError(
                "互聯帳本非合法 UTF-8（{0}）：{1}".format(self._path, exc)
            ) from exc
        except OSError as exc:
            raise SessionStoreError(
                "無法讀取互聯帳本（{0}）：{1}".format(self._path, exc)
            ) from exc
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SessionStoreError(
                "互聯帳本非合法 JSON（{0}）：{1}（line {2}, column {3}）".format(
                    self._path, exc.msg, exc.lineno, exc.colno
                )
            ) from exc
        if not isinstance(data, dict):
            raise SessionStoreError(
                "互聯帳本頂層須為 JSON object（{0}）：實得 {1}".format(
                    self._path, type(data).__name__
                )
            )
        version = data.get("version")
        kind = data.get("kind")
        if version != IC_SCHEMA_VERSION or kind != IC_LEDGER_KIND:
            raise SessionStoreError(
                "這不是互聯帳本（{0}）：version={1}、kind={2}，互聯帳本須為 "
                "version={3}、kind='{4}'；請以 --state 指定互聯狀態檔".format(
                    self._path, version, kind, IC_SCHEMA_VERSION, IC_LEDGER_KIND
                )
            )
        try:
            ledger = IcLedger.model_validate(data)
        except ValidationError as exc:
            raise SessionStoreError(
                "互聯帳本 schema 驗證失敗（{0}）：{1}；請以 'list --raw --state "
                "<狀態檔>' 檢視原始帳本後修正".format(self._path, exc)
            ) from exc
        return IcLoaded(next_seq=ledger.next_seq, sessions=list(ledger.sessions))

    def save(  # type: ignore[override]
        self, sessions: Iterable[IcSession], next_seq: int
    ) -> None:
        """原子且 durable 寫入互聯帳本（重用 ``_write_atomic``）。

        例外：
          - SessionStoreError：寫入失敗（已清 temp 檔）。
        """
        ledger = IcLedger(
            version=IC_SCHEMA_VERSION,
            kind=IC_LEDGER_KIND,
            next_seq=next_seq,
            sessions=list(sessions),
        )
        payload = json.dumps(ledger.model_dump(), ensure_ascii=False, indent=2)
        self._write_atomic(payload)
