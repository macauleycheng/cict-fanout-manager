"""互聯 REST 寫入端點整合測試（主執行緒 live-server 子行程，真 HTTP）。

互聯 allocator 的寫入以 ``store.locked()`` 的 SIGALRM 取鎖，只能在主執行緒；
本檔沿用既有 live-server 子行程寫法（見 conftest.py），涵蓋建立（duts+profile
與明確 port 對）、列出、釋放、衝突 409、VLAN 池耗盡 409、帳本故障 503、釋放後
port 重用、與打流側共存，以及 API 與 CLI 共用同一互聯帳本的跨行程一致性。
全程不連任何設備（只用 127.0.0.1 與暫存目錄）。

歷史（HISTORY）：
  - 0.1.0  互聯 REST 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List

from topo_manager.interconnect.allocator import InterconnectAllocator
from topo_manager.interconnect.store import IcSessionStore
from topo_manager.loader import load_topology

_SESSIONS = "/interconnect/sessions"


def _mesh(duts: List[str], group: str = "A-g1", profile: str = "mesh") -> dict:
    return {"group": group, "duts": duts, "profile": profile}


def _pair_body(a: str, b: str, group: str = "A-g1") -> dict:
    return {"group": group, "pairs": [{"a": a, "b": b}]}


def _cli(server, *args: str) -> subprocess.CompletedProcess:
    """以子行程跑 ``topo-manager interconnect ...``，指向 live-server 的互聯帳本。"""
    return subprocess.run(
        [sys.executable, "-m", "topo_manager", "interconnect", *args],
        capture_output=True,
        text=True,
    )


def _cli_with_state(server, command: str, *extra: str) -> subprocess.CompletedProcess:
    return _cli(server, command, str(server.topology_path), *extra, "--state", str(server.ic_state_path))


def _ledger(server) -> dict:
    return json.loads(Path(server.ic_state_path).read_text(encoding="utf-8"))


# ----------------------------------------------------------------------
# 建立 / 列出 / 釋放
# ----------------------------------------------------------------------
def test_create_profile_ring(ic_live_server) -> None:
    client = ic_live_server.client
    response = client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2", "dut-A3", "dut-A4"], profile="ring"))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["session_id"] == "ic-0001"
    assert body["status"] == "allocated"
    assert body["phase"] == "allocated"
    assert body["profile"] == "ring"
    assert body["fanout"] == "ic-1-1"
    assert len(body["links"]) == 4
    vlans = [link["vlan"] for link in body["links"]]
    assert len(set(vlans)) == 4
    assert all(100 <= vlan <= 3000 for vlan in vlans)
    assert all(link["kind"] == "paired" for link in body["links"])
    first = body["links"][0]
    assert first["a_dut"] == "dut-A1" and first["b_dut"] == "dut-A2"
    assert first["a_node"].startswith("ic-1-1:")
    assert len(_ledger(ic_live_server)["sessions"]) == 1


def test_create_explicit_pairs(ic_live_server) -> None:
    response = ic_live_server.client.post(_SESSIONS, json=_pair_body("dut-A1:p3", "dut-A2:Eth11"))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["profile"] == "explicit"
    assert body["duts"] == ["dut-A1", "dut-A2"]
    assert body["links"][0]["kind"] == "explicit"
    assert body["links"][0]["a_port"] == "Eth11"
    assert body["links"][0]["b_port"] == "Eth11"


def test_list_and_available_reflect_sessions(ic_live_server) -> None:
    client = ic_live_server.client
    client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"]))
    client.post(_SESSIONS, json=_mesh(["dut-A3", "dut-A4"]))
    listing = client.get(_SESSIONS).json()
    assert listing["count"] == 2
    assert [item["session_id"] for item in listing["sessions"]] == ["ic-0001", "ic-0002"]
    assert all(item["status"] == "active" for item in listing["sessions"])
    available = client.get("/interconnect/resources/available").json()
    group = next(item for item in available["groups"] if item["name"] == "A-g1")
    assert len(group["buildable_links"]) == 4
    dut_a1 = next(item for item in group["duts"] if item["name"] == "dut-A1")
    assert dut_a1["ports"][0]["occupied_by"] == "ic-0001"
    assert dut_a1["ports"][1]["occupied_by"] is None


def test_two_non_overlapping_sessions_coexist(ic_live_server) -> None:
    client = ic_live_server.client
    first = client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"]))
    second = client.post(_SESSIONS, json=_mesh(["dut-A3", "dut-A4"]))
    assert first.status_code == 201 and second.status_code == 201
    first_vlans = {link["vlan"] for link in first.json()["links"]}
    second_vlans = {link["vlan"] for link in second.json()["links"]}
    assert first_vlans.isdisjoint(second_vlans)
    assert client.get(_SESSIONS).json()["count"] == 2


def test_delete_returns_warning_and_frees_ports(ic_live_server) -> None:
    client = ic_live_server.client
    created = client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"])).json()
    response = client.delete("{0}/{1}".format(_SESSIONS, created["session_id"]))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "released"
    assert body["warning"]
    assert client.get(_SESSIONS).json()["count"] == 0
    again = client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"]))
    assert again.status_code == 201, again.text
    assert again.json()["session_id"] == "ic-0002"


def test_delete_unknown_is_404(ic_live_server) -> None:
    response = ic_live_server.client.delete(_SESSIONS + "/ic-0042")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "InterconnectSessionNotFoundError"


def test_delete_releasing_session_is_ledger_only(ic_live_server, ic_small_path: Path) -> None:
    client = ic_live_server.client
    created = client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"])).json()
    allocator = InterconnectAllocator(
        load_topology(str(ic_small_path)), IcSessionStore(ic_live_server.ic_state_path)
    )
    allocator.begin_release(created["session_id"])
    assert client.get(_SESSIONS).json()["sessions"][0]["phase"] == "releasing"
    response = client.delete("{0}/{1}".format(_SESSIONS, created["session_id"]))
    assert response.status_code == 200
    assert client.get(_SESSIONS).json()["count"] == 0


# ----------------------------------------------------------------------
# 衝突 / 失敗無副作用
# ----------------------------------------------------------------------
def test_ring_conflicts_with_other_session_409(ic_live_server) -> None:
    client = ic_live_server.client
    assert client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"])).status_code == 201
    assert client.post(_SESSIONS, json=_mesh(["dut-A3", "dut-A4"])).status_code == 201
    before = _ledger(ic_live_server)
    response = client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2", "dut-A3", "dut-A4"], profile="ring"))
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["type"] == "InterconnectConflictError"
    assert "ic-0001" in error["message"]
    assert _ledger(ic_live_server) == before


def test_explicit_pair_conflicts_with_profile_session(ic_live_server) -> None:
    client = ic_live_server.client
    assert client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"])).status_code == 201
    response = client.post(_SESSIONS, json=_pair_body("dut-A1:p0", "dut-A3:p0"))
    assert response.status_code == 409


def test_groups_are_independent(ic_live_server) -> None:
    client = ic_live_server.client
    assert client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"])).status_code == 201
    other = client.post(_SESSIONS, json=_mesh(["dut-A5", "dut-A6"], group="A-g2"))
    assert other.status_code == 201
    assert other.json()["fanout"] == "ic-1-1"


def test_invalid_request_over_http_is_400_and_no_ledger(ic_live_server) -> None:
    response = ic_live_server.client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"], profile="ring"))
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "InterconnectRequestError"
    assert not Path(ic_live_server.ic_state_path).exists()


def test_vlan_pool_exhausted_409(ic_small_path: Path, tmp_path: Path) -> None:
    from conftest import _start_ic_live_server, _stop_live_server

    proc, server = _start_ic_live_server(
        ic_small_path,
        tmp_path,
        extra_env={"TOPO_MANAGER_IC_VLAN_MIN": "100", "TOPO_MANAGER_IC_VLAN_MAX": "100"},
    )
    try:
        response = server.client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2", "dut-A3"]))
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "VlanPoolExhaustedError"
        single = server.client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"]))
        assert single.status_code == 201
        assert single.json()["links"][0]["vlan"] == 100
    finally:
        _stop_live_server(proc, server)


def test_ledger_io_failure_on_write_503(ic_small_path: Path, tmp_path: Path) -> None:
    """互聯帳本的父路徑是一般檔案（無法建目錄）：寫入端點回 503，且伺服器仍存活。"""
    from conftest import _start_ic_live_server, _stop_live_server

    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    proc, server = _start_ic_live_server(ic_small_path, tmp_path, ic_state=blocker / "ic.json")
    try:
        response = server.client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"]))
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "SessionStoreError"
        assert "Traceback" not in response.text
        assert server.client.delete(_SESSIONS + "/ic-0001").status_code in (404, 503)
        assert server.client.get("/topology").status_code == 200
    finally:
        _stop_live_server(proc, server)


# ----------------------------------------------------------------------
# 拓樸無互聯區段（真 server）
# ----------------------------------------------------------------------
def test_no_section_live_server(ic_no_section_live_server) -> None:
    client = ic_no_section_live_server.client
    assert client.get("/topology").status_code == 200
    assert client.get("/sessions").status_code == 200
    for response in (
        client.get("/interconnect/summary"),
        client.post(_SESSIONS, json=_mesh(["a", "b"])),
        client.delete(_SESSIONS + "/ic-0001"),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "InterconnectNotConfiguredError"


# ----------------------------------------------------------------------
# 與打流側共存
# ----------------------------------------------------------------------
def test_coexists_with_traffic_side(ic_live_server) -> None:
    client = ic_live_server.client
    traffic = client.post("/sessions", json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"})
    assert traffic.status_code == 201, traffic.text
    interconnect = client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"]))
    assert interconnect.status_code == 201
    traffic_ledger = json.loads(ic_live_server.state_path.read_text(encoding="utf-8"))
    ic_ledger = _ledger(ic_live_server)
    assert traffic_ledger["version"] == 2 and len(traffic_ledger["sessions"]) == 1
    assert ic_ledger["kind"] == "interconnect" and len(ic_ledger["sessions"]) == 1
    assert client.get("/sessions").json()["count"] == 1
    assert client.get(_SESSIONS).json()["count"] == 1


# ----------------------------------------------------------------------
# API 與 CLI 共用同一互聯帳本
# ----------------------------------------------------------------------
def test_api_create_visible_to_cli_list(ic_live_server) -> None:
    created = ic_live_server.client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"])).json()
    result = _cli_with_state(ic_live_server, "list")
    assert result.returncode == 0, result.stderr
    assert created["session_id"] in result.stdout
    assert "dut-A1" in result.stdout


def test_cli_create_visible_to_api_list(ic_live_server) -> None:
    result = _cli_with_state(
        ic_live_server, "allocate", "--group", "A-g1", "--duts", "dut-A1,dut-A2", "--profile", "mesh"
    )
    assert result.returncode == 0, result.stderr
    listing = ic_live_server.client.get(_SESSIONS).json()
    assert [item["session_id"] for item in listing["sessions"]] == ["ic-0001"]
    conflict = ic_live_server.client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"]))
    assert conflict.status_code == 409


def test_cli_ledger_only_release_then_api_recreates(ic_live_server) -> None:
    client = ic_live_server.client
    created = client.post(_SESSIONS, json=_pair_body("dut-A1:p3", "dut-A2:p3")).json()
    result = _cli_with_state(ic_live_server, "release", "--session", created["session_id"], "--ledger-only")
    assert result.returncode == 0, result.stderr
    assert client.post(_SESSIONS, json=_pair_body("dut-A1:p3", "dut-A2:p3")).status_code == 201


def test_api_delete_then_cli_list_empty(ic_live_server) -> None:
    client = ic_live_server.client
    created = client.post(_SESSIONS, json=_mesh(["dut-A1", "dut-A2"])).json()
    assert client.delete("{0}/{1}".format(_SESSIONS, created["session_id"])).status_code == 200
    result = _cli_with_state(ic_live_server, "list")
    assert result.returncode == 0
    assert "無 active" in result.stdout


def test_interleaved_api_and_cli_writes_are_consistent(ic_live_server) -> None:
    """API 與 CLI 輪流在同組建立不重疊的 session，最終 session_id / VLAN / port 皆無重複。"""
    client = ic_live_server.client
    plan = []
    for index in range(4):
        plan.append(("dut-A1:p{0}".format(index), "dut-A2:p{0}".format(index)))
        plan.append(("dut-A3:p{0}".format(index), "dut-A4:p{0}".format(index)))
    for position, (left, right) in enumerate(plan):
        if position % 2 == 0:
            response = client.post(_SESSIONS, json=_pair_body(left, right))
            assert response.status_code == 201, response.text
        else:
            result = _cli_with_state(
                ic_live_server, "allocate", "--group", "A-g1", "--pair", "{0}={1}".format(left, right)
            )
            assert result.returncode == 0, result.stdout + result.stderr
    sessions = _ledger(ic_live_server)["sessions"]
    assert len(sessions) == 8
    assert len({item["session_id"] for item in sessions}) == 8
    vlans = [link["vlan"] for item in sessions for link in item["links"]]
    nodes = [node for item in sessions for link in item["links"] for node in (link["a_node"], link["b_node"])]
    assert len(vlans) == len(set(vlans)) == 8
    assert len(nodes) == len(set(nodes)) == 16
    assert client.get(_SESSIONS).json()["count"] == 8
