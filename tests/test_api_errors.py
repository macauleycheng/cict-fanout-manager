"""topo_manager REST API 錯誤與邊界測試（Phase 3，設計 §6、§10.2）。

涵蓋 status_for 映射單元測試、請求驗證 400、路徑/找不到 404、硬衝突 409、
啟動期 fail-fast、非預期例外 500。write-path 的錯誤（硬衝突、找不到、無路徑）
以主執行緒 live-server 驗證（見 ``test_api.py`` 模組 docstring 的 harness 說明）；
輸入驗證 400、fail-fast、internal 500 則以 ``TestClient`` 驗證（不觸 Phase 2 鎖）。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 status_for 映射 + 錯誤路徑整合測試（設計 §6）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest
from fastapi.testclient import TestClient

from topo_manager.api import create_app
from topo_manager.api.deps import Settings
from topo_manager.api.errors import status_for
from topo_manager.errors import (
    ConfigError,
    HardConflictError,
    PathLookupError,
    SessionNotFoundError,
    SessionStoreError,
    TopologyError,
    TopologyFileError,
    TopologyGraphError,
    TopologyParseError,
    TopologyReferenceError,
    TopologyValidationError,
    VlanPoolExhaustedError,
)

# 注意：_LiveServer / live_server fixture 於 conftest.py 定義，pytest 依名稱
# 自動注入；因 from __future__ import annotations 使型別註解延遲求值，故
# 以字串前置參照 "_LiveServer" 標註即可，無需跨模組 import（避免環境中殘留的
# 已安裝 tests 套件遮蔽 workspace conftest）。


# ----------------------------------------------------------------------
# status_for 映射單元測試（設計 §6.2）
# ----------------------------------------------------------------------


def test_status_for_mapping() -> None:
    """status_for 對每個 TopologyError 子類回正確狀態碼（設計 §6.2）。"""
    assert status_for(HardConflictError("x")) == 409
    assert status_for(VlanPoolExhaustedError("x")) == 409
    assert status_for(SessionNotFoundError("x")) == 404
    assert status_for(PathLookupError("x")) == 404
    assert status_for(SessionStoreError("x")) == 503
    assert status_for(ConfigError("x")) == 500
    # 其餘拓樸子類落 500（fail-fast 下請求期不應發生，保底 500）。
    assert status_for(TopologyFileError("x")) == 500
    assert status_for(TopologyParseError("x")) == 500
    assert status_for(TopologyValidationError("x")) == 500
    assert status_for(TopologyReferenceError("x")) == 500
    assert status_for(TopologyGraphError("x")) == 500
    # 基底本身（理論上不直接拋）亦落 500。
    assert status_for(TopologyError("x")) == 500


# ----------------------------------------------------------------------
# 請求驗證 400（TestClient，不觸 allocator）
# ----------------------------------------------------------------------


def test_bad_input_missing_field(api_client: Tuple[TestClient, Path]) -> None:
    """POST 缺 dut_port → 400、結構化、ledger 未變（AC-10）。"""
    client, state = api_client
    response = client.post("/sessions", json={"generator_port": "ixia-1/1"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "ValidationError"
    # 未觸 allocator：state 檔未建立（分配從未發生）。
    assert not state.exists()


def test_bad_input_empty_string(api_client: Tuple[TestClient, Path]) -> None:
    """POST dut_port 空字串（min_length=1）→ 400（AC-10）。"""
    client, _ = api_client
    response = client.post(
        "/sessions", json={"generator_port": "ixia-1/1", "dut_port": ""}
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ValidationError"


def test_bad_input_extra_field(api_client: Tuple[TestClient, Path]) -> None:
    """POST 多餘欄位（extra=forbid）→ 400（AC-10）。"""
    client, _ = api_client
    response = client.post(
        "/sessions",
        json={
            "generator_port": "ixia-1/1",
            "dut_port": "dut-A1:Eth0",
            "unexpected": "x",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ValidationError"


def test_no_traceback_in_error_body(api_client: Tuple[TestClient, Path]) -> None:
    """錯誤回應 body 不含 Python traceback（NFR-3）。"""
    client, _ = api_client
    response = client.post("/sessions", json={"generator_port": "ixia-1/1"})
    assert "Traceback" not in response.text


# ----------------------------------------------------------------------
# 啟動期 fail-fast（TestClient 建 app 即拋）
# ----------------------------------------------------------------------


def test_create_app_fail_fast_missing_yaml(tmp_path: Path) -> None:
    """指向缺失 YAML 的 settings → create_app 拋 TopologyError（FR-A6）。"""
    settings = Settings(
        topology_path=str(tmp_path / "does-not-exist.yaml"),
        state_path=str(tmp_path / "state" / "allocations.json"),
        vlan_min=100,
        vlan_max=3000,
    )
    with pytest.raises(TopologyError):
        create_app(settings)


def test_create_app_fail_fast_bad_vlan(three_tier_path: Path, tmp_path: Path) -> None:
    """VLAN 範圍非法的 settings → create_app 拋 ConfigError（FR-A6、§4.4）。"""
    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        vlan_min=5000,
        vlan_max=6000,
    )
    with pytest.raises(ConfigError):
        create_app(settings)


# ----------------------------------------------------------------------
# 非預期例外 500（TestClient + monkeypatch 注入假例外）
# ----------------------------------------------------------------------


def test_internal_error_500(three_tier_path: Path, tmp_path: Path) -> None:
    """非 TopologyError 例外 → 500 結構化、無 traceback（FR-3.4）。

    以 ``raise_server_exceptions=False`` 讓例外被 handler 攔截轉 500；
    monkeypatch 讓 build_allocator（讀路徑 /sessions 使用）拋非預期 RuntimeError。
    """
    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        vlan_min=100,
        vlan_max=3000,
    )
    app = create_app(settings)

    def _boom(_settings):
        raise RuntimeError("注入的非預期例外")

    # 讀路徑 GET /sessions 於 routes 模組呼叫 build_allocator，替換之。
    import topo_manager.api.routes as routes_module

    original = routes_module.build_allocator
    routes_module.build_allocator = _boom
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/sessions")
        assert response.status_code == 500
        body = response.json()
        assert body["error"]["type"] == "InternalError"
        assert "Traceback" not in response.text
    finally:
        routes_module.build_allocator = original


# ----------------------------------------------------------------------
# write-path 錯誤整合測試（主執行緒 live-server）
# ----------------------------------------------------------------------


def test_hard_conflict_409(live_server: "_LiveServer") -> None:
    """同一 (gen,dut) 兩次 POST → 第二次 409、HardConflictError、無 traceback（AC-7）。"""
    payload = {"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"}
    first = live_server.client.post("/sessions", json=payload)
    assert first.status_code == 201

    second = live_server.client.post("/sessions", json=payload)
    assert second.status_code == 409
    body = second.json()
    assert body["error"]["type"] == "HardConflictError"
    assert "Traceback" not in second.text


def test_delete_not_found_404(live_server: "_LiveServer") -> None:
    """DELETE 不存在 session → 404、SessionNotFoundError（AC-8）。"""
    response = live_server.client.request("DELETE", "/sessions/s-9999")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "SessionNotFoundError"


def test_path_not_found_404(live_server: "_LiveServer") -> None:
    """POST 帶不存在端點 → 404、PathLookupError、無 traceback（AC-9）。"""
    response = live_server.client.post(
        "/sessions",
        json={"generator_port": "no-such-gen", "dut_port": "dut-A1:Eth0"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["type"] == "PathLookupError"
    assert "Traceback" not in response.text
