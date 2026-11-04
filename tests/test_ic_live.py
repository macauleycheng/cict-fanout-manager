"""互聯 fanout 真機 live 測試（tests/test_ic_live.py，互聯設計 §10.1、FEAT-004）。

本檔所有測試皆標 ``@pytest.mark.live``：預設因 pyproject 的
``addopts = "-m 'not live'"`` 不被收集；即使以 ``-m live`` 明確收集，缺少連線
env 時亦一律 ``pytest.skip``（絕不 fail、絕不 error）。真機位址/帳密僅由環境
變數提供，永不自任何版控檔讀取（憑證只讀 env，用記憶體內 ``FanoutConfig``，
不寫入任何檔）。

連線 env（缺任一即整檔 skip）：
  - ``TOPO_FANOUT_HOST``：互聯 fanout 可達位址。
  - ``TOPO_FANOUT_USERNAME`` / ``TOPO_FANOUT_PASSWORD``：登入帳密。

安全閥（設計 §10.1，寫在測試開頭且為 hard assert）：
  - 只允許 VLAN 3999 與 Eth1/42、Eth1/43（硬編在本檔，不讀設定、不可被參數
    覆寫）。
  - 寫入前讀兩 port 的 ``Link Status`` 必須為 Down（且非管理路徑，即
    ``Port Admin`` 為 Up）。
  - 記錄測前快照（兩 port 的 ``show running-config interface``、
    ``show interfaces switchport``、``Port Admin``；VLAN 3999 不存在）。
  - 整段 ``try/finally``：finally 以 ``release``（park + 清 VLAN，僅限 3999）
    還原。
  - 結束後開新 SSH 連線重新讀取，與測前快照逐項比對，並確認 ``show vlan``
    無 3999。
  - 不碰 Eth1/1、Eth1/11、Eth1/13 與 VLAN 1/2/3/4/10/20/4093。

情境 A（驗收全流程，互聯設計 §10.1）：tmp 拓樸 1 台互聯 fanout ``live-ic``、
1 組 4 DUT，僅 d0:p0→port 42、d1:p0→port 43 參與（其餘 link 接在不會被下發
的 port 號）；``InterconnectAllocator``（``vlan_min=vlan_max=3999``）allocate
``{d0,d1}`` mesh → ``InterconnectDeployer(force_access_mode=False)``
``apply(apply=True)`` → 讀回確認 → ``release(apply=True)`` 回 parking。對
Eth1/43（hybrid 帶 4093 tagged）不下 ``switchport mode access``，避免動到
池外設定。

情境 B（補驗 ``switchport mode access`` 序列）：對 Eth1/42 單 port（本來就是
access）以 ``force_access_mode=True`` 跑同一組 phase1/phase2 與 park，確認
``Mode`` 仍為 Access、序列無錯誤輸出。

歷史（HISTORY）：
  - 0.1.0  FEAT-004 初版：情境 A/B，live marker，缺 env 一律 skip。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

import pytest

from ic_fakes import write_topology
from topo_manager.drivers.config import FanoutConfig, FanoutEntry
from topo_manager.drivers.ec_ssh_ic import (
    EcSshInterconnectDriver,
    build_park_commands,
    build_phase1_commands,
    build_phase2_commands,
)
from topo_manager.drivers.ic_deployer import InterconnectDeployer
from topo_manager.interconnect.allocator import InterconnectAllocator
from topo_manager.interconnect.store import IcSessionStore
from topo_manager.loader import load_topology

pytestmark = pytest.mark.live

# 安全閥：本檔唯一允許觸碰的 VLAN 與 port（硬編、不可覆寫）。
_ALLOWED_VLAN = 3999
_ALLOWED_PORTS = (42, 43)
_FANOUT_NAME = "live-ic"
_GROUP_NAME = "g1"


def _env_credentials() -> Optional[Dict[str, str]]:
    """讀連線 env；缺任一回 None（供整檔 skip 判斷）。憑證只讀 env，不落檔。"""
    host = os.environ.get("TOPO_FANOUT_HOST")
    username = os.environ.get("TOPO_FANOUT_USERNAME")
    password = os.environ.get("TOPO_FANOUT_PASSWORD")
    if not host or not username or not password:
        return None
    return {"host": host, "username": username, "password": password}


def _make_driver(creds: Dict[str, str], force_access_mode: bool) -> EcSshInterconnectDriver:
    """以 env 憑證建一台 ``EcSshInterconnectDriver``（憑證僅存於記憶體）。"""
    return EcSshInterconnectDriver(
        creds["host"],
        creds["username"],
        creds["password"],
        force_access_mode=force_access_mode,
    )


def _make_topology(tmp_path: Path):
    """tmp 拓樸：1 台互聯 fanout ``live-ic``、1 組 4 DUT；僅 d0/d1 的 p0 接 42/43。

    其餘 link 接在 20..33，只存在於 tmp 拓樸；本檔下發的指令只以 session 的
    42/43 為範圍（不經整機 park）。
    """
    duts = []
    links = []
    il_ports = ["Eth8", "Eth9", "Eth10", "Eth11"]
    # d0 的 p0 → port 42，d1 的 p0 → port 43；其餘 p1..p3 與 d2/d3 的全部 port
    # 接在 50 以上、本檔絕不觸碰的號碼。
    port_map = {
        ("dut-g1-0", "Eth8"): 42,
        ("dut-g1-1", "Eth8"): 43,
    }
    # 互聯 fanout 僅 48 port；其餘 14 條 link 接在 20..33（僅出現在記憶體內的
    # tmp 拓樸，本檔絕不對其下發指令；安全閥只允許 42/43）。
    next_spare = 20
    for i in range(4):
        name = "dut-g1-{0}".format(i)
        duts.append(
            {
                "name": name,
                "model": "modelA",
                "tg_ports": ["Eth0", "Eth1"],
                "il_ports": list(il_ports),
                "slot": i + 1,
                "group": _GROUP_NAME,
            }
        )
        for il_port in il_ports:
            key = (name, il_port)
            if key in port_map:
                fanout_port = port_map[key]
            else:
                fanout_port = next_spare
                next_spare += 1
            links.append(
                {
                    "a": {"dev": name, "port": il_port},
                    "b": {"dev": _FANOUT_NAME, "port": fanout_port},
                }
            )
    data = {
        "version": 1,
        "site": "ic-live-test",
        "generators": [{"name": "ixia-1", "type": "ixia", "ports": [1, 2]}],
        "fanouts": [{"name": "leaf-1", "role": "leaf", "port_count": 32}],
        "racks": [{"name": "rack-1", "duts": duts}],
        "links": [
            {"a": {"dev": "ixia-1", "port": 1}, "b": {"dev": "leaf-1", "port": 1}},
            {"a": {"dev": "leaf-1", "port": 11}, "b": {"dev": duts[0]["name"], "port": "Eth0"}},
        ],
        "interconnect": {
            "fanouts": [{"name": _FANOUT_NAME, "port_count": 48, "rack": "rack-1"}],
            "links": links,
        },
    }
    return load_topology(write_topology(tmp_path, data))


def _snapshot_port(driver: EcSshInterconnectDriver, port: int) -> Dict[str, object]:
    """單一 port 的快照：status 與 switchport 讀回結果。"""
    return {
        "status": driver.read_port_status(port),
        "switchport": driver.read_port_switchport(port),
    }


def _assert_vlan_absent(driver: EcSshInterconnectDriver) -> None:
    """斷言 VLAN 3999 不存在（``show vlan id 3999``）。"""
    assert driver.read_vlan_members(_ALLOWED_VLAN) is None


def _emit(driver: EcSshInterconnectDriver, label: str) -> None:
    """以 ``pytest -s`` 印出兩 port 與 VLAN 3999 的原始 show 輸出（不含憑證）。"""
    print("\n=== {0} ===".format(label))
    for port in _ALLOWED_PORTS:
        for command in (
            "show running-config interface ethernet 1/{0}".format(port),
            "show interfaces switchport ethernet 1/{0}".format(port),
            "show interfaces status ethernet 1/{0}".format(port),
        ):
            print("$ {0}".format(command))
            print(driver.read_show(command))
    print("$ show vlan id {0}".format(_ALLOWED_VLAN))
    print(driver.read_show("show vlan id {0}".format(_ALLOWED_VLAN)))


def _print_commands(title: str, batches: object) -> None:
    """印出一組設定批次（供 pytest -s 留存實際下發的指令）。"""
    print("\n--- {0} ---".format(title))
    for batch in batches:
        for command in batch:
            print("  " + command)
        print("  --- (batch end) ---")


@pytest.fixture
def live_credentials() -> Dict[str, str]:
    """缺任一連線 env 即 skip（絕不 fail）。"""
    creds = _env_credentials()
    if creds is None:
        pytest.skip(
            "缺 TOPO_FANOUT_HOST/USERNAME/PASSWORD，略過互聯 live 測試"
        )
    return creds


def _safety_precheck(driver: EcSshInterconnectDriver) -> Dict[int, Dict[str, object]]:
    """寫入前的安全閥：兩 port 須 Link Down 且非管理路徑；VLAN 3999 須不存在。

    RETURN：
      - ``{port: 測前快照}``，供 finally 後的獨立 SSH 複驗比對。
    """
    _assert_vlan_absent(driver)
    before: Dict[int, Dict[str, object]] = {}
    for port in _ALLOWED_PORTS:
        status = driver.read_port_status(port)
        assert status.enabled is True, "Eth1/{0} 的 Port Admin 必須是 Up（非管理路徑關閉）".format(port)
        assert status.link_up is False, "Eth1/{0} 的 Link Status 必須是 Down 才可寫入".format(port)
        before[port] = _snapshot_port(driver, port)
    return before


@pytest.mark.live
def test_scenario_a_full_lifecycle_hybrid_port_preserves_4093(
    tmp_path: Path, live_credentials: Dict[str, str]
) -> None:
    """情境 A：{d0,d1} mesh 全流程（apply → verify → release），43 不下 mode access。"""
    topology = _make_topology(tmp_path)
    store = IcSessionStore(tmp_path / "interconnect.json")
    allocator = InterconnectAllocator(
        topology, store, vlan_min=_ALLOWED_VLAN, vlan_max=_ALLOWED_VLAN
    )
    config = FanoutConfig(
        fanouts={
            _FANOUT_NAME: FanoutEntry(
                host=live_credentials["host"],
                username=live_credentials["username"],
                password=live_credentials["password"],
            )
        }
    )

    def _factory(name: str, entry: object) -> EcSshInterconnectDriver:
        assert name == _FANOUT_NAME
        return _make_driver(live_credentials, force_access_mode=False)

    # 池範圍必須與 allocator 一致（3999..3999）；否則 3999 會被視為池外 VLAN，
    # 讀回與 park／清 VLAN 都不會處理它。
    deployer = InterconnectDeployer(
        _factory,
        config,
        store=store,
        allocator=allocator,
        vlan_min=_ALLOWED_VLAN,
        vlan_max=_ALLOWED_VLAN,
        force_access_mode=False,
    )

    precheck_driver = _make_driver(live_credentials, force_access_mode=False)
    precheck_driver.connect()
    try:
        before = _safety_precheck(precheck_driver)
        _emit(precheck_driver, "A: 測前狀態")
    finally:
        precheck_driver.disconnect()

    session = allocator.allocate_profile(_GROUP_NAME, ["dut-g1-0", "dut-g1-1"], "mesh")
    assert sorted({link.vlan for link in session.links}) == [_ALLOWED_VLAN]

    try:
        _print_commands("A: apply 計畫", deployer.plan_enable(session).fanouts[0].batches)
        result = deployer.apply(session, apply=True)
        print("A: apply notes: {0}".format(result.notes))
        assert "no shutdown" in result.fanouts[0].commands
        verify = deployer.verify_session(session)
        assert verify.ok, verify.problems

        confirm_driver = _make_driver(live_credentials, force_access_mode=False)
        confirm_driver.connect()
        try:
            _emit(confirm_driver, "A: apply 後（啟用）")
            for port in _ALLOWED_PORTS:
                switchport = confirm_driver.read_port_switchport(port)
                assert switchport.native_vlan == _ALLOWED_VLAN
                assert switchport.allowed.get(_ALLOWED_VLAN) == "u"
                assert 1 not in switchport.allowed
                status = confirm_driver.read_port_status(port)
                assert status.enabled is True
            members = confirm_driver.read_vlan_members(_ALLOWED_VLAN)
            assert members == frozenset({"Eth1/42", "Eth1/43"})
            assert confirm_driver.read_port_switchport(43).allowed.get(4093) == "t"
        finally:
            confirm_driver.disconnect()
    finally:
        _print_commands("A: release 計畫", deployer.plan_release(session).fanouts[0].batches)
        deployer.release(session, apply=True)

    # release 後的 parking 狀態：Port Admin Down、switchport 與測前相同、3999 已刪。
    # 之後由測試自己把管理面還原為測前的 Up（park 的語意是 shutdown）。
    parked_driver = _make_driver(live_credentials, force_access_mode=False)
    parked_driver.connect()
    try:
        _emit(parked_driver, "A: release 後（parking）")
        _assert_vlan_absent(parked_driver)
        for port in _ALLOWED_PORTS:
            parked = _snapshot_port(parked_driver, port)
            assert parked["status"].enabled is False
            assert parked["switchport"] == before[port]["switchport"]
        parked_driver.enable_phase2(list(_ALLOWED_PORTS))
    finally:
        parked_driver.disconnect()

    # 結束後開新 SSH 連線重新讀取，與測前快照逐項比對。
    recheck_driver = _make_driver(live_credentials, force_access_mode=False)
    recheck_driver.connect()
    try:
        _emit(recheck_driver, "A: 獨立新 SSH 複驗")
        _assert_vlan_absent(recheck_driver)
        for port in _ALLOWED_PORTS:
            after = _snapshot_port(recheck_driver, port)
            assert after["status"] == before[port]["status"]
            assert after["switchport"] == before[port]["switchport"]
    finally:
        recheck_driver.disconnect()


@pytest.mark.live
def test_scenario_b_force_access_mode_sequence_on_access_port(
    tmp_path: Path, live_credentials: Dict[str, str]
) -> None:
    """情境 B：對 Eth1/42（本來就是 access）以 force_access_mode=True 跑同序列。"""
    driver = _make_driver(live_credentials, force_access_mode=True)
    driver.connect()
    port = 42
    try:
        before = _safety_precheck(driver)
        _emit(driver, "B: 測前狀態")
        _print_commands("B: phase 1", [build_phase1_commands([(port, _ALLOWED_VLAN)], True)])
        driver.enable_phase1([(port, _ALLOWED_VLAN)])
        _emit(driver, "B: phase 1 後（port 仍 shutdown）")
        switchport = driver.read_port_switchport(port)
        assert switchport.mode.lower() == "access"
        assert switchport.native_vlan == _ALLOWED_VLAN
        assert switchport.allowed.get(_ALLOWED_VLAN) == "u"
        assert 1 not in switchport.allowed
        status = driver.read_port_status(port)
        assert status.enabled is False  # phase 1 後仍 shutdown

        _print_commands("B: phase 2", [build_phase2_commands([port])])
        driver.enable_phase2([port])
        status = driver.read_port_status(port)
        assert status.enabled is True
        _emit(driver, "B: phase 2 後（啟用）")
    finally:
        try:
            switchport = driver.read_port_switchport(port)
            stray = [_ALLOWED_VLAN] if _ALLOWED_VLAN in switchport.allowed else []
            clean = not stray and switchport.native_vlan == 1
            _print_commands("B: park", [build_park_commands(port, stray, clean)])
            driver.park_ports([(port, stray, clean)])
            driver.clear_vlan_definitions([_ALLOWED_VLAN], _ALLOWED_VLAN, _ALLOWED_VLAN)
            # park 把 port 還原為 shutdown；測前快照是 Port Admin Up（非管理路徑
            # 關閉），故 park 後需 no shutdown 還原到測前的管理面狀態。
            driver.enable_phase2([port])
        finally:
            driver.disconnect()

    recheck_driver = _make_driver(live_credentials, force_access_mode=True)
    recheck_driver.connect()
    try:
        _emit(recheck_driver, "B: 獨立新 SSH 複驗")
        _assert_vlan_absent(recheck_driver)
        after = _snapshot_port(recheck_driver, port)
        assert after["status"] == before[port]["status"]
        assert after["switchport"] == before[port]["switchport"]
    finally:
        recheck_driver.disconnect()
