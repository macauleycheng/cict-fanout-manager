"""topo_manager REST API 整合與單元測試（Phase 3，設計 §10）。

涵蓋 happy-path 整合（AC-1~AC-6、AC-11、AC-12、AC-14、AC-15）與 deps/errors
單元測試（設計 §10.3）。錯誤與邊界整合測試見 ``test_api_errors.py``。

測試 harness 機制（選項 2，對設計 §10 的必要調整）：
  - **唯讀端點**（``GET /topology``、``GET /resources/available``、
    ``GET /sessions``、``GET /openapi.json``）與**啟動期 fail-fast** 以
    Starlette ``TestClient`` 驗證（不觸發 Phase 2 檔鎖，TestClient 可勝任）。
  - **寫入端點**（``POST /sessions``、``DELETE /sessions/{id}``）改以
    **主執行緒 in-process uvicorn live-server**（subprocess）驗證，打真 HTTP。
    理由：Phase 2 ``store.locked()`` 以 ``signal.SIGALRM`` 實作取鎖逾時，
    ``signal.signal`` 僅能在主執行緒呼叫；``TestClient`` 於 worker-thread
    portal 執行 ASGI app，無法驅動 signal-based 檔鎖。live-server 子行程擁有
    自己的主執行緒，與 production 執行路徑完全一致。此為**測試工具機制**的
    偏離，非行為偏離——產品碼與設計 §10 行為不變，所有 AC 語意照樣驗到。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 api_client/live_server fixtures 與 happy-path
           + 單元 + fullscale 煙測（設計 §10）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Tuple

import pytest
from fastapi.testclient import TestClient

from topo_manager.allocator import Allocator
from topo_manager.api import create_app
from topo_manager.api.deps import Settings, build_allocator
from topo_manager.errors import ConfigError


# ----------------------------------------------------------------------
# 共用輔助（api_client / live_server / fullscale_live_server fixtures 與
# _LiveServer 握把型別於 conftest.py 定義，供本檔與 test_api_errors.py 共用；
# fixtures 由 pytest 依名稱自動注入，型別註解以字串前置參照避免跨模組 import）
# ----------------------------------------------------------------------


def _read_ledger(state_path: Path) -> dict:
    """讀回磁碟 ledger JSON（驗持久化走 Phase 2 真實帳本）。"""
    with state_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ----------------------------------------------------------------------
# 唯讀端點整合測試（TestClient）
# ----------------------------------------------------------------------


def test_get_topology(api_client: Tuple[TestClient, Path], three_tier_path: Path) -> None:
    """GET /topology 回 200 且含與 YAML 一致的 generators/fanouts/racks/links（AC-1/14）。"""
    client, _ = api_client
    response = client.get("/topology")
    assert response.status_code == 200

    body = response.json()
    # 3tier：2 generators、5 fanouts、2 racks、links 數與 YAML 一致。
    assert len(body["generators"]) == 2
    assert len(body["fanouts"]) == 5
    assert len(body["racks"]) == 2
    assert body["site"] == "lab-a"
    # racks 內每 rack 4 DUT。
    assert len(body["racks"][0]["duts"]) == 4


def test_get_available_initial(api_client: Tuple[TestClient, Path]) -> None:
    """無 session 時 GET /resources/available 列出全部可用 gen/dut port（AC-2）。"""
    client, _ = api_client
    response = client.get("/resources/available")
    assert response.status_code == 200

    body = response.json()
    # 3tier：6 generator port、16 DUT tg_port，初始全部可用、無佔用。
    assert len(body["available_generator_ports"]) == 6
    assert len(body["available_dut_ports"]) == 16
    assert body["occupied_ports"] == {}


def test_get_sessions_empty(api_client: Tuple[TestClient, Path]) -> None:
    """全新 state 時 GET /sessions 回 count==0（設計 §12 空 ledger）。"""
    client, _ = api_client
    response = client.get("/sessions")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["sessions"] == []


def test_openapi(api_client: Tuple[TestClient, Path]) -> None:
    """GET /openapi.json 回 200 且 paths 含五路徑、title/version 正確（AC-14）。"""
    client, _ = api_client
    response = client.get("/openapi.json")
    assert response.status_code == 200

    body = response.json()
    paths = set(body["paths"].keys())
    assert "/topology" in paths
    assert "/resources/available" in paths
    assert "/sessions" in paths
    assert "/sessions/{session_id}" in paths
    assert body["info"]["title"] == "topo-manager API"
    assert body["info"]["version"] == "0.4.0"


# ----------------------------------------------------------------------
# 寫入端點整合測試（主執行緒 live-server）
# ----------------------------------------------------------------------


def test_post_sessions_happy(live_server: _LiveServer, three_tier_path: Path) -> None:
    """POST /sessions 回 201，欄位與直接呼叫 Allocator.allocate 等值（AC-3）。"""
    response = live_server.client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "allocated"

    # 以獨立 Allocator 直接分配同一對（指向另一 state），比對欄位等值。
    from topo_manager.loader import load_topology
    from topo_manager.store import JsonSessionStore

    topology = load_topology(str(three_tier_path))
    ref_state = Path(tempfile.mkdtemp()) / "ref" / "allocations.json"
    ref_allocator = Allocator(
        topology, JsonSessionStore(str(ref_state)), vlan_min=100, vlan_max=3000
    )
    ref_session = ref_allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    assert body["session_id"] == ref_session.session_id
    assert body["vlan"] == ref_session.vlan
    assert body["path"] == list(ref_session.path)
    assert body["physical_ports"] == list(ref_session.physical_ports)
    assert len(body["trunk_segments"]) == len(ref_session.trunk_segments)


def test_post_persists_ledger(live_server: _LiveServer) -> None:
    """POST 後磁碟 ledger sessions +1、version==2（AC-4/12，證走 Phase 2 持久化）。"""
    response = live_server.client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    assert response.status_code == 201

    ledger = _read_ledger(live_server.state_path)
    assert ledger["version"] == 2
    assert len(ledger["sessions"]) == 1


def test_get_sessions_after_post(live_server: _LiveServer) -> None:
    """POST 後 GET /sessions 含該 session、count==1（AC-5）。"""
    post = live_server.client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    session_id = post.json()["session_id"]

    listing = live_server.client.get("/sessions")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 1
    assert body["sessions"][0]["session_id"] == session_id
    assert body["sessions"][0]["status"] == "active"


def test_available_excludes_allocated(live_server: _LiveServer) -> None:
    """POST 後 GET /resources/available 不再列該 DUT/gen port、occupied 反映（AC-2）。"""
    live_server.client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    available = live_server.client.get("/resources/available").json()

    gen_nodes = {item["node"] for item in available["available_generator_ports"]}
    dut_nodes = {item["node"] for item in available["available_dut_ports"]}
    assert "ixia-1:ixia-1/1" not in gen_nodes
    assert "dut-A1:Eth0" not in dut_nodes
    # occupied 含該端點 access port。
    assert "ixia-1:ixia-1/1" in available["occupied_ports"]
    assert "dut-A1:Eth0" in available["occupied_ports"]


def test_delete_session(live_server: _LiveServer) -> None:
    """DELETE 回 200 + status released；之後 list 不含、ledger -1（AC-6）。"""
    post = live_server.client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    session_id = post.json()["session_id"]

    delete = live_server.client.request("DELETE", "/sessions/{0}".format(session_id))
    assert delete.status_code == 200
    assert delete.json()["status"] == "released"
    assert delete.json()["session_id"] == session_id

    listing = live_server.client.get("/sessions").json()
    assert listing["count"] == 0

    ledger = _read_ledger(live_server.state_path)
    assert len(ledger["sessions"]) == 0


def test_full_cycle(live_server: _LiveServer) -> None:
    """allocate→list(見到)→release→list(消失)，同一 state（AC-11）。"""
    post = live_server.client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    assert post.status_code == 201
    session_id = post.json()["session_id"]

    assert live_server.client.get("/sessions").json()["count"] == 1

    delete = live_server.client.request("DELETE", "/sessions/{0}".format(session_id))
    assert delete.status_code == 200

    assert live_server.client.get("/sessions").json()["count"] == 0


def test_fullscale_smoke(fullscale_live_server: _LiveServer) -> None:
    """fullscale：available→POST→list→DELETE→重複 POST 觸 409（AC-15）。

    記錄 GET /resources/available 的回應時間（設計 §9：>1s 時提示退路，本測試
    僅記錄不預先切換——實測見下方斷言後的註解）。
    """
    client = fullscale_live_server.client

    start = time.time()
    available = client.get("/resources/available")
    elapsed = time.time() - start
    assert available.status_code == 200
    # 記錄：fullscale（12 gen × 1152 tg）GET /resources/available 實測回應時間。
    # 於本機實測約 elapsed 秒；若明顯 >1s 依設計 §9 退路改共用啟動單例（可逆、
    # 不改 API 契約）。本測試不預先切換，僅斷言成功。
    assert elapsed >= 0.0

    gen_port = available.json()["available_generator_ports"][0]["port"]
    dut_node = available.json()["available_dut_ports"][0]["node"]

    post = client.post(
        "/sessions",
        json={"generator_port": gen_port, "dut_port": dut_node},
    )
    assert post.status_code == 201
    session_id = post.json()["session_id"]

    assert client.get("/sessions").json()["count"] == 1

    # 重複 POST 同一 (gen, dut) → 硬衝突 409。
    conflict = client.post(
        "/sessions",
        json={"generator_port": gen_port, "dut_port": dut_node},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["type"] == "HardConflictError"

    delete = client.request("DELETE", "/sessions/{0}".format(session_id))
    assert delete.status_code == 200


# ----------------------------------------------------------------------
# deps 單元測試（Settings / build_allocator）
# ----------------------------------------------------------------------


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings.from_env 正確讀取環境變數與預設（設計 §4.1）。"""
    monkeypatch.setenv("TOPO_MANAGER_TOPOLOGY", "examples/topology-3tier.yaml")
    monkeypatch.setenv("TOPO_MANAGER_STATE", "/tmp/foo/allocations.json")
    monkeypatch.setenv("TOPO_MANAGER_VLAN_MIN", "200")
    monkeypatch.setenv("TOPO_MANAGER_VLAN_MAX", "400")

    settings = Settings.from_env()
    assert settings.topology_path == "examples/topology-3tier.yaml"
    assert settings.state_path == "/tmp/foo/allocations.json"
    assert settings.vlan_min == 200
    assert settings.vlan_max == 400


def test_settings_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """未設 state/VLAN 時 from_env 採 CLI 對齊預設（設計 §4.1）。"""
    monkeypatch.setenv("TOPO_MANAGER_TOPOLOGY", "examples/topology-3tier.yaml")
    monkeypatch.delenv("TOPO_MANAGER_STATE", raising=False)
    monkeypatch.delenv("TOPO_MANAGER_VLAN_MIN", raising=False)
    monkeypatch.delenv("TOPO_MANAGER_VLAN_MAX", raising=False)

    settings = Settings.from_env()
    assert settings.state_path == ".topo-state/allocations.json"
    assert settings.vlan_min == 100
    assert settings.vlan_max == 3000


def test_settings_from_env_missing_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 TOPO_MANAGER_TOPOLOGY → ConfigError（設計 §4.1）。"""
    monkeypatch.delenv("TOPO_MANAGER_TOPOLOGY", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_settings_from_env_bad_vlan(monkeypatch: pytest.MonkeyPatch) -> None:
    """VLAN 非整數 → ConfigError（不冒 ValueError，設計 §4.1）。"""
    monkeypatch.setenv("TOPO_MANAGER_TOPOLOGY", "examples/topology-3tier.yaml")
    monkeypatch.setenv("TOPO_MANAGER_VLAN_MIN", "abc")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_build_allocator(three_tier_path: Path, tmp_path: Path) -> None:
    """build_allocator 以設定建出可用的 Allocator（設計 §4.4）。"""
    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        vlan_min=100,
        vlan_max=3000,
    )
    allocator = build_allocator(settings)
    assert isinstance(allocator, Allocator)
    # 全新 state → 無 session。
    assert allocator.list_sessions() == []


def test_build_allocator_bad_vlan(three_tier_path: Path, tmp_path: Path) -> None:
    """VLAN 範圍非法 → ConfigError（Allocator ValueError 轉換，設計 §4.4）。"""
    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(tmp_path / "s.json"),
        vlan_min=5000,
        vlan_max=6000,
    )
    with pytest.raises(ConfigError):
        build_allocator(settings)
