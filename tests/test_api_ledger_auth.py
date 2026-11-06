"""純帳本寫入的 token 閘門與 GET /capabilities 測試（Design 1）。

涵蓋：
  - ``require_ledger_write``：未設 token → 403（WriteLockedError）；有設 token
    但 header 缺漏／錯誤 → 401（ApiTokenError）；token 正確 → 放行。
  - ``GET /capabilities``：回報 device_push／write_requires_token／writes_unlocked。
  - 讀端點（GET /sessions、/resources/available）不受 token 影響。

歷史（HISTORY）：
  - 0.1.0  Design 1 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from topo_manager.api import auth, create_app
from topo_manager.api.deps import Settings
from topo_manager.errors import ApiTokenError, WriteLockedError

_TOKEN = "ledger-secret"


def _request(token, header=None):
    """組替身 Request：settings.api_token 與選填的 X-API-Token header。"""
    settings = SimpleNamespace(api_token=token)
    headers = {} if header is None else {"X-API-Token": header}
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    return SimpleNamespace(app=app, headers=headers)


def _run(coro):
    return asyncio.run(coro)


def test_require_ledger_write_is_coroutine() -> None:
    """必須是 async def：同步 dependency 會進 threadpool，違反 SIGALRM 前提。"""
    import inspect

    assert inspect.iscoroutinefunction(auth.require_ledger_write)


@pytest.mark.parametrize("configured", [None, ""])
def test_no_token_configured_locks_writes(configured) -> None:
    """未設（None／空字串）token → WriteLockedError，即使 header 帶了值。"""
    with pytest.raises(WriteLockedError):
        _run(auth.require_ledger_write(_request(configured)))
    with pytest.raises(WriteLockedError):
        _run(auth.require_ledger_write(_request(configured, header="anything")))


def test_missing_or_wrong_header_is_api_token_error() -> None:
    """有設 token 但 header 缺漏或不符 → ApiTokenError，訊息不含 token。"""
    with pytest.raises(ApiTokenError) as missing:
        _run(auth.require_ledger_write(_request(_TOKEN)))
    with pytest.raises(ApiTokenError) as wrong:
        _run(auth.require_ledger_write(_request(_TOKEN, header="nope")))
    assert _TOKEN not in str(missing.value)
    assert _TOKEN not in str(wrong.value)


def test_correct_token_passes() -> None:
    """token 正確 → 不丟例外。"""
    assert _run(auth.require_ledger_write(_request(_TOKEN, header=_TOKEN))) is None


def test_non_ascii_header_does_not_crash() -> None:
    """非 ASCII header 以 UTF-8 bytes 比較，應為 ApiTokenError 而非 TypeError。"""
    with pytest.raises(ApiTokenError):
        _run(auth.require_ledger_write(_request(_TOKEN, header="權杖")))


def _client(three_tier_path: Path, tmp_path: Path, token, push: bool = False):
    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        group_state_path=str(tmp_path / "state" / "groups.json"),
        enable_device_push=push,
        api_token=token,
    )
    return TestClient(create_app(settings))


_BODY = {"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"}


def test_http_post_without_server_token_is_403(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """server 未設 token：POST／DELETE 一律 403 WriteLockedError。"""
    client = _client(three_tier_path, tmp_path, token=None)
    post = client.post("/sessions", json=_BODY, headers={"X-API-Token": "x"})
    assert post.status_code == 403
    assert post.json()["error"]["type"] == "WriteLockedError"
    delete = client.delete("/sessions/s-0001")
    assert delete.status_code == 403
    assert delete.json()["error"]["type"] == "WriteLockedError"


def test_http_post_with_wrong_or_missing_token_is_401(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """server 有設 token：缺 header／錯 token → 401，且帳本未被寫入。"""
    client = _client(three_tier_path, tmp_path, token=_TOKEN)
    assert client.post("/sessions", json=_BODY).status_code == 401
    wrong = client.post("/sessions", json=_BODY, headers={"X-API-Token": "bad"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["type"] == "ApiTokenError"
    assert client.get("/sessions").json()["count"] == 0


def test_http_reads_are_not_gated(three_tier_path: Path, tmp_path: Path) -> None:
    """讀端點不論 server 是否設 token 都開放。"""
    for token in (None, _TOKEN):
        client = _client(three_tier_path, tmp_path, token=token)
        assert client.get("/sessions").status_code == 200
        assert client.get("/resources/available").status_code == 200
        assert client.get("/topology").status_code == 200
        assert client.get("/groups").status_code == 200
        assert client.get("/capabilities").status_code == 200


@pytest.mark.parametrize(
    "token,push,expected",
    [
        (None, False, {"device_push": False, "write_requires_token": True, "writes_unlocked": False}),
        (_TOKEN, False, {"device_push": False, "write_requires_token": True, "writes_unlocked": True}),
        (_TOKEN, True, {"device_push": True, "write_requires_token": True, "writes_unlocked": True}),
    ],
)
def test_capabilities_reports_mode(
    three_tier_path: Path, tmp_path: Path, token, push, expected
) -> None:
    """/capabilities 依 device_push 與 token 設定回報；回應不含 token 內容。"""
    client = _client(three_tier_path, tmp_path, token=token, push=push)
    response = client.get("/capabilities")
    assert response.status_code == 200
    assert response.json() == expected
    if token:
        assert token not in response.text


# ----------------------------------------------------------------------
# Web UI 靜態 marker：Create Session 的 group 欄位與雙模式寫入端點
# ----------------------------------------------------------------------
def test_ui_has_group_input_and_datalist(three_tier_path: Path, tmp_path: Path) -> None:
    """index.html 的 Create Session 表單有選填的 group 輸入與 datalist。"""
    body = _client(three_tier_path, tmp_path, token=_TOKEN).get("/").text
    assert 'id="inp-group"' in body
    assert 'list="group-names"' in body
    assert 'id="group-names"' in body


def test_ui_app_js_switches_write_endpoints_by_capability(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """app.js 讀 /capabilities，並依 device_push 在兩組寫入端點間切換、帶 group。"""
    js = _client(three_tier_path, tmp_path, token=_TOKEN).get("/static/app.js").text
    # 讀取寫入能力。
    assert '"/capabilities"' in js
    assert "isDevicePush" in js
    # 設備下發模式：合併端點。
    assert "/sessions/allocate-and-apply" in js
    assert "/release-and-remove" in js
    # 純帳本模式：POST /sessions 與 DELETE /sessions/{id}。
    assert 'pushOn ? "/sessions/allocate-and-apply" : "/sessions"' in js
    assert 'apiFetchAuthed("DELETE"' in js
    # 兩種模式都經 apiFetchAuthed（帶 X-API-Token）。
    assert "body.group = groupName" in js
    # 後端鎖住寫入（403）有友善文案。
    assert "WriteLockedError" in js


def test_ui_ic_js_switches_write_endpoints_by_capability(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """ic.js 讀 /capabilities，並依 device_push 在互聯的兩組寫入端點間切換。"""
    js = _client(three_tier_path, tmp_path, token=_TOKEN).get("/static/ic.js").text
    assert '"/capabilities"' in js
    assert "icIsDevicePush" in js
    # 設備下發模式：合併端點。
    assert "/interconnect/sessions/allocate-and-apply" in js
    assert "/release-and-remove" in js
    # 純帳本模式：POST /interconnect/sessions 與 DELETE /interconnect/sessions/{id}。
    assert ': "/interconnect/sessions";' in js
    assert 'icApiFetchAuthed("DELETE", "/interconnect/sessions/" + sid)' in js
    # server 未設 token（403）有友善文案。
    assert "WriteLockedError" in js


# ----------------------------------------------------------------------
# 互聯帳本寫入同樣以 token 閘控（POST／DELETE /interconnect/sessions）
# ----------------------------------------------------------------------
_IC_BODY = {"group": "A-g1", "duts": ["dut-A1", "dut-A2"], "profile": "mesh"}


def _ic_client(ic_small_path: Path, tmp_path: Path, token):
    settings = Settings(
        topology_path=str(ic_small_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        ic_state_path=str(tmp_path / "state" / "interconnect.json"),
        group_state_path=str(tmp_path / "state" / "groups.json"),
        api_token=token,
    )
    return TestClient(create_app(settings))


def test_ic_write_without_server_token_is_403(
    ic_small_path: Path, tmp_path: Path
) -> None:
    """server 未設 token：互聯 POST／DELETE 一律 403 WriteLockedError。"""
    client = _ic_client(ic_small_path, tmp_path, token=None)
    post = client.post("/interconnect/sessions", json=_IC_BODY)
    assert post.status_code == 403
    assert post.json()["error"]["type"] == "WriteLockedError"
    delete = client.delete("/interconnect/sessions/ic-0001")
    assert delete.status_code == 403
    assert delete.json()["error"]["type"] == "WriteLockedError"


def test_ic_write_with_wrong_or_missing_token_is_401(
    ic_small_path: Path, tmp_path: Path
) -> None:
    """server 有設 token：缺 header／錯 token → 401，且互聯帳本未被寫入。"""
    client = _ic_client(ic_small_path, tmp_path, token=_TOKEN)
    assert client.post("/interconnect/sessions", json=_IC_BODY).status_code == 401
    wrong = client.post(
        "/interconnect/sessions", json=_IC_BODY, headers={"X-API-Token": "bad"}
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["type"] == "ApiTokenError"
    assert (
        client.delete("/interconnect/sessions/ic-0001").status_code == 401
    )
    assert client.get("/interconnect/sessions").json()["count"] == 0


def test_ic_reads_and_plan_preview_stay_open(
    ic_small_path: Path, tmp_path: Path
) -> None:
    """互聯讀端點與 plan 預覽（不入帳、無副作用）不受 token 影響。"""
    for token in (None, _TOKEN):
        client = _ic_client(ic_small_path, tmp_path, token=token)
        assert client.get("/interconnect/summary").status_code == 200
        assert client.get("/interconnect/sessions").status_code == 200
        assert client.get("/interconnect/resources/available").status_code == 200
        assert client.post("/interconnect/plan", json=_IC_BODY).status_code == 200


def test_ui_ic_topology_draws_duts_left_and_fanout_right(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """互聯拓樸：DUT 排左欄、fanout switch 在右；預覽以虛線畫出（結構斷言）。"""
    client = _client(three_tier_path, tmp_path, token=_TOKEN)
    js = client.get("/static/ic.js").text
    css = client.get("/static/ic.css").text
    # 版面：fanout 方框與 port 圓點、DUT 由上而下一排。
    assert 'class": "ic-fanout"' in js
    assert '"ic-port"' in js
    assert "icLinkOrderedPorts" in js
    assert "function renderIcGroup(group, groupLinks, dashed)" in js
    # 舊的「方形四角」排法已移除。
    assert "icCornerFor" not in js
    assert "icCenterOf" not in js
    # 預覽：renderIcTopology 在沒有選取時用 Preview 的計畫並以虛線畫。
    assert "icState.preview" in js
    assert '" preview"' in js
    # 每條 link 的顏色用 inline style（CSS 的 .ic-edge.active 會蓋過 stroke 屬性）。
    assert '"stroke:" + color' in js
    # CSS：fanout、port、預覽虛線。
    assert ".ic-fanout rect" in css
    assert ".ic-edge.preview" in css
    assert "stroke-dasharray" in css
