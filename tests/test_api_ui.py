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
  7. 前端純函式 mirror（審查 Issue 6/7）：以真實 session payload 複刻 app.js 的
     ``devOf``/``portOf``、``computeHighlightedEdgeKeys``、``edgePortsOf``，並把
     ``edgePortsOf`` 綁到後端 ``routes._occupied_access_ports`` 的 access-port
     推導上（契約測試），防兩份平行實作漂移。

歷史（HISTORY）：
  - 0.5.0  Web UI 初版，建立靜態服務整合測試與 2tier/3tier BFS mirror 測試
           （設計 §9）。
  - 0.5.1  審查 Issue 6/7：補 devOf/portOf、computeHighlightedEdgeKeys、
           edgePortsOf 的 Python mirror 測試，並與後端 _occupied_access_ports
           對照固定契約（用真實 session payload 複刻 JS 邏輯並斷言）。
  - 0.6.0  Web UI 互聯頁籤：補頁籤版面 marker 斷言（打流 / 互聯容器、頁籤列、
           互聯清單與建立表單欄位）、ic.js / tabs.js / ic.css 靜態資源回應與
           結構性斷言（六個互聯端點目標、字面 POST/DELETE、純函式名、禁用
           split 慣用法），以及 ic.js 兩個純函式（icSessionMatchesFilter /
           buildSessionDrawLinks）的 Python-mirror 回歸測試（輸入採在
           topology-interconnect-small.yaml 實建的真實 session / summary 形狀，
           含 group 缺席 → [] 的 not-found 契約）。OpenAPI 補互聯 7 個 path key
           的成員資格斷言，/ 與 /static* 仍不入 paths。

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


def test_get_index_html_has_tab_markers(api_client: Tuple[TestClient, Path]) -> None:
    """GET / 含頁籤版面 marker：打流 / 互聯容器、頁籤列、互聯清單與建立表單欄位（AC-1）。

    打流 marker 由 ``test_get_index_html`` 守護；本測試補互聯頁籤新增的 marker，
    並確認兩個 .tab-panel 容器與頁籤列同時存在（單頁已改為頁籤式）。
    """
    client, _ = api_client
    body = client.get("/").text
    for marker in (
        "tabbar",               # 頁籤列
        "tab-traffic",          # 打流頁籤容器
        "tab-interconnect",     # 互聯頁籤容器
        "ic-topology",          # 互聯拓樸區（每組一張小圖）
        "ic-session-list",      # 互聯 session 清單
        "ic-session-filter",    # 互聯清單 filter
        "ic-create-panel",      # 互聯建立表單
        "ic-sel-group",         # 建立表單：group 下拉
        "ic-sel-duts",          # 建立表單：DUT 多選（profile 模式）
        "ic-sel-profile",       # 建立表單：profile 下拉
        "ic-mode-pairs",        # 建立表單：pairs 模式切換
        "ic-btn-allocate",      # 建立按鈕
        "ic-error-banner",      # 互聯錯誤 banner
    ):
        assert marker in body, "index.html 缺互聯 marker：{0}".format(marker)
    # 打流頁籤既有 marker 仍在（頁籤化後不得回歸）。
    assert "topology-svg" in body
    assert "session-list" in body


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


def test_get_static_ic_js(api_client: Tuple[TestClient, Path]) -> None:
    """GET /static/ic.js 回 200、javascript，且結構符合互聯設計（設計 §7、AC-2/AC-3）。"""
    client, _ = api_client
    response = client.get("/static/ic.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"].lower()

    body = response.text
    # 六個互聯端點目標字串都出現（服出去的 JS 確實打既有互聯端點）。
    assert '"/interconnect/summary"' in body
    assert '"/interconnect/resources/available"' in body
    assert '"/interconnect/sessions"' in body
    assert '"/interconnect/plan"' in body
    # 字面 HTTP method（AC-3：由 §4.6 的字面 method call site 保證）。
    assert '"POST"' in body
    assert '"DELETE"' in body
    # 可抽出的純函式名存在（供 Python-mirror 測試對照）。
    assert "buildSessionDrawLinks" in body
    assert "icSessionMatchesFilter" in body
    # 不得呼叫任何會連設備的端點目標（apply / verify / park / release-device；
    # 這些是帶 token 的 REST 或 CLI 的事，不出現在 Web UI 呼叫中）。亦不呼叫
    # /interconnect/plan/park（park 計畫，Web UI 不用）或處理 X-API-Token。
    assert "/interconnect/apply" not in body
    assert "/interconnect/verify" not in body
    assert "/interconnect/plan/park" not in body
    assert "/interconnect/release-device" not in body
    # 不設定 X-API-Token header（Web UI 不處理 token；bare 字樣僅出現在註解）。
    assert '"X-API-Token"' not in body
    assert "setRequestHeader" not in body
    # 不得出現限制長度的 split 慣用法（JS limit 會丟掉 port 段，回 undefined）。
    assert "split(':', 1)" not in body
    assert "split(':',1)" not in body
    assert 'split(":", 1)' not in body
    assert 'split(":",1)' not in body


def test_get_static_tabs_js(api_client: Tuple[TestClient, Path]) -> None:
    """GET /static/tabs.js 回 200、content-type 含 javascript（設計 §7）。"""
    client, _ = api_client
    response = client.get("/static/tabs.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"].lower()
    assert "activateTab" in response.text


def test_get_static_ic_css(api_client: Tuple[TestClient, Path]) -> None:
    """GET /static/ic.css 回 200、content-type 含 css（設計 §7）。"""
    client, _ = api_client
    response = client.get("/static/ic.css")
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
    # 互聯 7 個 path key 仍在（Web UI 只讀既有端點，不新增後端端點，AC-4）。
    ic_paths = {p for p in paths if p.startswith("/interconnect")}
    assert len(ic_paths) == 7
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


# ----------------------------------------------------------------------
# 前端純函式 mirror 測試（審查 Issue 6/7）：以純 Python 複刻 app.js 的三個
# 純函式（devOf/portOf、computeHighlightedEdgeKeys、edgePortsOf），用真實
# session payload 斷言其輸出，把「前端與後端對同一語意的兩份實作」綁在一起。
# ----------------------------------------------------------------------


def _dev_of(node_id: str) -> str:
    """app.js devOf 的 Python mirror：取首個 ':' 前的 device 段。

    與 app.js 一致：找不到 ':' 時回整串（對齊 Python ``split(':', 1)[0]``），
    port 段可含 '/'、不含 ':'。

    參數：
      - node_id：節點 id（``"<dev>:<port>"`` 或裸名）。

    回傳：
      - device 段字串。
    """
    idx = node_id.find(":")
    if idx == -1:
        return node_id
    return node_id[:idx]


def _port_of(node_id: str) -> str:
    """app.js portOf 的 Python mirror：取首個 ':' 後的 port 段（可含 '/'）。

    參數：
      - node_id：節點 id（``"<dev>:<port>"``）。

    回傳：
      - port 段字串（無 ':' 時回整串，與 JS slice(indexOf+1) 等價）。
    """
    idx = node_id.find(":")
    return node_id[idx + 1:]


def _edge_key(node_a: str, node_b: str) -> str:
    """app.js edgeKey 的 Python mirror：兩節點 id 排序後以 '|' 連接（無向）。"""
    return "|".join(sorted([node_a, node_b]))


def _compute_highlighted_edge_keys(path):
    """app.js computeHighlightedEdgeKeys 的 Python mirror。

    沿 path 相鄰節點配對；同一 device 的連續 hop（設備內佈線，無跨設備邊）
    跳過；其餘以 ``_edge_key`` 產生無向邊鍵集合。

    參數：
      - path：session 的 ``path`` 節點 id 序列。

    回傳：
      - 高亮邊鍵的 set。
    """
    keys = set()
    for i in range(len(path) - 1):
        a = path[i]
        b = path[i + 1]
        if _dev_of(a) == _dev_of(b):
            continue
        keys.add(_edge_key(a, b))
    return keys


def _edge_ports_of(session: dict):
    """app.js edgePortsOf 的 Python mirror：physical_ports 扣除所有 trunk 兩端。

    參數：
      - session：``GET /sessions`` 形狀的 session dict（具 ``physical_ports``
        與 ``trunk_segments[].a_node/b_node``）。

    回傳：
      - access port 節點 id 的 list（保序，與 JS 一致）。
    """
    trunk_ends = set()
    for segment in session.get("trunk_segments", []):
        trunk_ends.add(segment["a_node"])
        trunk_ends.add(segment["b_node"])
    result = []
    for node in session.get("physical_ports", []):
        if node not in trunk_ends:
            result.append(node)
    return result


def _real_session_payloads(topology_path: Path, tmp_path: Path):
    """以 Allocator 在真實拓樸上建數筆 session，回 ``GET /sessions`` 形狀的 dict 清單。

    直接走 Phase 2 Allocator（與 test_allocator 相同手法）再經公開
    ``SessionResponse`` 平鋪，取得與 ``GET /sessions`` 等值的真實 payload
    （含跨 spine/agg 的 trunk 段與設備內 hop），供前端純函式 mirror 斷言。

    參數：
      - topology_path：範例拓樸 YAML 路徑。
      - tmp_path：pytest tmp 目錄（放獨立狀態檔）。

    回傳：
      - (payloads, sessions) 二元組：payloads 為 dict 清單（mirror 用），
        sessions 為 Phase 2 ``Session`` 清單（後端契約對照用）。
    """
    from topo_manager.allocator import Allocator
    from topo_manager.api.schemas import SessionResponse
    from topo_manager.loader import load_topology
    from topo_manager.store import JsonSessionStore

    store = JsonSessionStore(tmp_path / "mirror-state" / "allocations.json")
    allocator = Allocator(load_topology(topology_path), store)
    allocator.allocate("ixia-1/1", "dut-A1:Eth0")
    allocator.allocate("ixia-1/2", "dut-A2:Eth0")
    allocator.allocate("trex-1/0", "dut-B1:Eth0")

    sessions = allocator.list_sessions()
    payloads = [
        SessionResponse.from_session(session, "active").model_dump()
        for session in sessions
    ]
    return payloads, sessions


def test_dev_port_of_mirror() -> None:
    """devOf/portOf mirror：含 '/' 的 port 與裸名輸入皆對齊 JS 行為（Issue 4/6）。"""
    # port 含 '/'：只在首個 ':' 切，port 段保留 '/'。
    assert _dev_of("ixia-1:ixia-1/1") == "ixia-1"
    assert _port_of("ixia-1:ixia-1/1") == "ixia-1/1"
    # 一般節點。
    assert _dev_of("leaf-1-1:11") == "leaf-1-1"
    assert _port_of("leaf-1-1:11") == "11"
    # 裸名（無 ':'）：回整串（與 Python split(':', 1)[0] 對齊，非 slice(0,-1)）。
    assert _dev_of("ixia-1/1") == "ixia-1/1"


def test_highlighted_edge_keys_skips_intra_device_hops(
    api_client: Tuple[TestClient, Path], three_tier_path: Path, tmp_path: Path
) -> None:
    """computeHighlightedEdgeKeys mirror：設備內 hop 不產生跨設備邊（Issue 6）。

    用真實 3tier session payload，驗高亮邊鍵集合：
      - 不含任何 ``devOf(a) == devOf(b)`` 的設備內 hop；
      - 每個鍵對應一條跨設備邊，數量 = path 中跨設備相鄰對數。
    """
    payloads, _ = _real_session_payloads(three_tier_path, tmp_path)
    assert payloads, "預期至少建出一筆 session"

    for session in payloads:
        path = session["path"]
        keys = _compute_highlighted_edge_keys(path)

        # 以獨立方式重算「跨設備相鄰對」，與 mirror 結果比對。
        expected_pairs = []
        for i in range(len(path) - 1):
            if _dev_of(path[i]) != _dev_of(path[i + 1]):
                expected_pairs.append((path[i], path[i + 1]))

        assert len(keys) == len(expected_pairs)
        for a, b in expected_pairs:
            assert _edge_key(a, b) in keys
            # 高亮鍵兩端必為不同 device（設備內 hop 已被跳過）。
            dev_a, dev_b = _edge_key(a, b).split("|")
            assert _dev_of(dev_a) != _dev_of(dev_b)


def test_edge_ports_of_matches_backend_occupied_access_ports(
    api_client: Tuple[TestClient, Path], three_tier_path: Path, tmp_path: Path
) -> None:
    """edgePortsOf mirror == 後端 _occupied_access_ports 推導（契約測試，Issue 6/7）。

    前端 ``edgePortsOf``（physical_ports 扣 trunk 兩端）與後端
    ``routes._occupied_access_ports`` 是對同一「access port」語意的兩份平行實作。
    以真實 session payload 分別跑兩者，斷言：
      - 單筆 session 的前端 access-port 集合 == 後端對該 session 推導出的 access
        port（其值在 occupied 對照中皆指向該 session_id）；
      - 全體 session 的前端 access-port 聯集 == 後端 ``occupied_ports`` 的鍵集。
    """
    from topo_manager.api.routes import _occupied_access_ports

    payloads, sessions = _real_session_payloads(three_tier_path, tmp_path)
    assert payloads, "預期至少建出一筆 session"

    backend_occupied = _occupied_access_ports(sessions)

    frontend_union = set()
    for session in payloads:
        frontend_access = _edge_ports_of(session)
        frontend_union.update(frontend_access)

        # 前端為該 session 推出的每個 access port，後端對照皆應指向同一 owner。
        for node in frontend_access:
            assert backend_occupied.get(node) == session["session_id"]

        # trunk 兩端節點不得出現在前端 access-port（已被扣除）。
        trunk_ends = set()
        for segment in session["trunk_segments"]:
            trunk_ends.add(segment["a_node"])
            trunk_ends.add(segment["b_node"])
        assert trunk_ends.isdisjoint(set(frontend_access))

    # 全體聯集與後端 occupied 鍵集一致（兩份平行實作綁定、防漂移）。
    assert frontend_union == set(backend_occupied.keys())


# ----------------------------------------------------------------------
# 互聯前端純函式 mirror 測試（AC-6）：以純 Python 複刻 ic.js 的兩個純函式
# （icSessionMatchesFilter / buildSessionDrawLinks），輸入採在
# topology-interconnect-small.yaml 上以 InterconnectAllocator 實建的真實
# session 與 GET /interconnect/summary 形狀，斷言其輸出並固定 not-found 契約。
# ----------------------------------------------------------------------


def _ic_session_matches_filter(session: dict, needle: str) -> bool:
    """ic.js icSessionMatchesFilter 的 Python mirror。

    把 session 的 session_id / group / profile 與每條 link 的 vlan / a_dut /
    a_port / b_dut / b_port 串接小寫後做子字串比對；needle 為空字串時命中。

    參數：
      - session：``GET /interconnect/sessions`` 形狀的 session dict。
      - needle：已 strip + lower 的 filter 字串。

    回傳：
      - 是否命中（bool）。
    """
    if not needle:
        return True
    parts = [
        session.get("session_id") or "",
        session.get("group") or "",
        session.get("profile") or "",
    ]
    for link in session.get("links", []):
        parts.append(str(link["vlan"]))
        parts.append(link.get("a_dut") or "")
        parts.append(link.get("a_port") or "")
        parts.append(link.get("b_dut") or "")
        parts.append(link.get("b_port") or "")
    return needle in " ".join(parts).lower()


def _ic_find_group_for_dut(summary: dict, dut_name: str):
    """ic.js icFindGroupForDut 的 Python mirror：找含該 DUT 名的組，無則 None。"""
    for group in summary.get("groups", []):
        for dut in group.get("duts", []):
            if dut["name"] == dut_name:
                return group
    return None


def _ic_label_for_dut(group: dict, dut_name: str):
    """ic.js icLabelForDut 的 Python mirror：在組內以 DUT 名查 label，無則 None。"""
    for dut in group.get("duts", []):
        if dut["name"] == dut_name:
            return dut["label"]
    return None


def _build_session_draw_links(session: dict, summary: dict):
    """ic.js buildSessionDrawLinks 的 Python mirror（含 not-found 契約）。

    先以第一條 link 的 a_dut（退用 session.group）在 summary 定位所屬組；比不到
    任何組即回 []（不丟例外）。組內找到後，對每條 link 查兩端 label（d0..d3），
    任一端查不到 label 時跳過該條 link。

    參數：
      - session：``GET /interconnect/sessions`` 形狀的 session dict。
      - summary：``GET /interconnect/summary`` 形狀的 dict。

    回傳：
      - ``[{groupName, aLabel, bLabel, aDut, aPort, bDut, bPort, vlan}, ...]``。
    """
    out = []
    if not session or not summary:
        return out
    links = session.get("links", [])
    group = None
    if links:
        group = _ic_find_group_for_dut(summary, links[0]["a_dut"])
    if group is None and session.get("group"):
        for candidate in summary.get("groups", []):
            if candidate["name"] == session["group"]:
                group = candidate
                break
    if group is None:
        return out
    for link in links:
        a_label = _ic_label_for_dut(group, link["a_dut"])
        b_label = _ic_label_for_dut(group, link["b_dut"])
        if a_label is None or b_label is None:
            continue
        out.append(
            {
                "groupName": group["name"],
                "aLabel": a_label,
                "bLabel": b_label,
                "aDut": link["a_dut"],
                "aPort": link["a_port"],
                "bDut": link["b_dut"],
                "bPort": link["b_port"],
                "vlan": link["vlan"],
            }
        )
    return out


def _real_ic_payloads(ic_topology_path: Path, tmp_path: Path):
    """以 InterconnectAllocator 在真實互聯拓樸上建兩筆 session，回真實形狀 payload。

    建一筆 duts+profile（mesh）與一筆明確 pairs，各經
    ``IcSessionResponse.from_ic_session(..., "active").model_dump()`` 平鋪成
    ``GET /interconnect/sessions`` 形狀；summary 以 REST 端點取真實形狀。

    參數：
      - ic_topology_path：互聯範例拓樸 YAML。
      - tmp_path：pytest tmp 目錄（放互聯帳本）。

    回傳：
      - (sessions, summary) 二元組：sessions 為 session dict 清單、summary 為
        ``GET /interconnect/summary`` 形狀 dict。
    """
    from fastapi.testclient import TestClient

    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings
    from topo_manager.api.ic_schemas import IcSessionResponse
    from topo_manager.interconnect.allocator import InterconnectAllocator
    from topo_manager.interconnect.store import IcSessionStore
    from topo_manager.loader import load_topology

    topology = load_topology(ic_topology_path)
    store = IcSessionStore(str(tmp_path / "ic-mirror" / "interconnect.json"))
    allocator = InterconnectAllocator(
        topology, store, vlan_min=1000, vlan_max=2000
    )
    sess_profile = allocator.allocate_profile(
        "A-g1", ["dut-A1", "dut-A2", "dut-A3"], "mesh"
    )
    sess_pairs = allocator.allocate_pairs(
        "B-g1", [("dut-B1:Eth8", "dut-B2:Eth8")]
    )
    sessions = [
        IcSessionResponse.from_ic_session(sess_profile, "active").model_dump(),
        IcSessionResponse.from_ic_session(sess_pairs, "active").model_dump(),
    ]

    settings = Settings(
        topology_path=str(ic_topology_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        ic_state_path=str(tmp_path / "state" / "interconnect.json"),
    )
    client = TestClient(create_app(settings))
    summary = client.get("/interconnect/summary").json()
    return sessions, summary


def test_ic_session_matches_filter_mirror(
    api_client: Tuple[TestClient, Path], ic_small_path: Path, tmp_path: Path
) -> None:
    """icSessionMatchesFilter mirror：以真實 session 形狀驗命中與不命中（AC-6）。"""
    sessions, _ = _real_ic_payloads(ic_small_path, tmp_path)
    assert sessions, "預期至少建出一筆互聯 session"

    first = sessions[0]
    # 空字串一律命中。
    assert _ic_session_matches_filter(first, "") is True
    # session_id / group / profile 子字串命中。
    assert _ic_session_matches_filter(first, first["session_id"].lower()) is True
    assert _ic_session_matches_filter(first, first["group"].lower()) is True
    assert _ic_session_matches_filter(first, first["profile"].lower()) is True
    # 第一條 link 的 VLAN 與 il_port 子字串命中。
    link0 = first["links"][0]
    assert _ic_session_matches_filter(first, str(link0["vlan"])) is True
    assert _ic_session_matches_filter(first, link0["a_port"].lower()) is True
    # 明顯不存在的字串不命中。
    assert _ic_session_matches_filter(first, "no-such-token-xyz") is False


def test_build_session_draw_links_mirror(
    api_client: Tuple[TestClient, Path], ic_small_path: Path, tmp_path: Path
) -> None:
    """buildSessionDrawLinks mirror：連線數 = links 數，label 對應該組 d0..d3（AC-6）。"""
    sessions, summary = _real_ic_payloads(ic_small_path, tmp_path)
    assert sessions, "預期至少建出一筆互聯 session"

    for session in sessions:
        draw = _build_session_draw_links(session, summary)
        # 連線數等於 session 的 link 數（本範例每端 DUT 皆在 summary 可查）。
        assert len(draw) == len(session["links"])

        group = _ic_find_group_for_dut(summary, session["links"][0]["a_dut"])
        assert group is not None
        label_by_name = {dut["name"]: dut["label"] for dut in group["duts"]}

        for drawn, link in zip(draw, session["links"]):
            assert drawn["groupName"] == group["name"]
            assert drawn["vlan"] == link["vlan"]
            assert drawn["aPort"] == link["a_port"]
            assert drawn["bPort"] == link["b_port"]
            # aLabel / bLabel 確實對應該組 duts[] 的 label。
            assert drawn["aLabel"] == label_by_name[link["a_dut"]]
            assert drawn["bLabel"] == label_by_name[link["b_dut"]]
            # 兩端 DUT 必同組（互聯 session 不跨組）。
            assert link["a_dut"] in label_by_name
            assert link["b_dut"] in label_by_name


def test_build_session_draw_links_group_absent_returns_empty(
    api_client: Tuple[TestClient, Path], ic_small_path: Path, tmp_path: Path
) -> None:
    """buildSessionDrawLinks not-found 契約：組在 summary 比不到時回 []（review Finding 4）。"""
    sessions, summary = _real_ic_payloads(ic_small_path, tmp_path)
    assert sessions, "預期至少建出一筆互聯 session"

    # 餵一個 groups 為空的 summary：任何 DUT 都定位不到組 → 回 []。
    empty_summary = dict(summary)
    empty_summary["groups"] = []
    assert _build_session_draw_links(sessions[0], empty_summary) == []

    # session 的 a_dut / group 改成 summary 不存在者 → 同樣回 []、不丟例外。
    orphan = dict(sessions[0])
    orphan["group"] = "no-such-group"
    orphan["links"] = [
        dict(link, a_dut="no-such-dut", b_dut="no-such-dut")
        for link in sessions[0]["links"]
    ]
    assert _build_session_draw_links(orphan, summary) == []
