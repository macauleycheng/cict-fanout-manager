"""topo_manager 群組業務層（groups/registry.py，群組設計 §業務層）。

``GroupRegistry`` 持有一個 ``GroupStore``，在記憶體維護兩個 derived index：
  - ``self._groups``：Dict[name, GroupEntry]。
  - ``self._owner``：Dict[session_id, name]（強制「一 session 至多一群」）。

``__init__`` load 一次供唯讀路徑用（建構期快照，不取鎖）；所有寫方法於鎖內
``_reload_locked()`` 重建 index 後再決策（對齊 ``Allocator``）：取鎖 → 鎖內
reload → 決策 → save → 釋鎖；save 失敗一律回捲記憶體狀態後重拋。

NOTES：
  - 「一 session 至多一群」不變量於執行期（``add_member``）與還原期
    （``_reload_locked``）用同一套檢查；還原期偵測到同一 session_id 出現於兩
    群時丟 ``SessionStoreError``。

歷史（HISTORY）：
  - 0.7.0  群組功能初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from topo_manager.errors import (
    GroupExistsError,
    GroupMembershipError,
    GroupNotFoundError,
    SessionStoreError,
)
from topo_manager.groups.models import GroupEntry
from topo_manager.groups.store import GroupStore


class GroupRegistry:
    """群組業務層：CRUD + 成員維護（群組設計 §業務層）。

    狀態封裝於實例屬性（不使用全域可變變數）：
      - ``self._store``：持久化層（``GroupStore``）。
      - ``self._groups``：Dict[name, GroupEntry]，name → entry。
      - ``self._owner``：Dict[session_id, name]，session_id → 所屬群組 name。
    """

    def __init__(self, store: GroupStore) -> None:
        """載入群組帳本一次並建立 derived index（建構期快照，供唯讀路徑）。

        參數：
          - store：持久化層（``GroupStore``）。

        例外：
          - SessionStoreError：帳本讀取/驗證失敗，或同一 session_id 出現於兩群
            （還原期不變量，由 ``_rebuild_indexes`` 偵測）。
        """
        self._store = store
        self._groups: Dict[str, GroupEntry] = {}
        self._owner: Dict[str, str] = {}
        self._rebuild_indexes(self._store.load().groups)

    # ------------------------------------------------------------------
    # derived index 重建
    # ------------------------------------------------------------------

    def _rebuild_indexes(self, groups: List[GroupEntry]) -> None:
        """由還原的群組清單重建 ``_groups`` 與 ``_owner`` 兩個 index。

        還原期不變量：同一 session_id 不得同時出現於兩個群組；違反即丟
        ``SessionStoreError``（含衝突的 session_id 與兩群名）。

        參數：
          - groups：store.load() 回傳的群組清單。
        """
        self._groups = {}
        self._owner = {}
        for group in groups:
            self._groups[group.name] = group
            for session_id in group.members:
                existing = self._owner.get(session_id)
                if existing is not None and existing != group.name:
                    raise SessionStoreError(
                        "群組帳本還原不一致：session {0} 同時屬於群組 {1} 與 "
                        "{2}；請以 'list --raw --state <狀態檔>' 檢視後修正".format(
                            session_id, existing, group.name
                        )
                    )
                self._owner[session_id] = group.name

    def _reload_locked(self) -> None:
        """鎖內以最新帳本重建全部 derived index（對齊 ``Allocator._reload_locked``）。

        前置：呼叫端已在 ``store.locked()`` 臨界區內。丟棄建構期快照，吃鎖內
        ``store.load()`` 的最新帳本，確保併發/交錯下決策基於最新狀態。
        """
        self._rebuild_indexes(self._store.load().groups)

    # ------------------------------------------------------------------
    # 唯讀（不取鎖，用建構期快照）
    # ------------------------------------------------------------------

    def list_groups(self) -> List[GroupEntry]:
        """回傳所有群組（建構期快照，不取鎖）。"""
        return list(self._groups.values())

    def get_group(self, name: str) -> GroupEntry:
        """取回指定群組；查無丟 ``GroupNotFoundError``（建構期快照，不取鎖）。

        參數：
          - name：群組名。

        回傳：
          - 命中的 ``GroupEntry``。

        例外：
          - GroupNotFoundError：name 不在目前快照中。
        """
        group = self._groups.get(name)
        if group is None:
            raise GroupNotFoundError("找不到群組：{0}".format(name))
        return group

    def group_of(self, session_id: str) -> Optional[str]:
        """回傳 session 所屬群組名；不屬任何群回 None（建構期快照，不取鎖）。"""
        return self._owner.get(session_id)

    # ------------------------------------------------------------------
    # 寫入（鎖內交易）
    # ------------------------------------------------------------------

    def create_group(self, name: str) -> GroupEntry:
        """建立一個空群組（鎖內交易）；已存在丟 ``GroupExistsError``。

        參數：
          - name：群組名。

        回傳：
          - 新建立的 ``GroupEntry``（空成員）。

        例外：
          - GroupExistsError：name 已存在。
          - SessionStoreError：取鎖/持久化寫入失敗（已回捲記憶體狀態）。
        """
        with self._store.locked():
            self._reload_locked()
            if name in self._groups:
                raise GroupExistsError("群組已存在：{0}".format(name))
            group = GroupEntry(name=name, members=[])
            self._groups[name] = group
            try:
                self._store.save(self._groups.values())
            except Exception:
                del self._groups[name]
                raise
            return group

    def delete_group(self, name: str) -> GroupEntry:
        """刪除一個群組（鎖內交易）；查無丟 ``GroupNotFoundError``。

        參數：
          - name：群組名。

        回傳：
          - 被刪除的 ``GroupEntry``。

        例外：
          - GroupNotFoundError：name 不存在。
          - SessionStoreError：取鎖/持久化寫入失敗（已回捲記憶體狀態）。
        """
        with self._store.locked():
            self._reload_locked()
            group = self._groups.get(name)
            if group is None:
                raise GroupNotFoundError("找不到群組：{0}".format(name))
            del self._groups[name]
            removed_owner = [
                sid for sid in group.members if self._owner.get(sid) == name
            ]
            for session_id in removed_owner:
                del self._owner[session_id]
            try:
                self._store.save(self._groups.values())
            except Exception:
                self._groups[name] = group
                for session_id in removed_owner:
                    self._owner[session_id] = name
                raise
            return group

    def add_member(
        self, name: str, session_id: str, auto_create: bool = False
    ) -> GroupEntry:
        """把 session 加入群組（鎖內交易），強制「一 session 至多一群」。

        不變量強制：
          1. name 不存在：auto_create=True 則新建空群；否則丟
             ``GroupNotFoundError``。
          2. session 已屬他群（_owner 值 != name）：丟 ``GroupMembershipError``
             （點名 session_id、現群、目標群）。
          3. session 已屬 name 本身：冪等 no-op（回現群，不重複加入）。
          4. 否則 append 到 members、更新 _owner、save（失敗回捲後重拋）。

        參數：
          - name：目標群組名。
          - session_id：欲加入的 session 識別碼（不透明字串鍵）。
          - auto_create：群不存在時是否自動新建空群（預設 False）。

        回傳：
          - 加入後（或冪等命中）的 ``GroupEntry``。

        例外：
          - GroupNotFoundError：name 不存在且未 auto_create。
          - GroupMembershipError：session 已屬他群。
          - SessionStoreError：取鎖/持久化寫入失敗（已回捲記憶體狀態）。
        """
        with self._store.locked():
            self._reload_locked()
            group = self._groups.get(name)
            created = False
            if group is None:
                if not auto_create:
                    raise GroupNotFoundError("找不到群組：{0}".format(name))
                group = GroupEntry(name=name, members=[])
                self._groups[name] = group
                created = True

            current = self._owner.get(session_id)
            if current is not None and current != name:
                # 新建的空群尚未落帳，回捲後再拋，避免留下副作用。
                if created:
                    del self._groups[name]
                raise GroupMembershipError(
                    "session {0} 已屬群組 {1}，無法加入群組 {2}；請先移除再重試"
                    .format(session_id, current, name)
                )
            if current == name:
                # 冪等 no-op：已在本群，直接回現群（不重複加入、不寫檔）。
                return group

            group.members.append(session_id)
            self._owner[session_id] = name
            try:
                self._store.save(self._groups.values())
            except Exception:
                group.members.remove(session_id)
                del self._owner[session_id]
                if created:
                    del self._groups[name]
                raise
            return group

    def remove_member(self, name: str, session_id: str) -> GroupEntry:
        """把 session 從指定群組移除（鎖內交易）；查無群組丟 ``GroupNotFoundError``。

        session 不在該群時為 no-op（回現群，不寫檔）。

        參數：
          - name：群組名。
          - session_id：欲移除的 session 識別碼。

        回傳：
          - 移除後（或 no-op）的 ``GroupEntry``。

        例外：
          - GroupNotFoundError：name 不存在。
          - SessionStoreError：取鎖/持久化寫入失敗（已回捲記憶體狀態）。
        """
        with self._store.locked():
            self._reload_locked()
            group = self._groups.get(name)
            if group is None:
                raise GroupNotFoundError("找不到群組：{0}".format(name))
            if session_id not in group.members:
                return group
            index = group.members.index(session_id)
            group.members.remove(session_id)
            had_owner = self._owner.get(session_id) == name
            if had_owner:
                del self._owner[session_id]
            try:
                self._store.save(self._groups.values())
            except Exception:
                group.members.insert(index, session_id)
                if had_owner:
                    self._owner[session_id] = name
                raise
            return group

    def remove_member_any(self, session_id: str) -> Optional[str]:
        """不需知道所屬群，直接查 ``_owner`` 找到群後移除（鎖內交易，供 release hook）。

        session 不屬任何群時為 no-op 回 None。

        參數：
          - session_id：欲移除的 session 識別碼。

        回傳：
          - 被移除所屬的群組名；session 不屬任何群時回 None。

        例外：
          - SessionStoreError：取鎖/持久化寫入失敗（已回捲記憶體狀態）。
        """
        with self._store.locked():
            self._reload_locked()
            name = self._owner.get(session_id)
            if name is None:
                return None
            group = self._groups[name]
            index = group.members.index(session_id)
            group.members.remove(session_id)
            del self._owner[session_id]
            try:
                self._store.save(self._groups.values())
            except Exception:
                group.members.insert(index, session_id)
                self._owner[session_id] = name
                raise
            return name
