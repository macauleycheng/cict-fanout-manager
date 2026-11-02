"""通用多層回歸測試（設計 §11.3、FR-6.2/6.3/6.4）—— 最重要的回歸保護。

以 @pytest.mark.parametrize 對兩層／三層範例跑「同一段」find_path，每筆帶該
範例的預期 role 清單（兩層 ['core','leaf']、三層 ['spine','agg','leaf']）。
斷言採可判定、不綁死整條序列的準則：首／末節點、無 #core、tier_level 單調
不減、fanout role 去重子序列等於預期清單。

這些預期清單是「參數化資料」，非程式分支——find_path 本體無任何
``if 層數==2/3`` 或 role 大小比較，證明「同一套演算法對兩份範例皆正確」。

歷史（HISTORY）：
  - 0.1.0  FEAT-003 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from topo_manager.graph import TopologyGraph
from topo_manager.loader import load_topology


def _dedupe_consecutive(values: List[str]) -> List[str]:
    """去除清單中的「連續重複」元素，回傳去重後的子序列。

    例如 ['core','core','leaf','leaf'] → ['core','leaf']。用於把路徑上
    同一台 fanout 的進／出 port（兩個相同 role 節點）併為一次。
    """
    result: List[str] = []
    for value in values:
        if len(result) == 0 or result[-1] != value:
            result.append(value)
    return result


@pytest.mark.parametrize(
    "yaml_name, expected_roles",
    [
        ("topology-2tier.yaml", ["core", "leaf"]),
        ("topology-3tier.yaml", ["spine", "agg", "leaf"]),
    ],
)
def test_find_path_generic_multitier(
    examples_dir: Path, yaml_name: str, expected_roles: List[str]
) -> None:
    """同一段 find_path 對兩份範例皆回正確路徑（設計 §11.3，通用多層回歸）。

    對固定 (ixia-1/1, dut-A1:Eth0) 斷言：
      - 路徑非空；首節點==ixia-1:ixia-1/1、末節點==dut-A1:Eth0。
      - 序列不含 kind=='fanout_core'。
      - 沿路徑 tier_level 單調不減（t[i] <= t[i+1]）。
      - fanout role 去除連續重複後的子序列 == expected_roles。
    """
    graph = TopologyGraph(load_topology(examples_dir / yaml_name))
    path = graph.find_path("ixia-1/1", "dut-A1:Eth0")

    # 路徑非空、首末節點正確。
    assert len(path) > 0
    assert path[0] == "ixia-1:ixia-1/1"
    assert path[-1] == "dut-A1:Eth0"

    # 不含 #core 虛擬節點。
    for node in path:
        assert graph._digraph.nodes[node].get("kind") != "fanout_core"

    # tier_level 單調不減。
    tier_sequence = [graph.tier_level_of(node) for node in path]
    for index in range(len(tier_sequence) - 1):
        assert tier_sequence[index] <= tier_sequence[index + 1]

    # fanout role 去除連續重複後的子序列等於預期清單。
    fanout_roles = [
        graph._digraph.nodes[node].get("role")
        for node in path
        if graph._digraph.nodes[node].get("kind") == "fanout"
    ]
    assert _dedupe_consecutive(fanout_roles) == expected_roles
