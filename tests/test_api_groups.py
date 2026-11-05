"""群組 REST 端點測試（FEAT-002）。

MODULE NAME：
  tests.test_api_groups

PURPOSE：
  驗證群組 REST 的七個端點（CRUD + ATLAS v2）與 allocate-time 歸群、釋放聯動
  （AC-9）。唯讀端點與請求驗證以 TestClient（主執行緒 + tmp_path）；寫入端點
  （POST /groups、POST /sessions、DELETE）因 Phase 2 signal-based 檔鎖只能在
  主執行緒取鎖，一律改用 group_live_server 子行程 fixture（見 conftest）。

NOTES：
  - ATLAS 讀取測試針對 2tier 範例：dut-A1:Eth0 → key "DUTA1"、label "PA"
    （tg_ports[Eth0,Eth1] 的 index 0）、value "1/0"（裸 EthN → unit 預設 1）；
    TG 側 ixia-1/1 → key "TG"、slot "1"、"1/1"。
  - 群組帳本一律落在 tmp_path（TestClient 走 group_state_path、live-server 走
    TOPO_MANAGER_GROUP_STATE），絕不污染工作目錄的 .topo-state/。

HISTORY：
  - 0.1.0  FEAT-002 初版：群組 CRUD / 驗證 / ATLAS / allocate-time 歸群 / AC-9。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def group_api_client(two_tier_path: Path, tmp_path: Path):
    """以 2tier + tmp_path 群組帳本建 app，回 (TestClient, group_state_path)。

    僅供唯讀端點（GET /groups、GET /groups/{name}、GET /groups/{name}/atlas）與
    請求驗證（body name 含 '/'、path name 含 '/'）使用；寫入端點改用
    group_live_server（signal 檔鎖需主執行緒）。
    """
    from fastapi.testclient import TestClient

    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    group_state = tmp_path / "state" / "groups.json"
    settings = Settings(
        topology_path=str(two_tier_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        group_state_path=str(group_state),
    )
    return TestClient(create_app(settings)), group_state


# ----------------------------------------------------------------------
# 唯讀端點 + 請求驗證（TestClient）
# ----------------------------------------------------------------------


def test_create_group_body_name_with_slash_is_400(group_api_client) -> None:
    """POST /groups body name 含 '/' → 400（欄位驗證，FEAT-002）。"""
    client, _ = group_api_client
    response = client.post("/groups", json={"name": "a/b"})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ValidationError"


def test_get_group_path_name_with_slash_is_404(group_api_client) -> None:
    """GET /groups/{name} path name 含 '/' → 404（路由不匹配，FEAT-002）。"""
    client, _ = group_api_client
    response = client.get("/groups/a/b")
    assert response.status_code == 404


def test_get_group_absent_is_404(group_api_client) -> None:
    """GET /groups/{name} 查無群組 → 404（GroupNotFoundError，FEAT-002）。"""
    client, _ = group_api_client
    response = client.get("/groups/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "GroupNotFoundError"


def test_list_groups_empty(group_api_client) -> None:
    """GET /groups 無群組時回空清單、count==0（FEAT-002）。"""
    client, _ = group_api_client
    response = client.get("/groups")
    assert response.status_code == 200
    body = response.json()
    assert body == {"groups": [], "count": 0}


def test_atlas_absent_group_is_404(group_api_client) -> None:
    """GET /groups/{name}/atlas 群組不存在 → 404（FEAT-002）。"""
    client, _ = group_api_client
    response = client.get("/groups/does-not-exist/atlas")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "GroupNotFoundError"


def test_atlas_shape_for_2tier(
    group_api_client, two_tier_path: Path, tmp_path: Path
) -> None:
    """GET /groups/{name}/atlas 回正確 ATLAS v2 shape（PA 排序、ixia slot，FEAT-002）。

    直接寫一份含一個成員的群組帳本與對應 traffic session 帳本到 tmp_path（避開
    TestClient 無法取 signal 鎖的寫入端點），再以唯讀 atlas 端點讀回斷言。
    """
    import json

    client, group_state = group_api_client

    # 以獨立 Allocator 在同一 state_path 建一筆 ixia-1/1 ↔ dut-A1:Eth0 的 session。
    from topo_manager.allocator import Allocator
    from topo_manager.loader import load_topology
    from topo_manager.store import JsonSessionStore

    state_path = tmp_path / "state" / "allocations.json"
    topology = load_topology(str(two_tier_path))
    allocator = Allocator(
        topology, JsonSessionStore(str(state_path)), vlan_min=100, vlan_max=3000
    )
    session = allocator.allocate("ixia-1/1", "dut-A1:Eth0")

    # 群組帳本：一個群組 T1，成員為剛建的 session。
    group_state.parent.mkdir(parents=True, exist_ok=True)
    group_state.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "group",
                "groups": [{"name": "T1", "members": [session.session_id]}],
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/groups/T1/atlas")
    assert response.status_code == 200
    body = response.json()
    # 頂層唯一 key = 群組名，值為 pair 清單。
    assert list(body.keys()) == ["T1"]
    assert len(body["T1"]) == 1
    pair = body["T1"][0]
    # DUT 側：key "DUTA1"、label "PA"（tg_ports index 0）、value "1/0"。
    assert pair["DUTA1"] == {"PA": "1/0"}
    # TG 側：key "TG"、slot "1"、"1/1"。
    assert pair["TG"] == {"1": "1/1"}


def test_atlas_empty_group(group_api_client) -> None:
    """GET /groups/{name}/atlas 空群組 → {name: []}（FEAT-002）。"""
    import json

    client, group_state = group_api_client
    group_state.parent.mkdir(parents=True, exist_ok=True)
    group_state.write_text(
        json.dumps(
            {"version": 1, "kind": "group", "groups": [{"name": "T2", "members": []}]}
        ),
        encoding="utf-8",
    )
    response = client.get("/groups/T2/atlas")
    assert response.status_code == 200
    assert response.json() == {"T2": []}


# ----------------------------------------------------------------------
# 寫入端點（group_live_server 子行程）
# ----------------------------------------------------------------------


def test_create_group_and_duplicate(group_live_server) -> None:
    """POST /groups 建立 201、重複建立 → 409（FEAT-002）。"""
    first = group_live_server.client.post("/groups", json={"name": "T1"})
    assert first.status_code == 201
    body = first.json()
    assert body == {"name": "T1", "members": [], "count": 0}

    dup = group_live_server.client.post("/groups", json={"name": "T1"})
    assert dup.status_code == 409
    assert dup.json()["error"]["type"] == "GroupExistsError"


def test_post_session_without_group(group_live_server) -> None:
    """POST /sessions 不帶 group → group 欄位為 None（FEAT-002）。"""
    response = group_live_server.client.post(
        "/sessions",
        json={"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"},
    )
    assert response.status_code == 201
    assert response.json()["group"] is None


def test_post_session_with_group_auto_creates(group_live_server) -> None:
    """POST /sessions 帶 group → 自動建群並回填 group；GET /groups 可見（FEAT-002）。"""
    response = group_live_server.client.post(
        "/sessions",
        json={
            "generator_port": "ixia-1/1",
            "dut_port": "dut-A1:Eth0",
            "group": "T1",
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["group"] == "T1"
    session_id = created["session_id"]

    # 群組已自動建立且含此 session。
    group = group_live_server.client.get("/groups/T1").json()
    assert group["name"] == "T1"
    assert session_id in group["members"]

    # GET /sessions 回填 group 欄位。
    listed = group_live_server.client.get("/sessions").json()
    match = [s for s in listed["sessions"] if s["session_id"] == session_id]
    assert len(match) == 1
    assert match[0]["group"] == "T1"


def test_add_member_already_in_other_group_is_409(group_live_server) -> None:
    """已屬他群的 session 再加入另一群 → 409（GroupMembershipError，FEAT-002）。"""
    # 先分配一筆並歸入 T1。
    created = group_live_server.client.post(
        "/sessions",
        json={
            "generator_port": "ixia-1/1",
            "dut_port": "dut-A1:Eth0",
            "group": "T1",
        },
    ).json()
    session_id = created["session_id"]

    # 另建一個群 T2，嘗試把同一 session 加入 → 409。
    assert group_live_server.client.post("/groups", json={"name": "T2"}).status_code == 201
    response = group_live_server.client.post(
        "/groups/T2/members", json={"session_id": session_id}
    )
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "GroupMembershipError"


def test_release_removes_from_group_ac9(group_live_server) -> None:
    """釋放已歸群的 session → 該群成員不再含其 id（AC-9，FEAT-002）。"""
    created = group_live_server.client.post(
        "/sessions",
        json={
            "generator_port": "ixia-1/1",
            "dut_port": "dut-A1:Eth0",
            "group": "T1",
        },
    ).json()
    session_id = created["session_id"]

    # 釋放該 session。
    released = group_live_server.client.delete("/sessions/{0}".format(session_id))
    assert released.status_code == 200

    # 群組仍存在，但成員已不含該 session_id（AC-9）。
    group = group_live_server.client.get("/groups/T1").json()
    assert session_id not in group["members"]
