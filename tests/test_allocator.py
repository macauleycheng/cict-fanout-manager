"""allocator.py 的 pytest 測試（設計 §11、FR-3、FR-4）。

涵蓋硬衝突（AC2、FR-4.3）、trunk 共用合法（FR-3.3）、VLAN 全域唯一與池內
（AC3）、最小可用 VLAN 與釋放回收（AC4/AC5）、未知 session 釋放
（AC6）、小池耗盡無副作用（AC9）、兩層/三層路徑與推導（AC13）、模擬重啟
還原（AC7）。以 tmp_path 存狀態、以範例 YAML（經 load_topology）驅動。

歷史（HISTORY）：
  - 0.2.0  FEAT-005 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from topo_manager.allocator import Allocator
from topo_manager.errors import (
    HardConflictError,
    SessionNotFoundError,
    VlanPoolExhaustedError,
)
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore


def _make_allocator(topology_path: Path, tmp_path: Path, **kwargs) -> Allocator:
    """以 tmp_path 下的獨立狀態檔建立 Allocator（測試輔助）。"""
    store = JsonSessionStore(tmp_path / "state" / "allocations.json")
    return Allocator(load_topology(topology_path), store, **kwargs)


def test_hard_conflict_blocks_and_names_owner(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """同一 (generator, dut) 再分配觸發硬衝突，訊息含擁有者 session_id（AC2、FR-4.3）。"""
    allocator = _make_allocator(three_tier_path, tmp_path)
    first = allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    with pytest.raises(HardConflictError) as exc_info:
        allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    message = str(exc_info.value)
    assert first.session_id in message
    # access port 應被列出（generator 端為一個 access port）。
    assert "ixia-1:ixia-1/1" in message


def test_shared_trunk_second_allocate_different_vlan(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """共用 trunk 的第二筆分配成功、VLAN 不同、trunk 兩端相同（FR-3.3）。"""
    allocator = _make_allocator(three_tier_path, tmp_path)
    first = allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    # dut-A2 與 dut-A1 共用 leaf-1-1 以上的 spine/agg trunk，但 access 不同
    # （用不同 generator port 以避開 generator 端 access 衝突）。
    second = allocator.allocate("ixia-1/2", "dut-A2:Eth0")

    assert first.vlan != second.vlan

    first_trunks = {(t.a_node, t.b_node) for t in first.trunk_segments}
    second_trunks = {(t.a_node, t.b_node) for t in second.trunk_segments}
    shared = first_trunks & second_trunks
    assert len(shared) > 0

    # trunk_segments 以各自 session 的 vlan 標記。
    assert all(t.vlan == first.vlan for t in first.trunk_segments)
    assert all(t.vlan == second.vlan for t in second.trunk_segments)


def test_vlan_globally_unique_within_pool(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """多筆分配的 VLAN 全域唯一且落在封閉池內（AC3）。"""
    allocator = _make_allocator(
        three_tier_path, tmp_path, vlan_min=100, vlan_max=3000
    )
    vlans = [
        allocator.allocate("ixia-1/1", "dut-A1:Eth0").vlan,
        allocator.allocate("ixia-1/2", "dut-A2:Eth0").vlan,
        allocator.allocate("trex-1/0", "dut-B1:Eth0").vlan,
    ]
    assert len(set(vlans)) == len(vlans)
    for vlan in vlans:
        assert 100 <= vlan <= 3000


def test_vlan_smallest_available_and_reclaim(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """VLAN 取最小可用值；釋放最小者後再分配會回收該值（AC4/AC5）。"""
    allocator = _make_allocator(
        three_tier_path, tmp_path, vlan_min=100, vlan_max=3000
    )
    s1 = allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    s2 = allocator.allocate("ixia-1/2", "dut-A2:Eth0")
    assert s1.vlan == 100
    assert s2.vlan == 101

    # 釋放最小者（100），下一筆分配應回收 100。
    allocator.release(s1.session_id)
    s3 = allocator.allocate("trex-1/0", "dut-B1:Eth0")
    assert s3.vlan == 100


def test_release_unknown_session_raises(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """釋放不存在的 session 觸發 SessionNotFoundError（AC6）。"""
    allocator = _make_allocator(three_tier_path, tmp_path)
    with pytest.raises(SessionNotFoundError) as exc_info:
        allocator.release("s-9999")
    assert "s-9999" in str(exc_info.value)


def test_tiny_pool_exhaustion_no_side_effects(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """VLAN 池 100..101 用盡後再分配丟 VlanPoolExhaustedError 且無副作用（AC9）。"""
    allocator = _make_allocator(
        three_tier_path, tmp_path, vlan_min=100, vlan_max=101
    )
    allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    allocator.allocate("ixia-1/2", "dut-A2:Eth0")

    sessions_before = {s.session_id: s for s in allocator.list_sessions()}
    with pytest.raises(VlanPoolExhaustedError) as exc_info:
        allocator.allocate("trex-1/0", "dut-B1:Eth0")
    message = str(exc_info.value)
    assert "100" in message and "101" in message

    # 無副作用：session 清單與既有狀態不變。
    sessions_after = {s.session_id: s for s in allocator.list_sessions()}
    assert set(sessions_after.keys()) == set(sessions_before.keys())


def test_two_tier_allocation_derivation(
    two_tier_path: Path, tmp_path: Path
) -> None:
    """兩層分配一次，path/trunk_segments/access_ports 正確（AC13）。"""
    allocator = _make_allocator(two_tier_path, tmp_path)
    session = allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    assert session.path == [
        "ixia-1:ixia-1/1",
        "core-1:1",
        "core-1:7",
        "leaf-1-1:1",
        "leaf-1-1:11",
        "dut-A1:Eth0",
    ]
    assert session.physical_ports == session.path
    # 唯一 trunk：core → leaf。
    trunks = [(t.a_node, t.b_node) for t in session.trunk_segments]
    assert trunks == [("core-1:7", "leaf-1-1:1")]
    assert session.access_ports == [
        "ixia-1:ixia-1/1",
        "core-1:1",
        "leaf-1-1:11",
        "dut-A1:Eth0",
    ]


def test_three_tier_allocation_derivation(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """三層分配一次，path/trunk_segments/access_ports 正確（AC13）。"""
    allocator = _make_allocator(three_tier_path, tmp_path)
    session = allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    assert session.path == [
        "ixia-1:ixia-1/1",
        "spine-1:1",
        "spine-1:7",
        "agg-1:1",
        "agg-1:2",
        "leaf-1-1:1",
        "leaf-1-1:11",
        "dut-A1:Eth0",
    ]
    trunks = [(t.a_node, t.b_node) for t in session.trunk_segments]
    assert trunks == [
        ("spine-1:7", "agg-1:1"),
        ("agg-1:2", "leaf-1-1:1"),
    ]
    assert session.access_ports == [
        "ixia-1:ixia-1/1",
        "spine-1:1",
        "leaf-1-1:11",
        "dut-A1:Eth0",
    ]


def test_simulated_restart_restores_state(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """重啟後以同一狀態重建 Allocator，list_sessions 逐欄位吻合且共用 trunk 不誤判（AC7）。"""
    state_path = tmp_path / "state" / "allocations.json"
    topology = load_topology(three_tier_path)

    first_allocator = Allocator(topology, JsonSessionStore(state_path))
    # 含共用 trunk 的兩筆（A1 與 A2 共用 spine/agg trunk）加上一筆獨立路徑。
    first_allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    first_allocator.allocate("ixia-1/2", "dut-A2:Eth0")
    first_allocator.allocate("trex-1/0", "dut-B1:Eth0")
    before = first_allocator.list_sessions()

    # 模擬重啟：全新 Allocator 從同一狀態檔還原，不應誤判共用 trunk。
    second_allocator = Allocator(topology, JsonSessionStore(state_path))
    after = second_allocator.list_sessions()

    assert len(after) == len(before)
    for original, restored in zip(before, after):
        assert restored.model_dump() == original.model_dump()
