"""api/push_common.py 的單元測試（tests/test_push_common.py）。

涵蓋 ``translate_device_errors`` 對 ``ApplyError.__cause__`` 鏈的解包、
``require_credentials_for`` 的連線前核對、預設 driver 工廠只建構不連線，以及
port 解析輔助。不連任何設備。

歷史（HISTORY）：
  - 0.1.0  設備下發 REST 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from topo_manager.allocator import Allocator
from topo_manager.api import push_common
from topo_manager.drivers.config import FanoutConfig, FanoutEntry
from topo_manager.errors import (
    ApplyError,
    ConfigError,
    DeviceTimeoutError,
    DriverConnectionError,
    DriverDependencyError,
    PortMappingError,
    ReadBackMismatchError,
)
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore


def _applyerror_from(cause: Exception, depth: int = 1) -> ApplyError:
    """組出 cause 在 __cause__ 鏈第 ``depth`` 層的 ApplyError。"""
    inner: Exception = cause
    for _ in range(depth - 1):
        wrapper = ApplyError("wrapper")
        wrapper.__cause__ = inner
        inner = wrapper
    outer = ApplyError("下發失敗")
    outer.__cause__ = inner
    return outer


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_timeout_cause_becomes_device_timeout(depth: int) -> None:
    with pytest.raises(DeviceTimeoutError):
        with push_common.translate_device_errors():
            raise _applyerror_from(DeviceTimeoutError("t"), depth)


@pytest.mark.parametrize("depth", [1, 2])
def test_connection_cause_becomes_driver_connection_error(depth: int) -> None:
    with pytest.raises(DriverConnectionError) as info:
        with push_common.translate_device_errors():
            raise _applyerror_from(DriverConnectionError("c"), depth)
    assert type(info.value) is DriverConnectionError


@pytest.mark.parametrize(
    "cause",
    [DriverDependencyError("no netmiko"), ConfigError("cfg"), PortMappingError("map")],
)
def test_config_class_causes_are_restored_to_original(cause: Exception) -> None:
    with pytest.raises(type(cause)) as info:
        with push_common.translate_device_errors():
            raise _applyerror_from(cause, 2)
    assert info.value is cause


def test_plain_apply_error_is_unchanged() -> None:
    original = ApplyError("設備拒絕指令")
    original.__cause__ = RuntimeError("rejected")
    with pytest.raises(ApplyError) as info:
        with push_common.translate_device_errors():
            raise original
    assert info.value is original


def test_other_errors_pass_through() -> None:
    with pytest.raises(ReadBackMismatchError):
        with push_common.translate_device_errors():
            raise ReadBackMismatchError("mismatch")


def _session(three_tier_path: Path, tmp_path: Path):
    store = JsonSessionStore(tmp_path / "state" / "allocations.json")
    return Allocator(load_topology(three_tier_path), store).derive_session(
        "ixia-1/1", "dut-A1:Eth0"
    )


def _entry(index: int) -> FanoutEntry:
    return FanoutEntry(
        host="fake-host-{0}".format(index),
        username="fake-user-{0}".format(index),
        password="fake-pass-{0}".format(index),
    )


def test_require_credentials_for(three_tier_path: Path, tmp_path: Path) -> None:
    """缺 credentials 的 fanout 列在 ConfigError；涵蓋時為 no-op。"""
    session = _session(three_tier_path, tmp_path)
    known = {"spine-1", "agg-1", "agg-2", "leaf-1-1", "leaf-2-1"}
    full = FanoutConfig(fanouts={name: _entry(i) for i, name in enumerate(sorted(known))})
    assert push_common.require_credentials_for(session, full, known) is None

    partial = FanoutConfig(fanouts={"spine-1": _entry(0)})
    with pytest.raises(ConfigError) as info:
        push_common.require_credentials_for(session, partial, known)
    listed = str(info.value).split("[", 1)[1].split("]", 1)[0]
    assert "agg-1" in listed
    assert "spine-1" not in listed


def test_default_factories_construct_without_connecting() -> None:
    """預設工廠只建構 driver（不連線），欄位取自 FanoutEntry。"""
    from topo_manager.drivers.ec_ssh import EcSshDriver
    from topo_manager.drivers.ec_ssh_ic import EcSshInterconnectDriver

    entry = FanoutEntry(
        host="fake-host-9",
        username="fake-user-9",
        password="fake-pass-9",
        device_type="cisco_ios",
        interface_template="Eth1/{port}",
    )
    traffic = push_common.default_driver_factory("f", entry)
    interconnect = push_common.default_ic_driver_factory("f", entry)
    assert type(traffic) is EcSshDriver
    assert isinstance(interconnect, EcSshInterconnectDriver)
    assert traffic._host == "fake-host-9"
    assert interconnect._host == "fake-host-9"
    assert interconnect.force_access_mode is True
    assert traffic._connection is None


def test_driver_factory_of_prefers_injected() -> None:
    marker = object()
    state = SimpleNamespace(push_driver_factory=marker, ic_push_driver_factory=None)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    assert push_common.driver_factory_of(request, interconnect=False) is marker
    assert (
        push_common.driver_factory_of(request, interconnect=True)
        is push_common.default_ic_driver_factory
    )


def test_port_helpers() -> None:
    assert push_common.interface_port("Eth1/7") == 7
    assert push_common.interface_port("weird") is None
    link = lambda a, b: SimpleNamespace(a_node=a, b_node=b)  # noqa: E731
    session = SimpleNamespace(links=[link("ic-1:3", "ic-1:19"), link("ic-2:3", "ic-1:3")])
    assert push_common.ports_by_fanout(session) == {"ic-1": [3, 19], "ic-2": [3]}


def test_module_does_not_import_drivers_or_netmiko_at_import_time() -> None:
    """push 層模組 import 時不觸發 drivers／netmiko 匯入（未啟用不碰設備層）。"""
    import subprocess

    code = (
        "import sys\n"
        "import topo_manager.api\n"
        "from topo_manager.api import push_common, push_routes, ic_push_routes\n"
        "assert not [m for m in sys.modules if m.startswith('topo_manager.drivers')], 'drivers'\n"
        "assert 'netmiko' not in sys.modules\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
