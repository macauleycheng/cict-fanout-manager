"""topo_manager REST API 錯誤與邊界測試（Phase 3，設計 §6、§10.2）。

涵蓋 status_for 映射單元測試、請求驗證 400、路徑/找不到 404、硬衝突 409、
啟動期 fail-fast、非預期例外 500、請求期拓樸漂移 → 503（Issue 1）。write-path
的錯誤（硬衝突、找不到、無路徑）與寫路徑非預期例外 500（Issue 3）以主執行緒
live-server 驗證（見 ``test_api.py`` 模組 docstring 的 harness 說明）；輸入驗證
400、fail-fast、讀路徑 internal 500、拓樸漂移 503 則以 ``TestClient`` 驗證
（不觸 Phase 2 鎖）。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 status_for 映射 + 錯誤路徑整合測試（設計 §6）。
  - 0.4.1  Phase 3 審查：Issue 1 請求期拓樸漂移讀端點回 503 / 寫端點（吃啟動
           單例）不受影響的整合測試；status_for 映射更新（五個環境性拓樸子類
           → 503）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Tuple

import httpx
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
    # 環境性拓樸子類：請求期拓樸漂移（讀路徑重讀 YAML / 建圖失敗）→ 503
    # （Issue 1；可修檔重試，非程式 bug）。
    assert status_for(TopologyFileError("x")) == 503
    assert status_for(TopologyParseError("x")) == 503
    assert status_for(TopologyValidationError("x")) == 503
    assert status_for(TopologyReferenceError("x")) == 503
    assert status_for(TopologyGraphError("x")) == 503
    # 基底本身（理論上不直接拋）未命中映射，保底落 500。
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
# 請求期拓樸漂移 → 503（Issue 1，TestClient；不觸 Phase 2 鎖）
# ----------------------------------------------------------------------


def _build_app_on_writable_topology(three_tier_path: Path, tmp_path: Path):
    """把 3tier YAML 複製到可寫的 tmp 路徑並建 app，回 (app, yaml_copy, settings)。

    以可寫副本為 topology_path，才能在啟動後「改壞 YAML」模擬請求期拓樸漂移；
    啟動時 YAML 仍合法，故 create_app 正常（fail-fast 不觸發）。
    """
    yaml_copy = tmp_path / "topology.yaml"
    shutil.copyfile(str(three_tier_path), str(yaml_copy))
    settings = Settings(
        topology_path=str(yaml_copy),
        state_path=str(tmp_path / "state" / "allocations.json"),
        vlan_min=100,
        vlan_max=3000,
    )
    app = create_app(settings)
    return app, yaml_copy, settings


def test_topology_drift_read_returns_503(three_tier_path: Path, tmp_path: Path) -> None:
    """啟動後 YAML 被改壞 → 讀端點回 503（非 500），body 結構化無 traceback（Issue 1）。

    讀路徑（GET /sessions、GET /resources/available）每請求以 build_allocator
    重讀 YAML；啟動後把 YAML 覆寫成非法內容，重讀會丟環境性 TopologyError
    子類，映射為 503（服務暫時不可用、可修檔重試），而非 500 InternalError。
    """
    app, yaml_copy, _settings = _build_app_on_writable_topology(
        three_tier_path, tmp_path
    )
    client = TestClient(app, raise_server_exceptions=False)

    # 啟動後把拓樸 YAML 改壞（語法/結構皆非法），模擬請求期拓樸漂移。
    yaml_copy.write_text("::: not valid yaml :::\n", encoding="utf-8")

    for path in ("/sessions", "/resources/available"):
        response = client.get(path)
        assert response.status_code == 503, path
        body = response.json()
        # 回結構化錯誤、type 為環境性拓樸子類名（非 InternalError）。
        assert body["error"]["type"] != "InternalError", path
        assert "Traceback" not in response.text, path


def test_topology_drift_missing_file_read_returns_503(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """啟動後 YAML 被刪除 → 讀端點回 503（TopologyFileError，Issue 1）。"""
    app, yaml_copy, _settings = _build_app_on_writable_topology(
        three_tier_path, tmp_path
    )
    client = TestClient(app, raise_server_exceptions=False)

    yaml_copy.unlink()  # 啟動後刪檔，模擬檔案被移除。

    response = client.get("/sessions")
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "TopologyFileError"


def test_topology_drift_write_singleton_unaffected(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """YAML 改壞後，寫路徑吃的啟動單例 allocator 仍可正常分配（Issue 1 讀寫不對稱）。

    寫端點（POST/DELETE）共用啟動期建立的單例 allocator，不每請求重讀 YAML；
    故即使拓樸檔在啟動後被改壞，寫路徑的分配決策不受影響——這正是「讀端點 503、
    寫端點照常」不對稱的核心。此處以單例 allocator 直接驗證（不觸 signal 檔鎖
    的 live-server 寫路徑由 test_api.py 的 happy-path 覆蓋）。
    """
    app, yaml_copy, _settings = _build_app_on_writable_topology(
        three_tier_path, tmp_path
    )

    # 改壞 YAML 後，啟動單例仍握有啟動時的合法拓樸快照。
    yaml_copy.write_text("::: not valid yaml :::\n", encoding="utf-8")

    allocator = app.state.allocator
    session = allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    assert session.session_id == "s-0001"
    assert session.vlan == 100
    # 讀端點此刻會 503，但寫端點用的單例完全不受 YAML 漂移影響。


def test_topology_drift_business_errors_not_503(
    three_tier_path: Path, tmp_path: Path
) -> None:
    """非環境性錯誤不因 Issue 1 改動而誤判：404/409/400 維持原狀態碼。

    確認把五個環境性拓樸子類映射成 503 並未波及業務錯誤的語意（這些錯誤與
    請求期拓樸載入無關）。以 status_for 直接核對映射不變。
    """
    assert status_for(PathLookupError("x")) == 404
    assert status_for(SessionNotFoundError("x")) == 404
    assert status_for(HardConflictError("x")) == 409
    assert status_for(VlanPoolExhaustedError("x")) == 409
    # ConfigError（設定/參數非法，非拓樸漂移）維持 500。
    assert status_for(ConfigError("x")) == 500


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


# ----------------------------------------------------------------------
# write-path 非預期例外 → 結構化 500（Issue 3，主執行緒 live-server）
# ----------------------------------------------------------------------


def test_write_path_unexpected_500(faulty_write_live_server: "_LiveServer") -> None:
    """寫路徑注入非 TopologyError（RuntimeError）→ 真 HTTP 回結構化 500、無 traceback（Issue 3）。

    faulty_write_live_server 把啟動單例 allocate 換成拋 RuntimeError。於**真
    server**（子行程主執行緒）打 POST /sessions，確認：
      - 回 500 且 body 為結構化 ``{"error": {"type": "InternalError", ...}}``；
      - body 不外洩 Python traceback（NFR-3）。
    Starlette ServerErrorMiddleware 會把註冊於 Exception 的 handler 回應送回
    client，故真 HTTP 下亦回 500 JSON（消除原審查的推測盲點）。
    """
    response = faulty_write_live_server.client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "InternalError"
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_write_path_unexpected_500_lock_released(
    faulty_write_live_server: "_LiveServer",
) -> None:
    """寫路徑注入例外後鎖乾淨釋放：後續讀端點正常、無殘留持鎖凍結（Issue 3）。

    allocate 在 Phase 2 ``store.locked()`` 臨界區內拋例外時，context manager 的
    ``finally`` 仍會關 fd 釋放 flock。驗證方式：POST 觸 500 後，GET /sessions
    （讀路徑每請求重建 allocator、鎖內 reload）仍能在逾時前正常回應——若鎖未
    釋放，讀路徑取鎖會被凍結至 SIGALRM 逾時。此處以兩次 POST→500 夾一次
    成功 GET，確認事件迴圈與檔鎖未被卡死。
    """
    # 非預期例外經 ServerErrorMiddleware 回 500 後會 re-raise 供 server log，
    # uvicorn 可能據此關閉該條 keep-alive 連線；為避免 httpx 重用已關閉連線而
    # 觸發 ReadError（與鎖無關的連線重用假象），每個請求用 ``Connection: close``
    # 並以獨立 client 送出，確保每次都是全新連線。
    base_url = faulty_write_live_server.base_url
    # 純帳本寫入自 Design 1 起需 token：本測試自建 client（未套 fixture 預設
    # header），故 POST 需顯式帶 X-API-Token（值同 conftest._DEFAULT_TEST_TOKEN）。
    close_headers = {"Connection": "close"}
    write_headers = {"Connection": "close", "X-API-Token": "test-ledger-token"}

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        first = client.post(
            "/sessions",
            json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
            headers=write_headers,
        )
    assert first.status_code == 500

    # 鎖若未釋放，下面的請求會被凍結到 SIGALRM 逾時（10s）；正常應在 timeout
    # 內即時回應。GET 為讀路徑（每請求重建 allocator、鎖內 reload），能取鎖即
    # 證明前一次寫路徑例外後 flock 已乾淨釋放。
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        listing = client.get("/sessions", headers=close_headers)
    assert listing.status_code == 200
    assert listing.json()["count"] == 0  # allocate 失敗，帳本未落任何 session。

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        second = client.post(
            "/sessions",
            json={"generator_port": "ixia-1/2", "dut_port": "dut-A2:Eth0"},
            headers=write_headers,
        )
    assert second.status_code == 500
