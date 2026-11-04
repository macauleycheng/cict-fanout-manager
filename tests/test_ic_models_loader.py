"""互聯區段 schema、載入驗證與序列化不變性測試（互聯設計 §2、§10）。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ic_fakes import make_ic_data, make_ic_topology, write_topology
from topo_manager.errors import (
    InterconnectLayoutError,
    TopologyReferenceError,
    TopologyValidationError,
)
from topo_manager.loader import load_topology


def _ic_links(data):
    return data["interconnect"]["links"]


def _duts(data):
    return data["racks"][0]["duts"]


def test_valid_section_loads(tmp_path: Path) -> None:
    topology = make_ic_topology(tmp_path, groups=3)
    assert topology.interconnect is not None
    assert len(topology.interconnect.links) == 48


def test_existing_examples_unchanged(examples_dir: Path) -> None:
    for path in sorted(examples_dir.glob("topology-*.yaml")):
        if "interconnect" in path.name:
            continue
        assert load_topology(path).interconnect is None


def test_model_dump_excludes_interconnect(tmp_path: Path) -> None:
    topology = make_ic_topology(tmp_path)
    assert "interconnect" not in topology.model_dump()
    assert "interconnect" not in topology.model_dump_json()


def test_api_topology_fields_unchanged(tmp_path: Path) -> None:
    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    data = make_ic_data()
    with_ic = write_topology(tmp_path, data, "with.yaml")
    del data["interconnect"]
    without = write_topology(tmp_path, data, "without.yaml")
    bodies = []
    for path in (with_ic, without):
        settings = Settings(
            topology_path=str(path),
            state_path=str(tmp_path / "s" / "a.json"),
            vlan_min=100,
            vlan_max=3000,
        )
        response = TestClient(create_app(settings)).get("/topology")
        assert response.status_code == 200
        bodies.append(response.json())
    assert set(bodies[0]) == set(bodies[1])
    assert bodies[0] == bodies[1]


def test_schema_unknown_key_rejected(tmp_path: Path) -> None:
    data = make_ic_data()
    data["interconnect"]["bogus"] = 1
    with pytest.raises(TopologyValidationError, match="interconnect"):
        load_topology(write_topology(tmp_path, data))


def test_schema_empty_lists_rejected(tmp_path: Path) -> None:
    data = make_ic_data()
    data["interconnect"]["links"] = []
    with pytest.raises(TopologyValidationError, match="interconnect.links"):
        load_topology(write_topology(tmp_path, data))
    data = make_ic_data()
    data["interconnect"]["fanouts"] = []
    with pytest.raises(TopologyValidationError, match="interconnect.fanouts"):
        load_topology(write_topology(tmp_path, data))


def test_layout_error_is_reference_error(tmp_path: Path) -> None:
    def mutate(data):
        data["interconnect"]["fanouts"][0]["name"] = "ixia-1"

    with pytest.raises(TopologyReferenceError):
        make_ic_topology(tmp_path, mutate=mutate)


def _expect(tmp_path: Path, mutate, pattern: str) -> None:
    with pytest.raises(InterconnectLayoutError, match=pattern):
        make_ic_topology(tmp_path, mutate=mutate)


def test_duplicate_ic_fanout_name(tmp_path: Path) -> None:
    def mutate(data):
        data["interconnect"]["fanouts"].append({"name": "ic-1", "port_count": 8})

    _expect(tmp_path, mutate, "互聯 fanout 名重複：ic-1")


@pytest.mark.parametrize("clash", ["ixia-1", "leaf-1", "dut-g1-0", "rack-1"])
def test_ic_name_clash_with_existing(tmp_path: Path, clash: str) -> None:
    def mutate(data):
        data["interconnect"]["fanouts"][0]["name"] = clash
        for link in _ic_links(data):
            link["b"]["dev"] = clash

    _expect(tmp_path, mutate, "與既有裝置/rack 名衝突")


def test_fanout_port_out_of_range(tmp_path: Path) -> None:
    def mutate(data):
        _ic_links(data)[2]["b"]["port"] = 49

    _expect(tmp_path, mutate, "第 3 條.*有效範圍 1..48")


def test_fanout_port_not_numeric(tmp_path: Path) -> None:
    def mutate(data):
        _ic_links(data)[0]["b"]["port"] = "x1"

    _expect(tmp_path, mutate, "第 1 條.*非合法 port 編號")


def test_reversed_direction(tmp_path: Path) -> None:
    def mutate(data):
        link = _ic_links(data)[0]
        link["a"], link["b"] = link["b"], link["a"]

    _expect(tmp_path, mutate, "方向寫反")


def test_link_to_traffic_side_device_rejected(tmp_path: Path) -> None:
    def mutate(data):
        _ic_links(data)[0]["b"] = {"dev": "leaf-1", "port": 5}

    _expect(tmp_path, mutate, "打流側裝置")


def test_a_end_not_dut(tmp_path: Path) -> None:
    def mutate(data):
        data["interconnect"]["fanouts"].append({"name": "ic-2", "port_count": 8})
        _ic_links(data)[0]["a"] = {"dev": "ic-2", "port": 1}

    _expect(tmp_path, mutate, "a 端 'ic-2' 不是 DUT")


def test_tg_port_rejected(tmp_path: Path) -> None:
    def mutate(data):
        _ic_links(data)[0]["a"]["port"] = "Eth0"

    _expect(tmp_path, mutate, "是 tg_port")


def test_port_not_in_il_ports(tmp_path: Path) -> None:
    def mutate(data):
        _ic_links(data)[0]["a"]["port"] = "Eth99"

    _expect(tmp_path, mutate, "Eth99 不在該 DUT 的 il_ports")


def test_duplicate_dut_port(tmp_path: Path) -> None:
    def mutate(data):
        _ic_links(data)[1]["a"] = dict(_ic_links(data)[0]["a"])

    _expect(tmp_path, mutate, "第 1 條與第 2 條重複使用 DUT port")


def test_duplicate_fanout_port(tmp_path: Path) -> None:
    def mutate(data):
        _ic_links(data)[1]["b"]["port"] = _ic_links(data)[0]["b"]["port"]

    _expect(tmp_path, mutate, "第 1 條與第 2 條重複使用互聯 fanout port")


def test_logical_name_must_match_position(tmp_path: Path) -> None:
    def mutate(data):
        dut = _duts(data)[0]
        dut["il_ports"] = ["p1", "p0", "Eth10", "Eth11"]
        for link in _ic_links(data)[:4]:
            link["a"]["port"] = {"Eth8": "p1", "Eth9": "p0"}.get(
                link["a"]["port"], link["a"]["port"]
            )

    _expect(tmp_path, mutate, "必須等於自身位置")


def test_logical_names_at_own_position_ok(tmp_path: Path) -> None:
    def mutate(data):
        dut = _duts(data)[0]
        dut["il_ports"] = ["p0", "p1", "p2", "p3"]
        mapping = dict(zip(["Eth8", "Eth9", "Eth10", "Eth11"], dut["il_ports"]))
        for link in _ic_links(data)[:4]:
            link["a"]["port"] = mapping[link["a"]["port"]]

    assert make_ic_topology(tmp_path, mutate=mutate).interconnect is not None


def test_missing_slot(tmp_path: Path) -> None:
    def mutate(data):
        del _duts(data)[1]["slot"]

    _expect(tmp_path, mutate, "缺少 group 或 slot")


def test_missing_group(tmp_path: Path) -> None:
    def mutate(data):
        del _duts(data)[1]["group"]

    _expect(tmp_path, mutate, "缺少 group 或 slot")


def test_group_not_four(tmp_path: Path) -> None:
    def mutate(data):
        dut = _duts(data)[3]
        data["interconnect"]["links"] = [
            link for link in _ic_links(data) if link["a"]["dev"] != dut["name"]
        ]

    _expect(tmp_path, mutate, "為 3 台，須恰為 4 台")


def test_cross_rack_group(tmp_path: Path) -> None:
    def mutate(data):
        moved = _duts(data).pop()
        data["racks"].append({"name": "rack-2", "duts": [moved]})

    _expect(tmp_path, mutate, "跨 rack")


def test_model_mismatch(tmp_path: Path) -> None:
    def mutate(data):
        _duts(data)[2]["model"] = "modelB"

    _expect(tmp_path, mutate, "model 不一致")


def test_duplicate_slot(tmp_path: Path) -> None:
    def mutate(data):
        _duts(data)[2]["slot"] = 1

    _expect(tmp_path, mutate, "slot 重複")


def test_il_ports_not_four(tmp_path: Path) -> None:
    def mutate(data):
        _duts(data)[0]["il_ports"] = ["Eth8", "Eth9", "Eth10"]
        data["interconnect"]["links"] = [
            link
            for link in _ic_links(data)
            if not (link["a"]["dev"] == _duts(data)[0]["name"] and link["a"]["port"] == "Eth11")
        ]

    _expect(tmp_path, mutate, "il_ports 為 3 個")


def test_il_port_without_link(tmp_path: Path) -> None:
    def mutate(data):
        name = _duts(data)[0]["name"]
        data["interconnect"]["links"] = [
            link
            for link in _ic_links(data)
            if not (link["a"]["dev"] == name and link["a"]["port"] == "Eth11")
        ]

    _expect(tmp_path, mutate, "未全部接上.*Eth11")


def test_group_across_fanouts(tmp_path: Path) -> None:
    def mutate(data):
        data["interconnect"]["fanouts"].append({"name": "ic-2", "port_count": 48})
        _ic_links(data)[0]["b"]["dev"] = "ic-2"

    _expect(tmp_path, mutate, "跨互聯 fanout.*ic-1, ic-2")


def test_fanout_rack_mismatch(tmp_path: Path) -> None:
    def mutate(data):
        data["interconnect"]["fanouts"][0]["rack"] = "rack-9"

    _expect(tmp_path, mutate, "屬於 rack 'rack-9'")


def test_fanout_without_rack_ok(tmp_path: Path) -> None:
    def mutate(data):
        del data["interconnect"]["fanouts"][0]["rack"]

    assert make_ic_topology(tmp_path, mutate=mutate).interconnect is not None
