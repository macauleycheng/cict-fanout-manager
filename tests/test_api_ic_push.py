"""互聯側設備下發 REST 整合測試（tests/test_api_ic_push.py）。

以主執行緒 live-server 子行程啟動啟用設備下發的 app（driver 由
``ic_fakes.FakeSwitchFarm`` 注入的 ``FakeEcSwitch`` 假設備，**絕不連真實設備**）。
apply／release-device／park 內部的 ``device_locked()`` 使用 ``SIGALRM``，只能在主
執行緒執行；這些測試在真 server 下成功，即證明 handler 沒有被丟進 threadpool。

歷史（HISTORY）：
  - 0.1.0  設備下發 REST 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

_IC_FANOUTS = ["core-1", "leaf-1", "leaf-2", "ic-1-1", "ic-2-1"]
_TRAFFIC_FANOUTS = ["spine-1", "agg-1", "agg-2", "leaf-1-1", "leaf-2-1"]


def _headers(server) -> dict:
    """帶正確 token 的 header。"""
    return {"X-API-Token": server.token}


def _create(server, duts: List[str]) -> dict:
    """先 POST 建互聯 session（只動帳本），回傳其 JSON。"""
    response = server.client.post(
        "/interconnect/sessions", json={"group": "A-g1", "duts": duts, "profile": "mesh"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _url(session: dict, tail: str) -> str:
    return "/interconnect/sessions/{0}/{1}".format(session["session_id"], tail)


def test_apply_success_in_main_thread_lists_fanouts(ic_push_live_server) -> None:
    """apply 成功 200：逐 fanout 結果含 ports 與 read-back；證明 SIGALRM 鎖在主執行緒可用。"""
    server = ic_push_live_server
    session = _create(server, ["dut-A1", "dut-A2"])
    response = server.client.post(_url(session, "apply"), headers=_headers(server))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == session["session_id"]
    assert body["action"] == "apply"
    assert body["vlans"]
    assert body["fanouts"]
    for item in body["fanouts"]:
        assert item["ok"] is True
        assert item["commands"]
        assert item["ports"]
        assert item["readback"] == {
            "verified": True,
            "checked": len(item["ports"]),
            "missing": [],
        }
    assert "signal only works" not in response.text


def test_verify_ok_after_apply_and_problems_before(ic_push_live_server) -> None:
    """verify：apply 前 ok=false（HTTP 200、flat problems）；apply 後 ok=true。"""
    server = ic_push_live_server
    session = _create(server, ["dut-A1", "dut-A2"])
    before = server.client.get(_url(session, "verify"), headers=_headers(server))
    assert before.status_code == 200
    assert before.json()["ok"] is False
    assert before.json()["problems"]
    assert all(isinstance(p, str) for p in before.json()["problems"])
    assert before.json()["fanouts"]

    server.client.post(_url(session, "apply"), headers=_headers(server))
    after = server.client.get(_url(session, "verify"), headers=_headers(server))
    assert after.status_code == 200
    assert after.json() == {
        "target": session["session_id"],
        "ok": True,
        "fanouts": before.json()["fanouts"],
        "problems": [],
    }


def test_release_device_then_session_is_gone(ic_push_live_server) -> None:
    """release-device：連設備釋放成功並自帳本移除；既有 DELETE 仍為 ledger-only。"""
    server = ic_push_live_server
    session = _create(server, ["dut-A1", "dut-A2"])
    server.client.post(_url(session, "apply"), headers=_headers(server))
    response = server.client.post(_url(session, "release-device"), headers=_headers(server))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"] == "release"
    assert all(item["ok"] for item in body["fanouts"])
    again = server.client.post(_url(session, "apply"), headers=_headers(server))
    assert again.status_code == 404
    assert again.json()["error"]["type"] == "InterconnectSessionNotFoundError"
    assert server.client.get("/interconnect/sessions").json()["count"] == 0


def test_existing_delete_stays_ledger_only(ic_push_live_server) -> None:
    """既有 DELETE 不需 token、不連設備，仍帶 ledger-only 警告。"""
    server = ic_push_live_server
    session = _create(server, ["dut-A1", "dut-A2"])
    response = server.client.delete("/interconnect/sessions/{0}".format(session["session_id"]))
    assert response.status_code == 200
    assert "warning" in response.json() and response.json()["warning"]


def test_park_skips_ports_held_by_a_session(ic_push_live_server) -> None:
    """park 成功：被 session 持有的 port 列在 skipped（fanout/port/session_id 三欄）。"""
    server = ic_push_live_server
    session = _create(server, ["dut-A1", "dut-A2"])
    response = server.client.post("/interconnect/park", headers=_headers(server))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["parked"]
    assert body["skipped"]
    assert all(
        set(item) == {"fanout", "port", "session_id"} and item["session_id"] == session["session_id"]
        for item in body["skipped"]
    )
    for item in body["fanouts"]:
        assert item["ports"] == body["parked"][item["fanout"]]
        assert item["readback"]["verified"] is True


def test_park_scope_and_bad_query(ic_push_live_server) -> None:
    """park：fanout 可縮小範圍；壞 query 400；未知 fanout 400（均在 token 通過之後）。"""
    server = ic_push_live_server
    scoped = server.client.post("/interconnect/park?fanout=ic-1-1", headers=_headers(server))
    assert scoped.status_code == 200
    assert list(scoped.json()["parked"]) == ["ic-1-1"]
    bad = server.client.post("/interconnect/park?ports=abc", headers=_headers(server))
    assert bad.status_code == 400
    assert bad.json()["error"]["type"] == "ValidationError"
    unknown = server.client.post("/interconnect/park?fanout=zzz", headers=_headers(server))
    assert unknown.status_code == 400
    assert unknown.json()["error"]["type"] == "InterconnectRequestError"


def test_unknown_session_404_on_all_session_endpoints(ic_push_live_server) -> None:
    """session 不存在：apply／verify／release-device 皆 404。"""
    server = ic_push_live_server
    headers = _headers(server)
    responses = [
        server.client.post("/interconnect/sessions/ic-9999/apply", headers=headers),
        server.client.get("/interconnect/sessions/ic-9999/verify", headers=headers),
        server.client.post("/interconnect/sessions/ic-9999/release-device", headers=headers),
    ]
    for response in responses:
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "InterconnectSessionNotFoundError"


@pytest.mark.parametrize(
    "mode,status,error_type",
    [
        ("auth", 502, "DriverConnectionError"),
        ("timeout", 504, "DeviceTimeoutError"),
        ("readtimeout", 504, "DeviceTimeoutError"),
        ("drop", 409, "ReadBackMismatchError"),
    ],
)
def test_failure_modes_for_apply(
    ic_small_path: Path, push_server_factory, mode: str, status: int, error_type: str
) -> None:
    """apply：認證 502、逾時 504、設備忽略設定 → read-back 不符 409。"""
    server = push_server_factory(ic_small_path, _IC_FANOUTS, mode)
    session = _create(server, ["dut-A1", "dut-A2"])
    response = server.client.post(_url(session, "apply"), headers=_headers(server))
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["type"] == error_type
    assert "Traceback" not in response.text
    assert "fake-pass" not in response.text
    if status == 409:
        assert "Port Admin" in error["message"]


@pytest.mark.parametrize(
    "mode,status",
    [("auth", 502), ("timeout", 504), ("readtimeout", 504)],
)
def test_failure_modes_for_verify_release_and_park(
    ic_small_path: Path, push_server_factory, mode: str, status: int
) -> None:
    """verify／release-device／park 的連線失敗與逾時同樣 502／504。"""
    server = push_server_factory(ic_small_path, _IC_FANOUTS, mode)
    session = _create(server, ["dut-A1", "dut-A2"])
    headers = _headers(server)
    responses = [
        server.client.get(_url(session, "verify"), headers=headers),
        server.client.post(_url(session, "release-device"), headers=headers),
        server.client.post("/interconnect/park", headers=headers),
    ]
    for response in responses:
        assert response.status_code == status, response.text
        assert "Traceback" not in response.text


def test_missing_ic_credentials_is_500_config_error(
    ic_small_path: Path, push_server_factory
) -> None:
    """互聯 fanout 不在 credentials：500 ConfigError（fail closed，不連線）。"""
    server = push_server_factory(ic_small_path, ["core-1"])
    session = _create(server, ["dut-A1", "dut-A2"])
    response = server.client.post(_url(session, "apply"), headers=_headers(server))
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "ConfigError"


def test_topology_without_interconnect_section_is_404_not_configured(
    three_tier_path: Path, push_server_factory
) -> None:
    """拓樸沒有互聯區段：404 InterconnectNotConfiguredError（≠ DevicePushDisabledError）。"""
    server = push_server_factory(three_tier_path, _TRAFFIC_FANOUTS)
    response = server.client.post("/interconnect/park", headers=_headers(server))
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "InterconnectNotConfiguredError"
