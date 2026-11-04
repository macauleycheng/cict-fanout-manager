"""drivers/ec_ssh.py 的 pytest 測試（設計 §8、FEAT-002）。

全程以注入的 fake connection（stub ``send_command`` / ``send_config_set`` /
``enable`` / ``disconnect``）繞過真 netmiko：本環境未安裝 netmiko，故測試絕不
import 真 ``netmiko.ConnectHandler``。涵蓋：

  - connect 流程：以注入 factory 建連、有 secret 時呼叫 enable、顯式關 pager。
  - read_vlans：餵真機 ECS5550-54X 的 show vlan fixture（key-value 多行區塊 +
    續行成員 + 'Eth1/ N(S)' token），驗 key-value 區塊 parser 解析為 tagged
    VlanMember、既有 VLAN 集合、interface 歸一化一致。
  - build_commands：樣板組裝 == 已知輸入的真機語法渲染（含 trunk 模式冪等與
    'add <vlan> tagged'）。
  - apply_vlan/clear_vlan：送出的指令集 == build_commands 結果（含 tagged）。
  - netmiko 例外：連線/逾時類例外被包成 DriverConnectionError（保留 __cause__、
    訊息不含密碼）。

歷史（HISTORY）：
  - 0.4.0  FEAT-002 初版。
  - 0.4.1  FEAT-003 4a 實機修正：show vlan fixture 改用真機 ECS5550-54X
           key-value 區塊格式；驗 parser 的續行成員收集、(S)=tagged、interface
           歸一化（'Eth1/ 2' → 'Eth1/2'）；build/apply/clear 改驗真機語法
           （interface ethernet 1/<port>、switchport mode trunk 冪等、
           allowed vlan add <vlan> tagged / remove <vlan>）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import List

import pytest

from topo_manager.drivers.base import PortRole_E, PortVlanChange
from topo_manager.drivers.ec_ssh import (
    _ACCESS_MODE_TEMPLATE,
    _APPLY_MEMBER_TEMPLATE,
    _CLEAR_MEMBER_TEMPLATE,
    _DISABLE_PAGER_TEMPLATE,
    _ENTER_INTF_TEMPLATE,
    _GLOBAL_TUNNEL_CONTROL_TEMPLATE,
    _NATIVE_VLAN_TEMPLATE,
    _NO_NATIVE_VLAN_TEMPLATE,
    _SHOW_DOT1Q_TUNNEL,
    _TRUNK_MODE_TEMPLATE,
    _TUNNEL_ACCESS_TEMPLATE,
    _TUNNEL_NORMAL_TEMPLATE,
    _TUNNEL_UPLINK_TEMPLATE,
    EcSshDriver,
)
from topo_manager.errors import (
    DriverConnectionError,
    DriverDependencyError,
    DriverParseError,
)

# 真機 ECS5550-54X 的 show vlan 輸出樣本（FEAT-003 4a 實測格式）：key-value 多行
# 區塊，每 VLAN 一區塊；成員列於 'Ports/Port Channels :' 行並可續行縮排；成員
# token 形如 'Eth1/ 2(S)'（斜線後可能有對齊空白，如 'Eth1/ 2' 與 'Eth1/10'），
# '(S)' = static/tagged 成員。Remote SPAN VLANs 區塊置於最後（此處留空）。
_SHOW_VLAN_FIXTURE = (
    "VLAN ID             : 1\n"
    "Type                : Static\n"
    "Name                : DefaultVlan\n"
    "Status              : Active\n"
    "Ports/Port Channels : Eth1/ 2(S) Eth1/ 3(S) Eth1/ 4(S) Eth1/ 5(S)\n"
    "                      Eth1/ 6(S) Eth1/ 7(S) Eth1/ 8(S) Eth1/10(S)\n"
    "\n"
    "VLAN ID             : 10\n"
    "Type                : Static\n"
    "Name                : vlan10\n"
    "Status              : Active\n"
    "Ports/Port Channels : Eth1/41(S)\n"
    "\n"
    "VLAN ID             : 4093\n"
    "Type                : Static\n"
    "Name                : vlan4093\n"
    "Status              : Active\n"
    "Ports/Port Channels : Eth1/41(S)\n"
    "\n"
    "Remote SPAN VLANs\n"
    "-----------------\n"
    "\n"
)

# 真機 ECS5550-54X 的 'show dot1q-tunnel' 輸出樣本（本次 live 實機擷取定稿，
# finding #1）：含全域 '802.1Q Tunnel Status : Enabled' 行、'802.1Q Tunnel TPID'
# 行與 per-port Mode 表。表頭為 'Port Mode Priority Mapping'，每行形如
# 'Eth 1/ 1 Uplink Disabled'——port token 是 'Eth 1/ 1'（'Eth' 後有空格、斜線後
# 個位數前有對齊空格），兩位數則為 'Eth 1/11'（斜線後無空格）。Mode 欄 ∈
# Uplink/Access/Normal。interface token 經 _normalize_interface 歸一化後與 apply
# 名一致（'Eth 1/ 1' → 'Eth1/1'、'Eth 1/41' → 'Eth1/41'）。
_SAMPLE_SHOW_DOT1Q_TUNNEL = (
    "802.1Q Tunnel Status : Enabled\n"
    "802.1Q Tunnel TPID   : 8100 (Hex)\n"
    "\n"
    "Port     Mode   Priority Mapping \n"
    "-------- ------ ---------------- \n"
    "Eth 1/ 1 Uplink Disabled         \n"
    "Eth 1/ 2 Normal Disabled         \n"
    "Eth 1/11 Access Disabled         \n"
    "Eth 1/41 Normal Disabled         \n"
    "Eth 1/42 Access Disabled         \n"
)

# status=Disabled 樣本（驗 read_tunnel_status 解析 Disabled→False）。
_SAMPLE_TUNNEL_DISABLED = "802.1Q Tunnel Status : Disabled\n"


class _FakeConnection:
    """最小 netmiko connection 替身（紀錄呼叫，回預錄字串）。

    屬性：
      - enabled：是否呼叫過 enable()（bool）。
      - sent_commands：send_command 收到的指令清單（List[str]）。
      - config_sets：send_config_set 收到的每批指令清單（List[List[str]]）。
      - disconnected：是否呼叫過 disconnect()（bool）。
    """

    def __init__(
        self,
        show_vlan_output: str = _SHOW_VLAN_FIXTURE,
        show_dot1q_output: str = "",
    ) -> None:
        self.enabled = False
        self.sent_commands: List[str] = []
        self.config_sets: List[List[str]] = []
        self.disconnected = False
        self._show_vlan_output = show_vlan_output
        self._show_dot1q_output = show_dot1q_output

    def enable(self) -> None:
        """進入特權模式（紀錄旗標）。"""
        self.enabled = True

    def send_command(self, command: str) -> str:
        """紀錄唯讀指令並回預錄輸出（show vlan / show dot1q-tunnel 各回 fixture）。"""
        self.sent_commands.append(command)
        if "dot1q-tunnel" in command:
            return self._show_dot1q_output
        if "vlan" in command:
            return self._show_vlan_output
        return ""

    def send_config_set(self, commands: List[str], **kwargs) -> str:
        """紀錄送出的設定指令批（吞下 config_mode_command 等 netmiko 參數）。"""
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
    """read_vlans 以真機 key-value 區塊格式解析成員、既有 VLAN、interface 歸一化。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    driver.connect()

    state = driver.read_vlans()

    # 既有 VLAN 集合取自各 'VLAN ID :' 區塊。
    assert state.existing_vlans == frozenset({1, 10, 4093})
    present = {(m.interface, m.vlan, m.tagged) for m in state.members}
    # VLAN 1 的續行成員與 interface 歸一化（'Eth1/ 2' → 'Eth1/2'）。
    assert ("Eth1/2", 1, True) in present
    assert ("Eth1/8", 1, True) in present
    # 續行行收集的成員也須被解析到。
    assert ("Eth1/10", 1, True) in present
    # (S) 判為 tagged=True。
    assert ("Eth1/41", 10, True) in present
    assert ("Eth1/41", 4093, True) in present


def test_read_vlans_interface_name_matches_apply_name() -> None:
    """read-back 解析出的 interface 名與 apply 用 interface 名一致（4b 比對前提）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    driver.connect()

    state = driver.read_vlans()

    # apply 時用 interface_template 產生的名（如 'Eth1/41'）須能在 read-back
    # 成員集合中以相同字串比對到（(vlan, tagged) in members 的前提）。
    interface = driver._interface_template.format(port=41)
    matched = {
        (m.vlan, m.tagged) for m in state.members if m.interface == interface
    }
    assert (4093, True) in matched


def test_read_vlans_parse_failure_raises() -> None:
    """show vlan 無任何可解析 VLAN 行時丟 DriverParseError。"""
    conn = _FakeConnection(show_vlan_output="no vlan here\njust text\n")
    driver = _make_driver(conn)
    driver.connect()

    with pytest.raises(DriverParseError):
        driver.read_vlans()


def test_build_commands_matches_template_render() -> None:
    """build_commands 的組裝 == 真機語法樣板渲染（進介面 + trunk 模式 + tagged）。"""
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

    # UPLINK（預設 role）互聯序列每介面含 dot1q-tunnel mode uplink（AC-4）。
    expected = [
        _ENTER_INTF_TEMPLATE.format(port="7"),
        _TRUNK_MODE_TEMPLATE,
        _APPLY_MEMBER_TEMPLATE.format(vlan=100),
        _TUNNEL_UPLINK_TEMPLATE,
        _ENTER_INTF_TEMPLATE.format(port="8"),
        _TRUNK_MODE_TEMPLATE,
        _APPLY_MEMBER_TEMPLATE.format(vlan=100),
        _TUNNEL_UPLINK_TEMPLATE,
    ]
    assert commands == expected
    # 加成員指令必帶 'tagged' 關鍵字（實機 running-config 語法）。
    assert "switchport allowed vlan add 100 tagged" in commands


def test_build_commands_trunk_mode_idempotent_per_interface() -> None:
    """同一介面在同一批中 trunk 模式只下一次（冪等去重）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    changes = [
        PortVlanChange(
            fanout="fanout-x", interface="Eth1/41", vlan=100,
            action="add", topology_node="fanout-x:41",
        ),
        PortVlanChange(
            fanout="fanout-x", interface="Eth1/41", vlan=200,
            action="add", topology_node="fanout-x:41",
        ),
    ]

    commands = driver.build_commands(changes)

    # 同一介面兩筆 add，trunk 模式指令只出現一次。
    assert commands.count(_TRUNK_MODE_TEMPLATE) == 1
    # 進介面指令以 port 編號組裝（interface ethernet 1/41）。
    assert _ENTER_INTF_TEMPLATE.format(port="41") in commands


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
    assert _ENTER_INTF_TEMPLATE.format(port="7") in sent
    assert _CLEAR_MEMBER_TEMPLATE.format(vlan=100) in sent
    # clear 不應帶 tagged、也不應下 trunk 模式（只移除成員）。
    assert "switchport allowed vlan remove 100" in sent
    assert _TRUNK_MODE_TEMPLATE not in sent


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


# --------------------------------------------------------------------
# Phase 4 QinQ / dot1q-tunnel 擴充測試（決策 4、設計測試計畫）。
# --------------------------------------------------------------------


def _edge_change(interface: str, vlan: int, action: str = "add") -> PortVlanChange:
    """建一筆 EDGE 角色變更（測試輔助）。"""
    return PortVlanChange(
        fanout="fanout-x", interface=interface, vlan=vlan,
        action=action, topology_node="fanout-x:{0}".format(interface),
        role=PortRole_E.EDGE,
    )


def _uplink_change(interface: str, vlan: int, action: str = "add") -> PortVlanChange:
    """建一筆 UPLINK 角色變更（測試輔助）。"""
    return PortVlanChange(
        fanout="fanout-x", interface=interface, vlan=vlan,
        action=action, topology_node="fanout-x:{0}".format(interface),
        role=PortRole_E.UPLINK,
    )


def test_build_commands_edge_add_sequence() -> None:
    """EDGE add 產出恰 4 行：進介面→access→native vlan→tunnel access（AC-3）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    commands = driver.build_commands([_edge_change("Eth1/42", 3999)])

    assert commands == [
        _ENTER_INTF_TEMPLATE.format(port="42"),
        _ACCESS_MODE_TEMPLATE,
        _NATIVE_VLAN_TEMPLATE.format(vlan=3999),
        _TUNNEL_ACCESS_TEMPLATE,
    ]


def test_build_commands_uplink_add_sequence() -> None:
    """UPLINK add 產出恰 4 行含 uplink：進介面→trunk→add tagged→uplink（AC-4）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    commands = driver.build_commands([_uplink_change("Eth1/41", 100)])

    assert commands == [
        _ENTER_INTF_TEMPLATE.format(port="41"),
        _TRUNK_MODE_TEMPLATE,
        _APPLY_MEMBER_TEMPLATE.format(vlan=100),
        _TUNNEL_UPLINK_TEMPLATE,
    ]


def test_build_commands_uplink_multi_vlan_trunk_uplink_once() -> None:
    """UPLINK 同介面多 VLAN：trunk 與 uplink 模式各只下一次（冪等去重）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    commands = driver.build_commands(
        [_uplink_change("Eth1/41", 100), _uplink_change("Eth1/41", 200)]
    )

    assert commands.count(_TRUNK_MODE_TEMPLATE) == 1
    assert commands.count(_TUNNEL_UPLINK_TEMPLATE) == 1
    # 兩個 VLAN 的 tagged 成員都有加。
    assert _APPLY_MEMBER_TEMPLATE.format(vlan=100) in commands
    assert _APPLY_MEMBER_TEMPLATE.format(vlan=200) in commands


def test_build_commands_uplink_remove_restores_tunnel_normal() -> None:
    """UPLINK remove 走 allowed vlan remove + no dot1q-tunnel mode（還原 Normal）。

    live 實測：互聯 port 清理須把 tunnel 模式自 uplink 還原為 Normal，真機正確
    字面為 'no switchport dot1q-tunnel mode'（決策 7、finding #1）。
    """
    conn = _FakeConnection()
    driver = _make_driver(conn)
    commands = driver.build_commands(
        [_uplink_change("Eth1/41", 3999, action="remove")]
    )

    assert commands == [
        _ENTER_INTF_TEMPLATE.format(port="41"),
        _CLEAR_MEMBER_TEMPLATE.format(vlan=3999),
        _TUNNEL_NORMAL_TEMPLATE,
    ]


def test_build_commands_uplink_remove_tunnel_normal_once() -> None:
    """UPLINK remove 同介面多 VLAN：tunnel 模式還原只下一次（冪等去重）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    commands = driver.build_commands(
        [
            _uplink_change("Eth1/41", 100, action="remove"),
            _uplink_change("Eth1/41", 200, action="remove"),
        ]
    )

    assert commands.count(_TUNNEL_NORMAL_TEMPLATE) == 1
    assert _CLEAR_MEMBER_TEMPLATE.format(vlan=100) in commands
    assert _CLEAR_MEMBER_TEMPLATE.format(vlan=200) in commands


def test_build_commands_edge_remove_sequence() -> None:
    """EDGE remove 走 no native vlan + mode normal（測試臨界清理，決策 7）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    commands = driver.build_commands([_edge_change("Eth1/42", 3999, action="remove")])

    assert commands == [
        _ENTER_INTF_TEMPLATE.format(port="42"),
        _NO_NATIVE_VLAN_TEMPLATE,
        _TUNNEL_NORMAL_TEMPLATE,
    ]


def test_clear_vlan_preserves_edge_role() -> None:
    """clear_vlan 重建變更保留 EDGE role（不掉成 UPLINK，決策 7）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    driver.connect()
    driver.clear_vlan([_edge_change("Eth1/42", 3999)])

    sent = conn.config_sets[0]
    # EDGE 清除走 no native vlan + mode normal，而非 UPLINK 的 allowed vlan remove。
    assert _NO_NATIVE_VLAN_TEMPLATE in sent
    assert _TUNNEL_NORMAL_TEMPLATE in sent
    assert not any("allowed vlan remove" in cmd for cmd in sent)


def test_setup_global_tunnel_dry_run_returns_commands() -> None:
    """setup_global_tunnel(apply=False) 回含 system-tunnel-control 指令、不連線（AC-5）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    commands = driver.setup_global_tunnel(apply=False)

    assert commands == [_GLOBAL_TUNNEL_CONTROL_TEMPLATE]
    # dry-run 不送出任何設定指令。
    assert conn.config_sets == []


def test_setup_global_tunnel_apply_sends_commands() -> None:
    """setup_global_tunnel(apply=True) 經 fake connection 送出同指令（AC-5）。"""
    conn = _FakeConnection()
    driver = _make_driver(conn)
    driver.connect()
    commands = driver.setup_global_tunnel(apply=True)

    assert commands == [_GLOBAL_TUNNEL_CONTROL_TEMPLATE]
    assert conn.config_sets == [[_GLOBAL_TUNNEL_CONTROL_TEMPLATE]]


def test_read_tunnel_status_enabled_disabled_unknown() -> None:
    """read_tunnel_status 解析 Enabled→True / Disabled→False / 未知→None。"""
    enabled = _make_driver(
        _FakeConnection(show_dot1q_output=_SAMPLE_SHOW_DOT1Q_TUNNEL)
    )
    enabled.connect()
    assert enabled.read_tunnel_status() is True

    disabled = _make_driver(
        _FakeConnection(show_dot1q_output=_SAMPLE_TUNNEL_DISABLED)
    )
    disabled.connect()
    assert disabled.read_tunnel_status() is False

    unknown = _make_driver(_FakeConnection(show_dot1q_output=""))
    unknown.connect()
    assert unknown.read_tunnel_status() is None


def test_read_tunnel_status_uses_single_parse_entry(monkeypatch) -> None:
    """read_tunnel_status 與 read_vlans 皆經 _parse_dot1q_tunnel_output（finding #3）。"""
    conn = _FakeConnection(show_dot1q_output=_SAMPLE_SHOW_DOT1Q_TUNNEL)
    driver = _make_driver(conn)
    driver.connect()

    calls = {"n": 0}
    original = driver._parse_dot1q_tunnel_output

    def _spy(raw):
        calls["n"] += 1
        return original(raw)

    monkeypatch.setattr(driver, "_parse_dot1q_tunnel_output", _spy)

    driver.read_tunnel_status()
    driver.read_vlans()
    # 兩條路徑都經唯一入口（read_tunnel_status 一次、read_vlans 一次）。
    assert calls["n"] == 2


def test_read_vlans_parses_tunnel_modes_and_status() -> None:
    """read_vlans 併讀後 tunnel_modes / tunnel_status 正確（per-port 定稿，AC-6）。"""
    conn = _FakeConnection(show_dot1q_output=_SAMPLE_SHOW_DOT1Q_TUNNEL)
    driver = _make_driver(conn)
    driver.connect()

    state = driver.read_vlans()

    assert state.tunnel_status is True
    # 真機樣本：'Eth 1/ 1'=Uplink（個位數斜線後有對齊空白）、'Eth 1/11'=Access
    # （兩位數斜線後無空白）、'Eth 1/41'=Normal。皆經正則容忍 'Eth' 後與斜線後
    # 空白並歸一化後與 apply 名對齊。
    assert ("Eth1/1", "Uplink") in state.tunnel_modes
    assert ("Eth1/2", "Normal") in state.tunnel_modes
    assert ("Eth1/11", "Access") in state.tunnel_modes
    assert ("Eth1/41", "Normal") in state.tunnel_modes
    assert ("Eth1/42", "Access") in state.tunnel_modes
    # interface token 經 _normalize_interface 與 apply 名一致。
    assert driver._normalize_interface("1", "42") == "Eth1/42"


def test_read_vlans_tunnel_empty_when_no_tunnel_block() -> None:
    """無 tunnel 區塊時 tunnel_modes 空、tunnel_status None（不丟錯，決策 3）。"""
    conn = _FakeConnection(show_dot1q_output="")
    driver = _make_driver(conn)
    driver.connect()

    state = driver.read_vlans()
    assert state.tunnel_modes == frozenset()
    assert state.tunnel_status is None
    # show vlan 仍正常解析。
    assert state.existing_vlans == frozenset({1, 10, 4093})


def test_show_dot1q_tunnel_command_has_no_pipe_filter() -> None:
    """送出的 show dot1q-tunnel 指令字面不含 '| include'（AC-7）。"""
    conn = _FakeConnection(show_dot1q_output=_SAMPLE_SHOW_DOT1Q_TUNNEL)
    driver = _make_driver(conn)
    driver.connect()
    driver.read_vlans()

    assert _SHOW_DOT1Q_TUNNEL in conn.sent_commands
    assert all("| include" not in cmd for cmd in conn.sent_commands)


# ----------------------------------------------------------------------
# 設備下發 REST：逾時類例外改丟 DeviceTimeoutError（0.5.2）
# ----------------------------------------------------------------------
class ReadTimeout(Exception):
    """替身：類別名與 netmiko 4.x send_command 逾時例外 ReadTimeout 相同。"""


class NetmikoAuthenticationException(Exception):
    """替身：類別名與 netmiko 認證失敗例外相同。"""


class ConnectionException(Exception):
    """替身：類別名與 netmiko ConnectionException 相同。"""


def _connect_raising(exc: Exception) -> None:
    def _factory(**kwargs):
        raise exc

    EcSshDriver(
        host="fanout-x", username="u", password="super-secret-pw", connection_factory=_factory
    ).connect()


def test_netmiko_timeout_raises_device_timeout_error() -> None:
    """NetmikoTimeoutException → DeviceTimeoutError，且仍是 DriverConnectionError。"""
    from topo_manager.errors import DeviceTimeoutError

    with pytest.raises(DeviceTimeoutError) as info:
        _connect_raising(NetmikoTimeoutException("timed out"))
    assert isinstance(info.value, DriverConnectionError)
    assert isinstance(info.value.__cause__, NetmikoTimeoutException)
    assert "fanout-x" in str(info.value)
    assert "super-secret-pw" not in str(info.value)


def test_read_timeout_raises_device_timeout_error() -> None:
    """ReadTimeout（netmiko 4.x send_command 逾時）同樣是 DeviceTimeoutError。"""
    from topo_manager.errors import DeviceTimeoutError

    with pytest.raises(DeviceTimeoutError):
        _connect_raising(ReadTimeout("read timed out"))


@pytest.mark.parametrize("exc_class", [NetmikoAuthenticationException, ConnectionException])
def test_auth_and_connection_errors_stay_plain_connection_error(exc_class) -> None:
    """認證失敗與 ConnectionException 不是逾時：維持純 DriverConnectionError。"""
    from topo_manager.errors import DeviceTimeoutError

    with pytest.raises(DriverConnectionError) as info:
        _connect_raising(exc_class("boom"))
    assert not isinstance(info.value, DeviceTimeoutError)
