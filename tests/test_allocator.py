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

import json
import multiprocessing
from pathlib import Path

import pytest

from topo_manager.allocator import Allocator
from topo_manager.errors import (
    HardConflictError,
    SessionNotFoundError,
    SessionStoreError,
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
    # access_ports 為衍生值（已移出模型）：以 _derive_access_ports 重算驗證。
    assert allocator._derive_access_ports(session.path) == [
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
    # access_ports 為衍生值（已移出模型）：以 _derive_access_ports 重算驗證。
    assert allocator._derive_access_ports(session.path) == [
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


# ======================================================================
# Phase 2 Hardening：還原層驗證（#2/#6/#9/#10）
# ======================================================================


def _write_ledger(path: Path, sessions: list, next_seq: int = 1) -> None:
    """以 tmp_path 手寫一份 v2 ledger JSON（還原驗證測試輔助）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": 2, "next_seq": next_seq, "sessions": sessions},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _session_dict(
    session_id: str,
    vlan: int,
    generator_port: str,
    dut_port: str,
    path: list,
    trunk_segments: list,
    physical_ports: list = None,
) -> dict:
    """組一筆 v2 session dict（physical_ports 預設 = path）。"""
    return {
        "session_id": session_id,
        "vlan": vlan,
        "generator_port": generator_port,
        "dut_port": dut_port,
        "path": path,
        "physical_ports": list(path) if physical_ports is None else physical_ports,
        "trunk_segments": trunk_segments,
    }


# 三層範例中 s-0001（ixia-1/1 → dut-A1:Eth0）的已知 path 與 trunk 段。
_A1_PATH = [
    "ixia-1:ixia-1/1", "spine-1:1", "spine-1:7", "agg-1:1",
    "agg-1:2", "leaf-1-1:1", "leaf-1-1:11", "dut-A1:Eth0",
]
_A1_TRUNKS = [
    {"a_node": "spine-1:7", "b_node": "agg-1:1", "vlan": 100},
    {"a_node": "agg-1:2", "b_node": "leaf-1-1:1", "vlan": 100},
]


def test_restore_duplicate_session_id_raises(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """還原偵測重複 session_id → SessionStoreError（#2）。"""
    state = tmp_path / "state" / "allocations.json"
    s = _session_dict("s-0001", 100, "ixia-1/1", "dut-A1:Eth0", _A1_PATH, _A1_TRUNKS)
    dup = _session_dict("s-0001", 200, "ixia-1/2", "dut-A2:Eth0", _A1_PATH, _A1_TRUNKS)
    _write_ledger(state, [s, dup])
    with pytest.raises(SessionStoreError) as exc_info:
        Allocator(load_topology(three_tier_path), JsonSessionStore(state))
    assert "s-0001" in str(exc_info.value)


def test_restore_malformed_id_raises(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """還原偵測 malformed session_id → SessionStoreError（#6）。"""
    state = tmp_path / "state" / "allocations.json"
    s = _session_dict(
        "manual-session", 100, "ixia-1/1", "dut-A1:Eth0", _A1_PATH, _A1_TRUNKS
    )
    _write_ledger(state, [s])
    with pytest.raises(SessionStoreError) as exc_info:
        Allocator(load_topology(three_tier_path), JsonSessionStore(state))
    assert "manual-session" in str(exc_info.value)


def test_restore_vlan_out_of_pool_raises(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """還原偵測 VLAN 超出當前 pool → SessionStoreError（#9）。"""
    state = tmp_path / "state" / "allocations.json"
    # VLAN 3001 超出 pool 100..3000（以帶 vlan 3001 的 trunk 段保持 trunk 一致）。
    trunks = [
        {"a_node": "spine-1:7", "b_node": "agg-1:1", "vlan": 3001},
        {"a_node": "agg-1:2", "b_node": "leaf-1-1:1", "vlan": 3001},
    ]
    s = _session_dict("s-0001", 3001, "ixia-1/1", "dut-A1:Eth0", _A1_PATH, trunks)
    _write_ledger(state, [s])
    with pytest.raises(SessionStoreError) as exc_info:
        Allocator(load_topology(three_tier_path), JsonSessionStore(state))
    assert "3001" in str(exc_info.value)


def test_restore_physical_ports_mismatch_raises(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """還原偵測 physical_ports 與 path 不符 → SessionStoreError（finding #1）。"""
    state = tmp_path / "state" / "allocations.json"
    bad_pp = list(_A1_PATH)
    bad_pp[0] = "ixia-1:ixia-1/9"  # 竄改使與 path 不一致
    s = _session_dict(
        "s-0001", 100, "ixia-1/1", "dut-A1:Eth0", _A1_PATH, _A1_TRUNKS,
        physical_ports=bad_pp,
    )
    _write_ledger(state, [s])
    with pytest.raises(SessionStoreError) as exc_info:
        Allocator(load_topology(three_tier_path), JsonSessionStore(state))
    assert "physical_ports" in str(exc_info.value)


def test_restore_topology_drift_raises(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """還原偵測拓樸漂移（記錄 trunk 段與重算不符）→ SessionStoreError（#10）。"""
    state = tmp_path / "state" / "allocations.json"
    # 移除一段 trunk 記錄，使帳本 trunk 段與以 path 重算不符。
    drift_trunks = [{"a_node": "spine-1:7", "b_node": "agg-1:1", "vlan": 100}]
    s = _session_dict("s-0001", 100, "ixia-1/1", "dut-A1:Eth0", _A1_PATH, drift_trunks)
    _write_ledger(state, [s])
    with pytest.raises(SessionStoreError) as exc_info:
        Allocator(load_topology(three_tier_path), JsonSessionStore(state))
    assert "s-0001" in str(exc_info.value)


def test_restore_legit_shared_trunk_not_flagged(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """合法共用 trunk 且 physical_ports==path 的帳本不誤判（AC9）。"""
    state = tmp_path / "state" / "allocations.json"
    a2_path = [
        "ixia-1:ixia-1/2", "spine-1:2", "spine-1:7", "agg-1:1",
        "agg-1:2", "leaf-1-1:1", "leaf-1-1:13", "dut-A2:Eth0",
    ]
    a2_trunks = [
        {"a_node": "spine-1:7", "b_node": "agg-1:1", "vlan": 101},
        {"a_node": "agg-1:2", "b_node": "leaf-1-1:1", "vlan": 101},
    ]
    s1 = _session_dict("s-0001", 100, "ixia-1/1", "dut-A1:Eth0", _A1_PATH, _A1_TRUNKS)
    s2 = _session_dict("s-0002", 101, "ixia-1/2", "dut-A2:Eth0", a2_path, a2_trunks)
    _write_ledger(state, [s1, s2], next_seq=3)
    allocator = Allocator(load_topology(three_tier_path), JsonSessionStore(state))
    assert len(allocator.list_sessions()) == 2


# ======================================================================
# Phase 2 Hardening：save 回捲（#7）
# ======================================================================


def test_allocate_save_failure_rolls_back_memory_and_ledger(
    three_tier_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """allocate 的 save 失敗 → 記憶體 index 與 _next_seq 回捲、帳本未改寫（#7）。"""
    state = tmp_path / "state" / "allocations.json"
    allocator = _make_allocator(three_tier_path, tmp_path)
    allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    before_bytes = (tmp_path / "state" / "allocations.json").read_bytes()
    seq_before = allocator._next_seq
    vlans_before = set(allocator._used_vlans)

    def _boom(sessions, next_seq):
        raise SessionStoreError("模擬 save 失敗")

    monkeypatch.setattr(allocator._store, "save", _boom)
    with pytest.raises(SessionStoreError):
        allocator.allocate("ixia-1/2", "dut-A2:Eth0")

    # 記憶體回捲：session 數、_next_seq、_used_vlans 均不變。
    assert len(allocator.list_sessions()) == 1
    assert allocator._next_seq == seq_before
    assert allocator._used_vlans == vlans_before
    # 帳本檔未被改寫。
    assert (tmp_path / "state" / "allocations.json").read_bytes() == before_bytes


# ======================================================================
# Phase 2 Hardening：802.1Q（#13）
# ======================================================================


@pytest.mark.parametrize("vlan_min,vlan_max", [(100, 9999), (1, 3000), (100, 4095)])
def test_802_1q_range_rejected(
    three_tier_path: Path, tmp_path: Path, vlan_min: int, vlan_max: int
) -> None:
    """超出 802.1Q 可用範圍 2..4094 的 pool 於建構期丟 ValueError（#13）。"""
    store = JsonSessionStore(tmp_path / "state" / "allocations.json")
    with pytest.raises(ValueError):
        Allocator(
            load_topology(three_tier_path), store,
            vlan_min=vlan_min, vlan_max=vlan_max,
        )


# ======================================================================
# Phase 2 Hardening：序號高水位（#11）
# ======================================================================


def test_seq_high_water_persists_across_restart(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """allocate 兩筆→release s-0002→重啟→下一筆得 s-0003，帳本含 next_seq（#11）。"""
    state = tmp_path / "state" / "allocations.json"
    topology = load_topology(three_tier_path)

    a = Allocator(topology, JsonSessionStore(state))
    a.allocate("ixia-1/1", "dut-A1:Eth0")
    a.allocate("ixia-1/2", "dut-A2:Eth0")
    a.release("s-0002")

    # 帳本持久化 next_seq 高水位（3）。
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["next_seq"] == 3

    # 重啟：同一 state 新建 Allocator，下一筆不重用 s-0002。
    b = Allocator(topology, JsonSessionStore(state))
    s3 = b.allocate("ixia-1/2", "dut-A2:Eth0")
    assert s3.session_id == "s-0003"


# ======================================================================
# Phase 2 Hardening：真多行程併發（#1）
# ======================================================================


def _concurrent_worker(topology_path: str, state_path: str, gen: str, dut: str):
    """多行程 worker：各自建 Allocator 並 allocate 一筆（併發測試用）。"""
    try:
        allocator = Allocator(
            load_topology(topology_path), JsonSessionStore(state_path)
        )
        session = allocator.allocate(gen, dut)
        return (session.session_id, session.vlan)
    except Exception as exc:  # pragma: no cover - 失敗時回傳供主行程斷言
        return ("ERROR", str(exc))


def test_concurrent_multiprocess_allocate(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """兩行程同時對尚不存在的 --state allocate 不相交端點，兩筆都成功（#1、AC2）。"""
    state = str(tmp_path / "newstate" / "allocations.json")
    topo = str(three_tier_path)

    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=2) as pool:
        results = pool.starmap(
            _concurrent_worker,
            [
                (topo, state, "ixia-1/1", "dut-A1:Eth0"),
                (topo, state, "ixia-1/2", "dut-A2:Eth0"),
            ],
        )

    # 兩筆都成功、session_id 與 VLAN 互不相同。
    assert all(sid != "ERROR" for sid, _ in results), results
    sids = {sid for sid, _ in results}
    vlans = {vlan for _, vlan in results}
    assert sids == {"s-0001", "s-0002"}
    assert len(vlans) == 2

    # 帳本兩筆都在且可重 load。
    reloaded = Allocator(load_topology(three_tier_path), JsonSessionStore(state))
    assert len(reloaded.list_sessions()) == 2
