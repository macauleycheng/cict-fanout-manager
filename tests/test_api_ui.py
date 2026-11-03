"""topo_manager Web UI 整合與資料層測試（設計 §9）。

以既有 ``api_client``（Starlette ``TestClient``）fixture 驗 Web UI 的靜態服務：
UI 路由唯讀、永不觸及 Phase 2 signal 檔鎖，故 TestClient 足矣（不需 live-server）。

涵蓋（設計 §9）：
  1. ``GET /`` → 200、``text/html``、含 marker ids/title。
  2. ``GET /static/app.js`` → 200、content-type 含 ``javascript``、body 含五個
     端點目標與 BFS tier 函式名、且不含限制長度的 split 慣用法。
  3. ``GET /static/app.css`` → 200、content-type 含 ``css``。
  4. ``GET /static/<缺檔>`` → 404（static 層，非 500、非業務錯誤）。
  5. OpenAPI 仍只列五個業務路徑、且**不**含 ``/`` 或 ``/static``（新增斷言）。
  6. 資料層 BFS mirror（AC-6）：以 2tier/3tier 的 ``GET /topology`` payload
     重建多源 BFS 分層，驗 generator 為 tier 0、DUT 為最大 tier、中間層序正確。

歷史（HISTORY）：
  - 0.5.0  Web UI 初版，建立靜態服務整合測試與 2tier/3tier BFS mirror 測試
           （設計 §9）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from fastapi.testclient import TestClient


# ----------------------------------------------------------------------
# 靜態服務整合測試（TestClient）
# ----------------------------------------------------------------------


def test_get_index_html(api_client: Tuple[TestClient, Path]) -> None:
    """GET / 回 200、text/html，且含測試所依賴的 marker ids/title（設計 §9.1、AC-2）。"""
    client, _ = api_client
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    body = response.text
    for marker in (
        "CICT Fanout Manager",
        "topology-svg",
        "sel-generator",
        "sel-dut",
        "session-list",
        "error-banner",
    ):
        assert marker in body, "index.html 缺 marker：{0}".format(marker)


def test_get_static_app_js(api_client: Tuple[TestClient, Path]) -> None:
    """GET /static/app.js 回 200、content-type 含 javascript，且結構符合設計（設計 §9.2）。"""
    client, _ = api_client
    response = client.get("/static/app.js")
    assert response.status_code == 200
    # starlette 以 stdlib mimetypes 解析 .js，可能是 application/javascript 或
    # text/javascript；兩者皆含子字串 javascript，單一檢查跨平台穩健（Finding 5）。
    assert "javascript" in response.headers["content-type"].lower()

    body = response.text
    # 五個端點目標都出現（結構性斷言：服出去的 JS 確實打五個既有端點，AC-7/AC-8）。
    assert '"/topology"' in body
    assert '"/resources/available"' in body
    assert '"/sessions"' in body
    assert '"POST"' in body
    assert '"DELETE"' in body
    # BFS tier 函式名存在、且不以 role 決定分層（AC-3/AC-6）。
    assert "computeTiers" in body
    # 不得出現限制長度的 split 慣用法（會回 undefined，Finding 1）。
    assert "split(':', 1)" not in body
    assert "split(':',1)" not in body
    assert 'split(":", 1)' not in body
    assert 'split(":",1)' not in body


def test_get_static_app_css(api_client: Tuple[TestClient, Path]) -> None:
    """GET /static/app.css 回 200、content-type 含 css（設計 §9.3、AC-3）。"""
    client, _ = api_client
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert "css" in response.headers["content-type"].lower()


def test_static_missing_is_404(api_client: Tuple[TestClient, Path]) -> None:
    """GET /static/<缺檔> 回 404（static 層，非 500、非業務錯誤；設計 §9.5、§11）。"""
    client, _ = api_client
    response = client.get("/static/does-not-exist.js")
    assert response.status_code == 404


def test_openapi_excludes_ui_paths(api_client: Tuple[TestClient, Path]) -> None:
    """OpenAPI 仍只列五個業務路徑，且不含 / 或 /static（**新增斷言**，設計 §9.4、AC-4）。

    本斷言為 Web UI 新增（非既有非回歸斷言）：確認掛上 UI 後 OpenAPI 文件未被
    污染（``GET /`` 以 include_in_schema=False 註冊、mount 本就不入 schema）。
    """
    client, _ = api_client
    response = client.get("/openapi.json")
    assert response.status_code == 200

    paths = set(response.json()["paths"].keys())
    # 五個業務路徑仍在。
    assert "/topology" in paths
    assert "/resources/available" in paths
    assert "/sessions" in paths
    assert "/sessions/{session_id}" in paths
    # UI 路徑不得出現在 OpenAPI。
    assert "/" not in paths
    assert "/static" not in paths
    assert not any(p.startswith("/static") for p in paths)


# ----------------------------------------------------------------------
# 資料層 BFS mirror 測試（AC-6）：以純 Python 複刻 app.js 的多源 BFS 分層，
# 驗其對 2tier/3tier payload 的分層結果與設計一致（守 JS 實作的契約）。
# ----------------------------------------------------------------------


def _device_tiers_from_topology(topology: dict) -> Dict[str, float]:
    """以 GET /topology payload 重建 app.js 的多源 BFS 裝置分層（純 Python mirror）。

    與 app.js computeTiers 等價：generator 裝置為 tier 0，沿無向裝置鄰接做
    多源 BFS，每台裝置 tier = 距最近 generator 的 BFS 距離；不可達者為無限大。
    不讀 role（與 JS 一致）。

    參數：
      - topology：``GET /topology`` 的 JSON payload（dict）。

    回傳：
      - 裝置名 → tier（int；不可達者為 ``float("inf")``）的 dict。
    """
    device_types: Dict[str, str] = {}
    for generator in topology["generators"]:
        device_types[generator["name"]] = "gen"
    for fanout in topology["fanouts"]:
        device_types[fanout["name"]] = "fanout"
    for rack in topology["racks"]:
        for dut in rack["duts"]:
            device_types[dut["name"]] = "dut"

    adjacency: Dict[str, set] = {name: set() for name in device_types}
    for link in topology["links"]:
        a_dev = link["a"]["dev"]
        b_dev = link["b"]["dev"]
        if a_dev == b_dev:
            continue
        adjacency[a_dev].add(b_dev)
        adjacency[b_dev].add(a_dev)

    tiers: Dict[str, float] = {name: float("inf") for name in device_types}
    queue: List[str] = []
    for name, dtype in device_types.items():
        if dtype == "gen":
            tiers[name] = 0
            queue.append(name)

    head = 0
    while head < len(queue):
        current = queue[head]
        head += 1
        for neighbor in adjacency[current]:
            if tiers[neighbor] == float("inf"):
                tiers[neighbor] = tiers[current] + 1
                queue.append(neighbor)

    return tiers


def test_bfs_tiers_two_tier(
    api_client: Tuple[TestClient, Path], two_tier_path: Path
) -> None:
    """2tier：generator tier 0、core→1、leaf→2、DUT 為最大 tier（設計 §9、AC-6）。

    api_client fixture 綁 3tier；本測試另以 2tier YAML 建獨立 app 取其 topology。
    """
    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    settings = Settings(
        topology_path=str(two_tier_path),
        state_path="/tmp/ui-bfs-2tier/allocations.json",
        vlan_min=100,
        vlan_max=3000,
    )
    client = TestClient(create_app(settings))
    topology = client.get("/topology").json()

    tiers = _device_tiers_from_topology(topology)

    assert tiers["ixia-1"] == 0
    assert tiers["trex-1"] == 0
    assert tiers["core-1"] == 1
    assert tiers["leaf-1-1"] == 2
    assert tiers["leaf-2-1"] == 2
    # DUT 為最大 tier。
    dut_names = [
        dut["name"]
        for rack in topology["racks"]
        for dut in rack["duts"]
    ]
    max_tier = max(tiers.values())
    for name in dut_names:
        assert tiers[name] == max_tier == 3


def test_bfs_tiers_three_tier(api_client: Tuple[TestClient, Path]) -> None:
    """3tier：generator tier 0、spine→1、agg→2、leaf→3、DUT 為最大 tier（設計 §9、AC-6）。

    api_client fixture 本就綁 3tier，直接取其 GET /topology。
    """
    client, _ = api_client
    topology = client.get("/topology").json()

    tiers = _device_tiers_from_topology(topology)

    assert tiers["ixia-1"] == 0
    assert tiers["trex-1"] == 0
    assert tiers["spine-1"] == 1
    assert tiers["agg-1"] == 2
    assert tiers["agg-2"] == 2
    assert tiers["leaf-1-1"] == 3
    assert tiers["leaf-2-1"] == 3

    dut_names = [
        dut["name"]
        for rack in topology["racks"]
        for dut in rack["duts"]
    ]
    max_tier = max(tiers.values())
    for name in dut_names:
        assert tiers[name] == max_tier == 4
