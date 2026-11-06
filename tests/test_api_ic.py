"""互聯 REST 唯讀 / plan / 驗證 / 錯誤映射測試（TestClient，不連任何設備）。

寫入端點因 ``store.locked()`` 的 SIGALRM 只能在主執行緒，改由
``test_api_ic_live.py`` 以主執行緒 live-server 驗證；本檔涵蓋：summary、
available、list、plan 三種、400 驗證、503 帳本故障與隔離、500 固定訊息、
拓樸無互聯區段、錯誤映射守門、Settings 環境變數、OpenAPI。

歷史（HISTORY）：
  - 0.1.0  互聯 REST 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from topo_manager import errors as errors_module
from topo_manager.api import create_app, ic_routes
from topo_manager.api.deps import Settings
from topo_manager.api.errors import _STATUS_BY_EXC, status_for
from topo_manager.errors import (
    ConfigError,
    InterconnectConflictError,
    InterconnectLayoutError,
    InterconnectNotConfiguredError,
    InterconnectRequestError,
    InterconnectSessionNotFoundError,
    VlanPoolExhaustedError,
)

_SMALL_TOPOLOGY = Path(__file__).resolve().parent.parent / "examples" / "topology-interconnect-small.yaml"
_SUMMARY = "/interconnect/summary"
_AVAILABLE = "/interconnect/resources/available"
_SESSIONS = "/interconnect/sessions"
_PLAN = "/interconnect/plan"
_PARK_PLAN = "/interconnect/plan/park"


def _profile_body(duts=("dut-A1", "dut-A2"), profile="mesh", group="A-g1"):
    return {"group": group, "duts": list(duts), "profile": profile}


# ----------------------------------------------------------------------
# summary
# ----------------------------------------------------------------------
def test_summary(ic_api_client) -> None:
    client, _ = ic_api_client
    response = client.get(_SUMMARY)
    assert response.status_code == 200
    body = response.json()
    assert body["group_count"] == 4
    assert body["dut_count"] == 16
    assert body["fanout_count"] == 2
    assert [item["name"] for item in body["fanouts"]] == ["ic-1-1", "ic-2-1"]
    assert body["fanouts"][0] == {"name": "ic-1-1", "wired_ports": 32, "port_count": 48}
    group = body["groups"][0]
    assert group["name"] == "A-g1"
    assert [d["label"] for d in group["duts"]] == ["d0", "d1", "d2", "d3"]
    assert [d["name"] for d in group["duts"]] == ["dut-A1", "dut-A2", "dut-A3", "dut-A4"]


def test_summary_does_not_touch_ledger(ic_api_client) -> None:
    client, state = ic_api_client
    assert client.get(_SUMMARY).status_code == 200
    assert not state.exists()


# ----------------------------------------------------------------------
# available
# ----------------------------------------------------------------------
def test_available_empty_ledger(ic_api_client) -> None:
    client, _ = ic_api_client
    body = client.get(_AVAILABLE).json()
    assert len(body["groups"]) == 4
    for group in body["groups"]:
        assert len(group["buildable_links"]) == 6
        for dut in group["duts"]:
            assert [p["p_index"] for p in dut["ports"]] == [0, 1, 2, 3]
            assert all(p["occupied_by"] is None for p in dut["ports"])


# ----------------------------------------------------------------------
# list
# ----------------------------------------------------------------------
def test_list_empty(ic_api_client) -> None:
    client, _ = ic_api_client
    assert client.get(_SESSIONS).json() == {"sessions": [], "count": 0}


# ----------------------------------------------------------------------
# plan（POST 預覽）
# ----------------------------------------------------------------------
def test_plan_preview_profile(ic_api_client) -> None:
    client, state = ic_api_client
    response = client.post(_PLAN, json=_profile_body(("dut-A1", "dut-A2", "dut-A3"), "ring"))
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "(preview)"
    assert body["action"] == "apply"
    assert body["vlans"] == [100, 101, 102]
    assert body["preview"]["profile"] == "ring"
    assert body["preview"]["status"] == "preview"
    assert [link["vlan"] for link in body["preview"]["links"]] == [100, 101, 102]
    assert len(body["fanouts"]) == 1
    assert len(body["fanouts"][0]["batches"]) == 2
    assert "ic-1-1" == body["fanouts"][0]["fanout"]
    assert not state.exists()


def test_plan_preview_pairs(ic_api_client) -> None:
    client, state = ic_api_client
    response = client.post(
        _PLAN,
        json={"group": "A-g1", "pairs": [{"a": "dut-A1:p3", "b": "dut-A2:p3"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["preview"]["profile"] == "explicit"
    assert body["preview"]["links"][0]["kind"] == "explicit"
    assert not state.exists()


def test_plan_has_no_device_info(ic_api_client) -> None:
    client, _ = ic_api_client
    text = client.post(_PLAN, json=_profile_body()).text
    assert "192.168" not in text
    assert "password" not in text.lower()
    assert "configured" not in text


def test_plan_does_not_check_conflicts(ic_api_client, tmp_path: Path) -> None:
    """預覽不檢查 port 衝突（與 CLI plan 一致）；即使帳本已佔用仍回 200。"""
    client, state = ic_api_client
    from topo_manager.interconnect.allocator import InterconnectAllocator
    from topo_manager.interconnect.store import IcSessionStore
    from topo_manager.loader import load_topology

    topology = load_topology(str(_SMALL_TOPOLOGY))
    InterconnectAllocator(topology, IcSessionStore(state)).allocate_profile(
        "A-g1", ["dut-A1", "dut-A2"], "mesh"
    )
    before = state.read_text(encoding="utf-8")
    assert client.post(_PLAN, json=_profile_body()).status_code == 200
    assert state.read_text(encoding="utf-8") == before


def test_session_plan_apply_and_release(ic_api_client) -> None:
    client, state = ic_api_client
    from topo_manager.interconnect.allocator import InterconnectAllocator
    from topo_manager.interconnect.store import IcSessionStore
    from topo_manager.loader import load_topology

    topology = load_topology(str(_SMALL_TOPOLOGY))
    session = InterconnectAllocator(topology, IcSessionStore(state)).allocate_profile(
        "A-g1", ["dut-A1", "dut-A2"], "mesh"
    )
    apply_plan = client.get("/interconnect/sessions/{0}/plan".format(session.session_id)).json()
    assert apply_plan["action"] == "apply"
    assert apply_plan["session_id"] == session.session_id
    release_plan = client.get(
        "/interconnect/sessions/{0}/plan".format(session.session_id), params={"action": "release"}
    ).json()
    assert release_plan["action"] == "release"
    assert release_plan["preview"] is None
    bad = client.get(
        "/interconnect/sessions/{0}/plan".format(session.session_id), params={"action": "nuke"}
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["type"] == "ValidationError"


def test_session_plan_not_found(ic_api_client) -> None:
    client, _ = ic_api_client
    response = client.get("/interconnect/sessions/ic-9999/plan")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "InterconnectSessionNotFoundError"


def test_park_plan_all_and_filtered(ic_api_client) -> None:
    client, _ = ic_api_client
    full = client.get(_PARK_PLAN).json()
    assert full["action"] == "park"
    assert full["session_id"] == "(park)"
    assert [item["fanout"] for item in full["fanouts"]] == ["ic-1-1", "ic-2-1"]
    one = client.get(_PARK_PLAN, params={"fanout": "ic-1-1", "ports": [1, 2]}).json()
    assert [item["fanout"] for item in one["fanouts"]] == ["ic-1-1"]
    commands = [c for batch in one["fanouts"][0]["batches"] for c in batch]
    assert "interface ethernet 1/1" in commands
    assert "interface ethernet 1/3" not in commands


def test_park_plan_unknown_fanout_and_bad_ports(ic_api_client) -> None:
    client, _ = ic_api_client
    unknown = client.get(_PARK_PLAN, params={"fanout": "nope"})
    assert unknown.status_code == 400
    assert unknown.json()["error"]["type"] == "InterconnectRequestError"
    bad_port = client.get(_PARK_PLAN, params={"ports": [999]})
    assert bad_port.status_code == 400
    not_int = client.get(_PARK_PLAN, params={"ports": ["x"]})
    assert not_int.status_code == 400
    assert not_int.json()["error"]["type"] == "ValidationError"


# ----------------------------------------------------------------------
# 400 驗證（皆發生在取鎖之前，TestClient 即可）
# ----------------------------------------------------------------------
_REQUEST_ERRORS = [
    ("neither", {"group": "A-g1"}),
    ("both", {"group": "A-g1", "duts": ["dut-A1", "dut-A2"], "profile": "mesh",
              "pairs": [{"a": "dut-A1:p0", "b": "dut-A2:p0"}]}),
    ("duts_without_profile", {"group": "A-g1", "duts": ["dut-A1", "dut-A2"]}),
    ("pairs_with_profile", {"group": "A-g1", "profile": "mesh",
                            "pairs": [{"a": "dut-A1:p0", "b": "dut-A2:p0"}]}),
    ("empty_duts", {"group": "A-g1", "duts": [], "profile": "mesh"}),
    ("empty_pairs", {"group": "A-g1", "pairs": []}),
    ("cross_group", {"group": "A-g1", "duts": ["dut-A1", "dut-A5"], "profile": "mesh"}),
    ("unknown_dut", {"group": "A-g1", "duts": ["dut-A1", "ghost"], "profile": "mesh"}),
    ("unknown_group", {"group": "Z-g9", "duts": ["dut-A1", "dut-A2"], "profile": "mesh"}),
    ("ring_two", {"group": "A-g1", "duts": ["dut-A1", "dut-A2"], "profile": "ring"}),
    ("bad_profile", {"group": "A-g1", "duts": ["dut-A1", "dut-A2"], "profile": "star"}),
    ("dup_dut", {"group": "A-g1", "duts": ["dut-A1", "dut-A1"], "profile": "mesh"}),
    ("one_dut", {"group": "A-g1", "duts": ["dut-A1"], "profile": "line"}),
    ("dup_port", {"group": "A-g1", "pairs": [{"a": "dut-A1:p3", "b": "dut-A2:p3"},
                                              {"a": "dut-A1:p3", "b": "dut-A3:p3"}]}),
    ("same_dut", {"group": "A-g1", "pairs": [{"a": "dut-A1:p0", "b": "dut-A1:p1"}]}),
    ("bad_format", {"group": "A-g1", "pairs": [{"a": "dut-A1", "b": "dut-A2:p0"}]}),
    ("unknown_port", {"group": "A-g1", "pairs": [{"a": "dut-A1:p9", "b": "dut-A2:p0"}]}),
]


@pytest.mark.parametrize("name,body", _REQUEST_ERRORS, ids=[n for n, _ in _REQUEST_ERRORS])
@pytest.mark.parametrize("path", [_SESSIONS, _PLAN])
def test_invalid_request_is_400(ic_api_client, path: str, name: str, body: dict) -> None:
    client, state = ic_api_client
    response = client.post(path, json=body)
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["error"]["type"] == "InterconnectRequestError"
    assert "Traceback" not in response.text
    assert not state.exists()


_SCHEMA_ERRORS = [
    ("missing_group", {"duts": ["dut-A1", "dut-A2"], "profile": "mesh"}),
    ("empty_group", {"group": "", "duts": ["dut-A1", "dut-A2"], "profile": "mesh"}),
    ("extra_field", {"group": "A-g1", "duts": ["dut-A1", "dut-A2"], "profile": "mesh", "x": 1}),
    ("blank_dut", {"group": "A-g1", "duts": ["dut-A1", "  "], "profile": "mesh"}),
    ("empty_pair_end", {"group": "A-g1", "pairs": [{"a": "", "b": "dut-A2:p0"}]}),
    ("wrong_type", {"group": "A-g1", "duts": "dut-A1", "profile": "mesh"}),
]


@pytest.mark.parametrize("name,body", _SCHEMA_ERRORS, ids=[n for n, _ in _SCHEMA_ERRORS])
def test_schema_violation_is_400_validation_error(ic_api_client, name: str, body: dict) -> None:
    client, _ = ic_api_client
    response = client.post(_SESSIONS, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "ValidationError"


def test_duts_are_stripped(ic_api_client) -> None:
    client, _ = ic_api_client
    response = client.post(_PLAN, json=_profile_body((" dut-A1", "dut-A2 ")))
    assert response.status_code == 200
    assert response.json()["preview"]["duts"] == ["dut-A1", "dut-A2"]


# ----------------------------------------------------------------------
# 503：帳本 I/O 故障；與打流側隔離
# ----------------------------------------------------------------------
def test_ledger_is_directory_503(ic_small_path: Path, tmp_path: Path) -> None:
    ledger = tmp_path / "ic-dir"
    ledger.mkdir()
    client = TestClient(create_app(Settings(str(ic_small_path), str(tmp_path / "a.json"),
                                            ic_state_path=str(ledger))))
    for path in (_AVAILABLE, _SESSIONS):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "SessionStoreError"
    assert client.get(_SUMMARY).status_code == 200
    assert client.get("/sessions").status_code == 200
    assert client.get("/topology").status_code == 200


def test_ledger_not_json_503(ic_small_path: Path, tmp_path: Path) -> None:
    ledger = tmp_path / "ic.json"
    ledger.write_text("not json", encoding="utf-8")
    client = TestClient(create_app(Settings(str(ic_small_path), str(tmp_path / "a.json"),
                                            ic_state_path=str(ledger))))
    response = client.get(_SESSIONS)
    assert response.status_code == 503
    assert "Traceback" not in response.text
    assert client.post(_PLAN, json=_profile_body()).status_code == 503


def test_ledger_pointed_at_traffic_ledger_503(ic_small_path: Path, tmp_path: Path) -> None:
    traffic = tmp_path / "allocations.json"
    traffic.write_text(json.dumps({"version": 2, "next_seq": 1, "sessions": []}), encoding="utf-8")
    client = TestClient(create_app(Settings(str(ic_small_path), str(tmp_path / "b.json"),
                                            ic_state_path=str(traffic))))
    response = client.get(_SESSIONS)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "SessionStoreError"
    assert client.get("/sessions").status_code == 200


def test_lock_failure_maps_to_503(ic_api_client, monkeypatch) -> None:
    """取鎖失敗（SessionStoreError）→ 503，帳本未變。"""
    from contextlib import contextmanager

    from topo_manager.errors import SessionStoreError
    from topo_manager.interconnect.store import IcSessionStore

    client, state = ic_api_client

    @contextmanager
    def _boom(self, *args, **kwargs):
        raise SessionStoreError("取鎖逾時")
        yield  # pragma: no cover

    monkeypatch.setattr(IcSessionStore, "locked", _boom)
    for response in (
        client.post(_SESSIONS, json=_profile_body()),
        client.delete(_SESSIONS + "/ic-0001"),
    ):
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "SessionStoreError"
    assert not state.exists()


# ----------------------------------------------------------------------
# 500：非預期例外固定訊息
# ----------------------------------------------------------------------
def test_unexpected_error_is_500(ic_small_path: Path, tmp_path: Path, monkeypatch) -> None:
    from topo_manager.interconnect.allocator import InterconnectAllocator

    def _boom(self):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(InterconnectAllocator, "list_sessions", _boom)
    client = TestClient(
        create_app(Settings(str(ic_small_path), str(tmp_path / "a.json"),
                            ic_state_path=str(tmp_path / "ic.json"))),
        raise_server_exceptions=False,
    )
    response = client.get(_SESSIONS)
    assert response.status_code == 500
    assert response.json()["error"]["type"] == "InternalError"
    assert "secret" not in response.text
    assert "Traceback" not in response.text


# ----------------------------------------------------------------------
# 拓樸沒有互聯區段
# ----------------------------------------------------------------------
_ALL_IC_ENDPOINTS = [
    ("get", _SUMMARY, None),
    ("get", _AVAILABLE, None),
    ("get", _SESSIONS, None),
    ("post", _SESSIONS, _profile_body()),
    ("delete", _SESSIONS + "/ic-0001", None),
    ("post", _PLAN, _profile_body()),
    ("get", _SESSIONS + "/ic-0001/plan", None),
    ("get", _PARK_PLAN, None),
]


@pytest.mark.parametrize("method,path,body", _ALL_IC_ENDPOINTS)
def test_no_interconnect_section_is_404(ic_no_section_api_client, method, path, body) -> None:
    kwargs = {"json": body} if body is not None else {}
    response = getattr(ic_no_section_api_client, method)(path, **kwargs)
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["type"] == "InterconnectNotConfiguredError"
    assert "interconnect" in error["message"]
    assert "Traceback" not in response.text


def test_no_interconnect_section_existing_endpoints_ok(ic_no_section_api_client) -> None:
    client = ic_no_section_api_client
    assert client.get("/topology").status_code == 200
    assert client.get("/sessions").status_code == 200
    assert client.get("/resources/available").status_code == 200
    assert client.get("/").status_code == 200


def test_no_section_invalid_ic_vlan_pool_does_not_block_startup(three_tier_path: Path, tmp_path: Path) -> None:
    settings = Settings(str(three_tier_path), str(tmp_path / "a.json"), ic_vlan_min=1, ic_vlan_max=5000)
    create_app(settings)


def test_invalid_ic_vlan_pool_fails_fast_with_section(ic_small_path: Path, tmp_path: Path) -> None:
    settings = Settings(str(ic_small_path), str(tmp_path / "a.json"), ic_vlan_min=1, ic_vlan_max=5000)
    with pytest.raises(ConfigError):
        create_app(settings)


# ----------------------------------------------------------------------
# 錯誤映射守門
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc,expected",
    [
        (InterconnectRequestError("x"), 400),
        (InterconnectConflictError("x"), 409),
        (InterconnectSessionNotFoundError("x"), 404),
        (InterconnectNotConfiguredError("x"), 404),
        (VlanPoolExhaustedError("x"), 409),
    ],
)
def test_status_for_interconnect(exc, expected: int) -> None:
    assert status_for(exc) == expected


def test_every_interconnect_error_is_mapped() -> None:
    """errors 模組中所有 Interconnect* 類別（除刻意不映射的 Layout）都必須在映射表。"""
    names = [
        name for name, obj in vars(errors_module).items()
        if inspect.isclass(obj) and name.startswith("Interconnect")
    ]
    assert names
    for name in names:
        cls = getattr(errors_module, name)
        if cls is InterconnectLayoutError:
            assert cls not in _STATUS_BY_EXC
            continue
        assert cls in _STATUS_BY_EXC, name


def test_layout_error_mapping_unchanged() -> None:
    """父類 Layout 不新增映射：仍沿用 TopologyReferenceError 的精確查表結果（500）。"""
    assert status_for(InterconnectLayoutError("x")) == 500


# ----------------------------------------------------------------------
# 非回歸守門：handler 皆為 coroutine（主執行緒）
# ----------------------------------------------------------------------
def test_all_ic_handlers_are_coroutines() -> None:
    handlers = [route.endpoint for route in ic_routes.router.routes]
    assert len(handlers) == 8
    for handler in handlers:
        assert inspect.iscoroutinefunction(handler), handler.__name__


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
def test_settings_defaults_unchanged() -> None:
    settings = Settings("t.yaml")
    assert settings.state_path == ".topo-state/allocations.json"
    assert (settings.vlan_min, settings.vlan_max) == (100, 3000)
    assert settings.ic_state_path == ".topo-state/interconnect.json"
    assert (settings.ic_vlan_min, settings.ic_vlan_max) == (100, 3000)


def test_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TOPO_MANAGER_TOPOLOGY", "t.yaml")
    for name in ("TOPO_MANAGER_STATE", "TOPO_MANAGER_VLAN_MIN", "TOPO_MANAGER_VLAN_MAX",
                 "TOPO_MANAGER_IC_STATE", "TOPO_MANAGER_IC_VLAN_MIN", "TOPO_MANAGER_IC_VLAN_MAX"):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env()
    assert defaults.ic_state_path == ".topo-state/interconnect.json"
    assert (defaults.ic_vlan_min, defaults.ic_vlan_max) == (100, 3000)
    monkeypatch.setenv("TOPO_MANAGER_STATE", "tr.json")
    monkeypatch.setenv("TOPO_MANAGER_IC_STATE", "ic.json")
    monkeypatch.setenv("TOPO_MANAGER_IC_VLAN_MIN", "200")
    monkeypatch.setenv("TOPO_MANAGER_IC_VLAN_MAX", "300")
    settings = Settings.from_env()
    assert settings.state_path == "tr.json"
    assert settings.ic_state_path == "ic.json"
    assert (settings.ic_vlan_min, settings.ic_vlan_max) == (200, 300)
    assert (settings.vlan_min, settings.vlan_max) == (100, 3000)


@pytest.mark.parametrize("name", ["TOPO_MANAGER_IC_VLAN_MIN", "TOPO_MANAGER_IC_VLAN_MAX"])
def test_settings_ic_vlan_not_integer(monkeypatch, name: str) -> None:
    monkeypatch.setenv("TOPO_MANAGER_TOPOLOGY", "t.yaml")
    monkeypatch.setenv(name, "abc")
    with pytest.raises(ConfigError):
        Settings.from_env()


# ----------------------------------------------------------------------
# OpenAPI
# ----------------------------------------------------------------------
def test_openapi_contains_interconnect(ic_api_client) -> None:
    client, _ = ic_api_client
    body = client.get("/openapi.json").json()
    assert body["info"]["version"] == "0.7.0"
    paths = body["paths"]
    expected = {
        _SUMMARY: {"get"},
        _AVAILABLE: {"get"},
        _SESSIONS: {"get", "post"},
        _SESSIONS + "/{session_id}": {"delete"},
        _PLAN: {"post"},
        _SESSIONS + "/{session_id}/plan": {"get"},
        _PARK_PLAN: {"get"},
    }
    for path, methods in expected.items():
        assert path in paths, path
        assert methods <= set(paths[path]), path
    schemas = body["components"]["schemas"]
    for name in ("IcAllocateRequest", "IcSessionResponse", "IcSessionListResponse",
                 "IcPlanResponse", "IcSummaryResponse", "IcAvailableResponse", "ErrorResponse"):
        assert name in schemas, name
    post = paths[_SESSIONS]["post"]
    assert "201" in post["responses"]
    for code in ("400", "404", "409", "503"):
        assert code in post["responses"]
    assert post["tags"] == ["interconnect"]


def test_openapi_present_without_section(ic_no_section_api_client) -> None:
    body = ic_no_section_api_client.get("/openapi.json").json()
    assert _SESSIONS in body["paths"]
