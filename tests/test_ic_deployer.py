"""InterconnectDeployer 的 pytest 測試（互聯設計 §6.4–§6.6、§10）。

以 ``FakeEcSwitch`` 注入 driver 工廠，驗證：dry-run 絕不連線、VLAN 被他人佔用
預檢拒絕且不寫入、phase 1 讀回失敗時不執行 no shutdown、release 兩階段（失敗
停在 releasing、重跑完成）、park 跳過被持有的 port 且只動互聯區段 port、
verify 偵測人為破壞、缺連線設定 → ConfigError。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from ic_fakes import FakeEcSwitch, make_ic_topology
from topo_manager.drivers.config import FanoutConfig, FanoutEntry
from topo_manager.drivers.ec_ssh_ic import EcSshInterconnectDriver
from topo_manager.drivers.ic_deployer import InterconnectDeployer
from topo_manager.errors import (
    ApplyError,
    ConfigError,
    InterconnectRequestError,
    InterconnectSessionNotFoundError,
    ReadBackMismatchError,
)
from topo_manager.interconnect.allocator import InterconnectAllocator
from topo_manager.interconnect.store import IcSessionStore

FANOUT = "ic-1"
DUTS = ["dut-g1-0", "dut-g1-1"]


class _Env:
    """測試環境：拓樸、帳本、allocator、fake 設備與 deployer。"""

    def __init__(self, tmp_path: Path, force: bool = True, configured: bool = True) -> None:
        self.topology = make_ic_topology(tmp_path)
        self.store = IcSessionStore(tmp_path / "interconnect.json")
        self.allocator = InterconnectAllocator(self.topology, self.store)
        self.switch = FakeEcSwitch()
        self.factory_calls: List[str] = []
        self.phase_at_connect: List[str] = []
        entries = {FANOUT: FanoutEntry(host="h", username="u", password="p")} if configured else {}
        self.config = FanoutConfig(fanouts=entries)
        self.force = force
        self.deployer = InterconnectDeployer(
            self._factory,
            self.config,
            store=self.store,
            allocator=self.allocator,
            force_access_mode=force,
        )

    def _factory(self, name: str, entry: object) -> EcSshInterconnectDriver:
        self.factory_calls.append(name)
        sessions = self.store.load().sessions
        self.phase_at_connect.extend(item.phase for item in sessions)
        return EcSshInterconnectDriver(
            entry.host,
            entry.username,
            entry.password,
            connection_factory=self.switch.factory,
            force_access_mode=self.force,
        )

    def allocate(self, duts: List[str] = DUTS, profile: str = "mesh"):
        return self.allocator.allocate_profile("g1", duts, profile)


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    return _Env(tmp_path)


def _parked_expectation(initial: Dict, ports: List[int]) -> Dict:
    expected = copy.deepcopy(initial)
    for port in ports:
        expected["ports"][port]["enabled"] = False
    return expected


# ------------------------------------------------------------------ dry-run


def test_dry_run_never_calls_driver_factory(env: _Env) -> None:
    session = env.allocate()
    plan = env.deployer.apply(session)
    release_plan = env.deployer.release(session)
    park = env.deployer.park(env.allocator.graph, env.allocator.owned_nodes(), fanouts=[FANOUT])
    assert env.factory_calls == []
    assert env.switch.config_batches == []
    assert plan.action == "apply" and release_plan.action == "release"
    assert not park.applied
    assert env.store.load().sessions[0].phase == "allocated"


def test_plan_enable_lists_phase1_then_phase2_for_both_ends(env: _Env) -> None:
    session = env.allocate()
    plan = env.deployer.plan_enable(session)
    assert plan.vlans == [100]
    fanout_plan = plan.fanouts[0]
    assert fanout_plan.configured
    assert fanout_plan.batches[0] == [
        "interface ethernet 1/1",
        "shutdown",
        "switchport mode access",
        "switchport allowed vlan add 100 untagged",
        "switchport native vlan 100",
        "switchport allowed vlan remove 1",
        "interface ethernet 1/5",
        "shutdown",
        "switchport mode access",
        "switchport allowed vlan add 100 untagged",
        "switchport native vlan 100",
        "switchport allowed vlan remove 1",
    ]
    assert fanout_plan.batches[1] == [
        "interface ethernet 1/1",
        "no shutdown",
        "interface ethernet 1/5",
        "no shutdown",
    ]


def test_plan_release_has_park_batch_and_vlan_clear_batch(env: _Env) -> None:
    session = env.allocate()
    batches = env.deployer.plan_release(session).fanouts[0].batches
    assert batches[0][:6] == [
        "interface ethernet 1/1",
        "shutdown",
        "switchport allowed vlan add 1 untagged",
        "no switchport native vlan",
        "switchport allowed vlan remove 100",
        "interface ethernet 1/5",
    ]
    assert batches[1] == ["vlan database", "no vlan 100"]


def test_plan_does_not_require_connection_config(tmp_path: Path) -> None:
    env = _Env(tmp_path, configured=False)
    session = env.allocate()
    plan = env.deployer.apply(session)
    assert not plan.fanouts[0].configured
    assert env.factory_calls == []


def test_plan_park_static_baseline_skips_owned(env: _Env) -> None:
    session = env.allocate()
    plan = env.deployer.plan_park(
        env.allocator.graph, [FANOUT], [1, 2, 5], owned=env.allocator.owned_nodes()
    )
    assert session.session_id
    assert plan.fanouts[0].commands == ["interface ethernet 1/2", "shutdown"]


# ------------------------------------------------------------------ apply / release


def test_apply_end_to_end_then_release_returns_to_parking(env: _Env) -> None:
    initial = env.switch.snapshot()
    session = env.allocate()

    result = env.deployer.apply(session, apply=True)
    for port in (1, 5):
        state = env.switch.ports[port]
        assert state["enabled"] and state["native"] == 100 and state["allowed"] == {100: "u"}
    assert 100 in env.switch.defined
    assert any("Link Status: Down" in note for note in result.notes)
    assert env.deployer.verify_session(session).ok
    assert env.switch.disconnect_count == env.switch.connect_count

    released = env.deployer.release(session, apply=True)
    assert released.action == "release"
    assert env.switch.snapshot() == _parked_expectation(initial, [1, 5])
    assert env.store.load().sessions == []
    assert env.deployer.verify_parked(env.allocator.graph, {}, fanouts=[FANOUT], ports=[1, 5]).ok


def test_apply_preserves_hybrid_port_with_pool_external_vlan(tmp_path: Path) -> None:
    env = _Env(tmp_path, force=False)
    env.switch.set_hybrid(5, tagged=[4093])
    initial = env.switch.snapshot()
    session = env.allocate()
    env.deployer.apply(session, apply=True)
    assert env.switch.ports[5]["allowed"] == {100: "u", 4093: "t"}
    env.deployer.release(session, apply=True)
    assert env.switch.snapshot() == _parked_expectation(initial, [1, 5])
    assert 4093 in env.switch.defined


def test_apply_three_dut_mesh_uses_distinct_vlans(env: _Env) -> None:
    session = env.allocate(DUTS + ["dut-g1-2"])
    env.deployer.apply(session, apply=True)
    assert sorted({link.vlan for link in session.links}) == [100, 101, 102]
    for link in session.links:
        members = env.switch.ports
        in_vlan = sorted(n for n in members if link.vlan in members[n]["allowed"])
        assert len(in_vlan) == 2
    assert env.deployer.verify_session(session).ok


def test_apply_is_idempotent(env: _Env) -> None:
    session = env.allocate()
    env.deployer.apply(session, apply=True)
    snapshot = env.switch.snapshot()
    env.deployer.apply(session, apply=True)
    assert env.switch.snapshot() == snapshot


def test_apply_precheck_rejects_vlan_used_by_other_port_without_writing(env: _Env) -> None:
    session = env.allocate()
    env.switch.defined.add(100)
    env.switch.ports[20]["allowed"][100] = "u"
    with pytest.raises(ApplyError, match="VLAN 100"):
        env.deployer.apply(session, apply=True)
    assert env.switch.config_batches == []
    assert env.switch.disconnect_count == env.switch.connect_count


def test_phase1_readback_failure_never_runs_no_shutdown(env: _Env) -> None:
    session = env.allocate()
    env.switch.silent_drop = {"switchport allowed vlan remove 1"}
    with pytest.raises(ReadBackMismatchError, match="VLAN 1"):
        env.deployer.apply(session, apply=True)
    assert "no shutdown" not in env.switch.commands
    assert not env.switch.ports[1]["enabled"] and not env.switch.ports[5]["enabled"]
    assert env.switch.disconnect_count == env.switch.connect_count


def test_apply_failure_message_hints_rerun(env: _Env) -> None:
    session = env.allocate()
    env.switch.silent_drop = {"switchport allowed vlan remove 1"}
    with pytest.raises(ReadBackMismatchError, match="冪等"):
        env.deployer.apply(session, apply=True)


def test_apply_final_readback_detects_port_not_up(env: _Env) -> None:
    session = env.allocate()
    env.switch.silent_drop = {"no shutdown"}
    with pytest.raises(ReadBackMismatchError, match="Port Admin"):
        env.deployer.apply(session, apply=True)


def test_apply_rejects_releasing_session(env: _Env) -> None:
    session = env.allocate()
    env.allocator.begin_release(session.session_id)
    with pytest.raises(InterconnectRequestError, match="釋放"):
        env.deployer.apply(session, apply=True)
    assert env.factory_calls == []


def test_apply_unknown_session_raises_not_found(env: _Env) -> None:
    session = env.allocate()
    env.allocator.release_now(session.session_id)
    with pytest.raises(InterconnectSessionNotFoundError):
        env.deployer.apply(session, apply=True)


def test_apply_missing_fanout_config_raises_config_error(tmp_path: Path) -> None:
    env = _Env(tmp_path, configured=False)
    session = env.allocate()
    with pytest.raises(ConfigError):
        env.deployer.apply(session, apply=True)
    with pytest.raises(ConfigError):
        env.deployer.release(session, apply=True)
    assert env.factory_calls == []
    assert env.store.load().sessions[0].phase == "allocated"


def test_release_marks_releasing_before_touching_device(env: _Env) -> None:
    session = env.allocate()
    env.deployer.apply(session, apply=True)
    env.phase_at_connect.clear()
    env.deployer.release(session, apply=True)
    assert env.phase_at_connect == ["releasing"]


def test_release_failure_leaves_session_releasing_and_rerun_completes(env: _Env) -> None:
    session = env.allocate()
    env.deployer.apply(session, apply=True)
    env.switch.silent_drop = {"shutdown"}
    with pytest.raises(ReadBackMismatchError):
        env.deployer.release(session, apply=True)
    assert env.store.load().sessions[0].phase == "releasing"
    assert env.allocator.owned_nodes()  # 資源仍被保留
    env.switch.silent_drop = set()
    env.deployer.release(session, apply=True)
    assert env.store.load().sessions == []
    assert not env.switch.ports[1]["enabled"]


def test_release_keeps_vlan_definition_with_foreign_member_and_warns(env: _Env) -> None:
    session = env.allocate()
    env.deployer.apply(session, apply=True)
    env.switch.ports[30]["allowed"][100] = "u"
    result = env.deployer.release(session, apply=True)
    assert any("VLAN 100" in note for note in result.notes)
    assert 100 in env.switch.defined
    assert env.store.load().sessions == []


# ------------------------------------------------------------------ park


def test_park_apply_skips_owned_ports_and_only_touches_interconnect_ports(env: _Env) -> None:
    session = env.allocate()
    env.deployer.apply(session, apply=True)
    outside = copy.deepcopy(env.switch.ports[40])
    result = env.deployer.park(env.allocator.graph, {}, fanouts=[FANOUT], apply=True)
    assert result.applied
    assert sorted((f, p) for f, p, _s in result.skipped) == [(FANOUT, 1), (FANOUT, 5)]
    assert all(owner == session.session_id for _f, _p, owner in result.skipped)
    assert env.switch.ports[1]["enabled"] and env.switch.ports[5]["enabled"]
    assert env.switch.ports[1]["native"] == 100
    assert result.parked[FANOUT] == [p for p in range(1, 17) if p not in (1, 5)]
    for port in result.parked[FANOUT]:
        assert not env.switch.ports[port]["enabled"]
    assert env.switch.ports[40] == outside  # 不在互聯區段 link 內，不動


def test_park_apply_cleans_pool_stray_but_keeps_out_of_pool_vlan(env: _Env) -> None:
    env.switch.ports[3]["allowed"][150] = "u"
    env.switch.ports[3]["native"] = 150
    env.switch.ports[3]["allowed"].pop(1)
    env.switch.defined.add(150)
    env.switch.set_hybrid(4, tagged=[4093])
    result = env.deployer.park(env.allocator.graph, {}, fanouts=[FANOUT], ports=[3, 4], apply=True)
    assert result.parked[FANOUT] == [3, 4]
    assert env.switch.ports[3]["allowed"] == {1: "u"} and env.switch.ports[3]["native"] == 1
    assert 150 not in env.switch.defined
    assert env.switch.ports[4]["allowed"] == {1: "u", 4093: "t"}
    assert 4093 in env.switch.defined


def test_park_rereads_ledger_under_device_lock(env: _Env) -> None:
    """傳入過期的 owned，apply 仍以鎖內帳本為準。"""
    session = env.allocate()
    result = env.deployer.park(
        env.allocator.graph, {}, fanouts=[FANOUT], ports=[1, 2], apply=True
    )
    assert [(p, o) for _f, p, o in result.skipped] == [(1, session.session_id)]
    assert result.parked[FANOUT] == [2]


def test_park_dry_run_lists_skipped_and_does_not_connect(env: _Env) -> None:
    session = env.allocate()
    result = env.deployer.park(
        env.allocator.graph, env.allocator.owned_nodes(), fanouts=[FANOUT], ports=[1, 2]
    )
    assert not result.applied and result.parked == {}
    assert [(p, o) for _f, p, o in result.skipped] == [(1, session.session_id)]
    assert env.factory_calls == []


def test_park_unknown_fanout_or_port_rejected(env: _Env) -> None:
    with pytest.raises(InterconnectRequestError, match="nope"):
        env.deployer.plan_park(env.allocator.graph, ["nope"])
    with pytest.raises(InterconnectRequestError, match="48"):
        env.deployer.plan_park(env.allocator.graph, [FANOUT], [48])


# ------------------------------------------------------------------ verify


def test_verify_session_detects_port_down_and_foreign_member(env: _Env) -> None:
    session = env.allocate()
    env.deployer.apply(session, apply=True)
    assert env.deployer.verify_session(session).ok
    env.switch.ports[1]["enabled"] = False
    env.switch.ports[20]["allowed"][100] = "u"
    result = env.deployer.verify_session(session)
    assert not result.ok
    text = "\n".join(result.problems)
    assert "Port Admin" in text and "VLAN 100 成員" in text


def test_verify_session_detects_changed_membership(env: _Env) -> None:
    session = env.allocate()
    env.deployer.apply(session, apply=True)
    env.switch.ports[5]["allowed"][1] = "u"
    result = env.deployer.verify_session(session)
    assert any("VLAN 1" in problem for problem in result.problems)


def test_verify_parked_detects_enabled_port_and_ignores_out_of_pool(env: _Env) -> None:
    env.switch.set_hybrid(2, tagged=[4093])
    for number in range(1, 17):
        env.switch.ports[number]["enabled"] = False
    assert env.deployer.verify_parked(env.allocator.graph, {}, fanouts=[FANOUT]).ok
    env.switch.ports[3]["enabled"] = True
    result = env.deployer.verify_parked(env.allocator.graph, {}, fanouts=[FANOUT])
    assert not result.ok
    assert any("1/3" in problem or "Eth1/3" in problem for problem in result.problems)


def test_verify_parked_skips_owned_ports(env: _Env) -> None:
    session = env.allocate()
    for number in range(1, 17):
        env.switch.ports[number]["enabled"] = False
    env.switch.ports[1]["enabled"] = True
    owned = env.allocator.owned_nodes()
    assert session.session_id in owned.values()
    assert env.deployer.verify_parked(env.allocator.graph, owned, fanouts=[FANOUT]).ok
