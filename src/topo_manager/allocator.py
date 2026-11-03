"""topo_manager 分配引擎（allocator.py，設計 §Allocator、FR-3、FR-4、Phase 2 Hardening）。

本模組是 Phase 2 的核心邏輯層：把已驗證的 ``Topology`` 建成 ``TopologyGraph``，
依「generator port → DUT port」查出實體路徑，推導出該條路徑的 physical_ports
/ trunk_segments / access_ports，再做：

  - 硬衝突檢查（FR-4）：僅以 access port 判定；若新路徑任一 access port 已被
    使用中的 session 佔用，整筆分配失敗並列出所有衝突 port 與擁有者
    session_id，且在任何狀態變更前完成（失敗不留副作用）。
  - 全域唯一 VLAN 分配（FR-3）：在封閉池 ``[vlan_min, vlan_max]`` 內挑選最小
    可用值；池耗盡則丟 ``VlanPoolExhaustedError``。建構期另做 802.1Q 範圍
    sanity check（#13）。
  - session 生命週期：allocate / release / list_sessions，狀態透過
    ``JsonSessionStore`` 持久化。``allocate``/``release`` 一律外包
    ``store.locked()`` 跨行程交易（取鎖 → 鎖內 reload 最新 ledger → 決策 →
    save → 釋鎖），save 失敗一律回捲記憶體狀態後再拋（#1）。

trunk vs access 的推導（設計 §路徑推導，由 context 驗證）：
  - physical_ports = 路徑全部節點（保序）。
  - trunk_segments = 掃描相鄰 (prev, cur) 對，兩端皆為 fanout 且分屬「不同」
    fanout 裝置者即為一段 trunk（a_node=prev 近 generator、b_node=cur 近 DUT）；
    同一台 fanout 內相鄰 port 不是 trunk。
  - access_ports = physical_ports 扣除所有 trunk 端節點 = 硬衝突判定集合；為
    衍生值，由 ``_derive_access_ports(path)`` 即時重算，不持久化（單一真相
    來源，決策 2a、#5/#8）。

狀態封裝於實例屬性（不使用全域可變變數）：derived index（_port_owner /
_used_vlans / _next_seq）在 __init__ 與每次交易鎖內由還原的 session 重算，
確保重啟/併發後一致。

歷史（HISTORY）：
  - 0.2.0  FEAT-005 初版，建立 TrunkSegment / Session 模型與 Allocator
           （路徑推導、硬衝突、VLAN 分配、session 生命週期、還原一致性檢查）。
  - 0.3.0  Phase 2 Hardening：Session 移除 access_ports（衍生即時重算）；
           __init__ 加 802.1Q 範圍檢查、load() 回傳改接 LoadedLedger；刪除
           容錯版 _parse_seq、改嚴格 _parse_seq_strict（不符 raise
           SessionStoreError）；_rebuild_from_restored 兩階段還原驗證（格式/
           重複/pool/physical_ports 一致/拓樸漂移）+ 序號高水位；allocate/
           release 外包 store.locked() 跨行程交易（_reload_locked /
           _allocate_locked / _release_locked），save 傳 next_seq。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import re
from typing import Dict, List, Set, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from topo_manager.errors import (
    HardConflictError,
    SessionNotFoundError,
    SessionStoreError,
    VlanPoolExhaustedError,
)
from topo_manager.graph import TopologyGraph
from topo_manager.models import Topology

if TYPE_CHECKING:
    from topo_manager.store import LoadedLedger

# 嚴格 session 序號格式（模組層常數）：^s-NNNN$（4 位數字）。
_SEQ_RE = re.compile(r"^s-(\d{4})$")

# 還原失敗訊息統一使用的修復指引（N#3：用 --raw --state，不寫死 topology）。
_RAW_HINT = "請以 'list --raw --state <狀態檔>' 檢視原始帳本後修正或移除該筆"


class TrunkSegment(BaseModel):
    """一段 fanout 對 fanout 的 trunk 連線（設計 §路徑推導）。

    欄位：
      - a_node：trunk 近 generator 端節點 id（``"<dev>:<port>"``）。
      - b_node：trunk 近 DUT 端節點 id（``"<dev>:<port>"``）。
      - vlan：此 session 在該 trunk 上使用的 VLAN（全域唯一）。
    """

    model_config = ConfigDict(extra="forbid")

    a_node: str
    b_node: str
    vlan: int


class Session(BaseModel):
    """一筆已建立的分配 session（設計 §Allocator）。

    欄位：
      - session_id：session 識別碼，形如 ``s-0001``，全域唯一。格式由還原層
        嚴格 parser ``_parse_seq_strict`` 做單一真相來源的驗證（M#1：不加
        pydantic ``Field(pattern=...)``）。
      - vlan：本 session 佔用的 VLAN，全域唯一且位於封閉池內。
      - generator_port：分配時指定的 generator 端輸入。
      - dut_port：分配時指定的 DUT 端輸入（``dev:port``）。
      - path：find_path 回傳的節點 id 序列（首 generator、末 DUT）。
      - physical_ports：路徑全部實體節點 id（= path，保序），還原層以等值
        校驗守其一致。
      - trunk_segments：路徑中的 fanout↔fanout trunk 段清單。

    注意：``access_ports`` 不入模型/帳本（決策 2a、#5/#8）；它是衍生值，由
    ``physical_ports − trunk 端`` 即時重算（``_derive_access_ports``），確保
    單一真相來源、不受帳本竄改影響。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    vlan: int
    generator_port: str
    dut_port: str
    path: List[str]
    physical_ports: List[str]
    trunk_segments: List[TrunkSegment]


class Allocator:
    """分配引擎：路徑推導、硬衝突、VLAN 分配與 session 生命週期（§Allocator）。

    狀態封裝於實例屬性：
      - ``self._graph``：由 topology 建成的 ``TopologyGraph``。
      - ``self._fanout_names``：fanout 裝置名集合（trunk 判定用）。
      - ``self._store``：持久化層（具 load/save/locked）。
      - ``self._sessions``：Dict[session_id, Session]。
      - ``self._port_owner``：Dict[access_port, session_id]，硬衝突索引。
      - ``self._used_vlans``：Set[int]，已佔用 VLAN。
      - ``self._next_seq``：下一個 session 序號（高水位）。
    """

    def __init__(
        self,
        topology: Topology,
        store: "object",
        vlan_min: int = 100,
        vlan_max: int = 3000,
    ) -> None:
        """建圖、載入既有 ledger 並重建 derived index（設計 §Allocator）。

        參數：
          - topology：已驗證的 ``Topology``。
          - store：持久化層（須具 ``load()``、``save(iterable, next_seq)`` 與
            ``locked()``）。
          - vlan_min / vlan_max：封閉 VLAN 池端點，須 ``0 < vlan_min <=
            vlan_max`` 且落在 802.1Q 可用範圍 ``2..4094``（排除保留的
            0/1/4095，#13）。

        例外：
          - ValueError：VLAN 池範圍非法（0<min<=max 或超出 802.1Q 範圍）。
          - SessionStoreError：還原時偵測到一致性問題（格式/重複/pool/
            physical_ports 不符/拓樸漂移/access port 衝突）。trunk 端 port 跨
            session 共用為合法，不觸發。
        """
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

        self._graph = TopologyGraph(topology)
        self._fanout_names: Set[str] = {fanout.name for fanout in topology.fanouts}
        self._store = store
        self._vlan_min = vlan_min
        self._vlan_max = vlan_max

        self._sessions: Dict[str, Session] = {}
        self._port_owner: Dict[str, str] = {}
        self._used_vlans: Set[int] = set()
        self._next_seq = 1

        # 建構時 load 一次：供 list/list --raw 這類唯讀命令直接用（不取鎖）。
        # allocate/release 一律在鎖內重新 load，建構時的快照僅供唯讀路徑。
        loaded = store.load()
        self._rebuild_from_restored(loaded)

    # ------------------------------------------------------------------
    # 還原與 derived index 重建
    # ------------------------------------------------------------------

    def _rebuild_from_restored(self, loaded: "LoadedLedger") -> None:
        """由還原的 ledger 重建全部 derived index（含 next_seq 高水位）。

        兩階段還原驗證（設計 §還原層）：
          階段 1（建任何索引前，逐筆）：session_id 格式（_parse_seq_strict）、
            重複 session_id、VLAN 落在當前 pool。
          階段 2（語意一致性）：physical_ports == path、重算 trunk 段與帳本
            記錄一致（拓樸漂移偵測）、同一 access port 不被兩 session 佔用。
          階段 3：序號高水位 _next_seq = max(loaded.next_seq or 0, max_seq+1)。

        全部不一致一律丟 ``SessionStoreError``（訊息含 --raw 修復指引）；trunk
        端 port 跨 session 共用為合法，不檢查。

        參數：
          - loaded：store.load() 回傳的 ``LoadedLedger``。
        """
        max_seq = 0
        seen_ids: Set[str] = set()

        for session in loaded.sessions:
            # 階段 1：格式（不符即 raise SessionStoreError）。
            seq = self._parse_seq_strict(session.session_id)

            # 階段 1：session_id 重複（在寫入任何索引前）。
            if session.session_id in seen_ids:
                raise SessionStoreError(
                    "ledger 還原不一致（session_id 重複）：{0}；{1}".format(
                        session.session_id, _RAW_HINT
                    )
                )
            seen_ids.add(session.session_id)

            # 階段 1：VLAN 落在當前 pool。
            if not (self._vlan_min <= session.vlan <= self._vlan_max):
                raise SessionStoreError(
                    "ledger 還原不一致（VLAN 超出當前 pool）：session {0} 的 "
                    "VLAN {1} 不在 {2}..{3}；{4}".format(
                        session.session_id,
                        session.vlan,
                        self._vlan_min,
                        self._vlan_max,
                        _RAW_HINT,
                    )
                )

            # 階段 2：physical_ports 與 path 一致（#10/finding #1）。
            if list(session.physical_ports) != list(session.path):
                raise SessionStoreError(
                    "ledger 還原不一致（physical_ports 與 path 不符）：session "
                    "{0}；{1}".format(session.session_id, _RAW_HINT)
                )

            # 階段 2：拓樸漂移——以 path 重算 trunk 段與帳本記錄比對。
            recomputed = {
                (prev, cur) for prev, cur in self._derive_trunk_pairs(session.path)
            }
            recorded = {
                (t.a_node, t.b_node) for t in session.trunk_segments
            }
            if recomputed != recorded:
                raise SessionStoreError(
                    "ledger 還原不一致（疑似拓樸變更，fanout 改名/移除）："
                    "session {0} 期望 trunk 段 {1}，實得重算 {2}；{3}".format(
                        session.session_id, sorted(recorded), sorted(recomputed),
                        _RAW_HINT,
                    )
                )

            # 階段 2：VLAN 重複（跨 session 全域唯一）。
            if session.vlan in self._used_vlans:
                raise SessionStoreError(
                    "ledger 還原不一致：VLAN {0} 重複出現於多個 session；{1}"
                    .format(session.vlan, _RAW_HINT)
                )

            # 階段 2：以重算的 access_ports 建索引，同一 access port 不得被兩
            # session 佔用（既有檢查保留）。
            access_ports = self._derive_access_ports(session.path)
            for access_port in access_ports:
                if access_port in self._port_owner:
                    raise SessionStoreError(
                        "ledger 還原不一致：access port {0} 同時被 {1} 與 {2} "
                        "佔用；{3}".format(
                            access_port,
                            self._port_owner[access_port],
                            session.session_id,
                            _RAW_HINT,
                        )
                    )
                self._port_owner[access_port] = session.session_id

            self._used_vlans.add(session.vlan)
            self._sessions[session.session_id] = session

            if seq > max_seq:
                max_seq = seq

        # 階段 3：序號高水位（取 ledger 持久化 next_seq 與 active 最大+1 的大者）。
        self._next_seq = max((loaded.next_seq or 0), max_seq + 1)

    @staticmethod
    def _parse_seq_strict(session_id: str) -> int:
        """嚴格解析 ``s-NNNN`` 的序號；不符即 raise ``SessionStoreError``。

        回傳：
          - 符合 ``^s-\\d{4}$`` 時回整數序號（``s-0007`` → 7）。

        例外：
          - SessionStoreError：不符格式（點名違規 id 與期望格式，附修復指引）。

        三處共用（還原階段 1、list_sessions 排序、high-water 推導），無容錯/
        吞例外分支——還原階段 1 已擋掉所有 malformed id，故對已驗證的記憶體
        session 呼叫必定成功回 int。
        """
        m = _SEQ_RE.match(session_id)
        if m is None:
            raise SessionStoreError(
                "session_id 格式非法（期望 ^s-\\d{{4}}$）：{0}；{1}".format(
                    session_id, _RAW_HINT
                )
            )
        return int(m.group(1))

    # ------------------------------------------------------------------
    # 路徑推導（physical / trunk / access）
    # ------------------------------------------------------------------

    @staticmethod
    def _dev_of(node_id: str) -> str:
        """取節點 id 的 dev 段（第一個 ':' 之前）。"""
        return node_id.split(":", 1)[0]

    def _derive_trunk_pairs(self, path: List[str]) -> List[List[str]]:
        """掃描相鄰 (prev, cur) 對，回傳 trunk 段的 [prev, cur] 清單（§路徑推導）。

        一對相鄰節點是 trunk，當且僅當兩端皆為 fanout 節點且分屬「不同」
        fanout 裝置；同一台 fanout 內相鄰 port 不是 trunk。
        """
        pairs: List[List[str]] = []
        for index in range(1, len(path)):
            prev = path[index - 1]
            cur = path[index]
            prev_dev = self._dev_of(prev)
            cur_dev = self._dev_of(cur)
            if (
                prev_dev in self._fanout_names
                and cur_dev in self._fanout_names
                and prev_dev != cur_dev
            ):
                pairs.append([prev, cur])
        return pairs

    def _derive_access_ports(self, path: List[str]) -> List[str]:
        """由路徑推導 access_ports = physical_ports 扣除所有 trunk 端節點。

        access_ports 即硬衝突判定集合；保序並去除所有 trunk 段的兩端節點。
        """
        trunk_ends: Set[str] = set()
        for prev, cur in self._derive_trunk_pairs(path):
            trunk_ends.add(prev)
            trunk_ends.add(cur)
        return [node for node in path if node not in trunk_ends]

    def _build_trunk_segments(
        self, path: List[str], vlan: int
    ) -> List[TrunkSegment]:
        """把 trunk 對清單包成 ``TrunkSegment``（帶本 session 選定的 vlan）。"""
        segments: List[TrunkSegment] = []
        for prev, cur in self._derive_trunk_pairs(path):
            segments.append(TrunkSegment(a_node=prev, b_node=cur, vlan=vlan))
        return segments

    # ------------------------------------------------------------------
    # VLAN 分配
    # ------------------------------------------------------------------

    def _pick_vlan(self) -> int:
        """在封閉池內挑選最小未使用 VLAN；池耗盡丟 VlanPoolExhaustedError。

        回傳：
          - 最小可用 VLAN（int）。

        例外：
          - VlanPoolExhaustedError：``[vlan_min, vlan_max]`` 內已無可用 VLAN
            （訊息含池範圍與已使用數量）。
        """
        for candidate in range(self._vlan_min, self._vlan_max + 1):
            if candidate not in self._used_vlans:
                return candidate
        raise VlanPoolExhaustedError(
            "VLAN 池已耗盡：範圍 {0}..{1} 共 {2} 個已全數使用".format(
                self._vlan_min, self._vlan_max, len(self._used_vlans)
            )
        )

    # ------------------------------------------------------------------
    # 交易原語（鎖內 reload / 鎖內決策）
    # ------------------------------------------------------------------

    def _reload_locked(self) -> None:
        """鎖內以最新 ledger 重建全部 derived index（含 next_seq 高水位，#1）。

        丟棄建構時的過期快照，吃鎖內 ``store.load()`` 回傳的完整
        ``LoadedLedger``（含 next_seq），確保併發/交錯下決策基於最新帳本。
        """
        self._sessions = {}
        self._port_owner = {}
        self._used_vlans = set()
        self._next_seq = 1
        loaded = self._store.load()
        self._rebuild_from_restored(loaded)

    def _allocate_locked(self, generator_port: str, dut_port: str) -> Session:
        """鎖內完成一筆分配決策並持久化（save 失敗回捲，#1）。

        前置：呼叫端已在 ``store.locked()`` 臨界區內、並已 ``_reload_locked()``。
        """
        path = self._graph.find_path(generator_port, dut_port)
        physical_ports = list(path)
        access_ports = self._derive_access_ports(path)

        # 硬衝突檢查：僅看 access port，於任何狀態變更前完成（FR-4）。
        conflicts: List[str] = []
        for access_port in access_ports:
            owner = self._port_owner.get(access_port)
            if owner is not None:
                conflicts.append(
                    "{0}（擁有者 session {1}）".format(access_port, owner)
                )
        if len(conflicts) > 0:
            raise HardConflictError(
                "硬衝突：下列 access port 已被使用中的 session 佔用，請先釋放：\n"
                "  {0}".format("\n  ".join(conflicts))
            )

        vlan = self._pick_vlan()
        session_id = "s-{0:04d}".format(self._next_seq)
        trunk_segments = self._build_trunk_segments(path, vlan)

        session = Session(
            session_id=session_id,
            vlan=vlan,
            generator_port=generator_port,
            dut_port=dut_port,
            path=path,
            physical_ports=physical_ports,
            trunk_segments=trunk_segments,
        )

        # 記鎖內 reload 後的高水位，供 save 失敗回捲（不用 -= 1）。
        seq_before = self._next_seq

        # COMMIT：先更新記憶體 index，再持久化；save 失敗回捲全部變更。
        self._sessions[session_id] = session
        for access_port in access_ports:
            self._port_owner[access_port] = session_id
        self._used_vlans.add(vlan)
        self._next_seq += 1

        try:
            self._store.save(self._sessions.values(), self._next_seq)
        except Exception:
            # 回捲記憶體狀態，確保分配失敗不留副作用。
            del self._sessions[session_id]
            for access_port in access_ports:
                if self._port_owner.get(access_port) == session_id:
                    del self._port_owner[access_port]
            self._used_vlans.discard(vlan)
            self._next_seq = seq_before
            raise

        return session

    def _release_locked(self, session_id: str) -> Session:
        """鎖內完成一筆釋放並持久化（save 失敗回捲，不碰 _next_seq，#1）。

        前置：呼叫端已在 ``store.locked()`` 臨界區內、並已 ``_reload_locked()``。
        """
        if session_id not in self._sessions:
            raise SessionNotFoundError(
                "找不到 session：{0}".format(session_id)
            )

        session = self._sessions[session_id]
        access_ports = self._derive_access_ports(session.path)

        # 先快照要移除的 owner 關係，便於 save 失敗時精準回捲。
        removed_owner_ports = [
            access_port
            for access_port in access_ports
            if self._port_owner.get(access_port) == session_id
        ]

        del self._sessions[session_id]
        for access_port in removed_owner_ports:
            del self._port_owner[access_port]
        self._used_vlans.discard(session.vlan)

        try:
            # 序號只增不減：release 不降 _next_seq，照舊寫入高水位。
            self._store.save(self._sessions.values(), self._next_seq)
        except Exception:
            # 回捲：還原 session、owner 關係與 VLAN（不碰 _next_seq）。
            self._sessions[session_id] = session
            for access_port in removed_owner_ports:
                self._port_owner[access_port] = session_id
            self._used_vlans.add(session.vlan)
            raise

        return session

    # ------------------------------------------------------------------
    # session 生命週期（跨行程交易）
    # ------------------------------------------------------------------

    def allocate(self, generator_port: str, dut_port: str) -> Session:
        """建立一筆新 session（設計 §Allocator、FR-3、FR-4、#1）。

        外包 ``store.locked()`` 跨行程交易：取鎖 → 鎖內重新 load 最新 ledger
        重建索引 → 決策（find_path → 推導 → 硬衝突 → 挑 VLAN → 建 Session →
        save）→ 釋鎖。save 失敗則回捲並重拋。

        參數：
          - generator_port：generator 端輸入（裸名或 ``dev:port``）。
          - dut_port：DUT 端輸入（``dev:port``）。

        回傳：
          - 新建立的 ``Session``。

        例外：
          - PathLookupError：find_path 失敗（原樣往外傳）。
          - HardConflictError：任一 access port 已被使用中 session 佔用。
          - VlanPoolExhaustedError：VLAN 池耗盡。
          - SessionStoreError：取鎖/持久化寫入失敗（已回捲記憶體狀態）。
        """
        with self._store.locked():
            self._reload_locked()
            return self._allocate_locked(generator_port, dut_port)

    def release(self, session_id: str) -> Session:
        """釋放一筆既有 session（設計 §Allocator、FR-4、#1）。

        外包 ``store.locked()`` 跨行程交易：取鎖 → 鎖內重新 load 最新 ledger
        重建索引 → 決策（移除 → save）→ 釋鎖。序號只增不減、release 不降
        _next_seq。

        參數：
          - session_id：欲釋放的 session 識別碼。

        回傳：
          - 被釋放的 ``Session``。

        例外：
          - SessionNotFoundError：session_id 不存在。
          - SessionStoreError：取鎖/持久化寫入失敗（已回捲記憶體狀態）。
        """
        with self._store.locked():
            self._reload_locked()
            return self._release_locked(session_id)

    def list_sessions(self) -> List[Session]:
        """回傳所有 session，依 session_id 尾端序號升冪排序（設計 §Allocator）。

        唯讀、不取鎖，以建構時快照為準（可能短暫落後於其他行程剛提交的寫入，
        需最新視圖者重跑指令）。

        回傳：
          - 依 ``_parse_seq_strict(session_id)`` 升冪排序的 ``Session`` 清單。
        """
        return sorted(
            self._sessions.values(),
            key=lambda session: self._parse_seq_strict(session.session_id),
        )
