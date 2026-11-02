"""topo_manager 拓樸圖引擎（graph.py，設計 §5、FR-3）—— 通用多層的核心。

本模組把已驗證的 ``Topology`` 建成有向多層圖，並提供路徑查找與摘要。關鍵
設計原則（NFR-4「層數不可寫死」）：方向與層級一律由接線拓樸動態推導，完全
不看 ``role`` 字串、不看層數。

兩個彼此分離的量（設計 §5 核心修正 N1/N3）：

  - ``dist_from_gen``（含 ``#core`` 虛擬中樞的無向 BFS 距離）：**僅用於方向
    推導**（§5.3）。``#core`` 造成的 +2 偏移對同一條實體邊的兩端等量影響，
    不改變「誰離 generator 近」的相對大小，故適合定方向、不適合當層級。
  - ``tier_level``（收縮圖層級，§5.6.1）：把同一台 fanout 的所有 port 收縮為
    單一超節點、移除 ``#core``，再從 generator 多源 BFS 得到的層級
    （generator=0、第一層 fanout=1、…、dut=最末）。**用於 tiers 摘要與路徑
    層序斷言**。

節點 id 一律字串 ``"<dev>:<port>"``；每台 fanout 另建虛擬中樞
``"<dev>:#core"``（kind='fanout_core'），於摘要與路徑輸出中一律排除。

歷史（HISTORY）：
  - 0.1.0  FEAT-003 初版，建立 TopologyGraph（建節點/邊、§5.3 方向推導與
           等距偵測、§5.6.1 收縮圖 tier_level、§5.5 find_path、§5.6.2
           summary）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

import networkx as nx

from topo_manager.errors import PathLookupError, TopologyGraphError
from topo_manager.models import Topology

# 虛擬中樞 port 名；節點 id 形如 "<dev>:#core"，kind 標為 fanout_core。
_FANOUT_CORE_PORT = "#core"

# 節點屬性 kind 的合法值。
_KIND_GENERATOR = "generator"
_KIND_FANOUT = "fanout"
_KIND_DUT = "dut"
_KIND_FANOUT_CORE = "fanout_core"


def _node_id(dev: str, port: str) -> str:
    """組出節點 id 字串 ``"<dev>:<port>"``（設計 §5.1）。"""
    return "{0}:{1}".format(dev, port)


class TopologyGraph:
    """由 ``Topology`` 建成的有向多層圖，提供路徑查找與摘要（設計 §5.4）。

    狀態一律封裝於實例屬性（不使用全域可變變數，對齊規範精神）：
      - ``self._digraph``：有向圖（networkx.DiGraph），含實體線（A 類，已定
        方向）與設備內部連通（B 類，雙向）。
      - ``self._tier_level``：Dict[str, int]，每個實體節點 id → 收縮圖層級
        （§5.6.1）；不含 ``#core`` 虛擬節點。
      - ``self._fanout_roles``：Dict[str, str]，fanout 裝置名 → role（供摘要
        分組與路徑 role 子序列斷言用）。
    """

    def __init__(self, topology: Topology) -> None:
        """依設計 §5.1~§5.3 建好有向圖並快取 §5.6.1 的 tier_level。

        參數：
          - topology：已通過 schema 與參照完整性驗證的 ``Topology``。

        例外：
          - TopologyGraphError：偵測到等距（同層）實體連線（§5.3），
            Phase 1 不支援。
        """
        self._fanout_roles: Dict[str, str] = {
            fanout.name: fanout.role for fanout in topology.fanouts
        }
        # fanout 裝置名集合，供建節點/邊時快速判斷端點是否為 fanout。
        self._fanout_names: Set[str] = set(self._fanout_roles.keys())

        # 計數語意的原始來源（§5.6.2）：由模型直接取，不受「用到才建」影響。
        self._generator_port_count = sum(
            len(generator.ports) for generator in topology.generators
        )
        self._dut_count = sum(len(rack.duts) for rack in topology.racks)
        self._dut_port_count = sum(
            len(dut.tg_ports) for rack in topology.racks for dut in rack.duts
        )
        self._fanout_count = len(topology.fanouts)

        # 建無向底圖（A+B）、依 §5.3 定方向並偵測等距邊，產出有向圖。
        physical_edges = self._collect_physical_edges(topology)
        internal_edges = self._collect_internal_edges(topology, physical_edges)
        self._digraph = self._build_digraph(
            topology, physical_edges, internal_edges
        )

        # 收縮圖層級（§5.6.1），快取供摘要與路徑斷言共用。
        self._tier_level = self._compute_tier_level(topology, physical_edges)

    @classmethod
    def from_topology(cls, topology: Topology) -> "TopologyGraph":
        """由 ``Topology`` 建立 ``TopologyGraph``（設計 §5.4 的替代建構入口）。"""
        return cls(topology)

    # ------------------------------------------------------------------
    # 建圖：節點與兩類 edge（§5.1、§5.2）
    # ------------------------------------------------------------------

    def _collect_physical_edges(
        self, topology: Topology
    ) -> List[Tuple[str, str]]:
        """蒐集 A 類實體線 edge（來自 links，設計 §5.2(A)）。

        每條 link 的兩端節點 id 組成一條無向待定方向邊；回傳 (a_id, b_id)
        清單，順序與 links 相同以利等距偵測報錯定位。
        """
        edges: List[Tuple[str, str]] = []
        for link in topology.links:
            a_id = _node_id(link.a.dev, link.a.port)
            b_id = _node_id(link.b.dev, link.b.port)
            edges.append((a_id, b_id))
        return edges

    def _collect_fanout_port_nodes(
        self, topology: Topology, physical_edges: List[Tuple[str, str]]
    ) -> Dict[str, Set[str]]:
        """依「用到才建」蒐集每台 fanout 實際出現在 links 的 port 節點。

        回傳 Dict[fanout_name, Set[node_id]]。只納入實際出現在 physical_edges
        端點、且其 dev 為 fanout 的節點（設計 §5.1）。
        """
        fanout_ports: Dict[str, Set[str]] = {
            name: set() for name in self._fanout_names
        }
        for a_id, b_id in physical_edges:
            for node in (a_id, b_id):
                dev = self._dev_of(node)
                if dev in self._fanout_names:
                    fanout_ports[dev].add(node)
        return fanout_ports

    def _collect_internal_edges(
        self, topology: Topology, physical_edges: List[Tuple[str, str]]
    ) -> List[Tuple[str, str]]:
        """蒐集 B 類設備內部連通 edge（§5.2(B)）：port ↔ #core 中樞。

        每台 fanout 的每個已建 port 節點與其 ``"<dev>:#core"`` 中樞相連；
        回傳 (port_id, core_id) 清單（有向圖中再建為雙向）。
        """
        fanout_ports = self._collect_fanout_port_nodes(topology, physical_edges)
        edges: List[Tuple[str, str]] = []
        for fanout_name, port_nodes in fanout_ports.items():
            core_id = _node_id(fanout_name, _FANOUT_CORE_PORT)
            for port_node in sorted(port_nodes):
                edges.append((port_node, core_id))
        return edges

    def _node_kind(self, node_id: str, topology: Topology) -> Tuple[str, str, str]:
        """由節點 id 推斷 (kind, dev, port)；dev/port 以首個 ':' 切出。

        注意：節點 id 可能含多個 ':'（port 名本身含 ':'），故 dev 取第一段、
        port 取其餘。``#core`` 節點歸類為 fanout_core。
        """
        dev = self._dev_of(node_id)
        port = node_id[len(dev) + 1 :]
        if port == _FANOUT_CORE_PORT:
            return (_KIND_FANOUT_CORE, dev, port)
        if dev in self._fanout_names:
            return (_KIND_FANOUT, dev, port)
        if dev in {generator.name for generator in topology.generators}:
            return (_KIND_GENERATOR, dev, port)
        return (_KIND_DUT, dev, port)

    @staticmethod
    def _dev_of(node_id: str) -> str:
        """取節點 id 的 dev 段（第一個 ':' 之前）。"""
        return node_id.split(":", 1)[0]

    def _build_digraph(
        self,
        topology: Topology,
        physical_edges: List[Tuple[str, str]],
        internal_edges: List[Tuple[str, str]],
    ) -> "nx.DiGraph":
        """建有向圖：先無向 BFS 定 dist_from_gen，再有向化並偵測等距邊。

        步驟對應設計 §5.3：
          1. 以 A+B 全部 edge 建無向圖 ``undirected``。
          2. 從所有 generator 節點多源 BFS 得 ``dist_from_gen``（僅供相對
             比較）。
          3. 對每條 A 類實體邊比較兩端距離定方向；等距者以「排除該邊重算」
             確認後丟 TopologyGraphError。
          4. B 類 edge 一律雙向。

        回傳：含節點屬性（kind/dev/port，fanout 另存 role）的有向圖。
        """
        undirected = nx.Graph()

        # 先把所有節點加入無向圖並標屬性，確保孤立節點（理論上不會有）也在圖中。
        all_nodes: Set[str] = set()
        for a_id, b_id in physical_edges:
            all_nodes.add(a_id)
            all_nodes.add(b_id)
        for port_id, core_id in internal_edges:
            all_nodes.add(port_id)
            all_nodes.add(core_id)

        for node in all_nodes:
            undirected.add_node(node)
        undirected.add_edges_from(physical_edges)
        undirected.add_edges_from(internal_edges)

        generator_nodes = [
            node
            for node in all_nodes
            if self._node_kind(node, topology)[0] == _KIND_GENERATOR
        ]

        dist_from_gen = self._multi_source_distances(undirected, generator_nodes)

        digraph = nx.DiGraph()
        for node in all_nodes:
            kind, dev, port = self._node_kind(node, topology)
            attrs: Dict[str, Any] = {"kind": kind, "dev": dev, "port": port}
            if kind == _KIND_FANOUT:
                attrs["role"] = self._fanout_roles[dev]
            digraph.add_node(node, **attrs)

        # (3) A 類實體邊有向化 + 等距偵測。
        for a_id, b_id in physical_edges:
            dist_a = dist_from_gen.get(a_id)
            dist_b = dist_from_gen.get(b_id)

            # 任一端不可從 generator 到達（理論上不應發生於合法範例）：
            # 無法以距離定方向，保守建雙向以免誤判為等距。
            if dist_a is None or dist_b is None:
                digraph.add_edge(a_id, b_id)
                digraph.add_edge(b_id, a_id)
                continue

            if dist_a < dist_b:
                digraph.add_edge(a_id, b_id)
            elif dist_a > dist_b:
                digraph.add_edge(b_id, a_id)
            else:
                # 快篩判定等距；以「排除此邊」重算確認（§5.3，避免自我參照）。
                self._reject_if_equidistant(
                    undirected, generator_nodes, a_id, b_id
                )
                # 排除後若不再相等，依重算結果定方向（病態橫接，非同層直連）。
                self._add_directed_by_excluded_distance(
                    digraph, undirected, generator_nodes, a_id, b_id
                )

        # (4) B 類內部連通邊：雙向。
        for port_id, core_id in internal_edges:
            digraph.add_edge(port_id, core_id)
            digraph.add_edge(core_id, port_id)

        return digraph

    @staticmethod
    def _multi_source_distances(
        graph: "nx.Graph", sources: List[str]
    ) -> Dict[str, int]:
        """對無向圖從多個來源做 BFS，回傳每個可達節點的最短跳數。

        以「新增一個超級來源、連到所有 sources」的等價手法實作多源 BFS；
        回傳的距離已扣除超級來源那一跳。無來源時回傳空 dict。
        """
        if len(sources) == 0:
            return {}
        working = graph.copy()
        super_source = "__super_source__"
        working.add_node(super_source)
        for source in sources:
            working.add_edge(super_source, source)
        raw = nx.single_source_shortest_path_length(working, super_source)
        distances: Dict[str, int] = {}
        for node, length in raw.items():
            if node == super_source:
                continue
            distances[node] = length - 1
        return distances

    def _reject_if_equidistant(
        self,
        undirected: "nx.Graph",
        generator_nodes: List[str],
        a_id: str,
        b_id: str,
    ) -> None:
        """以「排除 (a,b) 邊」重算兩端距 generator 的距離，相等即丟錯（§5.3）。

        參數：
          - undirected：含全部 A+B edge 的無向底圖。
          - generator_nodes：所有 generator 節點 id。
          - a_id / b_id：待判實體邊兩端。

        例外：
          - TopologyGraphError：排除該邊後兩端距離相等（等距/同層直連），
            訊息含兩端 id 與相等距離值。
        """
        pruned = undirected.copy()
        if pruned.has_edge(a_id, b_id):
            pruned.remove_edge(a_id, b_id)
        distances = self._multi_source_distances(pruned, generator_nodes)
        dist_a = distances.get(a_id)
        dist_b = distances.get(b_id)
        if dist_a is not None and dist_b is not None and dist_a == dist_b:
            raise TopologyGraphError(
                "拓樸建圖失敗：偵測到等距（同層）實體連線，Phase 1 不支援。\n"
                "  link：{0} — {1}（排除此線後，兩端距 generator 皆為 {2} 跳）\n"
                "  請移除該同層直連，或改經上層 fanout 轉接。".format(
                    a_id, b_id, dist_a
                )
            )

    def _add_directed_by_excluded_distance(
        self,
        digraph: "nx.DiGraph",
        undirected: "nx.Graph",
        generator_nodes: List[str],
        a_id: str,
        b_id: str,
    ) -> None:
        """排除該邊重算距離後，依不相等的距離為病態橫接定方向（§5.3 範圍註）。

        僅在快篩判定兩端相等、但排除該邊後距離不再相等時呼叫（非同層直連的
        病態橫接）。距離仍相等的情形已於 ``_reject_if_equidistant`` 丟錯。
        若重算後仍無法判定（皆不可達或仍相等），保守建雙向。
        """
        pruned = undirected.copy()
        if pruned.has_edge(a_id, b_id):
            pruned.remove_edge(a_id, b_id)
        distances = self._multi_source_distances(pruned, generator_nodes)
        dist_a = distances.get(a_id)
        dist_b = distances.get(b_id)
        if dist_a is not None and dist_b is not None and dist_a < dist_b:
            digraph.add_edge(a_id, b_id)
        elif dist_a is not None and dist_b is not None and dist_a > dist_b:
            digraph.add_edge(b_id, a_id)
        else:
            digraph.add_edge(a_id, b_id)
            digraph.add_edge(b_id, a_id)

    # ------------------------------------------------------------------
    # 層級計算（§5.6.1 收縮圖 tier_level）
    # ------------------------------------------------------------------

    def _compute_tier_level(
        self, topology: Topology, physical_edges: List[Tuple[str, str]]
    ) -> Dict[str, int]:
        """建收縮圖 G_tier 並以多源 BFS 算每個實體節點的 tier_level（§5.6.1）。

        收縮規則：取 A 類實體邊；把同一台 fanout 的所有 port 節點收縮為單一
        超節點（以 fanout 裝置名當超節點 id）；generator/dut 節點不變；不納入
        ``#core`` 虛擬中樞。從 generator 超節點多源 BFS 得距離即 tier_level，
        再映射回每個實體節點（同台 fanout 所有 port 共用）。

        回傳：Dict[node_id, tier_level]，僅含實體節點（不含 #core）。
        """
        contracted = nx.Graph()

        def super_node(node_id: str) -> str:
            """把實體節點映射到收縮圖的超節點 id：fanout→裝置名，其餘→原 id。"""
            kind, dev, _ = self._node_kind(node_id, topology)
            if kind == _KIND_FANOUT:
                return dev
            return node_id

        # 依 A 類實體邊建收縮圖；收縮後同台 fanout 的 self-loop 自然被忽略。
        physical_nodes: Set[str] = set()
        for a_id, b_id in physical_edges:
            physical_nodes.add(a_id)
            physical_nodes.add(b_id)
            super_a = super_node(a_id)
            super_b = super_node(b_id)
            contracted.add_node(super_a)
            contracted.add_node(super_b)
            if super_a != super_b:
                contracted.add_edge(super_a, super_b)

        generator_supers = [
            node
            for node in physical_nodes
            if self._node_kind(node, topology)[0] == _KIND_GENERATOR
        ]
        super_distances = self._multi_source_distances(
            contracted, generator_supers
        )

        tier_level: Dict[str, int] = {}
        for node in physical_nodes:
            level = super_distances.get(super_node(node))
            if level is not None:
                tier_level[node] = level
        return tier_level

    # ------------------------------------------------------------------
    # 路徑查找（§5.5）
    # ------------------------------------------------------------------

    def find_path(self, generator_port: str, dut_port: str) -> List[str]:
        """回傳 generator_port 到 dut_port 的最短實體路徑（節點 id 序列）。

        參數：
          - generator_port：generator 端，允許裸名（如 ``ixia-1/1``）或
            ``dev:port``；裸名以「比對節點屬性 port」在 generator 節點中解析。
          - dut_port：DUT 端，一律要求 ``dev:port`` 形式（裸名必不唯一）。

        回傳：
          - 節點 id 清單，首為 generator 節點、末為 dut 節點，已清洗掉
            ``kind=='fanout_core'`` 的 ``#core`` 虛擬節點。

        例外：
          - PathLookupError：端點不存在、裸名解析不唯一、dut_port 未含 ':'、
            或兩端無連通路徑。
        """
        src = self._resolve_generator_node(generator_port)
        dst = self._resolve_dut_node(dut_port)

        try:
            raw_path = nx.shortest_path(self._digraph, src, dst)
        except (nx.NetworkXNoPath, nx.NodeNotFound) as exc:
            raise PathLookupError(
                "{0} 與 {1} 之間無可用路徑".format(generator_port, dut_port)
            ) from exc

        # 輸出清洗：移除 kind=='fanout_core' 的 #core 虛擬中樞。
        cleaned: List[str] = []
        for node in raw_path:
            if self._digraph.nodes[node].get("kind") == _KIND_FANOUT_CORE:
                continue
            cleaned.append(node)
        return cleaned

    def _resolve_generator_node(self, generator_port: str) -> str:
        """解析 generator 端輸入為節點 id（設計 §5.5 裸名屬性比對）。

        先視為 ``dev:port``：若該節點存在且 kind=='generator' 直接採用。
        否則以「比對節點屬性 port == 輸入」在所有 generator 節點中解析，
        命中 0 筆或多筆皆丟 PathLookupError。
        """
        if self._digraph.has_node(generator_port):
            if self._digraph.nodes[generator_port].get("kind") == _KIND_GENERATOR:
                return generator_port

        matches: List[str] = []
        for node, data in self._digraph.nodes(data=True):
            if data.get("kind") == _KIND_GENERATOR and data.get("port") == generator_port:
                matches.append(node)

        if len(matches) == 0:
            raise PathLookupError(
                "找不到 generator port：{0}".format(generator_port)
            )
        if len(matches) > 1:
            raise PathLookupError(
                "generator port '{0}' 不唯一，候選：{1}".format(
                    generator_port, ", ".join(sorted(matches))
                )
            )
        return matches[0]

    def _resolve_dut_node(self, dut_port: str) -> str:
        """解析 DUT 端輸入為節點 id（設計 §5.5：一律要求 ``dev:port``）。

        不含 ':' 直接丟 PathLookupError；節點不存在亦丟 PathLookupError。
        """
        if ":" not in dut_port:
            raise PathLookupError(
                "DUT 端點須為 'dev:port' 形式，例如 'dut-A1:Eth0'（實得 '{0}'）"
                .format(dut_port)
            )
        if not self._digraph.has_node(dut_port):
            raise PathLookupError(
                "找不到 DUT 端點：{0}".format(dut_port)
            )
        return dut_port

    # ------------------------------------------------------------------
    # 摘要（§5.6.2）
    # ------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """回傳拓樸摘要 dict（設計 §5.6.2）。

        鍵：
          - generator_port_count：generator port 總數。
          - dut_count：DUT 台數。
          - dut_port_count：DUT tg_port 總數。
          - fanout_count：fanout 台數。
          - nodes_by_role：以 role 分組的 fanout 台數（Dict[str,int]）。
          - tiers：各 tier_level 的實體節點數（Dict[int,int]），key 為 int
            tier_level，排除所有 kind=='fanout_core' 節點。
        """
        nodes_by_role: Dict[str, int] = {}
        for role in self._fanout_roles.values():
            nodes_by_role[role] = nodes_by_role.get(role, 0) + 1

        tiers: Dict[int, int] = {}
        for node_id, level in self._tier_level.items():
            # tier_level 僅含實體節點（#core 於 §5.6.1 已排除），此處再保險。
            if self._digraph.nodes[node_id].get("kind") == _KIND_FANOUT_CORE:
                continue
            tiers[level] = tiers.get(level, 0) + 1

        return {
            "generator_port_count": self._generator_port_count,
            "dut_count": self._dut_count,
            "dut_port_count": self._dut_port_count,
            "fanout_count": self._fanout_count,
            "nodes_by_role": nodes_by_role,
            "tiers": tiers,
        }

    def tier_level_of(self, node_id: str) -> int:
        """回傳指定實體節點的 tier_level（供測試路徑層序斷言，§11.3）。

        參數：
          - node_id：實體節點 id（不應為 #core 虛擬節點）。

        回傳：
          - 該節點的 tier_level（int）。

        例外：
          - KeyError：node_id 不在 tier_level 快取中（非實體節點或不可達）。
        """
        return self._tier_level[node_id]
