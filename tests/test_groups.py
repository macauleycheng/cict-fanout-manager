"""群組後端核心測試（groups/models/store/registry/atlas，群組設計 Part 2）。

涵蓋：
  - models/store：round-trip、重啟還原（AC-7）、壞 version/kind/schema →
    SessionStoreError。
  - registry：create/add/remove/delete、auto_create、一 session 至多一群
    （GroupMembershipError）、冪等 re-add、remove_member_any、還原期重複
    （SessionStoreError）、GroupNotFoundError/GroupExistsError。
  - atlas：ixia 裸名 vs dev:port 結果一致、trex、PA/PB 依 tg_ports 順序、
    Eth0→1/0、Eth5→1/5、Eth2/5→2/5、'Eth 2/5'→2/5、dut-A1→DUTA1、port 不在
    tg_ports → AtlasFormatError、索引 >= 26 → AtlasFormatError、空群 →
    {name:[]}、2tier 端對端斷言。

歷史（HISTORY）：
  - 0.7.0  群組功能初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from topo_manager.allocator import Session
from topo_manager.errors import (
    AtlasFormatError,
    GroupExistsError,
    GroupMembershipError,
    GroupNotFoundError,
    SessionStoreError,
)
from topo_manager.groups.atlas import build_atlas_v2
from topo_manager.groups.models import (
    GROUP_LEDGER_KIND,
    GROUP_SCHEMA_VERSION,
    GroupEntry,
    GroupLedger,
)
from topo_manager.groups.registry import GroupRegistry
from topo_manager.groups.store import GroupStore
from topo_manager.loader import load_topology
from topo_manager.models import Dut, Rack, Topology


# ----------------------------------------------------------------------
# 測試輔助：直接以 extra='forbid' 填滿所有欄位建 Session。
# ----------------------------------------------------------------------


def _session(
    session_id: str,
    generator_port: str,
    dut_port: str,
    vlan: int = 100,
) -> Session:
    """建一筆最小合法 Session（path/physical_ports 不影響 ATLAS，填佔位值）。"""
    path = [generator_port, dut_port]
    return Session(
        session_id=session_id,
        vlan=vlan,
        generator_port=generator_port,
        dut_port=dut_port,
        path=path,
        physical_ports=list(path),
        trunk_segments=[],
    )


def _dut(name: str, tg_ports):
    """建一個 Dut（供 atlas 的自組拓樸用）。"""
    return Dut(name=name, model="m", tg_ports=list(tg_ports))


def _topology(duts):
    """以一個 rack 包起傳入 DUT 清單，建最小 Topology（atlas 只讀 racks）。"""
    return Topology(
        version=1,
        site="lab",
        generators=[],
        fanouts=[],
        racks=[Rack(name="rack-1", duts=list(duts))],
        links=[],
    )


# ----------------------------------------------------------------------
# models / store
# ----------------------------------------------------------------------


def test_store_missing_file_is_empty(tmp_path: Path) -> None:
    loaded = GroupStore(tmp_path / "x" / "groups.json").load()
    assert loaded.groups == []


def test_store_default_path() -> None:
    assert str(GroupStore().path) == ".topo-state/groups.json"


def test_store_round_trip(tmp_path: Path) -> None:
    store = GroupStore(tmp_path / "s" / "groups.json")
    groups = [
        GroupEntry(name="T1", members=["s-0001", "s-0003"]),
        GroupEntry(name="T2", members=[]),
    ]
    store.save(groups)
    loaded = GroupStore(store.path).load()
    assert loaded.groups == groups
    data = store.load_raw()
    assert data["version"] == GROUP_SCHEMA_VERSION
    assert data["kind"] == GROUP_LEDGER_KIND


def test_store_restart_restore(tmp_path: Path) -> None:
    """AC-7：重啟（新 store 實例讀同檔）後群組與成員還原。"""
    path = tmp_path / "s" / "groups.json"
    GroupStore(path).save([GroupEntry(name="T1", members=["s-0001"])])
    reloaded = GroupStore(path).load()
    assert reloaded.groups == [GroupEntry(name="T1", members=["s-0001"])]


@pytest.mark.parametrize(
    "data,pattern",
    [
        ({"version": 2, "kind": "group", "groups": []}, "這不是群組帳本"),
        ({"version": 1, "kind": "other", "groups": []}, "這不是群組帳本"),
        ({"version": 1, "kind": "group", "groups": [{}]}, "schema 驗證失敗"),
        ({"version": 1, "kind": "group", "groups": [], "x": 1}, "schema 驗證失敗"),
    ],
)
def test_store_bad_content(tmp_path: Path, data, pattern) -> None:
    path = tmp_path / "groups.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SessionStoreError, match=pattern):
        GroupStore(path).load()


def test_store_bad_json_and_top_level(tmp_path: Path) -> None:
    path = tmp_path / "groups.json"
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(SessionStoreError, match="非合法 JSON"):
        GroupStore(path).load()
    path.write_text("[1]", encoding="utf-8")
    with pytest.raises(SessionStoreError, match="頂層須為 JSON object"):
        GroupStore(path).load()


def test_store_group_ledger_model() -> None:
    """GroupLedger 可序列化回正確 version/kind。"""
    ledger = GroupLedger(
        version=GROUP_SCHEMA_VERSION,
        kind=GROUP_LEDGER_KIND,
        groups=[GroupEntry(name="T1")],
    )
    dumped = ledger.model_dump()
    assert dumped["version"] == 1 and dumped["kind"] == "group"
    assert dumped["groups"][0]["members"] == []


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------


def _registry(tmp_path: Path) -> GroupRegistry:
    return GroupRegistry(GroupStore(tmp_path / "s" / "groups.json"))


def test_registry_create_and_get(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    created = reg.create_group("T1")
    assert created.name == "T1" and created.members == []
    assert reg.get_group("T1").name == "T1"
    assert [g.name for g in reg.list_groups()] == ["T1"]


def test_registry_create_existing_raises(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create_group("T1")
    with pytest.raises(GroupExistsError, match="T1"):
        reg.create_group("T1")


def test_registry_get_missing_raises(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(GroupNotFoundError, match="T1"):
        reg.get_group("T1")


def test_registry_add_member_flow(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create_group("T1")
    group = reg.add_member("T1", "s-0001")
    assert group.members == ["s-0001"]
    assert reg.group_of("s-0001") == "T1"
    # 持久化後重啟還原。
    reloaded = GroupRegistry(GroupStore(tmp_path / "s" / "groups.json"))
    assert reloaded.get_group("T1").members == ["s-0001"]


def test_registry_add_member_group_missing(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(GroupNotFoundError, match="T1"):
        reg.add_member("T1", "s-0001")


def test_registry_add_member_auto_create(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    group = reg.add_member("T1", "s-0001", auto_create=True)
    assert group.name == "T1" and group.members == ["s-0001"]
    assert reg.group_of("s-0001") == "T1"


def test_registry_at_most_one_group(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create_group("T1")
    reg.create_group("T2")
    reg.add_member("T1", "s-0001")
    with pytest.raises(GroupMembershipError, match="s-0001.*T1.*T2"):
        reg.add_member("T2", "s-0001")
    # T2 仍為空，未受失敗影響。
    assert reg.get_group("T2").members == []


def test_registry_add_member_idempotent(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create_group("T1")
    reg.add_member("T1", "s-0001")
    group = reg.add_member("T1", "s-0001")  # 冪等 no-op
    assert group.members == ["s-0001"]


def test_registry_remove_member(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create_group("T1")
    reg.add_member("T1", "s-0001")
    reg.add_member("T1", "s-0002")
    group = reg.remove_member("T1", "s-0001")
    assert group.members == ["s-0002"]
    assert reg.group_of("s-0001") is None
    # 移除不存在的成員為 no-op。
    assert reg.remove_member("T1", "s-9999").members == ["s-0002"]


def test_registry_remove_member_group_missing(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(GroupNotFoundError):
        reg.remove_member("T1", "s-0001")


def test_registry_remove_member_any(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create_group("T1")
    reg.add_member("T1", "s-0001")
    assert reg.remove_member_any("s-0001") == "T1"
    assert reg.group_of("s-0001") is None
    # 不屬任何群時 no-op 回 None。
    assert reg.remove_member_any("s-0001") is None


def test_registry_delete_group(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.create_group("T1")
    reg.add_member("T1", "s-0001")
    deleted = reg.delete_group("T1")
    assert deleted.name == "T1"
    assert reg.group_of("s-0001") is None
    with pytest.raises(GroupNotFoundError):
        reg.get_group("T1")


def test_registry_delete_missing_raises(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    with pytest.raises(GroupNotFoundError, match="T1"):
        reg.delete_group("T1")


def test_registry_restore_duplicate_session_raises(tmp_path: Path) -> None:
    """還原期：同一 session_id 出現於兩群 → SessionStoreError。"""
    path = tmp_path / "s" / "groups.json"
    store = GroupStore(path)
    store._ensure_dir_durable(path.parent)
    store.save(
        [
            GroupEntry(name="T1", members=["s-0001"]),
            GroupEntry(name="T2", members=["s-0001"]),
        ]
    )
    with pytest.raises(SessionStoreError, match="s-0001.*T1.*T2"):
        GroupRegistry(GroupStore(path))


# ----------------------------------------------------------------------
# atlas builder
# ----------------------------------------------------------------------


def test_atlas_ixia_bare_vs_devport_identical(tmp_path: Path) -> None:
    topo = _topology([_dut("dut-A1", ["Eth0", "Eth1"])])
    bare = build_atlas_v2(
        "T1", [_session("s-0001", "ixia-1/1", "dut-A1:Eth0")], topo
    )
    devport = build_atlas_v2(
        "T1", [_session("s-0001", "ixia-1:ixia-1/1", "dut-A1:Eth0")], topo
    )
    assert bare == devport
    assert bare == {"T1": [{"DUTA1": {"PA": "1/0"}, "TG": {"1": "1/1"}}]}


def test_atlas_trex_slot(tmp_path: Path) -> None:
    topo = _topology([_dut("dut-A1", ["Eth0", "Eth1"])])
    result = build_atlas_v2(
        "T1", [_session("s-0001", "trex-1/0", "dut-A1:Eth0")], topo
    )
    assert result == {"T1": [{"DUTA1": {"PA": "1/0"}, "TG": {"1": "1/0"}}]}


def test_atlas_label_order_follows_tg_ports(tmp_path: Path) -> None:
    """PA/PB 依 dut.tg_ports [Eth0, Eth1] 順序。"""
    topo = _topology([_dut("dut-A1", ["Eth0", "Eth1"])])
    result = build_atlas_v2(
        "T1",
        [
            _session("s-0001", "ixia-1/1", "dut-A1:Eth0"),
            _session("s-0002", "ixia-1/2", "dut-A1:Eth1"),
        ],
        topo,
    )
    assert result["T1"][0]["DUTA1"] == {"PA": "1/0"}
    assert result["T1"][1]["DUTA1"] == {"PB": "1/1"}


@pytest.mark.parametrize(
    "port_raw,expected_value",
    [
        ("Eth0", "1/0"),
        ("Eth5", "1/5"),
        ("Eth2/5", "2/5"),
        ("Eth 2/5", "2/5"),
    ],
)
def test_atlas_dut_port_value(tmp_path: Path, port_raw, expected_value) -> None:
    topo = _topology([_dut("dut-A1", [port_raw])])
    result = build_atlas_v2(
        "T1", [_session("s-0001", "ixia-1/1", "dut-A1:" + port_raw)], topo
    )
    assert result["T1"][0]["DUTA1"] == {"PA": expected_value}


def test_atlas_dut_key_normalization(tmp_path: Path) -> None:
    """dut-A1 → DUTA1（去 dash、轉大寫）。"""
    topo = _topology([_dut("dut-A1", ["Eth0"]), _dut("dut-B2", ["Eth0"])])
    result = build_atlas_v2(
        "T1",
        [
            _session("s-0001", "ixia-1/1", "dut-A1:Eth0"),
            _session("s-0002", "ixia-1/2", "dut-B2:Eth0"),
        ],
        topo,
    )
    assert "DUTA1" in result["T1"][0]
    assert "DUTB2" in result["T1"][1]


def test_atlas_port_not_in_tg_ports_raises(tmp_path: Path) -> None:
    topo = _topology([_dut("dut-A1", ["Eth0", "Eth1"])])
    with pytest.raises(AtlasFormatError):
        build_atlas_v2(
            "T1", [_session("s-0001", "ixia-1/1", "dut-A1:Eth9")], topo
        )


def test_atlas_unknown_dut_raises(tmp_path: Path) -> None:
    topo = _topology([_dut("dut-A1", ["Eth0"])])
    with pytest.raises(AtlasFormatError):
        build_atlas_v2(
            "T1", [_session("s-0001", "ixia-1/1", "dut-Z9:Eth0")], topo
        )


def test_atlas_bad_tg_port_raises(tmp_path: Path) -> None:
    topo = _topology([_dut("dut-A1", ["Eth0"])])
    with pytest.raises(AtlasFormatError):
        build_atlas_v2(
            "T1", [_session("s-0001", "bogusport", "dut-A1:Eth0")], topo
        )


def test_atlas_label_index_over_26_raises(tmp_path: Path) -> None:
    """索引 >= 26（第 27 個 tg_port）→ AtlasFormatError。"""
    ports = ["Eth{0}".format(i) for i in range(27)]
    topo = _topology([_dut("dut-A1", ports)])
    with pytest.raises(AtlasFormatError):
        build_atlas_v2(
            "T1", [_session("s-0001", "ixia-1/1", "dut-A1:Eth26")], topo
        )


def test_atlas_empty_group(tmp_path: Path) -> None:
    topo = _topology([_dut("dut-A1", ["Eth0"])])
    assert build_atlas_v2("T1", [], topo) == {"T1": []}


def test_atlas_end_to_end_2tier(two_tier_path: Path) -> None:
    """以真實 2tier 拓樸端對端斷言（dut-A1 tg_ports [Eth0, Eth1]）。"""
    topo = load_topology(two_tier_path)
    result = build_atlas_v2(
        "T1",
        [
            _session("s-0001", "ixia-1/1", "dut-A1:Eth0"),
            _session("s-0002", "ixia-1/2", "dut-A1:Eth1"),
        ],
        topo,
    )
    assert result == {
        "T1": [
            {"DUTA1": {"PA": "1/0"}, "TG": {"1": "1/1"}},
            {"DUTA1": {"PB": "1/1"}, "TG": {"1": "1/2"}},
        ]
    }
