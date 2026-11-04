"""``interconnect`` 子命令群的 pytest 測試（互聯設計 §8、§10）。

涵蓋：全部子命令的 happy path 與錯誤路徑（exit 1、中文訊息、不冒 traceback）、
dry-run 預設不連線、``--apply`` 才寫、``--duts``／``--pair`` 與 ``--apply``／
``--ledger-only`` 的 argparse 互斥（exit 2）、``list --raw`` 不需拓樸，以及以
``FakeEcSwitch`` 注入 driver 工廠的端到端 allocate → apply → verify →
release --apply。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from ic_fakes import FakeEcSwitch, make_ic_data, write_topology
from topo_manager.cli import main
from topo_manager.drivers.ec_ssh_ic import EcSshInterconnectDriver
from topo_manager.interconnect import cli as ic_cli


class _Env:
    """測試環境：拓樸檔、帳本路徑、連線設定檔與注入的假設備。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.topo = str(write_topology(tmp_path, make_ic_data(1)))
        self.state = str(tmp_path / "interconnect.json")
        creds = tmp_path / "creds.json"
        creds.write_text(
            json.dumps({"fanouts": {"ic-1": {"host": "h", "username": "u", "password": "p"}}}),
            encoding="utf-8",
        )
        self.creds = str(creds)
        self.switch = FakeEcSwitch()
        self.factory_calls: List[str] = []

        def _make(force: bool):
            def _factory(name: str, entry: object) -> EcSshInterconnectDriver:
                self.factory_calls.append(name)
                return EcSshInterconnectDriver(
                    entry.host,
                    entry.username,
                    entry.password,
                    connection_factory=self.switch.factory,
                    force_access_mode=force,
                )

            return _factory

        monkeypatch.setattr(ic_cli, "_make_driver_factory", _make)

    def run(self, *args: str) -> int:
        return main(list(args))

    def sub(self, name: str, *extra: str) -> List[str]:
        """組出 ``interconnect <name> <topo> --state ...`` 的共用前綴。"""
        return ["interconnect", name, self.topo, "--state", self.state] + list(extra)

    def dev(self, name: str, *extra: str) -> List[str]:
        """同 sub，另加 --credentials。"""
        return self.sub(name, "--credentials", self.creds, *extra)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    monkeypatch.chdir(tmp_path)
    for key in ("HOST", "USERNAME", "PASSWORD", "DEVICE_TYPE", "INTERFACE_TEMPLATE"):
        monkeypatch.delenv("TOPO_FANOUT_" + key, raising=False)
    return _Env(tmp_path, monkeypatch)


def _allocate_mesh(env: _Env) -> None:
    assert main(env.sub("allocate", "--group", "g1", "--duts", "dut-g1-0,dut-g1-1", "--profile", "mesh")) == 0


# ------------------------------------------------------------------ summary / allocate / list


def test_summary_prints_groups(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(["interconnect", "summary", env.topo]) == 0
    out = capsys.readouterr().out
    assert "互聯 fanout 台數：1" in out and "ic-1：16/48" in out
    assert "g1（rack rack-1，fanout ic-1）：d0=dut-g1-0" in out


def test_allocate_profile_prints_links_and_persists(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    out = capsys.readouterr().out
    assert "ic-0001" in out and "d0:p0(Eth8)@ic-1:1 <-> d1:p0(Eth8)@ic-1:5  VLAN 100" in out
    assert main(env.sub("list")) == 0
    listed = capsys.readouterr().out
    assert "共 1 筆" in listed and "phase allocated" in listed


def test_allocate_explicit_pair_including_p3(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.sub("allocate", "--group", "g1", "--pair", "dut-g1-0:p3=dut-g1-1:p3")) == 0
    out = capsys.readouterr().out
    assert "profile：explicit" in out and "d0:p3(Eth11)@ic-1:4" in out


def test_list_empty_and_raw_without_topology(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.sub("list")) == 0
    assert "目前無 active 互聯 session" in capsys.readouterr().out
    _allocate_mesh(env)
    capsys.readouterr()
    assert main(["interconnect", "list", "--raw", "--state", env.state]) == 0
    raw = json.loads(capsys.readouterr().out)
    assert raw["kind"] == "interconnect" and raw["sessions"][0]["session_id"] == "ic-0001"


def test_list_without_topology_non_raw_is_config_error(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(["interconnect", "list", "--state", env.state]) == 1
    assert "錯誤：" in capsys.readouterr().err


def test_allocate_conflict_and_unknown_group_exit_1(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    capsys.readouterr()
    assert main(env.sub("allocate", "--group", "g1", "--duts", "dut-g1-0,dut-g1-1", "--profile", "line")) == 1
    assert "錯誤：" in capsys.readouterr().err
    assert main(env.sub("allocate", "--group", "nope", "--duts", "a,b", "--profile", "mesh")) == 1
    assert "錯誤：" in capsys.readouterr().err


def test_allocate_requires_profile_with_duts_and_rejects_profile_with_pair(
    env: _Env, capsys: pytest.CaptureFixture
) -> None:
    assert main(env.sub("allocate", "--group", "g1", "--duts", "dut-g1-0,dut-g1-1")) == 1
    assert "--profile" in capsys.readouterr().err
    assert main(env.sub("allocate", "--group", "g1", "--pair", "a:p0=b:p0", "--profile", "mesh")) == 1
    assert "不可與 --profile" in capsys.readouterr().err
    assert main(env.sub("allocate", "--group", "g1", "--pair", "bad-format")) == 1
    assert "格式錯誤" in capsys.readouterr().err


def test_duts_and_pair_are_mutually_exclusive(env: _Env) -> None:
    with pytest.raises(SystemExit) as info:
        main(env.sub("allocate", "--group", "g1", "--duts", "a,b", "--pair", "a:p0=b:p0"))
    assert info.value.code == 2


def test_bad_vlan_range_is_config_error(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.sub("list", "--vlan-min", "1", "--vlan-max", "10")) == 1
    assert "錯誤：" in capsys.readouterr().err


def test_topology_without_interconnect_section_exits_1(
    examples_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    assert main(["interconnect", "summary", str(examples_dir / "topology-2tier.yaml")]) == 1
    assert "interconnect" in capsys.readouterr().err


# ------------------------------------------------------------------ plan


def test_plan_modes_never_connect_or_write_ledger(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.sub("plan", "--group", "g1", "--duts", "dut-g1-0,dut-g1-1", "--profile", "mesh")) == 0
    out = capsys.readouterr().out
    assert "預覽" in out and "$ interface ethernet 1/1" in out and "$ no shutdown" in out
    assert not Path(env.state).exists()

    _allocate_mesh(env)
    capsys.readouterr()
    assert main(env.sub("plan", "--session", "ic-0001", "--release")) == 0
    assert "$ no vlan 100" in capsys.readouterr().out
    assert main(env.sub("plan", "--park", "--ports", "1,2")) == 0
    out = capsys.readouterr().out
    assert "interface ethernet 1/2" in out and "interface ethernet 1/1" not in out
    assert env.factory_calls == [] and env.switch.config_batches == []


def test_plan_requires_exactly_one_mode(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.sub("plan")) == 1
    assert "plan 請擇一" in capsys.readouterr().err
    assert main(env.sub("plan", "--park", "--session", "ic-0001")) == 1
    assert main(env.sub("plan", "--release")) == 1


def test_plan_unknown_session_exits_1(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.sub("plan", "--session", "ic-0009")) == 1
    assert "ic-0009" in capsys.readouterr().err


# ------------------------------------------------------------------ release


def test_release_dry_run_default_keeps_ledger(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    capsys.readouterr()
    assert main(env.sub("release", "--session", "ic-0001")) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "$ no vlan 100" in out
    assert main(env.sub("list")) == 0
    assert "共 1 筆" in capsys.readouterr().out
    assert env.factory_calls == []


def test_release_apply_and_ledger_only_are_exclusive(env: _Env) -> None:
    _allocate_mesh(env)
    with pytest.raises(SystemExit) as info:
        main(env.sub("release", "--session", "ic-0001", "--apply", "--ledger-only"))
    assert info.value.code == 2


def test_release_ledger_only_removes_without_connecting(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    capsys.readouterr()
    assert main(env.sub("release", "--session", "ic-0001", "--ledger-only")) == 0
    out = capsys.readouterr().out
    assert "設備未 park" in out
    assert main(env.sub("list")) == 0
    assert "目前無 active" in capsys.readouterr().out
    assert env.factory_calls == []


def test_release_unknown_session_exits_1(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.sub("release", "--session", "ic-0042")) == 1
    assert "錯誤：" in capsys.readouterr().err


# ------------------------------------------------------------------ apply / park / verify


def test_apply_dry_run_default_does_not_connect(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    capsys.readouterr()
    assert main(env.dev("apply", "--session", "ic-0001")) == 0
    assert "DRY-RUN" in capsys.readouterr().out
    assert env.factory_calls == [] and env.switch.config_batches == []


def test_apply_without_credentials_exits_1(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    capsys.readouterr()
    assert main(env.sub("apply", "--session", "ic-0001", "--apply")) == 1
    assert "錯誤：" in capsys.readouterr().err
    assert env.factory_calls == []


def test_verify_without_credentials_exits_1(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    capsys.readouterr()
    assert main(env.sub("verify", "--session", "ic-0001")) == 1
    assert "錯誤：" in capsys.readouterr().err


def test_verify_requires_target(env: _Env) -> None:
    with pytest.raises(SystemExit) as info:
        main(env.sub("verify"))
    assert info.value.code == 2


def test_park_dry_run_then_apply_with_setup_alias(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.dev("park", "--ports", "1,2")) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "$ shutdown" in out
    assert env.factory_calls == []

    assert main(env.dev("setup", "--ports", "1,2", "--apply")) == 0
    out = capsys.readouterr().out
    assert "已 park：ic-1 port 1, 2" in out
    assert not env.switch.ports[1]["enabled"] and not env.switch.ports[2]["enabled"]
    assert env.switch.ports[3]["enabled"]


def test_park_bad_ports_exits_1(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.dev("park", "--ports", "a,b")) == 1
    assert "--ports" in capsys.readouterr().err


def test_end_to_end_allocate_apply_verify_release(env: _Env, capsys: pytest.CaptureFixture) -> None:
    initial_parked = {n: dict(p) for n, p in env.switch.ports.items()}
    assert main(env.dev("park", "--apply")) == 0
    assert main(env.dev("verify", "--parked")) == 0
    _allocate_mesh(env)
    capsys.readouterr()

    assert main(env.dev("apply", "--session", "ic-0001", "--apply")) == 0
    assert "回讀驗證" in capsys.readouterr().out
    for port in (1, 5):
        state = env.switch.ports[port]
        assert state["enabled"] and state["native"] == 100 and state["allowed"] == {100: "u"}
    assert main(env.dev("verify", "--session", "ic-0001")) == 0
    assert "相符" in capsys.readouterr().out

    assert main(env.dev("release", "--session", "ic-0001", "--apply")) == 0
    assert "已釋放" in capsys.readouterr().out
    assert main(env.sub("list")) == 0
    assert "目前無 active" in capsys.readouterr().out
    assert main(env.dev("verify", "--parked")) == 0
    for port in (1, 5):
        state = env.switch.ports[port]
        assert not state["enabled"] and state["native"] == 1 and state["allowed"] == {1: "u"}
    assert 100 not in env.switch.defined
    assert initial_parked[3]["allowed"] == env.switch.ports[3]["allowed"]
    assert env.switch.disconnect_count == env.switch.connect_count


def test_verify_with_problems_exits_1(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    assert main(env.dev("apply", "--session", "ic-0001", "--apply")) == 0
    capsys.readouterr()
    env.switch.ports[1]["enabled"] = False
    assert main(env.dev("verify", "--session", "ic-0001")) == 1
    out = capsys.readouterr().out
    assert "發現" in out and "Port Admin" in out


def test_verify_parked_detects_unparked_ports(env: _Env, capsys: pytest.CaptureFixture) -> None:
    assert main(env.dev("verify", "--parked")) == 1
    assert "Port Admin 為 Up" in capsys.readouterr().out


def test_apply_error_from_device_exits_1_without_traceback(env: _Env, capsys: pytest.CaptureFixture) -> None:
    _allocate_mesh(env)
    capsys.readouterr()
    env.switch.silent_drop = {"switchport allowed vlan remove 1"}
    assert main(env.dev("apply", "--session", "ic-0001", "--apply")) == 1
    err = capsys.readouterr().err
    assert err.startswith("錯誤：") and "Traceback" not in err
    assert "no shutdown" not in env.switch.commands
