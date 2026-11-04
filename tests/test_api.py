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
  - 0.4.1  Phase 3 審查：Issue 6 多 session（含多段/共用 trunk）的 access port
           佔用/可用斷言；Issue 4 fullscale 效能斷言改為實際守門（<2s 裕度，
           設計 §9 期望 <1s）。

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


# fullscale GET /resources/available 的效能守門預算（秒，Issue 4）。設計 §9
# 期望 <1s；取 2s 作 CI 裕度（避免抖動誤報）的同時仍能抓到明顯回歸。
_FULLSCALE_AVAILABLE_BUDGET_S = 2.0


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
    assert body["info"]["version"] == "0.5.0"


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


def test_available_multi_session_trunk_not_occupied(
    live_server: _LiveServer,
) -> None:
    """多 session（含多段 trunk 路徑）下：trunk 端不被誤標佔用、access port 正確佔用（Issue 6）。

    3tier 的 generator→DUT 路徑經 spine→agg→leaf 多段 trunk。兩筆走**同一 rack**
    （共用 agg-1/leaf-1-1 的 trunk 段）的 session：
      - 兩端 access port（generator 端 ``<gen>:<port>``、DUT 端 ``dut:port``）
        應各自進 occupied 且 owner 為對應 session；
      - 共用的 trunk 端節點（``spine-1:7``、``agg-1:1``、``agg-1:2``、
        ``leaf-1-1:1``）**不得**出現在 occupied（trunk 可被多 session 共用，
        這正是 _occupied_access_ports 扣除 trunk 端的目的）。
    """
    client = live_server.client

    # 兩筆同 rack（leaf-1-1）的 session，路徑共用 agg-1↔leaf-1-1 trunk。
    first = client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    assert first.status_code == 201
    first_id = first.json()["session_id"]

    second = client.post(
        "/sessions",
        json={"generator_port": "ixia-1/2", "dut_port": "dut-A2:Eth0"},
    )
    assert second.status_code == 201
    second_id = second.json()["session_id"]

    available = client.get("/resources/available").json()
    occupied = available["occupied_ports"]

    # 兩筆各自的兩端 access port 被正確標佔用、owner 正確。
    assert occupied.get("ixia-1:ixia-1/1") == first_id
    assert occupied.get("dut-A1:Eth0") == first_id
    assert occupied.get("ixia-1:ixia-1/2") == second_id
    assert occupied.get("dut-A2:Eth0") == second_id

    # 共用的 trunk 端節點不得被誤標為佔用（可被多 session 共用）。
    for trunk_node in ("spine-1:7", "agg-1:1", "agg-1:2", "leaf-1-1:1"):
        assert trunk_node not in occupied, trunk_node

    # 被佔用的 DUT access port 不再出現在可用清單。
    dut_nodes = {item["node"] for item in available["available_dut_ports"]}
    assert "dut-A1:Eth0" not in dut_nodes
    assert "dut-A2:Eth0" not in dut_nodes
    # 共用 trunk 端（非 generator/DUT access port）本就不在 gen/dut 可用清單中，
    # 但未被標佔用——以 occupied 不含它們即足以證明不被誤佔（上方已斷言）。

    # 另一 rack（rack-2，leaf-2-1）的 DUT 仍完全可用（未受 rack-1 佔用影響）。
    assert "dut-B1:Eth0" in dut_nodes
    assert "dut-B4:Eth1" in dut_nodes


def test_available_cross_rack_sessions_occupancy(
    live_server: _LiveServer,
) -> None:
    """跨 rack 多 session：各 rack 的 trunk 端互不干擾、僅自身 access port 佔用（Issue 6）。

    一筆走 rack-1（agg-1/leaf-1-1）、一筆走 rack-2（agg-2/leaf-2-1）。兩條路徑
    不共用 trunk，但同經 spine-1。驗證：各自的 access port 佔用正確、兩條路徑的
    所有 trunk 端皆不被標佔用、且 occupied 恰為 4 個 access port（兩筆各兩端）。
    """
    client = live_server.client

    first = client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    assert first.status_code == 201
    first_id = first.json()["session_id"]

    second = client.post(
        "/sessions",
        json={"generator_port": "trex-1/0", "dut_port": "dut-B1:Eth0"},
    )
    assert second.status_code == 201
    second_id = second.json()["session_id"]

    occupied = client.get("/resources/available").json()["occupied_ports"]

    # access port = physical_ports − trunk 端。兩筆各自的 access port（含路徑
    # 中間的非 trunk 端節點，如 spine 面向 generator 的 port、leaf 面向 DUT 的
    # port）皆正確標佔用、owner 正確；occupied 恰為這兩組 access port 的聯集，
    # 不含任何 trunk 端。
    assert occupied == {
        # rack-1（first）：generator 端、spine 面向 gen 的 port、leaf 面向 DUT
        # 的 port、DUT 端。
        "ixia-1:ixia-1/1": first_id,
        "spine-1:1": first_id,
        "leaf-1-1:11": first_id,
        "dut-A1:Eth0": first_id,
        # rack-2（second）。
        "trex-1:trex-1/0": second_id,
        "spine-1:3": second_id,
        "leaf-2-1:11": second_id,
        "dut-B1:Eth0": second_id,
    }

    # 兩條路徑各自的 trunk 端皆不被標佔用（上方 == 已排除，這裡再明示守門）。
    for trunk_node in (
        "spine-1:7",
        "agg-1:1",
        "agg-1:2",
        "leaf-1-1:1",
        "spine-1:8",
        "agg-2:1",
        "agg-2:2",
        "leaf-2-1:1",
    ):
        assert trunk_node not in occupied, trunk_node


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
    # 效能守門（Issue 4）：fullscale（12 gen × 1152 tg）GET /resources/available
    # 每請求重建 allocator（load_topology + 建圖 + 帳本還原）是熱點。設計 §9
    # 門檻為 <1s；本機實測約 0.7s。此處斷言 < _FULLSCALE_AVAILABLE_BUDGET_S
    # （2s，帶裕度避免 CI 抖動誤報），若核心日後明顯變慢，CI 會抓到回歸。
    assert elapsed < _FULLSCALE_AVAILABLE_BUDGET_S, (
        "fullscale GET /resources/available 耗時 {0:.3f}s 超過守門門檻 "
        "{1:.1f}s（設計 §9 期望 <1s）".format(elapsed, _FULLSCALE_AVAILABLE_BUDGET_S)
    )

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


# ----------------------------------------------------------------------
# 設備下發 REST 的 OpenAPI 契約（預設關閉時與既有逐位元一致）
# ----------------------------------------------------------------------
_PUSH_PATHS = [
    ("/sessions/{session_id}/apply", "post"),
    ("/sessions/{session_id}/verify", "get"),
    ("/fanouts/setup", "post"),
    ("/interconnect/sessions/{session_id}/apply", "post"),
    ("/interconnect/sessions/{session_id}/verify", "get"),
    ("/interconnect/park", "post"),
    ("/interconnect/sessions/{session_id}/release-device", "post"),
]


def test_openapi_unchanged_when_push_disabled(
    api_client: Tuple[TestClient, Path],
) -> None:
    """未啟用：版本 0.5.0、路徑集合不含 7 個下發路徑、description 仍含「僅限 CLI」。"""
    client, _ = api_client
    body = client.get("/openapi.json").json()
    assert body["info"]["version"] == "0.5.0"
    assert "僅限 CLI" in body["info"]["description"]
    assert "X-API-Token" not in body["info"]["description"]
    for path, _method in _PUSH_PATHS:
        assert path not in body["paths"]


def test_openapi_when_push_enabled(three_tier_path: Path, tmp_path: Path) -> None:
    """啟用：版本 0.6.0、含 7 個下發路徑且各自文件化 401／404／409／502／504。"""
    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        enable_device_push=True,
        api_token="t",
    )
    body = TestClient(create_app(settings)).get("/openapi.json").json()
    assert body["info"]["version"] == "0.6.0"
    assert "僅限 CLI" not in body["info"]["description"]
    assert "X-API-Token" in body["info"]["description"]
    for path, method in _PUSH_PATHS:
        responses = body["paths"][path][method]["responses"]
        for code in ("401", "404", "409", "502", "504"):
            assert code in responses, (path, code)
