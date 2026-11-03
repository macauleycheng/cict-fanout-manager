"""fanout 下發層純邏輯 planner（planner.py，設計 §5、§7）。

本模組只做「由 Session 與連線設定推導要下發什麼」的純邏輯，完全不碰 I/O、
不 import netmiko：

  - map_interface：把拓樸 port 以 interface_template 套成設備介面名，並以模組
    常數正則 fail-closed 驗證（不符即丟 PortMappingError，不臆測）。
  - _derive_edge_nodes：由 ``session.path`` 扣除所有 trunk 端節點，得邊緣節點
    （與 allocator ``_derive_access_ports`` 同邏輯，決策 5）。
  - plan_changes：掃 ``Session.trunk_segments`` 兩端節點產 UPLINK（tagged）
    變更，並掃 ``_derive_edge_nodes`` 產 EDGE（native/tunnel access）變更，以
    (fanout, interface, vlan) 去重（同鍵異 role fail-closed）、依 interface
    排序（設計 §5、§7.1、決策 5、finding #3/#4/#5）。
  - diff_expected_vs_actual：依 ``change.role`` 分流——UPLINK 以
    (interface, vlan, tagged=True) 比對、EDGE 以 ``(interface, "Access")`` 是否
    在 ``actual.tunnel_modes`` 判達成（設計 §7.3、決策 6、finding #5）。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版，建立 map_interface（fail-closed 正則）、plan_changes
           （trunk 兩端掃描 + (fanout,interface,vlan) 去重 + interface 排序）、
           diff_expected_vs_actual（tagged 嚴格比對）。純邏輯，不 import
           netmiko。
  - 0.5.0  Phase 4 QinQ/dot1q-tunnel 擴充（決策 5、6）：新增 ``_derive_edge_nodes``
           純函式；``plan_changes`` 加邊緣掃描分支（split→過濾→map 順序，
           generator/DUT 於過濾被排除），trunk 分支顯式帶 UPLINK role、邊緣分支
           帶 EDGE role，去重鍵維持 (fanout,interface,vlan)、同鍵異 role 丟
           ``PortMappingError`` fail-closed；``diff_expected_vs_actual`` 依 role
           分流判定。import ``PortRole_E``。純邏輯，不 import netmiko。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Optional

from topo_manager.drivers.base import (
    PortRole_E,
    PortVlanChange,
    VlanMember,
    VlanState,
)
from topo_manager.errors import PortMappingError

# 設備介面名的合法格式（fail-closed 驗證用）。TODO(4a)：目前僅支援
# ``EthX/Y``（Edgecore/Cisco 風格），後續可依設備型號擴充或改為可設定。
_INTERFACE_NAME_RE = re.compile(r"^Eth\d+/\d+$")

# interface_template 的預設值（當呼叫端未提供 template 時使用，設計 §6）。
_DEFAULT_INTERFACE_TEMPLATE = "Eth1/{port}"


def map_interface(
    fanout: str, topology_port: str, template: Optional[str] = None
) -> str:
    """把拓樸 port 以 interface_template 套成設備介面名（fail-closed，設計 §5）。

    INPUT：
      - fanout：目標 fanout 裝置名（str），僅供錯誤訊息標示。
      - topology_port：拓樸中該 fanout 端的 port 值（str，如 ``"7"``）。
      - template：介面名樣板（str，須含 ``"{port}"``）；None 時用模組預設
        ``"Eth1/{port}"``。

    OUTPUT：無。

    RETURN：
      - 套出並通過格式驗證的設備介面名（str，如 ``"Eth1/7"``）。

    例外：
      - PortMappingError：套出的介面名不符 ``_INTERFACE_NAME_RE``（fail-closed，
        不臆測），訊息含 fanout、原始 port、套出的介面名與期望格式。
    """
    effective_template = template if template is not None else _DEFAULT_INTERFACE_TEMPLATE
    interface = effective_template.replace("{port}", str(topology_port))
    if _INTERFACE_NAME_RE.match(interface) is None:
        raise PortMappingError(
            "無法將拓樸 port 對映為合法介面名（fanout {0}）：port {1} 經樣板 "
            "'{2}' 套出 '{3}'，不符期望格式 {4}".format(
                fanout,
                topology_port,
                effective_template,
                interface,
                _INTERFACE_NAME_RE.pattern,
            )
        )
    return interface


def _split_node(node_id: str) -> List[str]:
    """把 ``"<dev>:<port>"`` 節點 id 切成 ``[dev, port]``（port 可含冒號）。

    INPUT：
      - node_id：節點 id 字串（str）。

    OUTPUT：無。

    RETURN：
      - 長度 2 的清單 ``[dev, port]``（以第一個 ':' 切）。
    """
    return node_id.split(":", 1)


def _derive_edge_nodes(session: "object") -> List[str]:
    """由 session.path 扣除所有 trunk 端節點，回邊緣節點清單（決策 5）。

    與 allocator ``_derive_access_ports`` 演算法等價（path 扣 trunk 端），兩處
    各自為純邏輯、互不依賴（planner 不 import allocator）。以鴨子型別取用
    ``session.path`` / ``session.trunk_segments``；session 無 ``path`` 屬性時回
    ``[]``（避免僅具 ``trunk_segments`` 的替身崩壞）。

    INPUT：
      - session：一筆 Session（具 ``path``（節點 id 保序 list）與
        ``trunk_segments``（每筆具 ``a_node``/``b_node``）；以鴨子型別取用）。

    OUTPUT：無。

    RETURN：
      - 邊緣節點 id 清單（List[str]，保序；session 無 ``path`` 時為空清單）。

    例外：不拋出。
    """
    path = getattr(session, "path", None)
    if path is None:
        return []
    trunk_ends = set()
    for segment in session.trunk_segments:
        trunk_ends.add(segment.a_node)
        trunk_ends.add(segment.b_node)
    return [node for node in path if node not in trunk_ends]


def plan_changes(
    session: "object",
    configured_fanouts: Mapping[str, object],
    templates: Mapping[str, Optional[str]],
) -> List[PortVlanChange]:
    """由一筆 Session 推導要下發的 tagged VLAN 變更清單（設計 §5、§7.1）。

    掃描 ``session.trunk_segments`` 的兩端節點（a_node/b_node）產生互聯
    （UPLINK）變更，並掃 ``_derive_edge_nodes(session)`` 產生邊緣（EDGE）變更，
    皆僅對「其 dev 有連線設定」的 fanout 以該 session 的 VLAN 產出 action='add'
    的 ``PortVlanChange``。邊緣分支取值順序嚴格為：``_split_node`` → 若 dev 不
    在 configured_fanouts 則 continue → 否則才 ``map_interface``（把過濾置於
    map_interface 之前，使 generator/DUT 等非 fanout 節點不會誤觸發對映，決策
    5、finding #5）。去重鍵維持 (fanout, interface, vlan)（不含 role，維持
    FR-3.3 不變量）：同鍵同 role 略過、同鍵異 role 視為資料異常 fail-closed 丟
    ``PortMappingError``（finding #4）。最終依 interface 升冪排序後回傳。

    INPUT：
      - session：一筆已分配的 Session（須具 ``vlan`` 與 ``trunk_segments``，
        後者每筆具 ``a_node``/``b_node``；邊緣推導另取 ``path``；以鴨子型別取
        用，避免與 allocator 互相 import）。
      - configured_fanouts：有連線設定的 fanout 集合（Mapping，key 為 fanout
        裝置名；value 內容本函式不使用，僅以 key 判斷是否納入）。
      - templates：各 fanout 的 interface_template（Mapping，key 為 fanout 名、
        value 為 template 或 None；缺 key 時 map_interface 用預設）。

    OUTPUT：無。

    RETURN：
      - 去重且依 interface 升冪排序的 ``PortVlanChange`` 清單（List，含 UPLINK
        與 EDGE 兩類）。

    例外：
      - PortMappingError：任一 fanout 端 port 無法對映為合法介面名（由
        ``map_interface`` 丟出），或同 (fanout, interface, vlan) 出現 EDGE 與
        UPLINK 角色衝突（資料異常 fail-closed）。
    """
    vlan = session.vlan
    # 以 (fanout, interface, vlan) 為鍵去重，保留首見的變更。
    deduped: Dict[tuple, PortVlanChange] = {}

    def _put(dev: str, interface: str, node_id: str, role: PortRole_E) -> None:
        """把一筆變更放入去重表：同鍵同 role 略過、同鍵異 role fail-closed。"""
        key = (dev, interface, vlan)
        existing = deduped.get(key)
        if existing is not None:
            if existing.role != role:
                raise PortMappingError(
                    "同一 fanout 同一介面同一 VLAN 不可同時為邊緣與互聯："
                    "{0} {1} VLAN {2}".format(dev, interface, vlan)
                )
            return
        deduped[key] = PortVlanChange(
            fanout=dev,
            interface=interface,
            vlan=vlan,
            action="add",
            topology_node=node_id,
            role=role,
        )

    # 互聯（UPLINK）：掃 trunk 兩端節點。
    for segment in session.trunk_segments:
        for node_id in (segment.a_node, segment.b_node):
            dev, port = _split_node(node_id)
            if dev not in configured_fanouts:
                continue
            interface = map_interface(dev, port, templates.get(dev))
            _put(dev, interface, node_id, PortRole_E.UPLINK)

    # 邊緣（EDGE）：掃 path 扣 trunk 端的節點（順序同上：split → 過濾 → map）。
    for node_id in _derive_edge_nodes(session):
        dev, port = _split_node(node_id)
        if dev not in configured_fanouts:
            continue
        interface = map_interface(dev, port, templates.get(dev))
        _put(dev, interface, node_id, PortRole_E.EDGE)

    return sorted(deduped.values(), key=lambda change: change.interface)


def diff_expected_vs_actual(
    expected: List[PortVlanChange], actual: VlanState
) -> List[PortVlanChange]:
    """比對期望變更與設備回讀狀態，依 role 分流判定缺漏（決策 6、finding #5）。

    依 ``change.role`` 分流：

      - UPLINK（互聯）：以 (interface, vlan, tagged=True) 為等值鍵——唯有當
        ``actual.members`` 含「同一 interface + 同一 vlan 且 tagged 為 True」的
        成員時才算達成；僅有同 (interface, vlan) 的 untagged 成員仍判缺漏。
      - EDGE（邊緣）：以 ``(interface, "Access")`` 是否在 ``actual.tunnel_modes``
        判達成（tunnel mode 為 Access 表邊緣 tunnel 已設；主判定建立在待本次
        live 確認可靠讀回的 ``show dot1q-tunnel`` Mode 上，而非未確認的
        ``show vlan`` native 旗標）。

    INPUT：
      - expected：本 fanout 期望達成的 ``PortVlanChange`` 清單（action='add'）。
      - actual：設備回讀的 ``VlanState``。

    OUTPUT：無。

    RETURN：
      - 設備尚未達成（缺漏）的 ``PortVlanChange`` 清單（保留 expected 的順序與
        元素；全部達成時回空清單）。
    """
    tagged_present = {
        (member.interface, member.vlan)
        for member in actual.members
        if member.tagged is True
    }
    access_present = {
        interface
        for (interface, mode) in actual.tunnel_modes
        if mode == "Access"
    }

    missing: List[PortVlanChange] = []
    for change in expected:
        if change.role == PortRole_E.EDGE:
            satisfied = change.interface in access_present
        else:
            satisfied = (change.interface, change.vlan) in tagged_present
        if not satisfied:
            missing.append(change)
    return missing
