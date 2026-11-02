"""topo_manager 分配引擎（allocator.py，設計 §Allocator、FR-3、FR-4）。

本模組是 Phase 2 的核心邏輯層：把已驗證的 ``Topology`` 建成 ``TopologyGraph``，
依「generator port → DUT port」查出實體路徑，推導出該條路徑的 physical_ports
/ trunk_segments / access_ports，再做：

  - 硬衝突檢查（FR-4）：僅以 access port 判定；若新路徑任一 access port 已被
    使用中的 session 佔用，整筆分配失敗並列出所有衝突 port 與擁有者
    session_id，且在任何狀態變更前完成（失敗不留副作用）。
  - 全域唯一 VLAN 分配（FR-3）：在封閉池 ``[vlan_min, vlan_max]`` 內挑選最小
    可用值；池耗盡則丟 ``VlanPoolExhaustedError``。
  - session 生命週期：allocate / release / list_sessions，狀態透過
    ``JsonSessionStore`` 持久化，save 失敗一律回捲記憶體狀態後再拋。

trunk vs access 的推導（設計 §路徑推導，由 context 驗證）：
  - physical_ports = 路徑全部節點（保序）。
  - trunk_segments = 掃描相鄰 (prev, cur) 對，兩端皆為 fanout 且分屬「不同」
    fanout 裝置者即為一段 trunk（a_node=prev 近 generator、b_node=cur 近 DUT）；
    同一台 fanout 內相鄰 port 不是 trunk。
  - access_ports = physical_ports 扣除所有 trunk 端節點 = 硬衝突判定集合。

狀態封裝於實例屬性（不使用全域可變變數）：derived index（_port_owner /
_used_vlans）在 __init__ 由還原的 session 重算，確保重啟後一致。

歷史（HISTORY）：
  - 0.2.0  FEAT-005 初版，建立 TrunkSegment / Session 模型與 Allocator
           （路徑推導、硬衝突、VLAN 分配、session 生命週期、還原一致性檢查）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Dict, List, Set

from pydantic import BaseModel, ConfigDict

from topo_manager.errors import (
    HardConflictError,
    SessionNotFoundError,
    SessionStoreError,
    VlanPoolExhaustedError,
)
from topo_manager.graph import TopologyGraph
from topo_manager.models import Topology


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
      - session_id：session 識別碼，形如 ``s-0001``，全域唯一。
      - vlan：本 session 佔用的 VLAN，全域唯一且位於封閉池內。
      - generator_port：分配時指定的 generator 端輸入。
      - dut_port：分配時指定的 DUT 端輸入（``dev:port``）。
      - path：find_path 回傳的節點 id 序列（首 generator、末 DUT）。
      - physical_ports：路徑全部實體節點 id（= path，保序）。
      - trunk_segments：路徑中的 fanout↔fanout trunk 段清單。
      - access_ports：physical_ports 扣除所有 trunk 端節點，硬衝突判定集合。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    vlan: int
    generator_port: str
    dut_port: str
    path: List[str]
    physical_ports: List[str]
    access_ports: List[str]
    trunk_segments: List[TrunkSegment]


class Allocator:
    """分配引擎：路徑推導、硬衝突、VLAN 分配與 session 生命週期（§Allocator）。

    狀態封裝於實例屬性：
      - ``self._graph``：由 topology 建成的 ``TopologyGraph``。
      - ``self._fanout_names``：fanout 裝置名集合（trunk 判定用）。
      - ``self._store``：持久化層（具 load/save）。
      - ``self._sessions``：Dict[session_id, Session]。
      - ``self._port_owner``：Dict[access_port, session_id]，硬衝突索引。
      - ``self._used_vlans``：Set[int]，已佔用 VLAN。
      - ``self._next_seq``：下一個 session 序號。
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
          - store：持久化層（須具 ``load()`` 與 ``save(iterable)``）。
          - vlan_min / vlan_max：封閉 VLAN 池端點，須 0 < vlan_min <= vlan_max。

        例外：
          - ValueError：VLAN 池範圍非法。
          - SessionStoreError：還原時偵測到一致性問題（同一 access port 被兩
            個 session 佔用、或 VLAN 重複）。trunk 端 port 跨 session 共用為
            合法，不觸發。
        """
        if not (0 < vlan_min <= vlan_max):
            raise ValueError(
                "VLAN 池範圍非法：須 0 < vlan_min <= vlan_max（實得 {0}..{1}）"
                .format(vlan_min, vlan_max)
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

        restored = store.load()
        self._rebuild_from_restored(restored)

    # ------------------------------------------------------------------
    # 還原與 derived index 重建
    # ------------------------------------------------------------------

    def _rebuild_from_restored(self, restored: List[Session]) -> None:
        """由還原的 session 重建 _sessions / _port_owner / _used_vlans / _next_seq。

        還原一致性檢查（設計 §Allocator）：重算每筆 session 的 access_ports，
        若同一 access port 出現在兩個 session、或 VLAN 重複，丟
        ``SessionStoreError``；trunk 端 port 跨 session 共用為合法，不檢查。

        參數：
          - restored：store.load() 回傳的 session 清單。
        """
        max_seq = 0
        for session in restored:
            if session.vlan in self._used_vlans:
                raise SessionStoreError(
                    "ledger 還原不一致：VLAN {0} 重複出現於多個 session".format(
                        session.vlan
                    )
                )
            # 以還原後的路徑重算 access_ports，不信任檔內既存欄位，確保一致。
            access_ports = self._derive_access_ports(session.path)
            for access_port in access_ports:
                if access_port in self._port_owner:
                    raise SessionStoreError(
                        "ledger 還原不一致：access port {0} 同時被 {1} 與 {2} 佔用"
                        .format(
                            access_port,
                            self._port_owner[access_port],
                            session.session_id,
                        )
                    )
                self._port_owner[access_port] = session.session_id

            self._used_vlans.add(session.vlan)
            self._sessions[session.session_id] = session

            seq = self._parse_seq(session.session_id)
            if seq > max_seq:
                max_seq = seq

        self._next_seq = max_seq + 1

    @staticmethod
    def _parse_seq(session_id: str) -> int:
        """解析 session_id 尾端整數序號（``s-0007`` → 7）；失敗回 0。"""
        try:
            return int(session_id.split("-")[1])
        except (IndexError, ValueError):
            return 0

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
    # session 生命週期
    # ------------------------------------------------------------------

    def allocate(self, generator_port: str, dut_port: str) -> Session:
        """建立一筆新 session（設計 §Allocator、FR-3、FR-4）。

        流程：find_path → 推導 → 硬衝突檢查（access port，變更前）→ 挑 VLAN
        → 建 Session → 先更新記憶體 index 再 store.save；save 失敗則回捲並重拋。

        參數：
          - generator_port：generator 端輸入（裸名或 ``dev:port``）。
          - dut_port：DUT 端輸入（``dev:port``）。

        回傳：
          - 新建立的 ``Session``。

        例外：
          - PathLookupError：find_path 失敗（原樣往外傳）。
          - HardConflictError：任一 access port 已被使用中 session 佔用。
          - VlanPoolExhaustedError：VLAN 池耗盡。
          - SessionStoreError：持久化寫入失敗（已回捲記憶體狀態）。
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
            access_ports=access_ports,
            trunk_segments=trunk_segments,
        )

        # COMMIT：先更新記憶體 index，再持久化；save 失敗回捲全部變更。
        self._sessions[session_id] = session
        for access_port in access_ports:
            self._port_owner[access_port] = session_id
        self._used_vlans.add(vlan)
        self._next_seq += 1

        try:
            self._store.save(self._sessions.values())
        except Exception:
            # 回捲記憶體狀態，確保分配失敗不留副作用。
            del self._sessions[session_id]
            for access_port in access_ports:
                if self._port_owner.get(access_port) == session_id:
                    del self._port_owner[access_port]
            self._used_vlans.discard(vlan)
            self._next_seq -= 1
            raise

        return session

    def release(self, session_id: str) -> Session:
        """釋放一筆既有 session（設計 §Allocator、FR-4）。

        參數：
          - session_id：欲釋放的 session 識別碼。

        回傳：
          - 被釋放的 ``Session``。

        例外：
          - SessionNotFoundError：session_id 不存在。
          - SessionStoreError：持久化寫入失敗（已回捲記憶體狀態）。
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
            self._store.save(self._sessions.values())
        except Exception:
            # 回捲：還原 session、owner 關係與 VLAN。
            self._sessions[session_id] = session
            for access_port in removed_owner_ports:
                self._port_owner[access_port] = session_id
            self._used_vlans.add(session.vlan)
            raise

        return session

    def list_sessions(self) -> List[Session]:
        """回傳所有 session，依 session_id 尾端序號升冪排序（設計 §Allocator）。

        回傳：
          - 依 ``int(session_id.split('-')[1])`` 升冪排序的 ``Session`` 清單。
        """
        return sorted(
            self._sessions.values(),
            key=lambda session: int(session.session_id.split("-")[1]),
        )
