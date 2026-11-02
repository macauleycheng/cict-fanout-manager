"""loader.py 的 pytest 測試（設計 §11.1、FR-6.1、FR-2）。

涵蓋：兩層/三層範例成功載入、故意錯誤 YAML 被參照完整性擋下、缺必填欄位、
未知欄位、fanout port 越界/非數字/非 ASCII 數字、generator 內 port 重複、
DUT 跨 rack 重名。負向案例以 tmp_path 寫入最小 YAML 驅動。

歷史（HISTORY）：
  - 0.1.0  FEAT-002 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from topo_manager.errors import (
    TopologyReferenceError,
    TopologyValidationError,
)
from topo_manager.loader import load_topology
from topo_manager.models import Topology


def _write_yaml(tmp_path: Path, text: str) -> Path:
    """把 YAML 內容寫入暫存檔並回傳路徑，供負向測試驅動。"""
    target = tmp_path / "topology.yaml"
    target.write_text(text, encoding="utf-8")
    return target


def _minimal_yaml(
    fanout_role: str = "core",
    fanout_port: str = "1",
    generator_ports: str = "      - ixia-1/1\n      - ixia-1/2\n",
    extra_generator_field: str = "",
    duplicate_dut: bool = False,
    omit_role: bool = False,
) -> str:
    """產生一份最小但結構完整的兩裝置拓樸 YAML，供各負向測試客製單點錯誤。

    參數以關鍵字控制要注入的單一缺陷；其餘部分維持合法，確保測試只命中目標
    檢查路徑。
    """
    role_line = "" if omit_role else "    role: {0}\n".format(fanout_role)
    second_dut = ""
    if duplicate_dut:
        # 在 rack-2 放一台與 rack-1 同名的 dut-A1，觸發跨 rack 重名（N4）。
        second_dut = (
            "  - name: rack-2\n"
            "    duts:\n"
            "      - name: dut-A1\n"
            "        model: modelB\n"
            "        tg_ports: [Eth0]\n"
        )
    return (
        "version: 1\n"
        "site: lab-a\n"
        "generators:\n"
        "  - name: ixia-1\n"
        "    type: ixia\n"
        "{extra_gen}"
        "    ports:\n"
        "{gen_ports}"
        "fanouts:\n"
        "  - name: core-1\n"
        "{role_line}"
        "    port_count: 24\n"
        "racks:\n"
        "  - name: rack-1\n"
        "    duts:\n"
        "      - name: dut-A1\n"
        "        model: modelA\n"
        "        tg_ports: [Eth0]\n"
        "{second_dut}"
        "links:\n"
        "  - a: {{dev: ixia-1, port: ixia-1/1}}\n"
        "    b: {{dev: core-1, port: {fanout_port}}}\n"
    ).format(
        extra_gen=extra_generator_field,
        gen_ports=generator_ports,
        role_line=role_line,
        second_dut=second_dut,
        fanout_port=fanout_port,
    )


def test_load_two_tier_ok(two_tier_path: Path) -> None:
    """兩層範例成功載入，回傳 Topology，欄位數正確。"""
    topology = load_topology(two_tier_path)
    assert isinstance(topology, Topology)
    assert len(topology.generators) == 2
    assert len(topology.fanouts) == 3
    assert len(topology.racks) == 2
    # 6 generator→core + 2 leaf→core + 16 DUT→leaf = 24 條 link。
    assert len(topology.links) == 24


def test_load_three_tier_ok(three_tier_path: Path) -> None:
    """三層範例成功載入，回傳 Topology，欄位數正確。"""
    topology = load_topology(three_tier_path)
    assert isinstance(topology, Topology)
    assert len(topology.generators) == 2
    assert len(topology.fanouts) == 5
    assert len(topology.racks) == 2
    # 6 generator→spine + 2 agg→spine + 2 leaf→agg + 16 DUT→leaf = 26 條 link。
    assert len(topology.links) == 26


def test_invalid_yaml_rejected(invalid_path: Path) -> None:
    """載入故意錯誤 YAML → TopologyReferenceError，訊息含 leaf-9-9。"""
    with pytest.raises(TopologyReferenceError) as exc_info:
        load_topology(invalid_path)
    assert "leaf-9-9" in str(exc_info.value)


def test_missing_required_field(tmp_path: Path) -> None:
    """砍掉 fanout 的 role → TopologyValidationError，訊息含 role。"""
    yaml_text = _minimal_yaml(omit_role=True)
    with pytest.raises(TopologyValidationError) as exc_info:
        load_topology(_write_yaml(tmp_path, yaml_text))
    assert "role" in str(exc_info.value)


def test_unknown_field_forbidden(tmp_path: Path) -> None:
    """多塞未知欄位 → 驗證失敗（extra='forbid'）。"""
    yaml_text = _minimal_yaml(extra_generator_field="    vendor: acme\n")
    with pytest.raises(TopologyValidationError):
        load_topology(_write_yaml(tmp_path, yaml_text))


def test_bad_port_reference_out_of_range(tmp_path: Path) -> None:
    """fanout port 越界 → TopologyReferenceError，訊息含範圍 1..port_count。"""
    yaml_text = _minimal_yaml(fanout_port="99")
    with pytest.raises(TopologyReferenceError) as exc_info:
        load_topology(_write_yaml(tmp_path, yaml_text))
    message = str(exc_info.value)
    assert "1..24" in message


def test_bad_port_reference_non_numeric(tmp_path: Path) -> None:
    """fanout 端 port 為 '5a' → TopologyReferenceError（非法 port 編號）。"""
    yaml_text = _minimal_yaml(fanout_port="5a")
    with pytest.raises(TopologyReferenceError) as exc_info:
        load_topology(_write_yaml(tmp_path, yaml_text))
    assert "5a" in str(exc_info.value)


def test_bad_port_reference_non_ascii_digit(tmp_path: Path) -> None:
    """fanout 端 port 為全形 '５' → TopologyReferenceError（鎖住 N5 判準）。"""
    # 以引號包住全形數字，確保 YAML 解析為字串而非整數。
    yaml_text = _minimal_yaml(fanout_port='"\uff15"')
    with pytest.raises(TopologyReferenceError):
        load_topology(_write_yaml(tmp_path, yaml_text))


def test_duplicate_port_in_generator(tmp_path: Path) -> None:
    """同一 generator 內 port 重複 → TopologyValidationError。"""
    dup_ports = "      - ixia-1/1\n      - ixia-1/1\n"
    yaml_text = _minimal_yaml(generator_ports=dup_ports)
    with pytest.raises(TopologyValidationError):
        load_topology(_write_yaml(tmp_path, yaml_text))


def test_duplicate_dut_name_across_racks(tmp_path: Path) -> None:
    """兩 rack 同名 DUT → TopologyReferenceError，訊息含重複名。"""
    yaml_text = _minimal_yaml(duplicate_dut=True)
    with pytest.raises(TopologyReferenceError) as exc_info:
        load_topology(_write_yaml(tmp_path, yaml_text))
    assert "dut-A1" in str(exc_info.value)
