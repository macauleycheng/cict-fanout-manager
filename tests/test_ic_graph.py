"""IcGraph 建構與查詢測試（互聯設計 §2.4、§10）。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from ic_fakes import make_ic_data, make_ic_topology, write_topology
from topo_manager.errors import InterconnectLayoutError, InterconnectRequestError
from topo_manager.graph import TopologyGraph
from topo_manager.interconnect.layout import build_ic_graph
from topo_manager.loader import load_topology


def test_build_basic(tmp_path: Path) -> None:
    graph = build_ic_graph(make_ic_topology(tmp_path, groups=2))
    assert list(graph.groups) == ["g1", "g2"]
    group = graph.groups["g2"]
    assert [d.index for d in group.duts] == [0, 1, 2, 3]
    assert group.fanout == "ic-1" and group.rack == "rack-1" and group.model == "modelA"
    # 組 2 (k=1) 第 0 台 p0 → fanout port 17。
    assert group.duts[0].ports[0].node == "ic-1:17"
    assert group.duts[3].ports[3].fanout_port == 32
    assert graph.nx.number_of_edges() == 32
    assert all(degree == 1 for _, degree in graph.nx.degree())


def test_duts_sorted_by_slot_and_ports_by_il_order(tmp_path: Path) -> None:
    def mutate(data):
        duts = data["racks"][0]["duts"]
        random.Random(1).shuffle(duts)
        duts[0]["il_ports"].reverse()

    topology = make_ic_topology(tmp_path, mutate=mutate)
    group = build_ic_graph(topology).groups["g1"]
    assert [d.slot for d in group.duts] == [1, 2, 3, 4]
    for dut in group.duts:
        assert [p.p_index for p in dut.ports] == [0, 1, 2, 3]
        assert [p.il_port for p in dut.ports] == list(
            next(
                d for d in topology.racks[0].duts if d.name == dut.name
            ).il_ports
        )


def test_resolve_port_literal_and_logical(tmp_path: Path) -> None:
    graph = build_ic_graph(make_ic_topology(tmp_path))
    assert graph.resolve_port("g1", "dut-g1-1", "Eth9").p_index == 1
    assert graph.resolve_port("g1", "dut-g1-1", "p3").il_port == "Eth11"


def test_resolve_port_errors(tmp_path: Path) -> None:
    graph = build_ic_graph(make_ic_topology(tmp_path))
    with pytest.raises(InterconnectRequestError, match="無 port 'p4'"):
        graph.resolve_port("g1", "dut-g1-1", "p4")
    with pytest.raises(InterconnectRequestError, match="找不到組"):
        graph.resolve_port("zz", "dut-g1-1", "p0")
    with pytest.raises(InterconnectRequestError, match="不屬於組"):
        graph.resolve_port("g1", "nobody", "p0")
    with pytest.raises(InterconnectRequestError, match="找不到 DUT"):
        graph.group_of_dut("nobody")


def test_summary(tmp_path: Path) -> None:
    summary = build_ic_graph(make_ic_topology(tmp_path, groups=2)).summary()
    assert summary["fanout_count"] == 1
    assert summary["group_count"] == 2
    assert summary["dut_count"] == 8
    assert summary["link_count"] == 32
    assert summary["fanouts"] == {"ic-1": {"used": 32, "total": 48}}
    assert summary["groups"][0]["duts"] == ["dut-g1-0", "dut-g1-1", "dut-g1-2", "dut-g1-3"]


def test_build_without_section_rejected(tmp_path: Path) -> None:
    data = make_ic_data()
    del data["interconnect"]
    topology = load_topology(write_topology(tmp_path, data))
    with pytest.raises(InterconnectLayoutError, match="沒有 interconnect"):
        build_ic_graph(topology)


def test_traffic_graph_unchanged_by_section(tmp_path: Path) -> None:
    data = make_ic_data()
    with_ic = load_topology(write_topology(tmp_path, data, "a.yaml"))
    del data["interconnect"]
    without = load_topology(write_topology(tmp_path, data, "b.yaml"))
    graph_a, graph_b = TopologyGraph(with_ic), TopologyGraph(without)
    assert graph_a.summary() == graph_b.summary()
