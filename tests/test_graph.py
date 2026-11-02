"""graph.py 的 pytest 測試（設計 §11.2、FR-3）。

涵蓋：兩層/三層 summary() 確切數字、tiers key 集合（int key）、tiers 排除
#core、兩層 vs 三層 max(tier)、等距同層直連偵測、路徑清洗掉 #core 虛擬節點。
以範例 YAML（經 load_topology）與 inline 最小拓樸驅動，負向路徑用
pytest.raises。

歷史（HISTORY）：
  - 0.1.0  FEAT-003 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from topo_manager.errors import TopologyGraphError
from topo_manager.graph import TopologyGraph
from topo_manager.loader import load_topology
from topo_manager.models import Topology


def test_summary_counts_two_tier(two_tier_path: Path) -> None:
    """兩層 summary() 回傳確切計數與 role 分組（設計 §11.2）。"""
    graph = TopologyGraph(load_topology(two_tier_path))
    summary = graph.summary()
    assert summary["generator_port_count"] == 6
    assert summary["dut_count"] == 8
    assert summary["dut_port_count"] == 16
    assert summary["fanout_count"] == 3
    assert summary["nodes_by_role"] == {"core": 1, "leaf": 2}


def test_summary_counts_three_tier(three_tier_path: Path) -> None:
    """三層 summary() 回傳確切計數與 role 分組（設計 §11.2）。"""
    graph = TopologyGraph(load_topology(three_tier_path))
    summary = graph.summary()
    assert summary["generator_port_count"] == 6
    assert summary["dut_count"] == 8
    assert summary["dut_port_count"] == 16
    assert summary["fanout_count"] == 5
    assert summary["nodes_by_role"] == {"spine": 1, "agg": 2, "leaf": 2}


def test_tiers_keys_two_tier(two_tier_path: Path) -> None:
    """兩層 tiers key 集合為 {0,1,2,3}（int key），端點層數字正確。"""
    graph = TopologyGraph(load_topology(two_tier_path))
    tiers = graph.summary()["tiers"]
    assert set(tiers.keys()) == {0, 1, 2, 3}
    assert tiers[0] == 6
    assert tiers[3] == 16


def test_tiers_keys_three_tier(three_tier_path: Path) -> None:
    """三層 tiers key 集合為 {0,1,2,3,4}（int key），端點層數字正確。"""
    graph = TopologyGraph(load_topology(three_tier_path))
    tiers = graph.summary()["tiers"]
    assert set(tiers.keys()) == {0, 1, 2, 3, 4}
    assert tiers[0] == 6
    assert tiers[4] == 16


def test_tiers_exclude_core(two_tier_path: Path) -> None:
    """tiers 統計不含任何 kind=='fanout_core' 的 #core 節點。"""
    graph = TopologyGraph(load_topology(two_tier_path))
    tiers = graph.summary()["tiers"]
    digraph = graph._digraph
    # tiers 計數總和須等於非 #core 節點總數。
    non_core_nodes = sum(
        1
        for _, data in digraph.nodes(data=True)
        if data.get("kind") != "fanout_core"
    )
    assert sum(tiers.values()) == non_core_nodes
    # 另確認圖中確有 #core 節點存在（證明是「被排除」而非「不存在」）。
    core_nodes = sum(
        1
        for _, data in digraph.nodes(data=True)
        if data.get("kind") == "fanout_core"
    )
    assert core_nodes == 3


def test_tiers_two_vs_three(two_tier_path: Path, three_tier_path: Path) -> None:
    """兩層 max(tiers)==3 嚴格小於三層 max(tiers)==4（層數由資料決定）。"""
    two_tiers = TopologyGraph(load_topology(two_tier_path)).summary()["tiers"]
    three_tiers = TopologyGraph(load_topology(three_tier_path)).summary()["tiers"]
    assert max(two_tiers.keys()) == 3
    assert max(three_tiers.keys()) == 4
    assert max(two_tiers.keys()) < max(three_tiers.keys())


def _equidistant_topology_dict() -> dict:
    """產生含同層直連（leaf-1-1:9 — leaf-2-1:9）的最小拓樸 dict。

    兩台 leaf 皆各有一條上行到 core、各接一個 DUT，另外在兩台 leaf 之間多一條
    對稱的同層直連。排除該直連後，兩端到 generator 距離相等，觸發 §5.3 等距
    偵測。
    """
    return {
        "version": 1,
        "site": "lab-x",
        "generators": [
            {"name": "ixia-1", "type": "ixia", "ports": ["ixia-1/1"]},
        ],
        "fanouts": [
            {"name": "core-1", "role": "core", "port_count": 24},
            {"name": "leaf-1-1", "role": "leaf", "port_count": 24},
            {"name": "leaf-2-1", "role": "leaf", "port_count": 24},
        ],
        "racks": [
            {
                "name": "rack-1",
                "duts": [
                    {"name": "dut-A1", "model": "m", "tg_ports": ["Eth0"]},
                ],
            },
            {
                "name": "rack-2",
                "duts": [
                    {"name": "dut-B1", "model": "m", "tg_ports": ["Eth0"]},
                ],
            },
        ],
        "links": [
            # generator → core
            {"a": {"dev": "ixia-1", "port": "ixia-1/1"}, "b": {"dev": "core-1", "port": "1"}},
            # leaf → core（兩條對稱上行）
            {"a": {"dev": "leaf-1-1", "port": "1"}, "b": {"dev": "core-1", "port": "7"}},
            {"a": {"dev": "leaf-2-1", "port": "1"}, "b": {"dev": "core-1", "port": "8"}},
            # DUT → leaf（兩台對稱）
            {"a": {"dev": "dut-A1", "port": "Eth0"}, "b": {"dev": "leaf-1-1", "port": "11"}},
            {"a": {"dev": "dut-B1", "port": "Eth0"}, "b": {"dev": "leaf-2-1", "port": "11"}},
            # 同層直連（待偵測的等距邊）
            {"a": {"dev": "leaf-1-1", "port": "9"}, "b": {"dev": "leaf-2-1", "port": "9"}},
        ],
    }


def test_equidistant_edge_rejected() -> None:
    """含同層直連的 inline 拓樸建圖 → TopologyGraphError（設計 §5.3）。"""
    topology = Topology.model_validate(_equidistant_topology_dict())
    with pytest.raises(TopologyGraphError) as exc_info:
        TopologyGraph(topology)
    message = str(exc_info.value)
    assert "leaf-1-1:9" in message
    assert "leaf-2-1:9" in message


def test_path_core_virtual_node_removed(two_tier_path: Path) -> None:
    """find_path 回傳路徑不含任何 kind=='fanout_core' 虛擬節點（設計 §5.5）。"""
    graph = TopologyGraph(load_topology(two_tier_path))
    path = graph.find_path("ixia-1/1", "dut-A1:Eth0")
    assert len(path) > 0
    for node in path:
        assert graph._digraph.nodes[node].get("kind") != "fanout_core"
        assert "#core" not in node
