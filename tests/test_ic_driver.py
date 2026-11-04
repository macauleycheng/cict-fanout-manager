"""EcSshInterconnectDriver 與指令組裝的 pytest 測試（互聯設計 §6.2、§10）。

涵蓋：指令序列逐字比對（phase 1／2、park 乾淨與有殘留、VLAN 清除）、
``FakeEcSwitch`` 上的端到端（啟用 → 讀回 → park → 清 VLAN，最終回到初始狀態，
含 hybrid + 4093 情境）、故意錯序在 fake 上失敗（證明 checked 掃描有效）、
``does not exist`` 僅清除批次容忍、``force_access_mode=False``、池外 VLAN 不被碰。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import copy
from typing import List

import pytest

from ic_fakes import FakeEcSwitch
from topo_manager.drivers.ec_ssh_ic import (
    EcSshInterconnectDriver,
    PortSwitchport,
    build_park_batch,
    build_park_commands,
    build_phase1_commands,
    build_phase2_commands,
    build_vlan_clear_commands,
    is_port_clean,
    pool_vlans,
)
from topo_manager.errors import ApplyError, DriverConnectionError

VLAN_MIN = 100
VLAN_MAX = 3000
LINK_VLAN = 3999  # 對應 findings 的測試 VLAN；此測試以 vlan_max=4000 涵蓋


def _driver(switch: FakeEcSwitch, force: bool = True) -> EcSshInterconnectDriver:
    driver = EcSshInterconnectDriver(
        "h", "u", "p", connection_factory=switch.factory, force_access_mode=force
    )
    driver.connect()
    return driver


# ---------------------------------------------------------------- 指令序列


def test_phase1_commands_exact_order() -> None:
    assert build_phase1_commands([(42, 3999), (43, 3999)]) == [
        "interface ethernet 1/42",
        "shutdown",
        "switchport mode access",
        "switchport allowed vlan add 3999 untagged",
        "switchport native vlan 3999",
        "switchport allowed vlan remove 1",
        "interface ethernet 1/43",
        "shutdown",
        "switchport mode access",
        "switchport allowed vlan add 3999 untagged",
        "switchport native vlan 3999",
        "switchport allowed vlan remove 1",
    ]


def test_phase1_without_force_access_mode_omits_mode_command() -> None:
    commands = build_phase1_commands([(43, 3999)], force_access_mode=False)
    assert "switchport mode access" not in commands
    assert commands[:2] == ["interface ethernet 1/43", "shutdown"]


def test_phase2_commands() -> None:
    assert build_phase2_commands([42, 43]) == [
        "interface ethernet 1/42",
        "no shutdown",
        "interface ethernet 1/43",
        "no shutdown",
    ]


def test_park_commands_clean_port_only_shuts_down() -> None:
    assert build_park_commands(42, [], clean=True) == ["interface ethernet 1/42", "shutdown"]


def test_park_commands_dirty_port_exact_order() -> None:
    assert build_park_commands(43, [3999, 150], clean=False) == [
        "interface ethernet 1/43",
        "shutdown",
        "switchport allowed vlan add 1 untagged",
        "no switchport native vlan",
        "switchport allowed vlan remove 150",
        "switchport allowed vlan remove 3999",
    ]


def test_park_batch_concatenates_ports() -> None:
    assert build_park_batch([(1, [], True), (2, [100], False)]) == (
        build_park_commands(1, [], True) + build_park_commands(2, [100], False)
    )


def test_vlan_clear_commands() -> None:
    assert build_vlan_clear_commands([3999, 100, 100]) == [
        "vlan database",
        "no vlan 100",
        "no vlan 3999",
    ]
    assert build_vlan_clear_commands([]) == []


def test_pool_vlans_only_returns_pool_members_excluding_one() -> None:
    allowed = {1: "u", 99: "t", 100: "u", 3000: "t", 3001: "t", 4093: "t"}
    assert pool_vlans(allowed, VLAN_MIN, VLAN_MAX) == [100, 3000]


def test_is_port_clean_ignores_out_of_pool_tagged_vlan() -> None:
    hybrid = PortSwitchport("Hybrid", 1, {1: "u", 4093: "t"})
    assert is_port_clean(hybrid, [])
    assert not is_port_clean(PortSwitchport("Access", 100, {100: "u"}), [100])
    assert not is_port_clean(PortSwitchport("Access", 1, {2: "u"}), [])


# ---------------------------------------------------------------- 端到端


def _enable(driver: EcSshInterconnectDriver, ports: List[int], vlan: int) -> None:
    driver.enable_phase1([(port, vlan) for port in ports])
    driver.enable_phase2(ports)


def _park(driver: EcSshInterconnectDriver, ports: List[int], vlan_max: int) -> None:
    items = []
    strays = set()
    for port in ports:
        switchport = driver.read_port_switchport(port)
        stray = pool_vlans(switchport.allowed, VLAN_MIN, vlan_max)
        strays.update(stray)
        items.append((port, stray, is_port_clean(switchport, stray)))
    driver.park_ports(items)
    driver.clear_vlan_definitions(strays, VLAN_MIN, vlan_max)


def test_end_to_end_returns_to_initial_state_and_keeps_4093() -> None:
    switch = FakeEcSwitch()
    switch.set_hybrid(43, tagged=[4093])
    initial = switch.snapshot()
    driver = _driver(switch, force=False)

    _enable(driver, [42, 43], LINK_VLAN)
    enabled_42 = driver.read_port_switchport(42)
    enabled_43 = driver.read_port_switchport(43)
    assert enabled_42.native_vlan == LINK_VLAN and enabled_42.allowed == {LINK_VLAN: "u"}
    assert enabled_43.native_vlan == LINK_VLAN
    assert enabled_43.allowed == {LINK_VLAN: "u", 4093: "t"}
    assert driver.read_vlan_members(LINK_VLAN) == frozenset({"Eth1/42", "Eth1/43"})
    assert driver.read_port_status(42).enabled and driver.read_port_status(43).enabled

    _park(driver, [42, 43], 4000)

    expected = copy.deepcopy(initial)
    for port in (42, 43):
        expected["ports"][port]["enabled"] = False  # park 後只多一個 shutdown
    assert switch.snapshot() == expected
    assert driver.read_vlan_members(LINK_VLAN) is None
    assert driver.read_port_switchport(43).allowed == {1: "u", 4093: "t"}
    assert 4093 in switch.defined


def test_force_access_mode_true_sets_access_on_hybrid_port() -> None:
    switch = FakeEcSwitch()
    switch.set_hybrid(43)
    driver = _driver(switch, force=True)
    driver.enable_phase1([(43, 150)])
    assert driver.read_port_switchport(43).mode == "Access"
    assert "switchport mode access" in switch.commands


def test_force_access_mode_false_leaves_hybrid_mode() -> None:
    switch = FakeEcSwitch()
    switch.set_hybrid(43)
    driver = _driver(switch, force=False)
    driver.enable_phase1([(43, 150)])
    assert driver.read_port_switchport(43).mode == "Hybrid"
    assert "switchport mode access" not in switch.commands


def test_out_of_pool_vlan_is_never_touched_by_park() -> None:
    switch = FakeEcSwitch()
    switch.set_hybrid(43, tagged=[4093])
    driver = _driver(switch, force=False)
    _enable(driver, [43], 150)
    _park(driver, [43], VLAN_MAX)
    assert not any("4093" in command for command in switch.commands)
    assert switch.ports[43]["allowed"] == {1: "u", 4093: "t"}


# ---------------------------------------------------------------- 錯序在 fake 上失敗


def test_native_before_add_fails_with_pvid_error() -> None:
    switch = FakeEcSwitch()
    driver = _driver(switch)
    with pytest.raises(ApplyError, match="Failed to set PVID"):
        driver._send_config_checked(
            ["interface ethernet 1/42", "switchport native vlan 3999"], "錯序"
        )
    assert switch.ports[42]["native"] == 1


def test_no_native_before_add_one_fails() -> None:
    switch = FakeEcSwitch()
    driver = _driver(switch, force=False)
    _enable(driver, [43], 150)
    with pytest.raises(ApplyError, match="Failed to set PVID"):
        driver._send_config_checked(
            ["interface ethernet 1/43", "shutdown", "no switchport native vlan"], "錯序 park"
        )


def test_wrong_park_order_corrupts_pvid_on_hybrid_port() -> None:
    """錯序（先 remove link VLAN）會讓 hybrid port 的 PVID 變成殘留 VLAN。"""
    switch = FakeEcSwitch()
    switch.set_hybrid(43, tagged=[4093])
    driver = _driver(switch, force=False)
    _enable(driver, [43], 150)
    driver._send_config_checked(
        ["interface ethernet 1/43", "switchport allowed vlan remove 150"], "錯序"
    )
    assert switch.ports[43]["native"] == 4093


def test_vlan_database_inside_interface_mode_is_rejected() -> None:
    switch = FakeEcSwitch()
    driver = _driver(switch)
    with pytest.raises(ApplyError, match="Invalid input"):
        driver._send_config_checked(
            ["interface ethernet 1/42", "vlan database", "no vlan 3999"], "混批"
        )


def test_does_not_exist_only_tolerated_in_clear_batches() -> None:
    switch = FakeEcSwitch()
    driver = _driver(switch)
    batch = ["interface ethernet 1/42", "switchport allowed vlan remove 3999"]
    with pytest.raises(ApplyError, match="does not exist"):
        driver._send_config_checked(batch, "移除")
    driver._send_config_checked(batch, "移除", tolerate_missing=True)


def test_checked_error_message_names_line_and_neighbours() -> None:
    switch = FakeEcSwitch()
    driver = _driver(switch)
    with pytest.raises(ApplyError) as excinfo:
        driver._send_config_checked(
            ["interface ethernet 1/42", "switchport native vlan 3999"], "錯序"
        )
    message = str(excinfo.value)
    assert "Failed to set PVID on ethernet 1/42" in message
    assert "switchport native vlan 3999" in message


def test_empty_batch_is_noop() -> None:
    switch = FakeEcSwitch()
    driver = _driver(switch)
    assert driver._send_config_checked([], "空") == ""
    assert switch.config_batches == []


def test_config_requires_connection() -> None:
    switch = FakeEcSwitch()
    driver = EcSshInterconnectDriver("h", "u", "p", connection_factory=switch.factory)
    with pytest.raises(DriverConnectionError):
        driver._send_config_checked(["interface ethernet 1/42"], "未連線")


# ---------------------------------------------------------------- VLAN 定義清除


def test_clear_vlan_definitions_keeps_vlan_with_members_and_warns() -> None:
    switch = FakeEcSwitch()
    driver = _driver(switch, force=False)
    _enable(driver, [42, 43], 150)
    switch.ports[44]["allowed"][150] = "u"  # 第三個成員
    cleared, warnings = driver.clear_vlan_definitions([150], VLAN_MIN, VLAN_MAX)
    assert cleared == []
    assert len(warnings) == 1 and "150" in warnings[0]
    assert 150 in switch.defined


def test_clear_vlan_definitions_skips_out_of_pool_and_missing() -> None:
    switch = FakeEcSwitch()
    switch.set_hybrid(43, tagged=[4093])
    driver = _driver(switch)
    cleared, warnings = driver.clear_vlan_definitions([1, 4093, 150], VLAN_MIN, VLAN_MAX)
    assert cleared == [] and warnings == []
    assert 4093 in switch.defined
    assert switch.config_batches == []


def test_clear_vlan_definitions_runs_in_separate_batch() -> None:
    switch = FakeEcSwitch()
    driver = _driver(switch, force=False)
    _enable(driver, [42], 150)
    driver.park_ports([(42, [150], False)])
    switch.config_batches.clear()
    cleared, _warnings = driver.clear_vlan_definitions([150], VLAN_MIN, VLAN_MAX)
    assert cleared == [150]
    assert switch.config_batches == [["vlan database", "no vlan 150"]]
