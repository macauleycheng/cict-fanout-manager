"""drivers/ec_ssh_ic.py 讀回 parser 的 pytest 測試（互聯設計 §6.3、§10）。

全部以 device-findings 的真機原始輸出為 fixture：access／hybrid、``No such
VLAN.``、空成員、Allowed 續行；缺欄位一律 ``DriverParseError``。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import pytest

from topo_manager.drivers.ec_ssh_ic import (
    PortStatus,
    PortSwitchport,
    parse_port_status,
    parse_port_switchport,
    parse_vlan_id,
)
from topo_manager.errors import DriverParseError

STATUS_UP_DOWN = """\
Information of Eth 1/42
 Basic Information:
  Port Type              : 10GBASE SFP+
  Port Admin             : Up
  Link Status            : Down
"""

STATUS_ADMIN_DOWN = """\
  Port Admin             : Down
  Link Status            : Down
  Link Down Reason       : Port Admin
"""

SWITCHPORT_ACCESS_PRE = """\
 VLAN Membership Mode          : Access
 Native VLAN                   : 1
 Allowed VLAN                  :     1(u)
"""

SWITCHPORT_HYBRID_PRE = """\
 VLAN Membership Mode          : Hybrid
 Native VLAN                   : 1
 Allowed VLAN                  :     1(u), 4093(t)
"""

SWITCHPORT_HYBRID_ENABLED = """\
 VLAN Membership Mode          : Hybrid
 Native VLAN                   : 3999
 Allowed VLAN                  :  3999(u), 4093(t)
"""

SWITCHPORT_WRAPPED = """\
 VLAN Membership Mode          : Hybrid
 Ingress Rule                  : Disabled
 Native VLAN                   : 100
 Allowed VLAN                  :  100(u), 101(t), 102(t),
                                  103(t), 104(t)
 Forbidden VLAN                :
"""

VLAN_TWO_MEMBERS = """\
VLAN ID             : 3999
Type                : Static
Name                : 
Status              : Active
Ports/Port Channels : Eth1/42(S) Eth1/43(S)
"""

VLAN_ALIGNED_MEMBERS = """\
VLAN ID             : 100
Type                : Static
Name                : 
Status              : Active
Ports/Port Channels : Eth1/ 2(S) Eth1/10(S)
"""

VLAN_EMPTY = """\
VLAN ID             : 3999
Type                : Static
Name                : 
Status              : Active
Ports/Port Channels :
"""


def test_status_enabled_link_down() -> None:
    assert parse_port_status(STATUS_UP_DOWN) == PortStatus(enabled=True, link_up=False)


def test_status_port_down_ignores_reason_line() -> None:
    assert parse_port_status(STATUS_ADMIN_DOWN) == PortStatus(enabled=False, link_up=False)


@pytest.mark.parametrize("raw", ["", "  Port Admin : Up\n", "  Link Status : Down\n", "garbage"])
def test_status_missing_field_raises(raw: str) -> None:
    with pytest.raises(DriverParseError):
        parse_port_status(raw)


def test_status_non_string_raises() -> None:
    with pytest.raises(DriverParseError):
        parse_port_status(None)  # type: ignore[arg-type]


def test_switchport_access_pre_test() -> None:
    assert parse_port_switchport(SWITCHPORT_ACCESS_PRE) == PortSwitchport("Access", 1, {1: "u"})


def test_switchport_hybrid_pre_test() -> None:
    result = parse_port_switchport(SWITCHPORT_HYBRID_PRE)
    assert result.mode == "Hybrid"
    assert result.native_vlan == 1
    assert result.allowed == {1: "u", 4093: "t"}


def test_switchport_hybrid_enabled() -> None:
    result = parse_port_switchport(SWITCHPORT_HYBRID_ENABLED)
    assert result.native_vlan == 3999
    assert result.allowed == {3999: "u", 4093: "t"}


def test_switchport_allowed_continuation_line() -> None:
    result = parse_port_switchport(SWITCHPORT_WRAPPED)
    assert result.allowed == {100: "u", 101: "t", 102: "t", 103: "t", 104: "t"}


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " Native VLAN : 1\n Allowed VLAN : 1(u)\n",
        " VLAN Membership Mode : Access\n Allowed VLAN : 1(u)\n",
        " VLAN Membership Mode : Access\n Native VLAN : 1\n",
    ],
)
def test_switchport_missing_field_raises(raw: str) -> None:
    with pytest.raises(DriverParseError):
        parse_port_switchport(raw)


def test_vlan_id_two_members() -> None:
    assert parse_vlan_id(VLAN_TWO_MEMBERS, 3999) == frozenset({"Eth1/42", "Eth1/43"})


def test_vlan_id_normalizes_aligned_members() -> None:
    assert parse_vlan_id(VLAN_ALIGNED_MEMBERS, 100) == frozenset({"Eth1/2", "Eth1/10"})


def test_vlan_id_no_such_vlan_is_none() -> None:
    assert parse_vlan_id("No such VLAN.\n", 3999) is None


def test_vlan_id_defined_without_members_is_empty_set() -> None:
    assert parse_vlan_id(VLAN_EMPTY, 3999) == frozenset()


def test_vlan_id_wrong_vlan_or_garbage_raises() -> None:
    with pytest.raises(DriverParseError):
        parse_vlan_id(VLAN_TWO_MEMBERS, 100)
    with pytest.raises(DriverParseError):
        parse_vlan_id("something unexpected", 100)
