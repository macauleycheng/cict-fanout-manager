"""topo_manager 群組帳本持久化（groups/store.py，群組設計 §持久化）。

``GroupStore`` 子類化 ``JsonSessionStore``，重用其原子且 durable 寫入
（``_write_atomic``）、跨行程鎖（``locked``）與 ``load_raw``；帳本另開檔
（預設 ``.topo-state/groups.json``，version=1、kind="group"），與打流側
version-2 帳本、互聯 version-1 帳本三方隔離：
  - ``GroupStore.load`` 遇 version != 1 或 kind 不符一律丟 ``SessionStoreError``
    （含路徑與修復指引，保留 ``__cause__``）。
  - ``save(groups)`` 不帶 next_seq（群組 name 由使用者給定，無序號高水位概念）。

寫入交易一律外包父類 ``self.locked()``：取鎖 → 鎖內 reload → 決策 → save →
釋鎖，與 ``Allocator.allocate/release`` 同一模式；鎖檔後綴沿用預設 ``.lock``。

歷史（HISTORY）：
  - 0.7.0  群組功能初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, NamedTuple, Union

from pydantic import ValidationError

from topo_manager.errors import SessionStoreError
from topo_manager.groups.models import (
    GROUP_LEDGER_KIND,
    GROUP_SCHEMA_VERSION,
    GroupEntry,
    GroupLedger,
)
from topo_manager.store import JsonSessionStore

# 群組帳本預設路徑（相對於工作目錄；.topo-state/ 已在 .gitignore）。
_DEFAULT_GROUP_STATE_PATH = ".topo-state/groups.json"


class GroupLoaded(NamedTuple):
    """``GroupStore.load()`` 回傳容器：還原的群組清單。"""

    groups: List[GroupEntry]


class GroupStore(JsonSessionStore):
    """群組帳本儲存層（子類化 JsonSessionStore）。"""

    def __init__(self, path: Union[str, Path] = _DEFAULT_GROUP_STATE_PATH) -> None:
        """記錄群組帳本檔路徑（預設 ``.topo-state/groups.json``）。"""
        super().__init__(path)

    def load(self) -> GroupLoaded:  # type: ignore[override]
        """讀取並驗證群組帳本；檔案不存在回空容器。

        回傳：
          - ``GroupLoaded(groups)``；檔案不存在時回 ``GroupLoaded(groups=[])``。

        例外：
          - SessionStoreError：I/O 失敗、非 UTF-8、非合法 JSON、頂層非 object、
            version/kind 不符（提示這不是群組帳本）、schema 驗證失敗。
        """
        if not self._path.exists():
            return GroupLoaded(groups=[])
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SessionStoreError(
                "群組帳本非合法 UTF-8（{0}）：{1}".format(self._path, exc)
            ) from exc
        except OSError as exc:
            raise SessionStoreError(
                "無法讀取群組帳本（{0}）：{1}".format(self._path, exc)
            ) from exc
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SessionStoreError(
                "群組帳本非合法 JSON（{0}）：{1}（line {2}, column {3}）".format(
                    self._path, exc.msg, exc.lineno, exc.colno
                )
            ) from exc
        if not isinstance(data, dict):
            raise SessionStoreError(
                "群組帳本頂層須為 JSON object（{0}）：實得 {1}".format(
                    self._path, type(data).__name__
                )
            )
        version = data.get("version")
        kind = data.get("kind")
        if version != GROUP_SCHEMA_VERSION or kind != GROUP_LEDGER_KIND:
            raise SessionStoreError(
                "這不是群組帳本（{0}）：version={1}、kind={2}，群組帳本須為 "
                "version={3}、kind='{4}'；請以 --state 指定群組狀態檔".format(
                    self._path, version, kind, GROUP_SCHEMA_VERSION,
                    GROUP_LEDGER_KIND,
                )
            )
        try:
            ledger = GroupLedger.model_validate(data)
        except ValidationError as exc:
            raise SessionStoreError(
                "群組帳本 schema 驗證失敗（{0}）：{1}；請以 'list --raw --state "
                "<狀態檔>' 檢視原始帳本後修正".format(self._path, exc)
            ) from exc
        return GroupLoaded(groups=list(ledger.groups))

    def save(self, groups: Iterable[GroupEntry]) -> None:  # type: ignore[override]
        """原子且 durable 寫入群組帳本（重用 ``_write_atomic``）。

        參數：
          - groups：要寫入的群組可迭代集合。

        例外：
          - SessionStoreError：寫入失敗（已清 temp 檔）。
        """
        ledger = GroupLedger(
            version=GROUP_SCHEMA_VERSION,
            kind=GROUP_LEDGER_KIND,
            groups=list(groups),
        )
        payload = json.dumps(ledger.model_dump(), ensure_ascii=False, indent=2)
        self._write_atomic(payload)
