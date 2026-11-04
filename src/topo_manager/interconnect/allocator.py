"""topo_manager 互聯分配引擎（interconnect/allocator.py，互聯設計 §5）。

``InterconnectAllocator`` 以「組（group）」為單位，在一組 4 台 DUT 內分配
互聯 session：
  - profile 模式：選定 2~4 台 DUT + mesh/line/ring，依固定配對表展開 link。
  - 明確 port 對模式：``("dutA:port", "dutB:port")`` 清單，可用任何未佔用
    il_port（含 p3）。
  - 每條 link 分配一個互聯領域內全域唯一的 VLAN（池 ``[vlan_min, vlan_max]``）。
  - 衝突只在 port 層級判定（含 ``releasing`` session 持有者）；硬衝突丟
    ``InterconnectConflictError``，於任何狀態變更前完成。
  - 生命週期：allocate → begin_release（phase=releasing）→ commit_release；
    ``release_now`` 在同一把鎖內完成兩步（--ledger-only 用）。
  - 交易模式沿用打流側 Allocator：取帳本鎖 → 鎖內重載 → 決策 → save →
    釋鎖；save 失敗回捲記憶體 index 後重拋。

NOTES：
  - 預覽（preview_*）不取鎖、不佔池、不入帳，只驗證輸入並以 vlan_min 起連續
    編號，session_id 為 ``(preview)``；不檢查 port 衝突與 VLAN 耗盡。
  - 唯讀方法（list/get/owned_nodes）使用建構時快照，不取鎖。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import re
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from topo_manager.errors import (
    InterconnectConflictError,
    InterconnectLayoutError,
    InterconnectRequestError,
    InterconnectSessionNotFoundError,
    SessionStoreError,
    VlanPoolExhaustedError,
)
from topo_manager.interconnect.layout import IcGraph, IcGroup, IcPort, build_ic_graph
from topo_manager.interconnect.models import IcLink, IcSession
from topo_manager.interconnect.pairing import (
    IC_PROFILES,
    expand_profile,
    pair_port_indices,
)
from topo_manager.models import Topology

# 嚴格 session 序號格式 ^ic-NNNN$。
_SEQ_RE = re.compile(r"^ic-(\d{4})$")
# 還原失敗訊息統一使用的修復指引。
_RAW_HINT = "請以 'list --raw --state <狀態檔>' 檢視原始帳本後修正或移除該筆"
# 預覽用 session_id 哨兵。
_PREVIEW_SESSION_ID = "(preview)"
# 合法 phase / profile / kind。
PHASE_ALLOCATED = "allocated"
PHASE_RELEASING = "releasing"
_PHASES = (PHASE_ALLOCATED, PHASE_RELEASING)
_PROFILES_ALL = IC_PROFILES + ("explicit",)
# 組名列表過長時顯示的上限。
_LIST_LIMIT = 10


class _LinkSpec(NamedTuple):
    """展開後的單條 link 規格（尚未配 VLAN）。"""

    a_dut: str
    a_port: IcPort
    b_dut: str
    b_port: IcPort
    kind: str


class InterconnectAllocator:
    """互聯分配引擎（互聯設計 §5）。

    狀態封裝於實例屬性：``_sessions``、``_port_owner``（fanout 節點 →
    session_id）、``_used_vlans``、``_next_seq``。
    """

    def __init__(
        self,
        topology: Topology,
        store: "object",
        vlan_min: int = 100,
        vlan_max: int = 3000,
    ) -> None:
        """建圖、載入既有帳本並重建 derived index。

        例外：
          - InterconnectLayoutError：拓樸沒有 interconnect 區段。
          - ValueError：VLAN 池範圍非法（0<min<=max 且落在 2..4094）。
          - SessionStoreError：還原一致性失敗。
        """
        if topology.interconnect is None:
            raise InterconnectLayoutError("拓樸沒有 interconnect 區段")
        if not (0 < vlan_min <= vlan_max):
            raise ValueError(
                "VLAN 池範圍非法：須 0 < vlan_min <= vlan_max（實得 {0}..{1}）"
                .format(vlan_min, vlan_max)
            )
        if not (2 <= vlan_min and vlan_max <= 4094):
            raise ValueError(
                "VLAN 池超出 802.1Q 可用範圍 2..4094（排除保留的 0/1/4095）："
                "實得 {0}..{1}".format(vlan_min, vlan_max)
            )
        self._graph: IcGraph = build_ic_graph(topology)
        self._store = store
        self._vlan_min = vlan_min
        self._vlan_max = vlan_max
        self._sessions: Dict[str, IcSession] = {}
        self._port_owner: Dict[str, str] = {}
        self._used_vlans: Set[int] = set()
        self._next_seq = 1
        self._rebuild_from_restored(store.load())

    @property
    def graph(self) -> IcGraph:
        """回傳互聯小圖。"""
        return self._graph

    # ------------------------------------------------------------------
    # 還原與 derived index 重建
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_seq_strict(session_id: str) -> int:
        """嚴格解析 ``ic-NNNN``；不符丟 SessionStoreError。"""
        match = _SEQ_RE.match(session_id)
        if match is None:
            raise SessionStoreError(
                "互聯帳本 session_id 格式錯誤：'{0}'（須為 ic-NNNN）；{1}".format(
                    session_id, _RAW_HINT
                )
            )
        return int(match.group(1))

    def _rebuild_from_restored(self, loaded) -> None:
        """由還原的帳本重建 derived index 並做一致性檢查（§5.5）。

        例外：
          - SessionStoreError：任何一致性問題（訊息帶修復指引）。
        """
        sessions: Dict[str, IcSession] = {}
        port_owner: Dict[str, str] = {}
        used_vlans: Set[int] = set()
        max_seq = 0
        for session in loaded.sessions:
            sid = session.session_id
            seq = self._parse_seq_strict(sid)
            if sid in sessions:
                raise SessionStoreError(
                    "互聯帳本 session_id 重複：{0}；{1}".format(sid, _RAW_HINT)
                )
            if session.phase not in _PHASES:
                raise SessionStoreError(
                    "互聯帳本 session {0} 的 phase '{1}' 非法（允許：{2}）；{3}"
                    .format(sid, session.phase, ", ".join(_PHASES), _RAW_HINT)
                )
            if session.profile not in _PROFILES_ALL:
                raise SessionStoreError(
                    "互聯帳本 session {0} 的 profile '{1}' 非法（允許：{2}）；{3}"
                    .format(sid, session.profile, ", ".join(_PROFILES_ALL), _RAW_HINT)
                )
            group = self._graph.groups.get(session.group)
            if group is None:
                raise SessionStoreError(
                    "互聯帳本 session {0} 的組 '{1}' 不在目前拓樸（拓樸漂移）；{2}"
                    .format(sid, session.group, _RAW_HINT)
                )
            if session.fanout != group.fanout:
                raise SessionStoreError(
                    "互聯帳本 session {0} 的 fanout '{1}' 與拓樸不符（應為 '{2}'）；"
                    "{3}".format(sid, session.fanout, group.fanout, _RAW_HINT)
                )
            if len(session.links) == 0:
                raise SessionStoreError(
                    "互聯帳本 session {0} 沒有任何 link；{1}".format(sid, _RAW_HINT)
                )
            for link in session.links:
                if not (self._vlan_min <= link.vlan <= self._vlan_max):
                    raise SessionStoreError(
                        "互聯帳本 session {0} 的 VLAN {1} 不在目前池 {2}..{3}；{4}"
                        .format(sid, link.vlan, self._vlan_min, self._vlan_max, _RAW_HINT)
                    )
                if link.vlan in used_vlans:
                    raise SessionStoreError(
                        "互聯帳本 VLAN {0} 被重複使用（session {1}）；{2}".format(
                            link.vlan, sid, _RAW_HINT
                        )
                    )
                used_vlans.add(link.vlan)
                self._check_link_wiring(sid, group, link)
                for node in (link.a_node, link.b_node):
                    if node in port_owner:
                        raise SessionStoreError(
                            "互聯帳本 port {0} 被 session {1} 與 {2} 同時佔用；{3}"
                            .format(node, port_owner[node], sid, _RAW_HINT)
                        )
                    port_owner[node] = sid
            self._check_links_match_profile(session, group)
            sessions[sid] = session
            max_seq = max(max_seq, seq)
        self._sessions = sessions
        self._port_owner = port_owner
        self._used_vlans = used_vlans
        self._next_seq = max(loaded.next_seq or 0, max_seq + 1)

    def _check_link_wiring(self, sid: str, group: IcGroup, link: IcLink) -> None:
        """檢查 link 兩端 DUT 屬該組且 node 與目前接線一致（偵測接線漂移）。"""
        if link.a_dut == link.b_dut:
            raise SessionStoreError(
                "互聯帳本 session {0} 有 DUT 自連的 link（{1}）；{2}".format(
                    sid, link.a_dut, _RAW_HINT
                )
            )
        for dut_name, il_port, node in (
            (link.a_dut, link.a_port, link.a_node),
            (link.b_dut, link.b_port, link.b_node),
        ):
            dut = next((item for item in group.duts if item.name == dut_name), None)
            if dut is None:
                raise SessionStoreError(
                    "互聯帳本 session {0} 的 DUT '{1}' 不屬於組 '{2}'；{3}".format(
                        sid, dut_name, group.name, _RAW_HINT
                    )
                )
            port = next((item for item in dut.ports if item.il_port == il_port), None)
            if port is None or port.node != node:
                raise SessionStoreError(
                    "互聯帳本 session {0} 的 {1}:{2} 對應 {3}，與目前拓樸接線不符"
                    "（接線漂移）；{4}".format(sid, dut_name, il_port, node, _RAW_HINT)
                )

    def _check_links_match_profile(self, session: IcSession, group: IcGroup) -> None:
        """paired link 須與 (profile, duts) 重新展開的結果逐條一致；explicit 須全為 explicit。"""
        sid = session.session_id
        if session.profile == "explicit":
            if any(link.kind != "explicit" for link in session.links):
                raise SessionStoreError(
                    "互聯帳本 session {0} 為 explicit 但含非 explicit link；{1}"
                    .format(sid, _RAW_HINT)
                )
            return
        try:
            specs = self._profile_specs(group, session.duts, session.profile)
        except InterconnectRequestError as exc:
            raise SessionStoreError(
                "互聯帳本 session {0} 無法依 profile 重新展開：{1}；{2}".format(
                    sid, exc, _RAW_HINT
                )
            ) from exc
        expected = [
            (s.a_dut, s.a_port.il_port, s.a_port.node,
             s.b_dut, s.b_port.il_port, s.b_port.node, "paired")
            for s in specs
        ]
        actual = [
            (l.a_dut, l.a_port, l.a_node, l.b_dut, l.b_port, l.b_node, l.kind)
            for l in session.links
        ]
        if expected != actual:
            raise SessionStoreError(
                "互聯帳本 session {0} 的 link 與 profile '{1}' 重新展開的結果不一致"
                "（配對表或帳本被改）；{2}".format(sid, session.profile, _RAW_HINT)
            )

    # ------------------------------------------------------------------
    # 輸入驗證與 link 展開
    # ------------------------------------------------------------------
    def _get_group(self, group: str) -> IcGroup:
        """取得組；不存在丟 InterconnectRequestError 並列出可用組名。"""
        found = self._graph.groups.get(group)
        if found is not None:
            return found
        names = list(self._graph.groups)
        shown = ", ".join(names[:_LIST_LIMIT])
        if len(names) > _LIST_LIMIT:
            shown += " …（共 {0} 組）".format(len(names))
        raise InterconnectRequestError(
            "找不到組 '{0}'；可用組：{1}".format(group, shown if names else "(無)")
        )

    def _group_dut(self, group: IcGroup, dut_name: str):
        """取得組內 DUT；未知 DUT 或跨組丟 InterconnectRequestError。"""
        for dut in group.duts:
            if dut.name == dut_name:
                return dut
        try:
            other = self._graph.group_of_dut(dut_name)
        except InterconnectRequestError:
            raise InterconnectRequestError(
                "找不到 DUT '{0}'".format(dut_name)
            ) from None
        raise InterconnectRequestError(
            "DUT '{0}' 不屬於組 '{1}'（屬於 '{2}'）".format(
                dut_name, group.name, other.name
            )
        )

    def _profile_specs(
        self, group: IcGroup, dut_names: List[str], profile: str
    ) -> List[_LinkSpec]:
        """依 profile 與選定 DUT 順序展開 link 規格（已驗證 group 為 IcGroup）。"""
        if profile not in IC_PROFILES:
            raise InterconnectRequestError(
                "未知 profile '{0}'（允許：{1}）".format(profile, ", ".join(IC_PROFILES))
            )
        if len(set(dut_names)) != len(dut_names):
            raise InterconnectRequestError(
                "DUT 清單含重複項：{0}".format(", ".join(dut_names))
            )
        selected = [self._group_dut(group, name) for name in dut_names]
        specs: List[_LinkSpec] = []
        for pos_a, pos_b in expand_profile(profile, len(selected)):
            dut_a, dut_b = selected[pos_a], selected[pos_b]
            idx_a, idx_b = pair_port_indices(dut_a.index, dut_b.index)
            specs.append(
                _LinkSpec(
                    dut_a.name, dut_a.ports[idx_a], dut_b.name, dut_b.ports[idx_b], "paired"
                )
            )
        return specs

    def _pair_specs(
        self, group: IcGroup, pairs: List[Tuple[str, str]]
    ) -> Tuple[List[_LinkSpec], List[str]]:
        """展開明確 port 對；回傳 (規格, 出現順序去重的 DUT 名)。"""
        if len(pairs) == 0:
            raise InterconnectRequestError("至少需要一組 port 對")
        specs: List[_LinkSpec] = []
        seen_nodes: Dict[str, str] = {}
        order: List[str] = []
        for pair in pairs:
            ends = []
            for text in pair:
                dut_name, sep, token = str(text).partition(":")
                if sep == "" or dut_name == "" or token == "":
                    raise InterconnectRequestError(
                        "port 對格式錯誤：'{0}'（須為 dut:port）".format(text)
                    )
                self._group_dut(group, dut_name)
                ends.append((dut_name, self._graph.resolve_port(group.name, dut_name, token)))
            (dut_a, port_a), (dut_b, port_b) = ends
            if dut_a == dut_b:
                raise InterconnectRequestError(
                    "port 對兩端不可為同一台 DUT：{0}".format(dut_a)
                )
            for dut_name, port in ends:
                if port.node in seen_nodes:
                    raise InterconnectRequestError(
                        "port 重複：{0}:{1}（{2}）在同一次請求中出現兩次".format(
                            dut_name, port.il_port, port.node
                        )
                    )
                seen_nodes[port.node] = dut_name
                if dut_name not in order:
                    order.append(dut_name)
            specs.append(_LinkSpec(dut_a, port_a, dut_b, port_b, "explicit"))
        return specs, order

    def _specs_for_profile(
        self, group: str, duts: List[str], profile: str
    ) -> Tuple[IcGroup, List[_LinkSpec]]:
        """驗證 profile 模式輸入並展開。"""
        ic_group = self._get_group(group)
        specs = self._profile_specs(ic_group, list(duts), profile)
        return ic_group, specs

    def _build_session(
        self,
        session_id: str,
        group: IcGroup,
        profile: str,
        dut_names: List[str],
        specs: List[_LinkSpec],
        vlans: List[int],
    ) -> IcSession:
        """由規格與 VLAN 組出 IcSession（phase=allocated）。"""
        links = [
            IcLink(
                vlan=vlan,
                a_dut=spec.a_dut,
                a_port=spec.a_port.il_port,
                a_node=spec.a_port.node,
                b_dut=spec.b_dut,
                b_port=spec.b_port.il_port,
                b_node=spec.b_port.node,
                kind=spec.kind,
            )
            for spec, vlan in zip(specs, vlans)
        ]
        return IcSession(
            session_id=session_id,
            group=group.name,
            fanout=group.fanout,
            profile=profile,
            duts=list(dut_names),
            phase=PHASE_ALLOCATED,
            links=links,
        )

    # ------------------------------------------------------------------
    # 交易原語
    # ------------------------------------------------------------------
    def _reload_locked(self) -> None:
        """鎖內以最新帳本重建全部 derived index。"""
        self._sessions = {}
        self._port_owner = {}
        self._used_vlans = set()
        self._next_seq = 1
        self._rebuild_from_restored(self._store.load())

    def _pick_vlans(self, count: int) -> List[int]:
        """挑選最小的 count 個未用 VLAN；不足丟 VlanPoolExhaustedError（先檢查再提交）。"""
        picked: List[int] = []
        for candidate in range(self._vlan_min, self._vlan_max + 1):
            if candidate not in self._used_vlans:
                picked.append(candidate)
                if len(picked) == count:
                    return picked
        raise VlanPoolExhaustedError(
            "VLAN 池不足：範圍 {0}..{1}，已用 {2} 個，本次需要 {3} 個".format(
                self._vlan_min, self._vlan_max, len(self._used_vlans), count
            )
        )

    def _check_conflicts(self, specs: List[_LinkSpec]) -> None:
        """硬衝突檢查（含 releasing 持有者），任何狀態變更前完成。"""
        conflicts: List[str] = []
        for spec in specs:
            for dut_name, port in ((spec.a_dut, spec.a_port), (spec.b_dut, spec.b_port)):
                owner = self._port_owner.get(port.node)
                if owner is not None:
                    conflicts.append(
                        "{0}:{1}（{2}）← 擁有者 session {3}".format(
                            dut_name, port.il_port, port.node, owner
                        )
                    )
        if len(conflicts) > 0:
            raise InterconnectConflictError(
                "硬衝突：下列互聯 port 已被使用中的 session 佔用，請先釋放：\n  {0}"
                .format("\n  ".join(conflicts))
            )

    def _commit_locked(
        self,
        group: IcGroup,
        profile: str,
        dut_names: List[str],
        specs: List[_LinkSpec],
    ) -> IcSession:
        """鎖內：衝突檢查 → 挑 VLAN → 建 session → save（失敗回捲）。"""
        self._check_conflicts(specs)
        vlans = self._pick_vlans(len(specs))
        session_id = "ic-{0:04d}".format(self._next_seq)
        session = self._build_session(session_id, group, profile, dut_names, specs, vlans)
        seq_before = self._next_seq
        self._sessions[session_id] = session
        for link in session.links:
            self._port_owner[link.a_node] = session_id
            self._port_owner[link.b_node] = session_id
            self._used_vlans.add(link.vlan)
        self._next_seq += 1
        try:
            self._store.save(self._sessions.values(), self._next_seq)
        except Exception:
            del self._sessions[session_id]
            for link in session.links:
                for node in (link.a_node, link.b_node):
                    if self._port_owner.get(node) == session_id:
                        del self._port_owner[node]
                self._used_vlans.discard(link.vlan)
            self._next_seq = seq_before
            raise
        return session

    def _require(self, session_id: str) -> IcSession:
        """取得 session；查無丟 InterconnectSessionNotFoundError。"""
        session = self._sessions.get(session_id)
        if session is None:
            raise InterconnectSessionNotFoundError(
                "找不到互聯 session：{0}".format(session_id)
            )
        return session

    def _remove_locked(self, session: IcSession) -> None:
        """鎖內移除 session 並 save（失敗回捲，不碰 _next_seq）。"""
        sid = session.session_id
        del self._sessions[sid]
        removed_nodes = [
            node
            for link in session.links
            for node in (link.a_node, link.b_node)
            if self._port_owner.get(node) == sid
        ]
        for node in removed_nodes:
            del self._port_owner[node]
        for link in session.links:
            self._used_vlans.discard(link.vlan)
        try:
            self._store.save(self._sessions.values(), self._next_seq)
        except Exception:
            self._sessions[sid] = session
            for node in removed_nodes:
                self._port_owner[node] = sid
            for link in session.links:
                self._used_vlans.add(link.vlan)
            raise

    # ------------------------------------------------------------------
    # 公開方法
    # ------------------------------------------------------------------
    def allocate_profile(self, group: str, duts: List[str], profile: str) -> IcSession:
        """以 profile 分配一個互聯 session（鎖內交易，失敗無副作用）。

        例外：
          - InterconnectRequestError：輸入不合法。
          - InterconnectConflictError：port 硬衝突。
          - VlanPoolExhaustedError：VLAN 不足。
          - SessionStoreError：取鎖/持久化失敗（已回捲）。
        """
        ic_group, specs = self._specs_for_profile(group, duts, profile)
        with self._store.locked():
            self._reload_locked()
            return self._commit_locked(ic_group, profile, list(duts), specs)

    def allocate_pairs(self, group: str, pairs: List[Tuple[str, str]]) -> IcSession:
        """以明確 port 對分配（profile 記為 explicit）。例外同 allocate_profile。"""
        ic_group = self._get_group(group)
        specs, order = self._pair_specs(ic_group, list(pairs))
        with self._store.locked():
            self._reload_locked()
            return self._commit_locked(ic_group, "explicit", order, specs)

    def _preview(
        self,
        group: IcGroup,
        profile: str,
        dut_names: List[str],
        specs: List[_LinkSpec],
    ) -> IcSession:
        """組出預覽 session（VLAN 自 vlan_min 連續編號，不佔池）。"""
        vlans = [self._vlan_min + offset for offset in range(len(specs))]
        return self._build_session(
            _PREVIEW_SESSION_ID, group, profile, dut_names, specs, vlans
        )

    def preview_profile(self, group: str, duts: List[str], profile: str) -> IcSession:
        """預覽 profile 分配（不取鎖、不佔池、不入帳）。"""
        ic_group, specs = self._specs_for_profile(group, duts, profile)
        return self._preview(ic_group, profile, list(duts), specs)

    def preview_pairs(self, group: str, pairs: List[Tuple[str, str]]) -> IcSession:
        """預覽明確 port 對分配（不取鎖、不佔池、不入帳）。"""
        ic_group = self._get_group(group)
        specs, order = self._pair_specs(ic_group, list(pairs))
        return self._preview(ic_group, "explicit", order, specs)

    def begin_release(self, session_id: str) -> IcSession:
        """標記 phase=releasing（冪等；已是 releasing 直接回傳，不寫帳本）。"""
        with self._store.locked():
            self._reload_locked()
            session = self._require(session_id)
            if session.phase == PHASE_RELEASING:
                return session
            updated = session.model_copy(update={"phase": PHASE_RELEASING})
            self._sessions[session_id] = updated
            try:
                self._store.save(self._sessions.values(), self._next_seq)
            except Exception:
                self._sessions[session_id] = session
                raise
            return updated

    def commit_release(self, session_id: str) -> IcSession:
        """移除 releasing 的 session；非 releasing 丟 InterconnectRequestError。"""
        with self._store.locked():
            self._reload_locked()
            session = self._require(session_id)
            if session.phase != PHASE_RELEASING:
                raise InterconnectRequestError(
                    "session {0} 的 phase 為 '{1}'，只有 '{2}' 才能 commit_release；"
                    "請先 begin_release".format(session_id, session.phase, PHASE_RELEASING)
                )
            self._remove_locked(session)
            return session

    def release_now(self, session_id: str) -> IcSession:
        """同一把鎖內直接移除 session（任何 phase；--ledger-only 用）。"""
        with self._store.locked():
            self._reload_locked()
            session = self._require(session_id)
            self._remove_locked(session)
            return session

    def list_sessions(self) -> List[IcSession]:
        """回傳所有 session（依序號升冪；建構時快照）。"""
        return sorted(
            self._sessions.values(),
            key=lambda item: self._parse_seq_strict(item.session_id),
        )

    def get_session(self, session_id: str) -> IcSession:
        """唯讀取回 session；查無丟 InterconnectSessionNotFoundError。"""
        return self._require(session_id)

    def owned_nodes(self) -> Dict[str, str]:
        """回傳 port 擁有者快照（fanout 節點 → session_id）。"""
        return dict(self._port_owner)
