"""滿規模三層範例的分配整合測試（設計 §11、§4 滿規模、AC12）。

以 examples/topology-fullscale.yaml（6 rack × 24 DUT × 8 tg_port）驅動 Allocator，
狀態存於 tmp_path。涵蓋：
  - 批次分配多筆（含同一 rack 內多台 DUT）皆成功，VLAN 互異且落在封閉池內。
  - 蓄意撞一個 access port（重用同一 generator port / 同一 DUT port）觸發
    HardConflictError。
  - 釋放一筆後可用同一資源重新分配成功。
  - 兩筆共用 trunk 的 session 具相同 trunk_segments（端點）但不同 VLAN，證明
    FR-3.3 的軟衝突（trunk 共用）。

歷史（HISTORY）：
  - 0.2.0  FEAT-006 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from topo_manager.allocator import Allocator
from topo_manager.errors import HardConflictError
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore


@pytest.fixture
def fullscale_path(examples_dir: Path) -> Path:
    """回傳滿規模範例 YAML 的絕對路徑。"""
    return examples_dir / "topology-fullscale.yaml"


def _make_allocator(topology_path: Path, tmp_path: Path, **kwargs) -> Allocator:
    """以 tmp_path 下的獨立狀態檔建立 Allocator（測試輔助）。"""
    store = JsonSessionStore(tmp_path / "state" / "allocations.json")
    return Allocator(load_topology(topology_path), store, **kwargs)


def test_batch_allocate_same_rack_multiple_duts(
    fullscale_path: Path, tmp_path: Path
) -> None:
    """批次分配多筆（含同 rack 多台 DUT）皆成功、VLAN 互異且在池內（AC12）。"""
    allocator = _make_allocator(
        fullscale_path, tmp_path, vlan_min=100, vlan_max=3000
    )

    # 12 個 generator port 一一對應到 rack-A（同一 rack）的前 12 台 DUT 的 Eth0，
    # 證明同一 rack 內多台 DUT 可同時分配（各走不同 leaf/不同 access port）。
    generator_ports = (
        ["ixia-1/{0}".format(index) for index in range(1, 5)]
        + ["trex-1/{0}".format(index) for index in range(0, 8)]
    )
    sessions = []
    for offset, gen_port in enumerate(generator_ports):
        dut_port = "dut-A{0}:Eth0".format(offset + 1)
        sessions.append(allocator.allocate(gen_port, dut_port))

    assert len(sessions) == len(generator_ports)
    vlans = [session.vlan for session in sessions]
    # VLAN 全域唯一且落在封閉池內。
    assert len(set(vlans)) == len(vlans)
    for vlan in vlans:
        assert 100 <= vlan <= 3000


def test_access_port_collision_same_generator_raises(
    fullscale_path: Path, tmp_path: Path
) -> None:
    """重用同一 generator port（撞 access port）觸發 HardConflictError（AC12）。"""
    allocator = _make_allocator(fullscale_path, tmp_path)
    first = allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    with pytest.raises(HardConflictError) as exc_info:
        # 同一 generator port 再分到別台 DUT：generator 端 access port 已被佔用。
        allocator.allocate("ixia-1/1", "dut-A2:Eth0")

    message = str(exc_info.value)
    assert first.session_id in message
    assert "ixia-1:ixia-1/1" in message


def test_access_port_collision_same_dut_raises(
    fullscale_path: Path, tmp_path: Path
) -> None:
    """重用同一 DUT port（撞 access port）觸發 HardConflictError（AC12）。"""
    allocator = _make_allocator(fullscale_path, tmp_path)
    first = allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    with pytest.raises(HardConflictError) as exc_info:
        # 不同 generator port 但同一 DUT port：DUT 端 access port 已被佔用。
        allocator.allocate("ixia-1/2", "dut-A1:Eth0")

    message = str(exc_info.value)
    assert first.session_id in message
    assert "dut-A1:Eth0" in message


def test_release_then_reallocate_same_resource(
    fullscale_path: Path, tmp_path: Path
) -> None:
    """釋放一筆後可用同一資源重新分配成功（AC12）。"""
    allocator = _make_allocator(fullscale_path, tmp_path)
    first = allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    # 釋放前，重用同一資源會衝突。
    with pytest.raises(HardConflictError):
        allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    allocator.release(first.session_id)

    # 釋放後同一資源可再次分配成功。
    again = allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    assert again.generator_port == "ixia-1/1"
    assert again.dut_port == "dut-A1:Eth0"
    assert again.path == first.path


def test_shared_trunk_same_segments_different_vlan(
    fullscale_path: Path, tmp_path: Path
) -> None:
    """同 rack 兩 DUT 共用 trunk：trunk 段相同但 VLAN 不同（FR-3.3、AC12）。"""
    allocator = _make_allocator(fullscale_path, tmp_path)

    # dut-A1 與 dut-A2 的 Eth0 都掛在 rack-A 的 leaf-1-1 上，共用 leaf→agg→spine
    # 的上行 trunk；以不同 generator port 避開 generator 端 access 衝突。
    first = allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    second = allocator.allocate("ixia-1/2", "dut-A2:Eth0")

    first_trunks = [(t.a_node, t.b_node) for t in first.trunk_segments]
    second_trunks = [(t.a_node, t.b_node) for t in second.trunk_segments]

    # 兩筆走同一條 leaf→agg→spine 上行，trunk 段（端點）完全相同。
    assert first_trunks == second_trunks
    assert len(first_trunks) > 0

    # VLAN 不同（軟衝突：trunk 共用合法，各 session 用自己的 VLAN 標記）。
    assert first.vlan != second.vlan
    assert all(t.vlan == first.vlan for t in first.trunk_segments)
    assert all(t.vlan == second.vlan for t in second.trunk_segments)
