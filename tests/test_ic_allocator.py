"""InterconnectAllocator 測試（互聯設計 §5、§10）。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ic_fakes import make_ic_data, make_ic_topology, write_topology
from topo_manager.errors import (
    HardConflictError,
    InterconnectConflictError,
    InterconnectLayoutError,
    InterconnectRequestError,
    InterconnectSessionNotFoundError,
    SessionStoreError,
    VlanPoolExhaustedError,
)
from topo_manager.interconnect.allocator import InterconnectAllocator
from topo_manager.interconnect.store import IcSessionStore
from topo_manager.loader import load_topology

D = ["dut-g1-0", "dut-g1-1", "dut-g1-2", "dut-g1-3"]


def _make(tmp_path: Path, groups: int = 1, **kwargs):
    topology = make_ic_topology(tmp_path, groups=groups)
    store = IcSessionStore(tmp_path / "state" / "interconnect.json")
    return topology, store, InterconnectAllocator(topology, store, **kwargs)


def _pairs(session):
    return [(l.a_dut, l.a_port, l.b_dut, l.b_port, l.vlan) for l in session.links]


def test_no_section_rejected(tmp_path: Path) -> None:
    data = make_ic_data()
    del data["interconnect"]
    topology = load_topology(write_topology(tmp_path, data))
    with pytest.raises(InterconnectLayoutError):
        InterconnectAllocator(topology, IcSessionStore(tmp_path / "s.json"))


@pytest.mark.parametrize("lo,hi", [(0, 5), (5, 4), (1, 10), (100, 4095)])
def test_vlan_range_invalid(tmp_path: Path, lo: int, hi: int) -> None:
    topology = make_ic_topology(tmp_path)
    with pytest.raises(ValueError):
        InterconnectAllocator(topology, IcSessionStore(tmp_path / "s.json"), lo, hi)


def test_profile_mesh_two(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    session = alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    assert session.session_id == "ic-0001"
    assert session.phase == "allocated" and session.fanout == "ic-1"
    assert _pairs(session) == [(D[0], "Eth8", D[1], "Eth8", 100)]
    assert session.links[0].a_node == "ic-1:1" and session.links[0].b_node == "ic-1:5"


def test_profile_mesh_four_and_link_nodes(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    session = alloc.allocate_profile("g1", D, "mesh")
    assert len(session.links) == 6
    assert [l.vlan for l in session.links] == list(range(100, 106))
    # d2-d3 用 p2/p2。
    last = session.links[-1]
    assert (last.a_port, last.b_port) == ("Eth10", "Eth10")


def test_pairing_independent_of_selected_order(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    a = alloc.preview_profile("g1", [D[1], D[3]], "mesh").links[0]
    b = alloc.preview_profile("g1", [D[3], D[1]], "mesh").links[0]
    assert {(a.a_node, a.b_node)} == {(b.b_node, b.a_node)}
    assert (a.a_dut, a.b_dut) == (D[1], D[3])


def test_ring_four_links_and_line(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    assert len(alloc.preview_profile("g1", D, "ring").links) == 4
    assert len(alloc.preview_profile("g1", D, "line").links) == 3
    assert len(alloc.preview_profile("g1", D[:3], "ring").links) == 3


def test_explicit_pairs_with_p3_parallel_link(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    first = alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    second = alloc.allocate_pairs("g1", [(D[0] + ":p3", D[1] + ":p3")])
    assert second.profile == "explicit" and second.links[0].kind == "explicit"
    assert second.links[0].a_port == "Eth11"
    assert second.session_id == "ic-0002"
    assert first.links[0].vlan != second.links[0].vlan


def test_explicit_accepts_literal_names_and_order(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    session = alloc.allocate_pairs(
        "g1", [(D[2] + ":Eth9", D[0] + ":p3"), (D[1] + ":p0", D[3] + ":p0")]
    )
    assert session.duts == [D[2], D[0], D[1], D[3]]
    assert [l.vlan for l in session.links] == [100, 101]


def test_pairs_coexist_in_group(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    s1 = alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    s2 = alloc.allocate_profile("g1", [D[2], D[3]], "mesh")
    assert {l.vlan for l in s1.links}.isdisjoint({l.vlan for l in s2.links})


def test_four_ring_conflicts_with_pair(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    with pytest.raises(InterconnectConflictError, match="ic-0001") as info:
        alloc.allocate_profile("g1", D, "ring")
    assert isinstance(info.value, HardConflictError)
    assert len(alloc.list_sessions()) == 1


def test_ring_other_order_coexists_with_pair(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    session = alloc.allocate_profile("g1", [D[0], D[2], D[1], D[3]], "ring")
    assert len(session.links) == 4


def test_releasing_still_occupies_then_reuse_after_commit(tmp_path: Path) -> None:
    _, store, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    alloc.begin_release("ic-0001")
    with pytest.raises(InterconnectConflictError):
        alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    released = alloc.commit_release("ic-0001")
    assert released.phase == "releasing"
    again = alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    assert again.session_id == "ic-0002"
    assert again.links[0].vlan == 100  # VLAN 回收後重用


def test_begin_release_idempotent_and_commit_requires_releasing(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    with pytest.raises(InterconnectRequestError, match="begin_release"):
        alloc.commit_release("ic-0001")
    first = alloc.begin_release("ic-0001")
    assert alloc.begin_release("ic-0001").phase == first.phase == "releasing"
    with pytest.raises(InterconnectSessionNotFoundError):
        alloc.begin_release("ic-9999")
    with pytest.raises(InterconnectSessionNotFoundError):
        alloc.commit_release("ic-9999")


def test_release_now(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    alloc.release_now("ic-0001")
    assert alloc.list_sessions() == [] and alloc.owned_nodes() == {}
    with pytest.raises(InterconnectSessionNotFoundError):
        alloc.get_session("ic-0001")
    with pytest.raises(InterconnectSessionNotFoundError):
        alloc.release_now("ic-0001")


def test_vlan_global_unique_across_groups(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path, groups=2)
    s1 = alloc.allocate_profile("g1", D, "mesh")
    s2 = alloc.allocate_profile("g2", [d.replace("g1", "g2") for d in D], "mesh")
    vlans = [l.vlan for l in s1.links + s2.links]
    assert len(set(vlans)) == 12


def test_vlan_exhausted_no_side_effect(tmp_path: Path) -> None:
    _, store, alloc = _make(tmp_path, vlan_min=100, vlan_max=102)
    with pytest.raises(VlanPoolExhaustedError, match="需要 6 個"):
        alloc.allocate_profile("g1", D, "mesh")
    assert alloc.list_sessions() == [] and alloc.owned_nodes() == {}
    assert not store.path.exists()
    alloc.allocate_profile("g1", D[:3], "mesh")
    with pytest.raises(VlanPoolExhaustedError):
        alloc.allocate_pairs("g1", [(D[0] + ":p3", D[1] + ":p3")])


def test_invalid_inputs(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path, groups=2)
    cases = [
        (lambda: alloc.allocate_profile("zz", D[:2], "mesh"), "找不到組 'zz'"),
        (lambda: alloc.allocate_profile("g1", [D[0]], "mesh"), "需要 2..4 台"),
        (lambda: alloc.allocate_profile("g1", D + [D[0]], "mesh"), "重複"),
        (lambda: alloc.allocate_profile("g1", [D[0], D[0]], "mesh"), "重複"),
        (lambda: alloc.allocate_profile("g1", D[:2], "star"), "未知 profile"),
        (lambda: alloc.allocate_profile("g1", D[:2], "ring"), "ring 至少需要 3 台"),
        (lambda: alloc.allocate_profile("g1", [D[0], "nobody"], "mesh"), "找不到 DUT"),
        (lambda: alloc.allocate_profile("g1", [D[0], "dut-g2-0"], "mesh"), "不屬於組 'g1'（屬於 'g2'）"),
        (lambda: alloc.allocate_pairs("g1", []), "至少需要一組"),
        (lambda: alloc.allocate_pairs("g1", [("bad", D[1] + ":p0")]), "格式錯誤"),
        (lambda: alloc.allocate_pairs("g1", [(D[0] + ":", D[1] + ":p0")]), "格式錯誤"),
        (lambda: alloc.allocate_pairs("g1", [(D[0] + ":p0", D[0] + ":p1")]), "同一台 DUT"),
        (lambda: alloc.allocate_pairs("g1", [(D[0] + ":p9", D[1] + ":p0")]), "無 port 'p9'"),
        (lambda: alloc.allocate_pairs("g1", [(D[0] + ":p0", "dut-g2-0:p0")]), "不屬於組"),
        (lambda: alloc.allocate_pairs("g1", [(D[0] + ":p0", D[1] + ":p0"), (D[2] + ":p0", D[1] + ":Eth8")]), "port 重複"),
    ]
    for call, pattern in cases:
        with pytest.raises(InterconnectRequestError, match=pattern):
            call()
    assert alloc.list_sessions() == []


def test_explicit_conflict_with_existing(tmp_path: Path) -> None:
    _, _, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", [D[0], D[1]], "mesh")
    with pytest.raises(InterconnectConflictError, match="ic-0001"):
        alloc.allocate_pairs("g1", [(D[0] + ":p0", D[2] + ":p3")])


def test_save_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    _, store, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", [D[0], D[1]], "mesh")

    def boom(*args, **kwargs):
        raise SessionStoreError("disk full")

    monkeypatch.setattr(store, "save", boom)
    with pytest.raises(SessionStoreError):
        alloc.allocate_profile("g1", [D[2], D[3]], "mesh")
    with pytest.raises(SessionStoreError):
        alloc.begin_release("ic-0001")
    with pytest.raises(SessionStoreError):
        alloc.release_now("ic-0001")
    monkeypatch.undo()
    fresh = InterconnectAllocator(make_ic_topology(tmp_path), store)
    sessions = fresh.list_sessions()
    assert [s.session_id for s in sessions] == ["ic-0001"]
    assert sessions[0].phase == "allocated"
    # 失敗後序號未被消耗。
    assert fresh.allocate_profile("g1", [D[2], D[3]], "mesh").session_id == "ic-0002"


def test_in_memory_rollback_after_failed_allocate(tmp_path: Path, monkeypatch) -> None:
    _, store, alloc = _make(tmp_path)
    monkeypatch.setattr(store, "save", lambda *a, **k: (_ for _ in ()).throw(SessionStoreError("x")))
    with pytest.raises(SessionStoreError):
        alloc.allocate_profile("g1", D[:2], "mesh")
    assert alloc.owned_nodes() == {} and alloc.list_sessions() == []
    monkeypatch.undo()
    assert alloc.allocate_profile("g1", D[:2], "mesh").session_id == "ic-0001"


def test_preview_does_not_consume(tmp_path: Path) -> None:
    _, store, alloc = _make(tmp_path)
    preview = alloc.preview_profile("g1", D, "mesh")
    assert preview.session_id == "(preview)"
    assert [l.vlan for l in preview.links] == list(range(100, 106))
    alloc.preview_pairs("g1", [(D[0] + ":p0", D[1] + ":p0")])
    assert alloc.owned_nodes() == {} and not store.path.exists()


def test_restart_restores_state(tmp_path: Path) -> None:
    topology, store, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", D[:3], "ring")
    alloc.allocate_pairs("g1", [(D[0] + ":p3", D[1] + ":p3")])
    alloc.begin_release("ic-0001")
    again = InterconnectAllocator(topology, IcSessionStore(store.path))
    assert [s.session_id for s in again.list_sessions()] == ["ic-0001", "ic-0002"]
    assert again.get_session("ic-0001").phase == "releasing"
    assert again.owned_nodes() == alloc.owned_nodes()
    with pytest.raises(InterconnectConflictError):
        again.allocate_pairs("g1", [(D[0] + ":p3", D[2] + ":p3")])
    assert again.allocate_profile("g1", [D[2], D[3]], "mesh").session_id == "ic-0003"


def test_ledger_has_version_and_kind(tmp_path: Path) -> None:
    _, store, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", D[:2], "mesh")
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["kind"] == "interconnect"
    assert data["next_seq"] == 2


# ---------------- 還原一致性 ----------------

def _tamper(tmp_path: Path, edit):
    topology, store, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", D, "mesh")
    data = json.loads(store.path.read_text(encoding="utf-8"))
    edit(data)
    store.path.write_text(json.dumps(data), encoding="utf-8")
    return topology, store


@pytest.mark.parametrize(
    "edit,pattern",
    [
        (lambda d: d["sessions"][0].update(session_id="x-1"), "session_id 格式錯誤"),
        (lambda d: d["sessions"][0].update(phase="weird"), "phase 'weird' 非法"),
        (lambda d: d["sessions"][0].update(profile="star"), "profile 'star' 非法"),
        (lambda d: d["sessions"][0].update(group="gone"), "拓樸漂移"),
        (lambda d: d["sessions"][0].update(fanout="ic-9"), "與拓樸不符"),
        (lambda d: d["sessions"][0]["links"][1].update(vlan=100), "VLAN 100 被重複使用"),
        (lambda d: d["sessions"][0]["links"][0].update(vlan=3500), "不在目前池"),
        (lambda d: d["sessions"][0]["links"][0].update(a_node="ic-1:40"), "接線漂移"),
        (lambda d: d["sessions"][0]["links"][1].update(a_node=d["sessions"][0]["links"][0]["a_node"], b_node=d["sessions"][0]["links"][0]["b_node"]), "接線漂移|同時佔用"),
        (lambda d: d["sessions"][0]["links"][0].update(b_dut=d["sessions"][0]["links"][0]["a_dut"]), "自連"),
        (lambda d: d["sessions"][0].update(links=[]), "沒有任何 link"),
        (lambda d: d["sessions"][0]["links"].pop(), "不一致"),
        (lambda d: d["sessions"][0]["links"][0].update(kind="explicit"), "不一致"),
        (lambda d: d["sessions"].append(dict(d["sessions"][0])), "重複"),
    ],
)
def test_restore_consistency_errors(tmp_path: Path, edit, pattern: str) -> None:
    topology, store = _tamper(tmp_path, edit)
    with pytest.raises(SessionStoreError, match=pattern) as info:
        InterconnectAllocator(topology, store)
    assert "list --raw --state" in str(info.value)


def test_restore_port_double_owned(tmp_path: Path) -> None:
    topology, store, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", D[:2], "mesh")
    data = json.loads(store.path.read_text(encoding="utf-8"))
    clone = json.loads(json.dumps(data["sessions"][0]))
    clone["session_id"] = "ic-0002"
    clone["links"][0]["vlan"] = 101
    data["sessions"].append(clone)
    store.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SessionStoreError, match="同時佔用"):
        InterconnectAllocator(topology, store)


def test_restore_pool_outside(tmp_path: Path) -> None:
    topology, store, _ = _make(tmp_path)
    first = InterconnectAllocator(topology, store)
    first.allocate_profile("g1", D[:2], "mesh")
    with pytest.raises(SessionStoreError, match="不在目前池"):
        InterconnectAllocator(topology, store, vlan_min=200, vlan_max=300)


def test_next_seq_high_water(tmp_path: Path) -> None:
    topology, store, alloc = _make(tmp_path)
    alloc.allocate_profile("g1", D[:2], "mesh")
    alloc.allocate_profile("g1", D[2:], "mesh")
    alloc.release_now("ic-0002")
    again = InterconnectAllocator(topology, store)
    assert again.allocate_profile("g1", D[2:], "mesh").session_id == "ic-0003"
