"""打流側設備下發 REST 整合測試（tests/test_api_push.py）。

以主執行緒 live-server 子行程（沿用 conftest 的 live-server 作法）啟動啟用設備
下發的 app，driver 一律由 ``ic_fakes.FakeSwitchFarm`` 的假 netmiko connection 注入，
**絕不連真實設備**。涵蓋：成功（逐 fanout 結果與 read-back 摘要）、502／504／409、
session 不存在 404、setup 的 body 規則、credentials 涵蓋核對，以及「server 啟動後
才由另一個 allocator 寫入帳本」的 session 仍找得到。

歷史（HISTORY）：
  - 0.1.0  設備下發 REST 初版。
  - 0.2.0  FEAT-002 合併端點整合測試：allocate-and-apply 成功（帳本有新 session
           且假設備顯示 VLAN 成員、逐 fanout read-back）、release-and-remove 成功
           （假設備成員消失且帳本不再有該 session）、allocate-and-apply 設備失敗
           （drop→409／auth→502／timeout→504）斷言帳本未殘留半成品（Allocator
           回捲）、release-and-remove 設備失敗斷言帳本仍保有 session；所有錯誤
           body 皆無 Traceback。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from topo_manager.api.deps import Settings, build_allocator

_TRAFFIC_FANOUTS = ["spine-1", "agg-1", "agg-2", "leaf-1-1", "leaf-2-1"]
_GENERATOR = "ixia-1/1"
_DUT_PORT = "dut-A1:Eth0"


def _headers(server) -> dict:
    """帶正確 token 的 header。"""
    return {"X-API-Token": server.token}


def _create_session(server) -> dict:
    """先 POST 建 session（只動帳本），回傳其 JSON。"""
    response = server.client.post(
        "/sessions", json={"generator_port": _GENERATOR, "dut_port": _DUT_PORT}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_apply_success_lists_each_fanout_with_readback(push_live_server) -> None:
    """成功：200，逐 fanout 結果含 read-back 摘要；verify 隨後 ok。"""
    session = _create_session(push_live_server)
    response = push_live_server.client.post(
        "/sessions/{0}/apply".format(session["session_id"]), headers=_headers(push_live_server)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    assert body["vlan"] == session["vlan"]
    assert len(body["fanouts"]) >= 2
    for item in body["fanouts"]:
        assert item["ok"] is True
        assert item["commands"]
        assert item["ports"]
        assert item["readback"]["verified"] is True
        assert item["readback"]["checked"] >= 1
        assert item["readback"]["missing"] == []

    verify = push_live_server.client.get(
        "/sessions/{0}/verify".format(session["session_id"]), headers=_headers(push_live_server)
    )
    assert verify.status_code == 200
    assert verify.json()["ok"] is True
    assert {i["fanout"] for i in verify.json()["fanouts"]} == {i["fanout"] for i in body["fanouts"]}


def test_verify_before_apply_reports_missing_with_200(push_live_server) -> None:
    """未 apply 就 verify：HTTP 200 且 ok=false，missing 列出不符項。"""
    session = _create_session(push_live_server)
    response = push_live_server.client.get(
        "/sessions/{0}/verify".format(session["session_id"]), headers=_headers(push_live_server)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert any(item["readback"]["missing"] for item in body["fanouts"])
    assert all(item["ok"] is False for item in body["fanouts"])


def test_apply_and_verify_unknown_session_404(push_live_server) -> None:
    """session 不存在：apply／verify 皆 404 SessionNotFoundError。"""
    apply_resp = push_live_server.client.post(
        "/sessions/nope/apply", headers=_headers(push_live_server)
    )
    verify_resp = push_live_server.client.get(
        "/sessions/nope/verify", headers=_headers(push_live_server)
    )
    for response in (apply_resp, verify_resp):
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "SessionNotFoundError"


@pytest.mark.parametrize(
    "mode,status,error_type",
    [
        ("auth", 502, "DriverConnectionError"),
        ("timeout", 504, "DeviceTimeoutError"),
        ("readtimeout", 504, "DeviceTimeoutError"),
    ],
)
def test_connection_failures_map_to_502_or_504(
    three_tier_path: Path, push_server_factory, mode: str, status: int, error_type: str
) -> None:
    """連線／認證失敗 502、逾時（含 ReadTimeout）504；apply 與 verify 路徑一致。"""
    server = push_server_factory(three_tier_path, _TRAFFIC_FANOUTS, mode)
    session = _create_session(server)
    sid = session["session_id"]
    apply_resp = server.client.post("/sessions/{0}/apply".format(sid), headers=_headers(server))
    verify_resp = server.client.get("/sessions/{0}/verify".format(sid), headers=_headers(server))
    for response in (apply_resp, verify_resp):
        assert response.status_code == status, response.text
        assert response.json()["error"]["type"] == error_type
        assert "Traceback" not in response.text
        assert "fake-pass" not in response.text


def test_readback_mismatch_409_lists_items(three_tier_path: Path, push_server_factory) -> None:
    """設備靜默忽略設定 → read-back 不符 409，message 列出介面與 VLAN。"""
    server = push_server_factory(three_tier_path, _TRAFFIC_FANOUTS, "drop")
    session = _create_session(server)
    response = server.client.post(
        "/sessions/{0}/apply".format(session["session_id"]), headers=_headers(server)
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["type"] == "ReadBackMismatchError"
    assert "Eth1/" in error["message"]
    assert "VLAN {0}".format(session["vlan"]) in error["message"]


def test_session_written_after_startup_is_found(push_live_server) -> None:
    """server 啟動後才由另一個 allocator 寫入帳本的 session，apply 仍找得到。"""
    settings = Settings(
        topology_path=str(push_live_server.topology_path),
        state_path=str(push_live_server.state_path),
    )
    session = build_allocator(settings).allocate(_GENERATOR, _DUT_PORT)
    response = push_live_server.client.post(
        "/sessions/{0}/apply".format(session.session_id), headers=_headers(push_live_server)
    )
    assert response.status_code == 200, response.text
    assert response.json()["session_id"] == session.session_id


def test_missing_credentials_for_session_fanout_is_500_before_connecting(
    three_tier_path: Path, push_server_factory
) -> None:
    """credentials 沒涵蓋 session 路徑上的 fanout：500 ConfigError，不靜默略過。"""
    server = push_server_factory(three_tier_path, ["spine-1"])
    session = _create_session(server)
    sid = session["session_id"]
    for response in (
        server.client.post("/sessions/{0}/apply".format(sid), headers=_headers(server)),
        server.client.get("/sessions/{0}/verify".format(sid), headers=_headers(server)),
    ):
        assert response.status_code == 500
        assert response.json()["error"]["type"] == "ConfigError"
        assert "credentials" in response.json()["error"]["message"]


def test_setup_all_and_selected_fanouts(push_live_server) -> None:
    """setup：空 body＝全部 fanout；指定名稱只動指定者；讀回 Enabled。"""
    headers = _headers(push_live_server)
    everything = push_live_server.client.post("/fanouts/setup", headers=headers)
    assert everything.status_code == 200, everything.text
    body = everything.json()
    assert body["ok"] is True
    assert sorted(i["fanout"] for i in body["fanouts"]) == sorted(_TRAFFIC_FANOUTS)
    assert all(i["tunnel_status"] is True and i["commands"] for i in body["fanouts"])

    selected = push_live_server.client.post(
        "/fanouts/setup", json={"fanouts": ["spine-1", "spine-1"]}, headers=headers
    )
    assert selected.status_code == 200
    assert [i["fanout"] for i in selected.json()["fanouts"]] == ["spine-1"]


@pytest.mark.parametrize(
    "content",
    [b"{not json", b'{"fanouts": []}', b'{"fanouts": ["nope"]}', b'{"extra": 1}', b"[1]"],
)
def test_setup_bad_body_is_400(push_live_server, content: bytes) -> None:
    """setup 壞 JSON／空陣列／未知 fanout／多餘欄位／非 object：400 DevicePushRequestError。"""
    response = push_live_server.client.post(
        "/fanouts/setup",
        content=content,
        headers=dict(_headers(push_live_server), **{"Content-Type": "application/json"}),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["type"] == "DevicePushRequestError"


def test_setup_unknown_fanout_message_lists_available(push_live_server) -> None:
    """未知 fanout 的 400 message 列出可用名稱。"""
    response = push_live_server.client.post(
        "/fanouts/setup", json={"fanouts": ["nope"]}, headers=_headers(push_live_server)
    )
    assert "spine-1" in response.json()["error"]["message"]


def test_setup_readback_disabled_is_409(three_tier_path: Path, push_server_factory) -> None:
    """setup 讀回不是 Enabled（設備忽略設定）→ 409 ReadBackMismatchError。"""
    server = push_server_factory(three_tier_path, _TRAFFIC_FANOUTS, "drop")
    response = server.client.post("/fanouts/setup", headers=_headers(server))
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "ReadBackMismatchError"
    assert "spine-1" in response.json()["error"]["message"]


def test_setup_timeout_is_504(three_tier_path: Path, push_server_factory) -> None:
    """setup 連線逾時 → 504。"""
    server = push_server_factory(three_tier_path, _TRAFFIC_FANOUTS, "timeout")
    response = server.client.post("/fanouts/setup", headers=_headers(server))
    assert response.status_code == 504
    assert response.json()["error"]["type"] == "DeviceTimeoutError"


# ----------------------------------------------------------------------
# FEAT-002 合併端點（allocate-and-apply / release-and-remove）
# ----------------------------------------------------------------------
def _allocator_for(server):
    """以 server 的拓樸與帳本路徑建 allocator（供測試直接讀回帳本狀態）。"""
    return build_allocator(
        Settings(
            topology_path=str(server.topology_path),
            state_path=str(server.state_path),
        )
    )


def _ledger_session_ids(server):
    """回傳帳本中目前所有 session_id 集合（每次重建 allocator 以讀最新帳本）。"""
    return {session.session_id for session in _allocator_for(server).list_sessions()}


def test_allocate_and_apply_success_creates_ledger_and_pushes(push_live_server) -> None:
    """allocate-and-apply 成功：帳本有新 session、假設備已下發、逐 fanout read-back。"""
    server = push_live_server
    response = server.client.post(
        "/sessions/allocate-and-apply",
        json={"generator_port": _GENERATOR, "dut_port": _DUT_PORT},
        headers=_headers(server),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "allocated-and-applied"
    # session_id / vlan 在內嵌的 session.* 之內，不在頂層。
    assert "session_id" not in body
    assert "vlan" not in body
    session = body["session"]
    assert session["status"] == "allocated"
    sid = session["session_id"]
    assert len(body["fanouts"]) >= 2
    for item in body["fanouts"]:
        assert item["ok"] is True
        assert item["commands"]
        assert item["ports"]
        assert item["readback"]["verified"] is True
        assert item["readback"]["missing"] == []

    # 帳本確實有這筆新 session。
    assert sid in _ledger_session_ids(server)
    # verify 隨後為 ok（證明假設備確有 VLAN 成員）。
    verify = server.client.get(
        "/sessions/{0}/verify".format(sid), headers=_headers(server)
    )
    assert verify.status_code == 200
    assert verify.json()["ok"] is True


def test_release_and_remove_success_tears_down_and_clears_ledger(push_live_server) -> None:
    """release-and-remove 成功：假設備成員消失、帳本不再有該 session。"""
    server = push_live_server
    created = server.client.post(
        "/sessions/allocate-and-apply",
        json={"generator_port": _GENERATOR, "dut_port": _DUT_PORT},
        headers=_headers(server),
    )
    sid = created.json()["session"]["session_id"]
    assert sid in _ledger_session_ids(server)

    response = server.client.post(
        "/sessions/{0}/release-and-remove".format(sid), headers=_headers(server)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "released-and-removed"
    assert body["session_id"] == sid
    assert all(item["ok"] for item in body["fanouts"])

    # 帳本已清除；再對該 session apply 得 404。
    assert sid not in _ledger_session_ids(server)
    again = server.client.post(
        "/sessions/{0}/apply".format(sid), headers=_headers(server)
    )
    assert again.status_code == 404
    assert again.json()["error"]["type"] == "SessionNotFoundError"


@pytest.mark.parametrize(
    "mode,status,error_type",
    [
        ("drop", 409, "ReadBackMismatchError"),
        ("auth", 502, "DriverConnectionError"),
        ("timeout", 504, "DeviceTimeoutError"),
    ],
)
def test_allocate_and_apply_device_failure_rolls_back_ledger(
    three_tier_path: Path, push_server_factory, mode: str, status: int, error_type: str
) -> None:
    """allocate-and-apply 設備失敗：Allocator.release 回捲，帳本不留半成品。"""
    server = push_server_factory(three_tier_path, _TRAFFIC_FANOUTS, mode)
    before = _ledger_session_ids(server)
    response = server.client.post(
        "/sessions/allocate-and-apply",
        json={"generator_port": _GENERATOR, "dut_port": _DUT_PORT},
        headers=_headers(server),
    )
    assert response.status_code == status, response.text
    assert response.json()["error"]["type"] == error_type
    assert "Traceback" not in response.text
    assert "fake-pass" not in response.text
    # 設備失敗後帳本不得殘留半成品 session。
    assert _ledger_session_ids(server) == before


def test_release_and_remove_device_failure_keeps_ledger(
    three_tier_path: Path, push_server_factory
) -> None:
    """release-and-remove 設備失敗：帳本仍保有該 session（不提早清除），結構化錯誤。

    push_server_factory 每個 server 行程只有單一 fake mode，無法同時「ok 建立 +
    壞設備釋放」；故先用一個外部 allocator 直接把 session 寫進共用帳本（只動帳本、
    不連設備），再對同一 tmp_path 另起一個 timeout server 呼叫 release-and-remove，
    斷言設備逾時 504 且帳本仍保有該 session。
    """
    server = push_server_factory(three_tier_path, _TRAFFIC_FANOUTS, "timeout")
    # 外部 allocator 直接寫帳本（與 server 共用同一 state_path）。
    session = _allocator_for(server).allocate(_GENERATOR, _DUT_PORT)
    sid = session.session_id
    assert sid in _ledger_session_ids(server)

    response = server.client.post(
        "/sessions/{0}/release-and-remove".format(sid), headers=_headers(server)
    )
    assert response.status_code == 504, response.text
    assert response.json()["error"]["type"] == "DeviceTimeoutError"
    assert "Traceback" not in response.text
    # 設備撤除失敗 → 帳本不得提早清除，session 仍在。
    assert sid in _ledger_session_ids(server)
