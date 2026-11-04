"""互聯範例拓樸的 pytest 測試（互聯設計 §9、§10）。

驗證：兩份範例可載入並印摘要；滿規模為 12 台互聯 fanout／36 組／144 DUT／576
link、每台互聯 fanout 48 port 滿載；重新生成與 check-in 檔位元組一致；兩份範例
的打流側行為（TopologyGraph.summary／find_path）與不含互聯的拓樸一致。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

import pytest
import yaml

from topo_manager.cli import main
from topo_manager.graph import TopologyGraph
from topo_manager.interconnect.layout import build_ic_graph
from topo_manager.loader import load_topology

SMALL = "topology-interconnect-small.yaml"
FULLSCALE = "topology-interconnect-fullscale.yaml"


@pytest.fixture
def gen_module(examples_dir: Path):
    """匯入 examples/gen_interconnect.py（examples 不是套件，臨時加入 sys.path）。"""
    sys.path.insert(0, str(examples_dir))
    try:
        import gen_interconnect

        yield gen_interconnect
    finally:
        sys.path.remove(str(examples_dir))


def _strip_interconnect(path: Path, tmp_path: Path) -> Path:
    """去掉 interconnect 區段與 DUT 的 il_ports/slot/group，寫成新 YAML。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.pop("interconnect")
    for rack in data["racks"]:
        for dut in rack["duts"]:
            for key in ("il_ports", "slot", "group"):
                dut.pop(key, None)
    out = tmp_path / "stripped.yaml"
    out.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return out


def test_fullscale_numbers_and_full_fanouts(examples_dir: Path) -> None:
    graph = build_ic_graph(load_topology(examples_dir / FULLSCALE))
    summary = graph.summary()
    assert summary["fanout_count"] == 12
    assert summary["group_count"] == 36
    assert summary["dut_count"] == 144
    assert summary["link_count"] == 576
    assert all(info == {"used": 48, "total": 48} for info in summary["fanouts"].values())


def test_small_numbers_not_full(examples_dir: Path) -> None:
    summary = build_ic_graph(load_topology(examples_dir / SMALL)).summary()
    assert (summary["fanout_count"], summary["group_count"]) == (2, 4)
    assert (summary["dut_count"], summary["link_count"]) == (16, 64)
    assert all(info == {"used": 32, "total": 48} for info in summary["fanouts"].values())


@pytest.mark.parametrize("name", [SMALL, FULLSCALE])
def test_cli_summary_prints_for_both_examples(
    examples_dir: Path, name: str, capsys: pytest.CaptureFixture
) -> None:
    assert main(["interconnect", "summary", str(examples_dir / name)]) == 0
    out = capsys.readouterr().out
    assert "互聯 fanout 台數" in out
    if name == FULLSCALE:
        assert "互聯 fanout 台數：12" in out
        assert "組數：36    DUT 數：144    link 數：576" in out


def test_regeneration_is_byte_identical(gen_module, examples_dir: Path) -> None:
    small = gen_module.render(gen_module.build_small(), gen_module.SMALL_HEADER)
    full = gen_module.render(gen_module.build_fullscale(), gen_module.FULLSCALE_HEADER)
    assert small.encode("utf-8") == (examples_dir / SMALL).read_bytes()
    assert full.encode("utf-8") == (examples_dir / FULLSCALE).read_bytes()
    gen_module.check_fullscale(gen_module.build_fullscale())


def test_fullscale_traffic_side_matches_plain_fullscale(examples_dir: Path) -> None:
    plain = TopologyGraph(load_topology(examples_dir / "topology-fullscale.yaml"))
    with_ic = TopologyGraph(load_topology(examples_dir / FULLSCALE))
    assert with_ic.summary() == plain.summary()
    for dut in ("dut-A1:Eth0", "dut-F24:Eth7"):
        assert with_ic.find_path("ixia-1/1", dut) == plain.find_path("ixia-1/1", dut)


@pytest.mark.parametrize("name", [SMALL, FULLSCALE])
def test_traffic_side_unaffected_by_interconnect_section(
    examples_dir: Path, tmp_path: Path, name: str
) -> None:
    with_ic = TopologyGraph(load_topology(examples_dir / name))
    stripped = TopologyGraph(load_topology(_strip_interconnect(examples_dir / name, tmp_path)))
    assert with_ic.summary() == stripped.summary()
    paths: Dict[str, object] = {}
    for dut in ("dut-A1:Eth0", "dut-B2:Eth1"):
        paths[dut] = with_ic.find_path("ixia-1/1", dut)
        assert paths[dut] == stripped.find_path("ixia-1/1", dut)
        assert paths[dut][0] == "ixia-1:ixia-1/1" and paths[dut][-1] == dut
