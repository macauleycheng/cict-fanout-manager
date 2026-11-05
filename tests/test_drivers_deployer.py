"""drivers/deployer.py 的 pytest 測試（設計 §7、FEAT-002）。

以 mock driver（注入 driver_factory）驗證編排層行為，不需真機、不 import
netmiko：

  - dry-run（apply=False）不呼叫 apply_vlan；apply=True 才呼叫。
  - 套用前快照以 tagged=True 跳過已存在成員（diff_expected_vs_actual）。
  - read-back 一致（通過）/ 不一致（丟 ReadBackMismatchError）。
  - detect_and_reapply：缺漏→重套→驗證全鏈路。
  - 部分失敗觸發 best-effort clear_vlan + ApplyError。

session 以範例三層拓樸經 Allocator.derive_session 純推導合成（不落帳），
確保 planner 能算出真實的 trunk 介面變更。

歷史（HISTORY）：
  - 0.4.0  FEAT-002 初版。
  - 0.6.0  FEAT-001 撤下發：新增以**真** EcSshDriver + 注入 FakeTrafficSwitch
           （connection_factory，每 host 共用同一假設備使 apply/remove 共享
           狀態）驗證 ``Deployer.remove`` 的四個案例：UPLINK happy、EDGE happy
           （以 tunnel_modes 還原為 Normal 判通過，證明不看 members）、UPLINK
           drop_config 殘留丟 ReadBackMismatchError、EDGE-only drop_config 殘留
           丟 ReadBackMismatchError（finding #1 守門：若回讀只看 members 會誤
           判通過）。另驗 remove(apply=False) 回 remove 分支指令且不連線。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import pytest

from topo_manager.allocator import Allocator
from topo_manager.drivers.base import (
    PortRole_E,
    PortVlanChange,
    VlanMember,
    VlanState,
)
from topo_manager.drivers.deployer import Deployer, SetupResult
from topo_manager.drivers.ec_ssh import EcSshDriver
from topo_manager.errors import ApplyError, ReadBackMismatchError
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore

from ic_fakes import FakeTrafficSwitch


class _FakeEntry:
    """最小 FanoutEntry 替身（具 interface_template）。"""

    def __init__(self, interface_template: str = "Eth1/{port}") -> None:
        self.host = "h"
        self.username = "u"
        self.password = "p"
        self.device_type = "cisco_ios"
        self.interface_template = interface_template


class _FakeConfig:
    """最小 FanoutConfig 替身（具 fanouts 映射）。"""

    def __init__(self, names: List[str]) -> None:
        self.fanouts: Dict[str, _FakeEntry] = {
            name: _FakeEntry() for name in names
        }


class _MockDriver:
    """可編程 mock FanoutDriver（紀錄呼叫、依腳本回 read_vlans）。

    建構參數：
      - snapshots：read_vlans 依序回傳的 VlanState 腳本（List[VlanState]）；
        超過長度後持續回最後一筆。
      - fail_on_apply：apply_vlan 是否丟例外（bool）。
    """

    def __init__(self, fanout: str) -> None:
        self.fanout = fanout
        self.connected = False
        self.disconnected = False
        self.apply_calls: List[List[PortVlanChange]] = []
        self.clear_calls: List[List[PortVlanChange]] = []
        self._snapshots: List[VlanState] = []
        self._read_index = 0
        self.fail_on_apply = False
        self.setup_calls = 0
        self._tunnel_status = True

    def program(self, snapshots: List[VlanState], fail_on_apply: bool = False) -> "_MockDriver":
        """設定 read_vlans 腳本與 apply 是否失敗（回自身便於串接）。"""
        self._snapshots = snapshots
        self.fail_on_apply = fail_on_apply
        return self

    def setup_global_tunnel(self, apply: bool = False) -> List[str]:
        """stub：回固定全域 setup 指令；apply=True 時紀錄呼叫次數。"""
        if apply:
            self.setup_calls += 1
        return ["dot1q-tunnel system-tunnel-control"]

    def read_tunnel_status(self):
        """stub：回固定全域 tunnel status（True=Enabled）。"""
        return self._tunnel_status

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def read_vlans(self) -> VlanState:
        if not self._snapshots:
            return VlanState(
                fanout=self.fanout, members=frozenset(), existing_vlans=frozenset()
            )
        index = min(self._read_index, len(self._snapshots) - 1)
        self._read_index += 1
        return self._snapshots[index]

    def build_commands(self, changes: List[PortVlanChange]) -> List[str]:
        return ["cmd {0} vlan {1}".format(c.interface, c.vlan) for c in changes]

    def apply_vlan(self, changes: List[PortVlanChange]) -> None:
        if self.fail_on_apply:
            raise RuntimeError("設備拒絕下發")
        self.apply_calls.append(list(changes))

    def clear_vlan(self, changes: List[PortVlanChange]) -> None:
        self.clear_calls.append(list(changes))

    def read_show(self, command: str) -> str:
        return ""


def _make_session(three_tier_path: Path, tmp_path: Path):
    """以 derive_session 純推導一筆三層 session（不落帳，供下發測試）。"""
    store = JsonSessionStore(tmp_path / "state" / "allocations.json")
    allocator = Allocator(load_topology(three_tier_path), store)
    return allocator.derive_session("ixia-1/1", "dut-A1:Eth0")


def _satisfied_state(fanout: str, changes: List[PortVlanChange]) -> VlanState:
    """由期望變更組出「全部達成」的 VlanState（供 read-back 通過）。

    依 role 分流組達成訊號（Phase 4 QinQ 擴充）：UPLINK 類以 tagged=True 成員
    表達、EDGE 類以 ``(interface, "Access")`` 的 tunnel_modes 表達（diff 依
    role 分流判定，決策 6）。
    """
    members = {
        VlanMember(vlan=c.vlan, interface=c.interface, tagged=True)
        for c in changes
        if c.role == PortRole_E.UPLINK
    }
    tunnel_modes = {
        (c.interface, "Access")
        for c in changes
        if c.role == PortRole_E.EDGE
    }
    return VlanState(
        fanout=fanout,
        members=frozenset(members),
        existing_vlans=frozenset({c.vlan for c in changes}),
        tunnel_modes=frozenset(tunnel_modes),
    )


def _empty_state(fanout: str) -> VlanState:
    """空 VlanState（設備上尚無任何相關成員）。"""
    return VlanState(fanout=fanout, members=frozenset(), existing_vlans=frozenset())


def test_dry_run_does_not_apply(three_tier_path: Path, tmp_path: Path) -> None:
    """apply=False：不連線、不呼叫 apply_vlan（dry-run 閘門）。"""
    session = _make_session(three_tier_path, tmp_path)
    config = _FakeConfig(["spine-1", "agg-1", "leaf-1-1"])
    created: List[_MockDriver] = []

    def _factory(name, entry):
        driver = _MockDriver(name)
        created.append(driver)
        return driver

    deployer = Deployer(_factory, config)
    deployer.apply(session, apply=False)

    # dry-run 期間 build_commands 可被呼叫（plan 純算），但不得 connect/apply。
    assert all(not d.connected for d in created)
    assert all(len(d.apply_calls) == 0 for d in created)


def test_apply_true_calls_apply_and_skips_satisfied(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """apply=True：空快照→套用；已 tagged 滿足→跳過（兩種 fanout 對照）。"""
    session = _make_session(three_tier_path, tmp_path)
    # 先用 planner 算出各 fanout 的期望變更，供組 read_vlans 腳本。
    config = _FakeConfig(["spine-1", "agg-1", "leaf-1-1"])
    planner_deployer = Deployer(lambda n, e: _MockDriver(n), config)
    grouped = planner_deployer._changes_by_fanout(session)

    drivers_by_name: Dict[str, _MockDriver] = {}

    def _factory(name, entry):
        changes = grouped.get(name, [])
        driver = _MockDriver(name)
        if name == "spine-1":
            # spine-1 已滿足：快照即為已達成 → 應跳過 apply，read-back 通過。
            driver.program([_satisfied_state(name, changes), _satisfied_state(name, changes)])
        else:
            # 其餘 fanout：套用前空、套用後達成。
            driver.program([_empty_state(name), _satisfied_state(name, changes)])
        drivers_by_name[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    deployer.apply(session, apply=True)

    # spine-1 已滿足 → 未呼叫 apply_vlan。
    assert len(drivers_by_name["spine-1"].apply_calls) == 0
    # 其餘有變更的 fanout → 呼叫 apply_vlan。
    for name, driver in drivers_by_name.items():
        if name == "spine-1":
            continue
        if grouped.get(name):
            assert len(driver.apply_calls) == 1


def test_read_back_mismatch_raises(three_tier_path: Path, tmp_path: Path) -> None:
    """read-back 仍缺漏時丟 ReadBackMismatchError 並 best-effort clear。"""
    session = _make_session(three_tier_path, tmp_path)
    config = _FakeConfig(["spine-1", "agg-1", "leaf-1-1"])
    planner_deployer = Deployer(lambda n, e: _MockDriver(n), config)
    grouped = planner_deployer._changes_by_fanout(session)

    # 找出第一個有變更的 fanout，讓它套用後 read-back 仍空（缺漏）。
    target = next(name for name in grouped if grouped[name])

    drivers_by_name: Dict[str, _MockDriver] = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        changes = grouped.get(name, [])
        if name == target:
            # 套用前空、套用後仍空 → read-back 缺漏。
            driver.program([_empty_state(name), _empty_state(name)])
        else:
            driver.program([_empty_state(name), _satisfied_state(name, changes)])
        drivers_by_name[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    with pytest.raises(ReadBackMismatchError):
        deployer.apply(session, apply=True)

    # best-effort clear：至少目標 fanout 被清理（含本次已套用項）。
    assert len(drivers_by_name[target].clear_calls) >= 1


def test_detect_and_reapply_full_chain(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """detect_and_reapply：缺漏→重套→再比對通過（全鏈路）。"""
    session = _make_session(three_tier_path, tmp_path)
    config = _FakeConfig(["spine-1", "agg-1", "leaf-1-1"])
    planner_deployer = Deployer(lambda n, e: _MockDriver(n), config)
    grouped = planner_deployer._changes_by_fanout(session)

    drivers_by_name: Dict[str, _MockDriver] = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        changes = grouped.get(name, [])
        # 第一次讀：缺漏（空）；重套後第二次讀：達成。
        driver.program([_empty_state(name), _satisfied_state(name, changes)])
        drivers_by_name[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    result = deployer.detect_and_reapply(session, apply=True)

    assert result.ok is True
    # 有變更的 fanout 都重套了缺漏項。
    for name, driver in drivers_by_name.items():
        if grouped.get(name):
            assert len(driver.apply_calls) == 1


def test_detect_without_apply_reports_missing(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """detect_and_reapply(apply=False)：僅回報缺漏、不重套。"""
    session = _make_session(three_tier_path, tmp_path)
    config = _FakeConfig(["spine-1", "agg-1", "leaf-1-1"])
    planner_deployer = Deployer(lambda n, e: _MockDriver(n), config)
    grouped = planner_deployer._changes_by_fanout(session)

    drivers_by_name: Dict[str, _MockDriver] = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        driver.program([_empty_state(name)])
        drivers_by_name[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    result = deployer.detect_and_reapply(session, apply=False)

    assert result.ok is False
    for name, driver in drivers_by_name.items():
        if grouped.get(name):
            assert name in result.missing_by_fanout
            assert len(driver.apply_calls) == 0


def test_partial_failure_triggers_clear_and_apply_error(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """其中一台 apply 失敗：best-effort clear 先前成功者並包成 ApplyError。"""
    session = _make_session(three_tier_path, tmp_path)
    config = _FakeConfig(["spine-1", "agg-1", "leaf-1-1"])
    planner_deployer = Deployer(lambda n, e: _MockDriver(n), config)
    grouped = planner_deployer._changes_by_fanout(session)

    # 依 plan 的 fanout 排序，挑最後一個有變更的 fanout 讓它 apply 失敗，
    # 確保它之前至少有一台成功套用（供驗證 best-effort clear）。
    ordered = [name for name in sorted(config.fanouts) if grouped.get(name)]
    assert len(ordered) >= 2
    failing = ordered[-1]
    first_success = ordered[0]

    drivers_by_name: Dict[str, _MockDriver] = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        changes = grouped.get(name, [])
        if name == failing:
            driver.program([_empty_state(name)], fail_on_apply=True)
        else:
            driver.program([_empty_state(name), _satisfied_state(name, changes)])
        drivers_by_name[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    with pytest.raises(ApplyError) as exc_info:
        deployer.apply(session, apply=True)

    assert failing in str(exc_info.value)
    # 先前成功套用的 fanout 被 best-effort clear。
    assert len(drivers_by_name[first_success].clear_calls) >= 1


# --------------------------------------------------------------------
# Phase 4 QinQ / dot1q-tunnel 擴充測試（決策 7、8、9）。
# --------------------------------------------------------------------


def _edge(interface: str, vlan: int) -> PortVlanChange:
    """建一筆 EDGE add 變更（測試輔助）。"""
    return PortVlanChange(
        fanout="f", interface=interface, vlan=vlan, action="add",
        topology_node="f:{0}".format(interface), role=PortRole_E.EDGE,
    )


def _uplink(interface: str, vlan: int) -> PortVlanChange:
    """建一筆 UPLINK add 變更（測試輔助）。"""
    return PortVlanChange(
        fanout="f", interface=interface, vlan=vlan, action="add",
        topology_node="f:{0}".format(interface), role=PortRole_E.UPLINK,
    )


def test_best_effort_clear_only_uplink() -> None:
    """_best_effort_clear 餵含 EDGE+UPLINK 時，driver.clear_vlan 僅收到 UPLINK（決策 7）。"""
    config = _FakeConfig(["f"])
    created = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        created[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    applied = [("f", [_edge("Eth1/42", 3999), _uplink("Eth1/41", 100)])]
    deployer._best_effort_clear(applied)

    assert len(created["f"].clear_calls) == 1
    cleared = created["f"].clear_calls[0]
    # 只有 UPLINK 被清，EDGE 不被餵給 driver。
    assert all(c.role == PortRole_E.UPLINK for c in cleared)
    assert {c.interface for c in cleared} == {"Eth1/41"}


def test_best_effort_clear_edge_only_does_not_connect() -> None:
    """applied 只含 EDGE 時過濾後空、不連線（不動邊緣 port，決策 7）。"""
    config = _FakeConfig(["f"])
    created = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        created[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    deployer._best_effort_clear([("f", [_edge("Eth1/42", 3999)])])

    # 過濾後為空：不應建立/連線該 driver，更不應 clear。
    assert "f" not in created


def test_setup_tunnel_dry_run_no_connect() -> None:
    """setup_tunnel(apply=False)：不連線、回 SetupResult 含指令、status=None（決策 8）。"""
    config = _FakeConfig(["spine-1", "agg-1"])
    created = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        created[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    results = deployer.setup_tunnel(["spine-1", "agg-1"], apply=False)

    assert len(results) == 2
    assert all(isinstance(r, SetupResult) for r in results)
    assert all(r.status is None for r in results)
    assert all(
        "dot1q-tunnel system-tunnel-control" in r.commands for r in results
    )
    # dry-run 未連線、未下發。
    assert all(not d.connected for d in created.values())
    assert all(d.setup_calls == 0 for d in created.values())


def test_setup_tunnel_apply_connects_and_reads_status() -> None:
    """setup_tunnel(apply=True)：連線下發並回回讀 status（決策 8）。"""
    config = _FakeConfig(["spine-1"])
    created = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        created[name] = driver
        return driver

    deployer = Deployer(_factory, config)
    results = deployer.setup_tunnel(["spine-1"], apply=True)

    assert len(results) == 1
    assert results[0].status is True
    assert created["spine-1"].connected is True
    assert created["spine-1"].disconnected is True
    assert created["spine-1"].setup_calls == 1


def test_format_missing_edge_mentions_tunnel_access() -> None:
    """_format_missing 對 EDGE 缺漏輸出 tunnel access 字樣（finding #8）。"""
    missing = [_edge("Eth1/42", 3999), _uplink("Eth1/41", 100)]
    message = Deployer._format_missing("f", missing)
    assert "tunnel access 設定" in message
    assert "tagged 成員" in message
    assert "Eth1/42" in message
    assert "Eth1/41" in message


# --------------------------------------------------------------------
# FEAT-001 撤下發（Deployer.remove）：以真 EcSshDriver + FakeTrafficSwitch
# 注入（connection_factory）驗證；絕不連真機。
# --------------------------------------------------------------------


class _FakeSegment:
    """最小 trunk segment 替身（具 a_node / b_node）。"""

    def __init__(self, a_node: str, b_node: str) -> None:
        self.a_node = a_node
        self.b_node = b_node


class _FakeSession:
    """最小 Session 替身：直接指定 vlan / trunk_segments / path。

    供精準控制 planner 產出的 role（UPLINK 來自 trunk 兩端、EDGE 來自 path 扣
    trunk 端），不經真實拓樸即能組出「純 UPLINK」或「純 EDGE」的下發計畫。
    """

    def __init__(
        self,
        vlan: int,
        trunk_segments: List[_FakeSegment],
        path: List[str],
        session_id: str = "s-test",
    ) -> None:
        self.vlan = vlan
        self.trunk_segments = trunk_segments
        self.path = path
        self.session_id = session_id


def _ec_factory_for(switches: Dict[str, FakeTrafficSwitch]):
    """回一個 driver_factory：每 host 共用同一 FakeTrafficSwitch（apply/remove 共狀態）。

    FakeTrafficSwitch 自身無 factory 方法，故在此包一層 connection_factory：
    回傳該 host 的假設備並累加其 ``connect_count``（供斷言「從未連線」）。
    """

    def _factory(name: str, entry: object) -> EcSshDriver:
        switch = switches[name]

        def _connection_factory(**kwargs: object) -> FakeTrafficSwitch:
            switch.connect_count += 1
            return switch

        return EcSshDriver(
            host=name,
            username="u",
            password="p",
            connection_factory=_connection_factory,
        )

    return _factory


def _uplink_session(vlan: int = 100) -> _FakeSession:
    """純 UPLINK session：trunk 兩端皆在 fanout 'f'（path 無額外邊緣節點）。"""
    seg = _FakeSegment("f:7", "f:8")
    # path 僅含 trunk 端 → _derive_edge_nodes 扣掉後為空 → 無 EDGE 變更。
    return _FakeSession(vlan=vlan, trunk_segments=[seg], path=["f:7", "f:8"])


def _edge_only_session(vlan: int = 3999) -> _FakeSession:
    """純 EDGE session：trunk 段兩端皆非 fanout 'f'；'f' 僅以邊緣節點出現。

    trunk 端為 generator/DUT（非 configured fanout）故不產 UPLINK；path 內的
    'f:42' 為邊緣節點 → 僅產 EDGE 變更。
    """
    seg = _FakeSegment("ixia-1:ixia-1/1", "dut-A:Eth0")
    return _FakeSession(
        vlan=vlan,
        trunk_segments=[seg],
        path=["ixia-1:ixia-1/1", "f:42", "dut-A:Eth0"],
    )


def test_remove_dry_run_returns_remove_commands_and_no_connect() -> None:
    """remove(apply=False)：回 remove 分支指令、絕不連線（connect_count 為 0）。"""
    switches = {"f": FakeTrafficSwitch()}
    config = _FakeConfig(["f"])
    deployer = Deployer(_ec_factory_for(switches), config)

    result = deployer.remove(_uplink_session(), apply=False)

    plan = next(fp for fp in result.fanouts if fp.fanout == "f")
    assert plan.changes  # 有變更
    assert all(c.action == "remove" for c in plan.changes)
    # remove 分支指令：UPLINK remove 應含 'allowed vlan remove' 與 tunnel 還原。
    assert any("allowed vlan remove 100" in cmd for cmd in plan.commands)
    assert "no switchport dot1q-tunnel mode" in plan.commands
    # 絕不連線。
    assert switches["f"].connect_count == 0


def test_remove_apply_uplink_happy_path() -> None:
    """(a) UPLINK：apply 後 remove，show vlan 不再列 tagged 成員 → 回讀通過。"""
    switches = {"f": FakeTrafficSwitch()}
    config = _FakeConfig(["f"])
    deployer = Deployer(_ec_factory_for(switches), config)
    session = _uplink_session()

    # 先下發，使假設備出現 tagged 成員。
    deployer.apply(session, apply=True)
    assert switches["f"].tagged.get(100)  # 成員存在

    # 撤下發：不應丟錯，且假設備不再有該 VLAN 的 tagged 成員。
    deployer.remove(session, apply=True)
    assert not switches["f"].tagged.get(100)


def test_remove_apply_edge_happy_path() -> None:
    """(b) EDGE：apply 後 remove，tunnel mode 還原為 Normal → 回讀通過（看 tunnel_modes）。"""
    switches = {"f": FakeTrafficSwitch()}
    config = _FakeConfig(["f"])
    deployer = Deployer(_ec_factory_for(switches), config)
    session = _edge_only_session()

    deployer.apply(session, apply=True)
    # apply 後該 EDGE port tunnel mode 為 Access。
    assert switches["f"].tunnel_modes.get(42) == "Access"

    # remove：EDGE 走 remove 分支還原 tunnel 模式 → 不再是 Access → 回讀通過。
    deployer.remove(session, apply=True)
    assert switches["f"].tunnel_modes.get(42) != "Access"


def test_remove_apply_uplink_drop_config_raises_mismatch() -> None:
    """(c) UPLINK drop_config：假設備靜默忽略 remove → tagged 殘留 → ReadBackMismatchError。"""
    switch = FakeTrafficSwitch()
    switches = {"f": switch}
    config = _FakeConfig(["f"])
    deployer = Deployer(_ec_factory_for(switches), config)
    session = _uplink_session()

    deployer.apply(session, apply=True)
    assert switch.tagged.get(100)

    # 下發完成後才開啟 drop_config：remove 指令被靜默忽略 → 成員殘留。
    switch.drop_config = True
    with pytest.raises(ReadBackMismatchError):
        deployer.remove(session, apply=True)


def test_remove_apply_edge_only_drop_config_raises_mismatch() -> None:
    """(d) EDGE-only drop_config：tunnel 仍 Access → 必丟 ReadBackMismatchError（finding #1 守門）。

    此案若回讀只看 members 會誤判通過（EDGE 的 members 本就為空）；必須以
    tunnel_modes 判定，故 drop_config 下 tunnel 仍 Access 時應判殘留。
    """
    switch = FakeTrafficSwitch()
    switches = {"f": switch}
    config = _FakeConfig(["f"])
    deployer = Deployer(_ec_factory_for(switches), config)
    session = _edge_only_session()

    deployer.apply(session, apply=True)
    assert switch.tunnel_modes.get(42) == "Access"

    switch.drop_config = True
    with pytest.raises(ReadBackMismatchError):
        deployer.remove(session, apply=True)
