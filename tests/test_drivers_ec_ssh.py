"""drivers/ec_ssh.py 的 pytest 測試（設計 §8、FEAT-002）。

全程以注入的 fake connection（stub ``send_command`` / ``send_config_set`` /
``enable`` / ``disconnect``）繞過真 netmiko：本環境未安裝 netmiko，故測試絕不
import 真 ``netmiko.ConnectHandler``。涵蓋：

  - connect 流程：以注入 factory 建連、有 secret 時呼叫 enable、顯式關 pager。
  - read_vlans：餵暫定 show vlan fixture，驗自寫行解析為 tagged VlanMember
    （TODO(4a) 以真機輸出取代）。
  - build_commands：樣板組裝 == 已知輸入的樣板渲染。
  - apply_vlan/clear_vlan：送出的指令集 == build_commands 結果。
  - netmiko 例外：連線/逾時類例外被包成 DriverConnectionError（保留 __cause__、
    訊息不含密碼）。

歷史（HISTORY）：
  - 0.4.0  FEAT-002 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import List

import pytest

from topo_manager.drivers.base import PortVlanChange
from topo_manager.drivers.ec_ssh import (
    _APPLY_MEMBER_TEMPLATE,
    _CLEAR_MEMBER_TEMPLATE,
    _CREATE_VLAN_TEMPLATE,
    _DISABLE_PAGER_TEMPLATE,
    _ENTER_INTF_TEMPLATE,
    EcSshDriver,
)
from topo_manager.errors import (
    DriverConnectionError,
    DriverDependencyError,
    DriverParseError,
)

# 暫定 show vlan fixture（TODO(4a)：以 192.168.42.237 真機輸出取代）。
_SHOW_VLAN_FIXTURE = (
    "VLAN   Name       Ports\n"
    "100    vlan100    Eth1/7, Eth1/8\n"
    "200    vlan200    Eth1/9\n"
)


class _FakeConnection:
    """最小 netmiko connection 替身（紀錄呼叫，回預錄字串）。

    屬性：
      - enabled：是否呼叫過 enable()（bool）。
      - sent_commands：send_command 收到的指令清單（List[str]）。
      - config_sets：send_config_set 收到的每批指令清單（List[List[str]]）。
      - disconnected：是否呼叫過 disconnect()（bool）。
    """

    def __init__(self, show_vlan_output: str = _SHOW_VLAN_FIXTURE) -> None:
        self.enabled = False
        self.sent_commands: List[str] = []
        self.config_sets: List[List[str]] = []
        self.disconnected = False
        self._show_vlan_output = show_vlan_output

    def enable(self) -> None:
        """進入特權模式（紀錄旗標）。"""
        self.enabled = True

    def send_command(self, command: str) -> str:
        """紀錄唯讀指令並回預錄輸出（show vlan 回 fixture，其餘回空字串）。"""
        self.sent_commands.append(command)
        if "vlan" in command:
            return self._show_vlan_output
        return ""

    def send_config_set(self, commands: List[str]) -> str:
        """紀錄送出的設定指令批。"""
        self.config_sets.append(list(commands))
        return ""

    def disconnect(self) -> None:
        """紀錄已斷線。"""
        self.disconnected = True


def _make_driver(connection: _FakeConnection, secret=None) -> EcSshDriver:
    """以注入的 fake connection factory 建立 EcSshDriver（測試輔助）。"""
    return EcSshDriver(
        host="fanout-x",
        username="u",
        password="p",
        secret=secret,
        connection_factory=lambda **kwargs: connection,
    )


def test_connect_flow_disables_pager_no_enable_without_secret() -> None:
    """無 secret：connect 建連、不呼叫 enable、顯式關 pager。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    driver.connect()

    assert conn.enabled is False
    assert _DISABLE_PAGER_TEMPLATE in conn.sent_commands


def test_connect_flow_calls_enable_with_secret() -> None:
    """有 secret：connect 後呼叫 enable 進入特權模式。"""
    conn = _FakeConnection()
    driver = _make_driver(conn, secret="en")
    driver.connect()

    assert conn.enabled is True
    assert _DISABLE_PAGER_TEMPLATE in conn.sent_commands


def test_read_vlans_parses_tagged_members() -> None:
    """read_vlans 自寫解析 show vlan fixture 為 tagged 成員與既有 VLAN。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    driver.connect()

    state = driver.read_vlans()

    assert state.existing_vlans == frozenset({100, 200})
    present = {(m.interface, m.vlan, m.tagged) for m in state.members}
    assert ("Eth1/7", 100, True) in present
    assert ("Eth1/8", 100, True) in present
    assert ("Eth1/9", 200, True) in present


def test_read_vlans_parse_failure_raises() -> None:
    """show vlan 無任何可解析 VLAN 行時丟 DriverParseError。"""
    conn = _FakeConnection(show_vlan_output="no vlan here\njust text\n")
    driver = _make_driver(conn)
    driver.connect()

    with pytest.raises(DriverParseError):
        driver.read_vlans()


def test_build_commands_matches_template_render() -> None:
    """build_commands 的組裝 == 已知輸入的樣板渲染（建 VLAN 去重）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    changes = [
        PortVlanChange(
            fanout="fanout-x", interface="Eth1/7", vlan=100,
            action="add", topology_node="fanout-x:7",
        ),
        PortVlanChange(
            fanout="fanout-x", interface="Eth1/8", vlan=100,
            action="add", topology_node="fanout-x:8",
        ),
    ]

    commands = driver.build_commands(changes)

    expected = [
        _CREATE_VLAN_TEMPLATE.format(vlan=100),
        _ENTER_INTF_TEMPLATE.format(interface="Eth1/7"),
        _APPLY_MEMBER_TEMPLATE.format(interface="Eth1/7", vlan=100),
        _ENTER_INTF_TEMPLATE.format(interface="Eth1/8"),
        _APPLY_MEMBER_TEMPLATE.format(interface="Eth1/8", vlan=100),
    ]
    assert commands == expected


def test_apply_vlan_sends_build_commands_result() -> None:
    """apply_vlan 送出的指令集 == build_commands 的結果。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    driver.connect()
    changes = [
        PortVlanChange(
            fanout="fanout-x", interface="Eth1/7", vlan=100,
            action="add", topology_node="fanout-x:7",
        ),
    ]

    driver.apply_vlan(changes)

    assert conn.config_sets == [driver.build_commands(changes)]


def test_clear_vlan_sends_remove_commands() -> None:
    """clear_vlan 以清除樣板組裝並送出（走 remove 分支）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    driver.connect()
    changes = [
        PortVlanChange(
            fanout="fanout-x", interface="Eth1/7", vlan=100,
            action="add", topology_node="fanout-x:7",
        ),
    ]

    driver.clear_vlan(changes)

    assert len(conn.config_sets) == 1
    sent = conn.config_sets[0]
    assert _ENTER_INTF_TEMPLATE.format(interface="Eth1/7") in sent
    assert _CLEAR_MEMBER_TEMPLATE.format(interface="Eth1/7", vlan=100) in sent


class NetmikoTimeoutException(Exception):
    """替身：類別名與 netmiko 的 NetmikoTimeoutException 相同，供 driver 以名辨識。"""


def test_netmiko_exception_wrapped_as_connection_error() -> None:
    """netmiko 連線類例外被包成 DriverConnectionError（保留 __cause__、不含密碼）。"""

    def _raising_factory(**kwargs):
        """建連即丟 netmiko 逾時例外（以類別名被 driver 辨識）。"""
        raise NetmikoTimeoutException("timed out")

    driver = EcSshDriver(
        host="fanout-x",
        username="u",
        password="super-secret-pw",
        connection_factory=_raising_factory,
    )

    with pytest.raises(DriverConnectionError) as exc_info:
        driver.connect()

    # 保留 __cause__（exception chaining）。
    assert isinstance(exc_info.value.__cause__, NetmikoTimeoutException)
    # 訊息含 host、不含密碼。
    message = str(exc_info.value)
    assert "fanout-x" in message
    assert "super-secret-pw" not in message


def test_missing_netmiko_raises_dependency_error() -> None:
    """未注入 factory 且 netmiko 未安裝時，connect 丟 DriverDependencyError。

    本環境未安裝 netmiko，故不注入 factory 的 connect() 會於延遲 import 失敗。
    若環境日後安裝了 netmiko，本測試改以 skip 表示前提不成立。
    """
    try:
        import netmiko  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("此環境已安裝 netmiko，無法驗證未安裝路徑")

    driver = EcSshDriver(host="fanout-x", username="u", password="p")
    with pytest.raises(DriverDependencyError) as exc_info:
        driver.connect()
    assert "netmiko" in str(exc_info.value)
