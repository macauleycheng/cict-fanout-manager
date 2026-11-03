"""fanout 下發層純邏輯 planner（planner.py，設計 §5、§7）。

本模組只做「由 Session 與連線設定推導要下發什麼」的純邏輯，完全不碰 I/O、
不 import netmiko：

  - map_interface：把拓樸 port 以 interface_template 套成設備介面名，並以模組
    常數正則 fail-closed 驗證（不符即丟 PortMappingError，不臆測）。
  - plan_changes：掃 ``Session.trunk_segments`` 兩端節點，對「有連線設定」的
    fanout 產生 tagged ``add`` 變更，並以 (fanout, interface, vlan) 去重、依
    interface 排序（設計 §5、§7.1、finding #3）。
  - diff_expected_vs_actual：比對期望成員與設備回讀的 ``VlanState``，以
    (interface, vlan, tagged=True) 為鍵回缺漏清單（設計 §7.3、finding #5）。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版，建立 map_interface（fail-closed 正則）、plan_changes
           （trunk 兩端掃描 + (fanout,interface,vlan) 去重 + interface 排序）、
           diff_expected_vs_actual（tagged 嚴格比對）。純邏輯，不 import
           netmiko。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import re
from typing import Dict, List, Mapping, Optional

from topo_manager.drivers.base import PortVlanChange, VlanMember, VlanState
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


def plan_changes(
    session: "object",
    configured_fanouts: Mapping[str, object],
    templates: Mapping[str, Optional[str]],
) -> List[PortVlanChange]:
    """由一筆 Session 推導要下發的 tagged VLAN 變更清單（設計 §5、§7.1）。

    掃描 ``session.trunk_segments`` 的兩端節點（a_node/b_node），對「其 dev 有
    連線設定」的 fanout，以該 session 的 VLAN 產生 action='add' 的
    ``PortVlanChange``。同一 (fanout, interface, vlan) 只保留一筆（多 tier 下
    同一 fanout 可能橫跨多段 trunk，需去重，finding #3），最終依 interface
    升冪排序後回傳。

    INPUT：
      - session：一筆已分配的 Session（須具 ``vlan`` 與 ``trunk_segments``，
        後者每筆具 ``a_node``/``b_node``；以鴨子型別取用，避免與 allocator
        互相 import）。
      - configured_fanouts：有連線設定的 fanout 集合（Mapping，key 為 fanout
        裝置名；value 內容本函式不使用，僅以 key 判斷是否納入）。
      - templates：各 fanout 的 interface_template（Mapping，key 為 fanout 名、
        value 為 template 或 None；缺 key 時 map_interface 用預設）。

    OUTPUT：無。

    RETURN：
      - 去重且依 interface 升冪排序的 ``PortVlanChange`` 清單（List）。

    例外：
      - PortMappingError：任一 trunk 端 port 無法對映為合法介面名（由
        ``map_interface`` 丟出）。
    """
    vlan = session.vlan
    # 以 (fanout, interface, vlan) 為鍵去重，保留首見的變更。
    deduped: Dict[tuple, PortVlanChange] = {}

    for segment in session.trunk_segments:
        for node_id in (segment.a_node, segment.b_node):
            dev, port = _split_node(node_id)
            if dev not in configured_fanouts:
                continue
            interface = map_interface(dev, port, templates.get(dev))
            key = (dev, interface, vlan)
            if key in deduped:
                continue
            deduped[key] = PortVlanChange(
                fanout=dev,
                interface=interface,
                vlan=vlan,
                action="add",
                topology_node=node_id,
            )

    return sorted(deduped.values(), key=lambda change: change.interface)


def diff_expected_vs_actual(
    expected: List[PortVlanChange], actual: VlanState
) -> List[PortVlanChange]:
    """比對期望變更與設備回讀狀態，回缺漏清單（設計 §7.3、finding #5）。

    以 (interface, vlan, tagged=True) 為等值鍵：唯有當設備回讀的 ``members``
    含「同一 interface + 同一 vlan 且 tagged 為 True」的成員時，才視為已達成。
    設備上若僅有同 (interface, vlan) 的 untagged 成員，仍判為缺漏（finding #5）。

    INPUT：
      - expected：本 fanout 期望達成的 ``PortVlanChange`` 清單（action='add'）。
      - actual：設備回讀的 ``VlanState``。

    OUTPUT：無。

    RETURN：
      - 設備尚未達成（缺漏）的 ``PortVlanChange`` 清單（保留 expected 的順序與
        元素；全部達成時回空清單）。
    """
    present = {
        (member.interface, member.vlan)
        for member in actual.members
        if member.tagged is True
    }
    return [
        change
        for change in expected
        if (change.interface, change.vlan) not in present
    ]
