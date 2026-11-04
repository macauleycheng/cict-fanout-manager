"""IcSessionStore 與 store.py 重構測試（互聯設計 §4、§10）。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from ic_fakes import make_ic_topology, write_topology, make_ic_data
from topo_manager.allocator import Allocator
from topo_manager.errors import SessionStoreError
from topo_manager.interconnect.allocator import InterconnectAllocator
from topo_manager.interconnect.models import IcLink, IcSession
from topo_manager.interconnect.store import IcSessionStore
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore

D = ["dut-g1-0", "dut-g1-1", "dut-g1-2", "dut-g1-3"]


def _session(sid: str = "ic-0001") -> IcSession:
    link = IcLink(
        vlan=100, a_dut="a", a_port="Eth8", a_node="ic-1:1",
        b_dut="b", b_port="Eth8", b_node="ic-1:5", kind="paired",
    )
    return IcSession(
        session_id=sid, group="g1", fanout="ic-1", profile="mesh",
        duts=["a", "b"], phase="allocated", links=[link],
    )


def test_missing_file_is_empty(tmp_path: Path) -> None:
    loaded = IcSessionStore(tmp_path / "x" / "i.json").load()
    assert loaded.next_seq is None and loaded.sessions == []


def test_default_path() -> None:
    assert str(IcSessionStore().path) == ".topo-state/interconnect.json"


def test_round_trip(tmp_path: Path) -> None:
    store = IcSessionStore(tmp_path / "s" / "i.json")
    store.save([_session()], 2)
    loaded = IcSessionStore(store.path).load()
    assert loaded.next_seq == 2
    assert loaded.sessions == [_session()]
    data = store.load_raw()
    assert data["version"] == 1 and data["kind"] == "interconnect"


def test_v2_ledger_rejected_by_ic_store(tmp_path: Path) -> None:
    path = tmp_path / "allocations.json"
    JsonSessionStore(path).save([], 1)
    with pytest.raises(SessionStoreError, match="這不是互聯帳本.*--state"):
        IcSessionStore(path).load()


def test_ic_ledger_rejected_by_v2_store(tmp_path: Path) -> None:
    path = tmp_path / "i.json"
    IcSessionStore(path).save([_session()], 2)
    with pytest.raises(SessionStoreError, match="版本不支援"):
        JsonSessionStore(path).load()


def test_ic_ledger_fed_to_traffic_allocator_rejected(tmp_path: Path, three_tier_path: Path) -> None:
    path = tmp_path / "i.json"
    IcSessionStore(path).save([_session()], 2)
    with pytest.raises(SessionStoreError):
        Allocator(load_topology(three_tier_path), JsonSessionStore(path))


def test_wrong_kind_rejected(tmp_path: Path) -> None:
    path = tmp_path / "i.json"
    path.write_text(json.dumps({"version": 1, "kind": "other", "sessions": []}), encoding="utf-8")
    with pytest.raises(SessionStoreError, match="這不是互聯帳本"):
        IcSessionStore(path).load()


@pytest.mark.parametrize(
    "text,pattern",
    [
        ("{bad", "非合法 JSON"),
        ("[1]", "頂層須為 JSON object"),
        (json.dumps({"version": 1, "kind": "interconnect", "sessions": [{}]}), "schema 驗證失敗"),
        (json.dumps({"version": 1, "kind": "interconnect", "sessions": [], "x": 1}), "schema 驗證失敗"),
    ],
)
def test_bad_content(tmp_path: Path, text: str, pattern: str) -> None:
    path = tmp_path / "i.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SessionStoreError, match=pattern):
        IcSessionStore(path).load()


def test_non_utf8(tmp_path: Path) -> None:
    path = tmp_path / "i.json"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(SessionStoreError, match="UTF-8"):
        IcSessionStore(path).load()


def test_write_atomic_cleans_temp_on_failure(tmp_path: Path, monkeypatch) -> None:
    store = IcSessionStore(tmp_path / "s" / "i.json")
    store.save([], 1)

    def boom(src, dst):
        raise OSError("nope")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(SessionStoreError, match="寫入 ledger 失敗"):
        store.save([_session()], 2)
    monkeypatch.undo()
    assert sorted(p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp")) == []
    assert store.load().sessions == []  # 原內容未變


def test_lock_suffix_default_and_device(tmp_path: Path) -> None:
    store = IcSessionStore(tmp_path / "s" / "i.json")
    with store.locked():
        assert (store.path.parent / "i.json.lock").exists()
    with store.device_locked():
        assert (store.path.parent / "i.json.device.lock").exists()
    plain = JsonSessionStore(tmp_path / "p" / "a.json")
    with plain.locked():
        assert (plain.path.parent / "a.json.lock").exists()


def test_nested_device_then_ledger_lock(tmp_path: Path) -> None:
    store = IcSessionStore(tmp_path / "s" / "i.json")
    with store.device_locked():
        with store.locked():
            store.save([_session()], 2)
        with store.locked():
            assert len(store.load().sessions) == 1
    # 巢狀離開後 SIGALRM handler 已還原（可再取鎖）。
    with store.device_locked(timeout=2):
        pass


def _worker(topology_path: str, state_path: str, index: int):
    try:
        topology = load_topology(topology_path)
        alloc = InterconnectAllocator(topology, IcSessionStore(state_path))
        names = ["dut-g{0}-{1}".format(index + 1, i) for i in range(4)]
        session = alloc.allocate_profile("g{0}".format(index + 1), names, "mesh")
        return (session.session_id, [l.vlan for l in session.links])
    except Exception as exc:  # pragma: no cover
        return ("ERROR", str(exc))


def test_concurrent_multiprocess_allocate(tmp_path: Path) -> None:
    topo_path = write_topology(tmp_path, make_ic_data(groups=3))
    state = str(tmp_path / "new" / "interconnect.json")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=3) as pool:
        results = pool.starmap(_worker, [(str(topo_path), state, k) for k in range(3)])
    assert all(r[0] != "ERROR" for r in results), results
    assert {r[0] for r in results} == {"ic-0001", "ic-0002", "ic-0003"}
    vlans = [v for _, vs in results for v in vs]
    assert len(vlans) == 18 and len(set(vlans)) == 18
    reloaded = InterconnectAllocator(load_topology(topo_path), IcSessionStore(state))
    nodes = [n for s in reloaded.list_sessions() for l in s.links for n in (l.a_node, l.b_node)]
    assert len(nodes) == len(set(nodes)) == 36


def _same_worker(topology_path: str, state_path: str):
    try:
        alloc = InterconnectAllocator(load_topology(topology_path), IcSessionStore(state_path))
        return alloc.allocate_profile("g1", D[:2], "mesh").session_id
    except Exception as exc:  # pragma: no cover
        return "ERROR:" + type(exc).__name__


def test_concurrent_same_ports_one_winner(tmp_path: Path) -> None:
    topo_path = write_topology(tmp_path, make_ic_data())
    state = str(tmp_path / "new" / "interconnect.json")
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=2) as pool:
        results = pool.starmap(_same_worker, [(str(topo_path), state)] * 2)
    assert sorted(results) == ["ERROR:InterconnectConflictError", "ic-0001"]
