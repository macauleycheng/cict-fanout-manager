"""設備下發 REST 的兩道閘門測試（tests/test_api_push_gate.py）。

涵蓋 11 個下發端點（含 FEAT-002 四個合併端點）的：未啟用 → 結構化 404（type 與
「無互聯區段」不同）、啟用後 token 缺漏／錯誤／非 ASCII／相同前綴 → 401，且 401
先於壞 query／壞 JSON body，並以注入的 driver 工廠斷言「從未被呼叫」、帳本檔
bytes 不變、credentials 檔（刻意指向不存在的路徑）未被讀取。僅用 TestClient：這些
請求都在取鎖與連線之前就被拒絕，不需要主執行緒。

歷史（HISTORY）：
  - 0.1.0  設備下發 REST 初版。
  - 0.2.0  FEAT-002：_PUSH_ENDPOINTS 增列四個合併端點（tagging／interconnect 的
           allocate-and-apply / release-and-remove），兩道閘門參數化測試一併涵蓋。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest
from fastapi.testclient import TestClient

from topo_manager.api import create_app
from topo_manager.api.deps import Settings

_TOKEN = "test-token-xyz"

# (method, path) 共 11 個下發端點（含 FEAT-002 四個合併端點）。
_PUSH_ENDPOINTS: List[Tuple[str, str]] = [
    ("POST", "/sessions/s-1/apply"),
    ("GET", "/sessions/s-1/verify"),
    ("POST", "/fanouts/setup"),
    ("POST", "/interconnect/sessions/ic-1/apply"),
    ("GET", "/interconnect/sessions/ic-1/verify"),
    ("POST", "/interconnect/park"),
    ("POST", "/interconnect/sessions/ic-1/release-device"),
    # FEAT-002 合併端點。
    ("POST", "/sessions/allocate-and-apply"),
    ("POST", "/sessions/s-1/release-and-remove"),
    ("POST", "/interconnect/sessions/allocate-and-apply"),
    ("POST", "/interconnect/sessions/ic-1/release-and-remove"),
]


def _build(topology: Path, tmp_path: Path, calls: list, enabled: bool = True) -> TestClient:
    """建 app；啟用時注入「被呼叫就記錄並失敗」的 driver 工廠。"""

    def _boom(*args: object, **kwargs: object) -> object:
        calls.append(args)
        raise AssertionError("driver 工廠不應被呼叫")

    settings = Settings(
        topology_path=str(topology),
        state_path=str(tmp_path / "state" / "allocations.json"),
        ic_state_path=str(tmp_path / "state" / "interconnect.json"),
        enable_device_push=enabled,
        api_token=_TOKEN if enabled else None,
        credentials_path=str(tmp_path / "does-not-exist.json") if enabled else None,
    )
    if enabled:
        return TestClient(
            create_app(settings, push_driver_factory=_boom, ic_push_driver_factory=_boom)
        )
    return TestClient(create_app(settings))


@pytest.fixture
def gated(ic_small_path: Path, tmp_path: Path):
    """啟用下發的 TestClient 與 driver 呼叫記錄。"""
    calls: list = []
    return _build(ic_small_path, tmp_path, calls), calls


def _snapshot(tmp_path: Path) -> dict:
    """帳本檔的 (存在, bytes)；不存在就是 (False, None)。"""
    result = {}
    for name in ("allocations.json", "interconnect.json"):
        path = tmp_path / "state" / name
        result[name] = path.read_bytes() if path.exists() else None
    return result


@pytest.mark.parametrize("method,path", _PUSH_ENDPOINTS)
def test_disabled_returns_structured_404(
    ic_small_path: Path, tmp_path: Path, method: str, path: str
) -> None:
    """未啟用：404 DevicePushDisabledError；即使帶錯 token 也一樣（不洩漏 token 是否正確）。"""
    client = _build(ic_small_path, tmp_path, [], enabled=False)
    for headers in ({}, {"X-API-Token": "wrong"}):
        response = client.request(method, path, headers=headers)
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "DevicePushDisabledError"


def test_disabled_type_differs_from_no_interconnect_section(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """未啟用的 404 與「拓樸沒有互聯區段」的 404 以 error.type 區分。"""
    client = _build(three_tier_path, tmp_path, [], enabled=False)
    disabled = client.post("/interconnect/park")
    not_configured = client.get("/interconnect/summary")
    assert disabled.status_code == not_configured.status_code == 404
    assert disabled.json()["error"]["type"] == "DevicePushDisabledError"
    assert not_configured.json()["error"]["type"] == "InterconnectNotConfiguredError"


@pytest.mark.parametrize("method,path", _PUSH_ENDPOINTS)
def test_missing_token_is_401(gated, tmp_path: Path, method: str, path: str) -> None:
    """缺 token：401 ApiTokenError；driver 工廠未被呼叫、帳本未動。"""
    client, calls = gated
    before = _snapshot(tmp_path)
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "ApiTokenError"
    assert calls == []
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("method,path", _PUSH_ENDPOINTS)
@pytest.mark.parametrize(
    "token",
    ["wrong", "test-token-xy", "test-token-xyz-and-more", "", "TEST-TOKEN-XYZ"],
)
def test_wrong_token_is_401(gated, tmp_path: Path, method: str, path: str, token: str) -> None:
    """錯 token（含相同前綴、較長、空字串、大小寫不同）：401，且不碰設備與帳本。"""
    client, calls = gated
    before = _snapshot(tmp_path)
    response = client.request(method, path, headers={"X-API-Token": token})
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "ApiTokenError"
    assert _token_not_echoed(response.text, token)
    assert calls == []
    assert _snapshot(tmp_path) == before


def _token_not_echoed(text: str, token: str) -> bool:
    """回應不得回顯送來的 token 或設定的 token。"""
    return _TOKEN not in text and (token == "" or token not in text)


def test_non_ascii_token_is_401_not_500(gated) -> None:
    """非 ASCII token：401（以 UTF-8 bytes 比較，不因 TypeError 變 500）。"""
    client, calls = gated
    response = client.post(
        "/interconnect/park", headers=[(b"x-api-token", "密鑰é".encode("utf-8"))]
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "ApiTokenError"
    assert calls == []


def test_401_precedes_bad_query_and_bad_body(gated) -> None:
    """壞 query／壞 JSON body 配錯 token 仍是 401（閘門先於參數驗證與 body 解析）。"""
    client, calls = gated
    bad_query = client.post("/interconnect/park?ports=abc", headers={"X-API-Token": "wrong"})
    bad_body = client.post(
        "/fanouts/setup",
        content=b"{not json",
        headers={"X-API-Token": "wrong", "Content-Type": "application/json"},
    )
    for response in (bad_query, bad_body):
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "ApiTokenError"
    assert calls == []


def test_correct_token_passes_gate_then_validates_params(gated) -> None:
    """token 正確才繼續：壞 query 變成 400 ValidationError（驗證確實發生在 token 之後）。"""
    client, calls = gated
    response = client.post("/interconnect/park?ports=abc", headers={"X-API-Token": _TOKEN})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ValidationError"
    assert calls == []


def test_existing_endpoints_need_no_token(gated) -> None:
    """既有不連設備端點不需 token，行為不變。"""
    client, _ = gated
    assert client.get("/sessions").status_code == 200
    assert client.get("/interconnect/summary").status_code == 200
    assert client.get("/topology").status_code == 200


def test_unknown_session_with_token_is_404_before_credentials(gated) -> None:
    """打流 session 不存在：404 先於 credentials 載入（credentials 檔不存在也不是 500）。"""
    client, calls = gated
    response = client.post("/sessions/nope/apply", headers={"X-API-Token": _TOKEN})
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "SessionNotFoundError"
    assert calls == []
