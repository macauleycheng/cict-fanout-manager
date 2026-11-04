"""topo_manager 互聯區段驗證與獨立小圖（interconnect/layout.py，互聯設計 §2.3、§2.4）。

本模組負責兩件事：
  - ``validate_interconnect(topology)``：對拓樸的 ``interconnect`` 區段做語意
    驗證（命名空間、fanout port、端點方向、il_port 歸屬、port 唯一、組規則）；
    遇第一個違規即丟 ``InterconnectLayoutError``，訊息含 link 序號與名稱。
  - ``build_ic_graph(topology)``：建立與 ``TopologyGraph`` 零共用的獨立小圖
    ``IcGraph``（networkx.Graph + 組/DUT/port 結構），供 allocator 使用。

NOTES：
  - ``build_ic_graph`` 的前置條件是拓樸已通過 ``validate_interconnect``
    （``load_topology`` 已保證）；對未驗證的拓樸行為未定義。
  - 本模組不被 ``graph.py`` 引用，打流側行為完全不受影響。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版：validate_interconnect、IcGraph、IcGroup、
           IcDut、IcPort、summary。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from topo_manager.errors import InterconnectLayoutError, InterconnectRequestError
from topo_manager.models import Dut, IcFanout, Topology

# 一組 DUT 的台數與每台互聯 port 數（配對表固定為 4 台，不開放設定）。
IC_GROUP_SIZE = 4
IC_PORTS_PER_DUT = 4

# il_port 邏輯名格式（p0..p3 之類）；字面名若符合此格式須等於自身位置。
_LOGICAL_PORT_RE = re.compile(r"^p\d+$")
# 組名列表過長時顯示的上限。
_LIST_LIMIT = 10


@dataclass(frozen=True)
class IcPort:
    """一台 DUT 的一個互聯 port。

    欄位：
      - p_index：邏輯序 0..3（il_ports 順序）。
      - il_port：DUT 側 port 名。
      - fanout：所接互聯 fanout 名。
      - fanout_port：互聯 fanout 的 port 編號（int）。
      - node：``"<fanout>:<port>"``，硬衝突判定鍵。
    """

    p_index: int
    il_port: str
    fanout: str
    fanout_port: int
    node: str


@dataclass(frozen=True)
class IcDut:
    """組內一台 DUT。

    欄位：
      - name：DUT 名。
      - index：組內名次 0..3（slot 升冪，即 d0..d3）。
      - slot：原始 slot 編號。
      - ports：長度 4 的 IcPort，依 p0..p3。
    """

    name: str
    index: int
    slot: int
    ports: Tuple[IcPort, ...]


@dataclass(frozen=True)
class IcGroup:
    """一組 4 台同款 DUT（16 個 fanout port，同一台互聯 fanout）。"""

    name: str
    rack: str
    model: str
    fanout: str
    duts: Tuple[IcDut, ...]


def _fanout_port_number(port: str) -> Optional[int]:
    """把 fanout port 字串轉為 int；非純 ASCII 數字回 None。"""
    if port.isascii() and port.isdigit():
        return int(port)
    return None


def validate_interconnect(topology: Topology) -> None:
    """驗證 ``interconnect`` 區段（互聯設計 §2.3），違規丟 InterconnectLayoutError。

    依序檢查：命名空間 → fanout port 合法性 → 端點方向/型別 → il_port 歸屬 →
    port 唯一 → il_port 命名 → 組規則。區段不存在時為 no-op。
    """
    section = topology.interconnect
    if section is None:
        return

    # ---- 蒐集既有命名空間與 DUT 資訊 ----
    other_names: Set[str] = set()
    generator_names = {item.name for item in topology.generators}
    fanout_names = {item.name for item in topology.fanouts}
    other_names |= generator_names | fanout_names
    duts: Dict[str, Dut] = {}
    dut_rack: Dict[str, str] = {}
    for rack in topology.racks:
        other_names.add(rack.name)
        for dut in rack.duts:
            other_names.add(dut.name)
            duts[dut.name] = dut
            dut_rack[dut.name] = rack.name

    # ---- (1) 命名空間 ----
    ic_fanouts: Dict[str, IcFanout] = {}
    for fanout in section.fanouts:
        if fanout.name in ic_fanouts:
            raise InterconnectLayoutError(
                "互聯 fanout 名重複：{0}".format(fanout.name)
            )
        if fanout.name in other_names:
            raise InterconnectLayoutError(
                "互聯 fanout 名 '{0}' 與既有裝置/rack 名衝突（憑證以名稱為 key，"
                "不可撞名）".format(fanout.name)
            )
        ic_fanouts[fanout.name] = fanout

    # ---- (2) 互聯 fanout port 合法性（b 端為互聯 fanout 者）----
    for index, link in enumerate(section.links, start=1):
        endpoint = link.b
        fanout = ic_fanouts.get(endpoint.dev)
        if fanout is None:
            continue
        number = _fanout_port_number(endpoint.port)
        if number is None:
            raise InterconnectLayoutError(
                "interconnect link 第 {0} 條：互聯 fanout '{1}' 的 port '{2}' "
                "非合法 port 編號（需為 1..{3} 的整數）".format(
                    index, endpoint.dev, endpoint.port, fanout.port_count
                )
            )
        if not (1 <= number <= fanout.port_count):
            raise InterconnectLayoutError(
                "interconnect link 第 {0} 條：{1} 無 port '{2}'（有效範圍 1..{3}）"
                .format(index, endpoint.dev, endpoint.port, fanout.port_count)
            )

    # ---- (3) 端點方向與型別 ----
    for index, link in enumerate(section.links, start=1):
        a_dev, b_dev = link.a.dev, link.b.dev
        for dev in (a_dev, b_dev):
            if dev in generator_names or dev in fanout_names:
                raise InterconnectLayoutError(
                    "interconnect link 第 {0} 條：'{1}' 是打流側裝置；互聯與打流"
                    "不共用任何 switch，a 端須為 DUT、b 端須為互聯 fanout".format(
                        index, dev
                    )
                )
        if a_dev in ic_fanouts and b_dev in duts:
            raise InterconnectLayoutError(
                "interconnect link 第 {0} 條：方向寫反（a='{1}' 是互聯 fanout、"
                "b='{2}' 是 DUT）；a 端須為 DUT、b 端須為互聯 fanout".format(
                    index, a_dev, b_dev
                )
            )
        if a_dev not in duts:
            raise InterconnectLayoutError(
                "interconnect link 第 {0} 條：a 端 '{1}' 不是 DUT；a 端須為 DUT、"
                "b 端須為互聯 fanout".format(index, a_dev)
            )
        if b_dev not in ic_fanouts:
            raise InterconnectLayoutError(
                "interconnect link 第 {0} 條：b 端 '{1}' 不是互聯 fanout；a 端須為 "
                "DUT、b 端須為互聯 fanout".format(index, b_dev)
            )

    # ---- (4) a.port 必須屬於該 DUT 的 il_ports ----
    for index, link in enumerate(section.links, start=1):
        dut = duts[link.a.dev]
        if link.a.port not in dut.il_ports:
            if link.a.port in dut.tg_ports:
                raise InterconnectLayoutError(
                    "interconnect link 第 {0} 條：{1}:{2} 是 tg_port，屬於打流側，"
                    "互聯 link 須使用 il_ports".format(
                        index, dut.name, link.a.port
                    )
                )
            raise InterconnectLayoutError(
                "interconnect link 第 {0} 條：{1}:{2} 不在該 DUT 的 il_ports"
                "（{3}）".format(
                    index, dut.name, link.a.port, ", ".join(dut.il_ports)
                )
            )

    # ---- (5) port 不重複使用 ----
    seen_dut_port: Dict[Tuple[str, str], int] = {}
    seen_fanout_port: Dict[Tuple[str, int], int] = {}
    for index, link in enumerate(section.links, start=1):
        dut_key = (link.a.dev, link.a.port)
        if dut_key in seen_dut_port:
            raise InterconnectLayoutError(
                "interconnect link 第 {0} 條與第 {1} 條重複使用 DUT port "
                "{2}:{3}".format(
                    seen_dut_port[dut_key], index, dut_key[0], dut_key[1]
                )
            )
        seen_dut_port[dut_key] = index
        number = int(link.b.port)
        fanout_key = (link.b.dev, number)
        if fanout_key in seen_fanout_port:
            raise InterconnectLayoutError(
                "interconnect link 第 {0} 條與第 {1} 條重複使用互聯 fanout port "
                "{2}:{3}".format(
                    seen_fanout_port[fanout_key], index, fanout_key[0], number
                )
            )
        seen_fanout_port[fanout_key] = index

    # ---- 參與互聯的 DUT（出現在任一互聯 link）----
    participants: List[str] = []
    for link in section.links:
        if link.a.dev not in participants:
            participants.append(link.a.dev)

    # ---- (6) il_port 命名：pN 形式須等於自身位置 ----
    for name in participants:
        dut = duts[name]
        for position, il_port in enumerate(dut.il_ports):
            if _LOGICAL_PORT_RE.match(il_port) and il_port != "p{0}".format(
                position
            ):
                raise InterconnectLayoutError(
                    "DUT '{0}' 的 il_port '{1}' 在位置 {2}：pN 形式的名稱必須等於"
                    "自身位置 'p{2}'（避免與邏輯名歧義）".format(
                        name, il_port, position
                    )
                )

    # ---- (7) 組規則 ----
    _validate_groups(section, duts, dut_rack, ic_fanouts, participants)


def _validate_groups(
    section,
    duts: Dict[str, Dut],
    dut_rack: Dict[str, str],
    ic_fanouts: Dict[str, IcFanout],
    participants: List[str],
) -> None:
    """驗證互聯組規則（§2.3-7）。違規丟 InterconnectLayoutError。"""
    for name in participants:
        dut = duts[name]
        if dut.group is None or dut.slot is None:
            raise InterconnectLayoutError(
                "參與互聯的 DUT '{0}' 缺少 group 或 slot 欄位".format(name)
            )

    by_group: Dict[str, List[str]] = {}
    for name in participants:
        by_group.setdefault(duts[name].group, []).append(name)

    # DUT -> 其互聯 link 所接 fanout 名清單（依 link 順序）。
    dut_fanouts: Dict[str, List[str]] = {}
    dut_linked_ports: Dict[str, Set[str]] = {}
    for link in section.links:
        dut_fanouts.setdefault(link.a.dev, []).append(link.b.dev)
        dut_linked_ports.setdefault(link.a.dev, set()).add(link.a.port)

    for group_name in sorted(by_group):
        members = by_group[group_name]
        racks = sorted({dut_rack[item] for item in members})
        if len(racks) > 1:
            raise InterconnectLayoutError(
                "組 '{0}' 的 DUT 跨 rack（{1}）；同一組必須在同一 rack".format(
                    group_name, ", ".join(racks)
                )
            )
        if len(members) != IC_GROUP_SIZE:
            raise InterconnectLayoutError(
                "組 '{0}' 參與互聯的 DUT 為 {1} 台，須恰為 {2} 台".format(
                    group_name, len(members), IC_GROUP_SIZE
                )
            )
        models = sorted({duts[item].model for item in members})
        if len(models) > 1:
            raise InterconnectLayoutError(
                "組 '{0}' 的 DUT model 不一致（{1}）；同一組須同款".format(
                    group_name, ", ".join(models)
                )
            )
        slots = [duts[item].slot for item in members]
        if len(set(slots)) != len(slots):
            raise InterconnectLayoutError(
                "組 '{0}' 的 slot 重複：{1}".format(
                    group_name,
                    ", ".join(
                        "{0}={1}".format(item, duts[item].slot) for item in members
                    ),
                )
            )
        for item in members:
            dut = duts[item]
            if len(dut.il_ports) != IC_PORTS_PER_DUT:
                raise InterconnectLayoutError(
                    "DUT '{0}' 的 il_ports 為 {1} 個，須恰為 {2} 個".format(
                        item, len(dut.il_ports), IC_PORTS_PER_DUT
                    )
                )
            missing = [
                port for port in dut.il_ports if port not in dut_linked_ports[item]
            ]
            if len(missing) > 0:
                raise InterconnectLayoutError(
                    "DUT '{0}' 的 il_port 未全部接上互聯 fanout，缺少 link：{1}"
                    .format(item, ", ".join(missing))
                )
        group_fanouts = sorted(
            {fanout for item in members for fanout in dut_fanouts[item]}
        )
        if len(group_fanouts) > 1:
            raise InterconnectLayoutError(
                "組 '{0}' 的 link 跨互聯 fanout（{1}）；同一組 16 條 link 須在同一台"
                "互聯 fanout".format(group_name, ", ".join(group_fanouts))
            )
        fanout = ic_fanouts[group_fanouts[0]]
        group_rack = racks[0]
        if fanout.rack is not None and fanout.rack != group_rack:
            raise InterconnectLayoutError(
                "互聯 fanout '{0}' 屬於 rack '{1}'，但組 '{2}' 屬於 rack '{3}'"
                .format(fanout.name, fanout.rack, group_name, group_rack)
            )


class IcGraph:
    """互聯獨立小圖（互聯設計 §2.4）。

    屬性：
      - nx：``networkx.Graph``，節點為 ``"<dut>:<il_port>"`` 與
        ``"<fanout>:<port>"``，每條 link 一條邊（屬性 kind="interconnect"）。
      - groups：組名 → IcGroup（依組名排序）。
      - fanouts：互聯 fanout 名 → IcFanout。
    """

    def __init__(
        self,
        graph: "nx.Graph",
        groups: Dict[str, IcGroup],
        fanouts: Dict[str, IcFanout],
    ) -> None:
        self.nx = graph
        self.groups = groups
        self.fanouts = fanouts
        self._dut_group: Dict[str, str] = {}
        for group in groups.values():
            for dut in group.duts:
                self._dut_group[dut.name] = group.name

    def group_of_dut(self, name: str) -> IcGroup:
        """回傳 DUT 所屬組；DUT 不在任何互聯組丟 InterconnectRequestError。"""
        group_name = self._dut_group.get(name)
        if group_name is None:
            raise InterconnectRequestError(
                "找不到 DUT '{0}'（不在任何互聯組）".format(name)
            )
        return self.groups[group_name]

    def resolve_port(self, group: str, dut_name: str, token: str) -> IcPort:
        """解析 port token（il_port 字面或 ``pN``）為 IcPort（§5.3）。

        參數：
          - group：組名。
          - dut_name：DUT 名（須屬該組）。
          - token：il_port 字面名，或 ``p0..p3``。
        例外：
          - InterconnectRequestError：組/DUT/port 不存在。
        """
        ic_group = self.groups.get(group)
        if ic_group is None:
            raise InterconnectRequestError(
                "找不到組 '{0}'".format(group)
            )
        target = None
        for dut in ic_group.duts:
            if dut.name == dut_name:
                target = dut
        if target is None:
            raise InterconnectRequestError(
                "DUT '{0}' 不屬於組 '{1}'".format(dut_name, group)
            )
        for port in target.ports:
            if port.il_port == token:
                return port
        match = re.match(r"^p([0-3])$", token)
        if match is not None:
            return target.ports[int(match.group(1))]
        raise InterconnectRequestError(
            "DUT '{0}' 無 port '{1}'（可用：{2}，或 p0..p3）".format(
                dut_name, token, ", ".join(port.il_port for port in target.ports)
            )
        )

    def summary(self) -> dict:
        """回傳摘要 dict（fanout/組/DUT/link 數、各 fanout 已接/總 port、各組資訊）。"""
        used: Dict[str, int] = {name: 0 for name in self.fanouts}
        for group in self.groups.values():
            for dut in group.duts:
                for port in dut.ports:
                    used[port.fanout] += 1
        return {
            "fanout_count": len(self.fanouts),
            "group_count": len(self.groups),
            "dut_count": len(self._dut_group),
            "link_count": self.nx.number_of_edges(),
            "fanouts": {
                name: {"used": used[name], "total": fanout.port_count}
                for name, fanout in self.fanouts.items()
            },
            "groups": [
                {
                    "name": group.name,
                    "rack": group.rack,
                    "fanout": group.fanout,
                    "duts": [dut.name for dut in group.duts],
                }
                for group in self.groups.values()
            ],
        }


def build_ic_graph(topology: Topology) -> IcGraph:
    """由已驗證的拓樸建立 ``IcGraph``（§2.4）。

    前置條件：``validate_interconnect`` 已通過。無 interconnect 區段丟
    ``InterconnectLayoutError``。
    """
    section = topology.interconnect
    if section is None:
        raise InterconnectLayoutError("拓樸沒有 interconnect 區段")

    duts: Dict[str, Dut] = {}
    dut_rack: Dict[str, str] = {}
    for rack in topology.racks:
        for dut in rack.duts:
            duts[dut.name] = dut
            dut_rack[dut.name] = rack.name

    graph = nx.Graph()
    # (dut, il_port) -> (fanout, port 編號)
    wiring: Dict[Tuple[str, str], Tuple[str, int]] = {}
    for link in section.links:
        fanout_port = int(link.b.port)
        wiring[(link.a.dev, link.a.port)] = (link.b.dev, fanout_port)
        graph.add_edge(
            "{0}:{1}".format(link.a.dev, link.a.port),
            "{0}:{1}".format(link.b.dev, fanout_port),
            kind="interconnect",
        )

    members: Dict[str, List[str]] = {}
    for dut_name in sorted({key[0] for key in wiring}):
        members.setdefault(duts[dut_name].group, []).append(dut_name)

    groups: Dict[str, IcGroup] = {}
    for group_name in sorted(members):
        ordered = sorted(members[group_name], key=lambda item: duts[item].slot)
        ic_duts: List[IcDut] = []
        group_fanout = ""
        for position, dut_name in enumerate(ordered):
            dut = duts[dut_name]
            ports: List[IcPort] = []
            for p_index, il_port in enumerate(dut.il_ports):
                fanout, number = wiring[(dut_name, il_port)]
                group_fanout = fanout
                ports.append(
                    IcPort(
                        p_index=p_index,
                        il_port=il_port,
                        fanout=fanout,
                        fanout_port=number,
                        node="{0}:{1}".format(fanout, number),
                    )
                )
            ic_duts.append(
                IcDut(
                    name=dut_name,
                    index=position,
                    slot=dut.slot,
                    ports=tuple(ports),
                )
            )
        groups[group_name] = IcGroup(
            name=group_name,
            rack=dut_rack[ordered[0]],
            model=duts[ordered[0]].model,
            fanout=group_fanout,
            duts=tuple(ic_duts),
        )

    fanouts = {fanout.name: fanout for fanout in section.fanouts}
    return IcGraph(graph, groups, fanouts)
