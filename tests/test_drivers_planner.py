"""drivers/planner.py 的 pytest 測試（設計 §5、§7、FEAT-001）。

涵蓋：
  - map_interface：合法套用、覆寫 template、非法 fail-closed（PortMappingError）。
  - plan_changes：以範例三層拓樸合成的 Session，驗證同一 fanout 跨多段 trunk
    的 (fanout, interface, vlan) 去重與 interface 升冪排序。
  - diff_expected_vs_actual：設備有同 (interface, vlan) 的 untagged 成員但缺
    tagged，仍判為缺漏（finding #5）。

本模組不 import netmiko（純邏輯測試）。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from topo_manager.allocator import Allocator
from topo_manager.drivers.base import PortVlanChange, VlanMember, VlanState
from topo_manager.drivers.planner import (
    diff_expected_vs_actual,
    map_interface,
    plan_changes,
)
from topo_manager.errors import PortMappingError
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore


def test_map_interface_default_template() -> None:
    """未給 template 時以預設 Eth1/{port} 套出合法介面名。"""
    assert map_interface("spine-1", "7") == "Eth1/7"


def test_map_interface_override_template() -> None:
    """覆寫 template 可改變套出的介面名。"""
    assert map_interface("spine-1", "3", "Eth2/{port}") == "Eth2/3"


def test_map_interface_illegal_fails_closed() -> None:
    """套出的介面名不符格式時 fail-closed 丟 PortMappingError。"""
    with pytest.raises(PortMappingError) as exc_info:
        map_interface("spine-1", "7", "GigabitEthernet{port}")
    message = str(exc_info.value)
    assert "spine-1" in message
    assert "GigabitEthernet7" in message


def test_plan_changes_dedup_and_sorted(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """同一 fanout 跨多段 trunk 的 (fanout, interface, vlan) 去重且依 interface 排序。

    三層拓樸中 spine-1 同時參與 spine↔agg-1 與 spine↔agg-2 兩段 trunk（分別經
    port 7、8）；某一條 session 的路徑僅經其一，但本測試以兩筆 session 合成
    「同一 fanout 多介面」並驗證排序與去重。
    """
    store = JsonSessionStore(tmp_path / "state" / "allocations.json")
    allocator = Allocator(load_topology(three_tier_path), store)
    session = allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    # 設定了所有本 session 會經過的 fanout。
    configured = {"spine-1": object(), "agg-1": object(), "leaf-1-1": object()}
    templates = {}

    changes = plan_changes(session, configured, templates)

    # 每個 (fanout, interface, vlan) 唯一（去重）。
    keys = [(c.fanout, c.interface, c.vlan) for c in changes]
    assert len(keys) == len(set(keys))

    # 全部標為該 session 的 vlan、action='add'。
    assert all(c.vlan == session.vlan for c in changes)
    assert all(c.action == "add" for c in changes)

    # 依 interface 升冪排序。
    interfaces = [c.interface for c in changes]
    assert interfaces == sorted(interfaces)

    # 應涵蓋 session trunk 兩端中有設定的 fanout。
    assert {c.fanout for c in changes}.issubset(set(configured))
    assert len(changes) > 0


def test_plan_changes_same_fanout_multi_trunk_dedup() -> None:
    """手造：同一 fanout 橫跨多段 trunk 但同一 port，(fanout,interface,vlan) 去重。"""

    class _FakeSegment:
        """最小 trunk 段替身（具 a_node/b_node）。"""

        def __init__(self, a_node: str, b_node: str) -> None:
            self.a_node = a_node
            self.b_node = b_node

    class _FakeSession:
        """最小 session 替身（具 vlan/trunk_segments）。"""

        vlan = 200
        trunk_segments = [
            _FakeSegment("spine-1:7", "agg-1:1"),
            # spine-1 以同一 port 7 再次出現於另一段（多 tier 合流場景）。
            _FakeSegment("spine-1:7", "agg-2:1"),
        ]

    configured = {"spine-1": object(), "agg-1": object(), "agg-2": object()}
    changes = plan_changes(_FakeSession(), configured, {})

    spine_changes = [c for c in changes if c.fanout == "spine-1"]
    # spine-1:7 僅產生一筆（去重）。
    assert len(spine_changes) == 1
    assert spine_changes[0].interface == "Eth1/7"
    assert spine_changes[0].vlan == 200
    # 依 interface 排序（Eth1/1 < Eth1/7）。
    interfaces = [c.interface for c in changes]
    assert interfaces == sorted(interfaces)


def test_diff_untagged_present_still_missing() -> None:
    """設備有同 (interface, vlan) 的 untagged 成員但缺 tagged，仍判缺漏（finding #5）。"""
    expected = [
        PortVlanChange(
            fanout="spine-1",
            interface="Eth1/7",
            vlan=200,
            action="add",
            topology_node="spine-1:7",
        ),
        PortVlanChange(
            fanout="spine-1",
            interface="Eth1/8",
            vlan=200,
            action="add",
            topology_node="spine-1:8",
        ),
    ]
    actual = VlanState(
        fanout="spine-1",
        members=frozenset(
            {
                # Eth1/7 有 tagged → 達成。
                VlanMember(vlan=200, interface="Eth1/7", tagged=True),
                # Eth1/8 僅有 untagged → 仍視為缺漏。
                VlanMember(vlan=200, interface="Eth1/8", tagged=False),
            }
        ),
        existing_vlans=frozenset({200}),
    )

    missing = diff_expected_vs_actual(expected, actual)
    missing_interfaces = {c.interface for c in missing}
    assert missing_interfaces == {"Eth1/8"}


def test_diff_all_present_returns_empty() -> None:
    """期望全部以 tagged 達成時回空清單。"""
    expected = [
        PortVlanChange(
            fanout="spine-1",
            interface="Eth1/7",
            vlan=200,
            action="add",
            topology_node="spine-1:7",
        ),
    ]
    actual = VlanState(
        fanout="spine-1",
        members=frozenset(
            {VlanMember(vlan=200, interface="Eth1/7", tagged=True)}
        ),
        existing_vlans=frozenset({200}),
    )
    assert diff_expected_vs_actual(expected, actual) == []
