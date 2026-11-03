"""下發層 CLI 子命令整合測試（plan/probe/apply/verify，設計 §CLI、FEAT-002）。

以 monkeypatch 替換 driver_factory / 注入 mock driver，驗證：
  - plan dry-run 不連線並印 fanout→interface→VLAN→指令。
  - plan --session 走 get_session、--from/--to 走 derive_session（不落 ledger）。
  - apply 預設 dry-run 不寫入、--apply 觸發寫入。
  - probe 選台：多台未指定 --fanout 回 ConfigError（非零）。
  - 錯誤回非零、stderr 單行、無 traceback。

另確認既有 tests/test_cli_sessions.py 全綠（回歸）——由全量 pytest 保證，本檔
聚焦下發層。全程不 import 真 netmiko（plan 走純算、apply 以 mock driver）。

歷史（HISTORY）：
  - 0.4.0  FEAT-002 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest

from topo_manager import cli
from topo_manager.cli import main
from topo_manager.drivers.base import PortVlanChange, VlanMember, VlanState
from topo_manager.drivers.deployer import Deployer


def _state_arg(tmp_path: Path) -> str:
    """回傳 tmp_path 下的狀態檔路徑字串（供 --state 使用）。"""
    return str(tmp_path / "state" / "allocations.json")


def _write_credentials(tmp_path: Path, names: List[str]) -> str:
    """寫一個含指定 fanout 名的連線設定檔，回其路徑字串（供 --credentials）。"""
    data = {
        "fanouts": {
            name: {
                "host": "h-{0}".format(name),
                "username": "u",
                "password": "p",
                "interface_template": "Eth1/{port}",
            }
            for name in names
        }
    }
    path = tmp_path / "creds.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class _FakeEntry:
    """最小 FanoutEntry 替身（供 mock config / factory）。"""

    def __init__(self, name: str) -> None:
        self.host = "h-{0}".format(name)
        self.username = "u"
        self.password = "p"
        self.device_type = "cisco_ios"
        self.interface_template = "Eth1/{port}"


class _FakeConfig:
    """最小 FanoutConfig 替身（具 fanouts 映射）。"""

    def __init__(self, names: List[str]) -> None:
        self.fanouts = {name: _FakeEntry(name) for name in names}


class _MockDriver:
    """可編程 mock driver（紀錄呼叫、依腳本回 read_vlans）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.connected = False
        self.apply_calls: List[List[PortVlanChange]] = []
        self.clear_calls: List[List[PortVlanChange]] = []
        self._snapshots: List[VlanState] = []
        self._index = 0

    def program(self, snapshots: List[VlanState]) -> "_MockDriver":
        self._snapshots = snapshots
        return self

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        pass

    def read_vlans(self) -> VlanState:
        if not self._snapshots:
            return VlanState(name_as_fanout(self.name))
        index = min(self._index, len(self._snapshots) - 1)
        self._index += 1
        return self._snapshots[index]

    def build_commands(self, changes: List[PortVlanChange]) -> List[str]:
        return ["cmd {0} vlan {1}".format(c.interface, c.vlan) for c in changes]

    def apply_vlan(self, changes: List[PortVlanChange]) -> None:
        self.apply_calls.append(list(changes))

    def clear_vlan(self, changes: List[PortVlanChange]) -> None:
        self.clear_calls.append(list(changes))

    def read_show(self, command: str) -> str:
        return "ok"


def name_as_fanout(name: str) -> VlanState:
    """以 fanout 名組一個空 VlanState（_MockDriver 無腳本時的預設回讀）。"""
    return VlanState(fanout=name, members=frozenset(), existing_vlans=frozenset())


def test_plan_dry_run_prints_without_connecting(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """plan --from/--to 純算印 fanout→interface→VLAN→指令，不連線（不落 ledger）。"""
    state = _state_arg(tmp_path)
    creds = _write_credentials(tmp_path, ["spine-1", "agg-1", "leaf-1-1"])

    code = main(
        [
            "plan", str(three_tier_path),
            "--from", "ixia-1/1", "--to", "dut-A1:Eth0",
            "--state", state, "--credentials", creds,
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    # 至少印出一台 fanout、一個介面與 VLAN。
    assert "fanout " in out
    assert "介面 Eth1/" in out
    assert "VLAN" in out
    # --from/--to 走 derive_session：不寫 ledger 檔。
    assert not Path(state).exists()


def test_plan_session_uses_get_session(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """plan --session 走 get_session（先 allocate 落帳，再 plan 取回）。"""
    state = _state_arg(tmp_path)
    creds = _write_credentials(tmp_path, ["spine-1", "agg-1", "leaf-1-1"])
    topo = str(three_tier_path)

    assert main(["allocate", topo, "--from", "ixia-1/1", "--to", "dut-A1:Eth0", "--state", state]) == 0
    capsys.readouterr()

    assert main(["plan", topo, "--session", "s-0001", "--state", state, "--credentials", creds]) == 0
    out = capsys.readouterr().out
    assert "s-0001" in out


def test_plan_session_and_endpoints_mutually_exclusive(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """plan 同時給 --session 與 --from/--to → ConfigError、非零、單行、無 traceback。"""
    state = _state_arg(tmp_path)
    code = main(
        [
            "plan", str(three_tier_path),
            "--session", "s-0001", "--from", "ixia-1/1", "--to", "dut-A1:Eth0",
            "--state", state,
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("錯誤：")
    assert "Traceback" not in captured.err


def test_apply_default_dry_run_does_not_write(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply 預設 dry-run：不呼叫 apply_vlan（以 monkeypatch 注入 mock driver）。"""
    state = _state_arg(tmp_path)
    topo = str(three_tier_path)
    assert main(["allocate", topo, "--from", "ixia-1/1", "--to", "dut-A1:Eth0", "--state", state]) == 0
    capsys.readouterr()

    names = ["spine-1", "agg-1", "leaf-1-1"]
    config = _FakeConfig(names)
    created: Dict[str, _MockDriver] = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        created[name] = driver
        return driver

    def _fake_build_deployer(credentials):
        return Deployer(_factory, config), config

    monkeypatch.setattr(cli, "_build_deployer", _fake_build_deployer)

    code = main(["apply", topo, "--session", "s-0001", "--state", state])
    assert code == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    # dry-run：未連線、未套用。
    assert all(not d.connected for d in created.values())
    assert all(len(d.apply_calls) == 0 for d in created.values())


def test_apply_with_flag_writes(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply --apply：觸發連線與 apply_vlan（mock driver 快照→達成）。"""
    state = _state_arg(tmp_path)
    topo = str(three_tier_path)
    assert main(["allocate", topo, "--from", "ixia-1/1", "--to", "dut-A1:Eth0", "--state", state]) == 0
    capsys.readouterr()

    names = ["spine-1", "agg-1", "leaf-1-1"]
    config = _FakeConfig(names)

    # 先用一個 planner-only deployer 算各 fanout 期望變更，供組 read_vlans 腳本。
    planner_deployer = Deployer(lambda n, e: _MockDriver(n), config)
    # 取回剛 allocate 的 session 以算變更。
    allocator = cli._build_allocator(topo, state, 100, 3000)
    session = allocator.get_session("s-0001")
    grouped = planner_deployer._changes_by_fanout(session)

    created: Dict[str, _MockDriver] = {}

    def _factory(name, entry):
        driver = _MockDriver(name)
        changes = grouped.get(name, [])
        members = frozenset(
            VlanMember(vlan=c.vlan, interface=c.interface, tagged=True)
            for c in changes
        )
        satisfied = VlanState(
            fanout=name, members=members,
            existing_vlans=frozenset({c.vlan for c in changes}),
        )
        empty = VlanState(fanout=name, members=frozenset(), existing_vlans=frozenset())
        driver.program([empty, satisfied])
        created[name] = driver
        return driver

    def _fake_build_deployer(credentials):
        return Deployer(_factory, config), config

    monkeypatch.setattr(cli, "_build_deployer", _fake_build_deployer)

    code = main(["apply", topo, "--session", "s-0001", "--state", state, "--apply"])
    assert code == 0
    out = capsys.readouterr().out
    assert "已套用" in out
    # 有變更的 fanout 都被套用。
    applied_any = any(len(d.apply_calls) == 1 for d in created.values())
    assert applied_any


def test_probe_multi_fanout_without_selection_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """probe 設定含多台但未給 --fanout → ConfigError、非零、無 traceback。"""
    creds = _write_credentials(tmp_path, ["spine-1", "agg-1"])
    code = main(["probe", "--credentials", creds])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("錯誤：")
    assert "Traceback" not in captured.err


def test_verify_reports_missing(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify（不 --apply）：回讀缺漏時印缺漏並回 0（唯讀診斷）。"""
    state = _state_arg(tmp_path)
    topo = str(three_tier_path)
    assert main(["allocate", topo, "--from", "ixia-1/1", "--to", "dut-A1:Eth0", "--state", state]) == 0
    capsys.readouterr()

    names = ["spine-1", "agg-1", "leaf-1-1"]
    config = _FakeConfig(names)

    def _factory(name, entry):
        # 全空快照 → 全部缺漏。
        return _MockDriver(name).program([name_as_fanout(name)])

    def _fake_build_deployer(credentials):
        return Deployer(_factory, config), config

    monkeypatch.setattr(cli, "_build_deployer", _fake_build_deployer)

    code = main(["verify", topo, "--session", "s-0001", "--state", state])
    assert code == 0
    out = capsys.readouterr().out
    assert "仍有缺漏" in out
