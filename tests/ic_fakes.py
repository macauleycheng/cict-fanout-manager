"""互聯測試共用輔助（tests/ic_fakes.py，互聯設計 §10）。

提供 ``make_ic_data``（以 dict 組出含 interconnect 區段的小拓樸，可由呼叫端
就地修改以注入錯誤變體）、``write_topology``（寫 tmp YAML）與
``make_ic_topology``（寫檔並以 ``load_topology`` 載入）。

預設拓樸：1 台打流 generator、1 台打流 fanout、``groups`` 組互聯 DUT（每組 4
台，il_ports=Eth8..Eth11，slot 1..4），全在 rack-1；互聯 fanout ``ic-1``
（48 port，最多承載 3 組）。組 k（0 起算）第 i 台 DUT 第 j 個 il_port 接
``ic-1`` 的 port ``k*16 + i*4 + j + 1``。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional

import yaml

from topo_manager.loader import load_topology
from topo_manager.models import Topology

IL_PORTS = ["Eth8", "Eth9", "Eth10", "Eth11"]


def make_ic_data(groups: int = 1) -> Dict:
    """回傳含 interconnect 區段的拓樸 dict。"""
    duts = []
    ic_links = []
    for k in range(groups):
        for i in range(4):
            name = "dut-g{0}-{1}".format(k + 1, i)
            duts.append(
                {
                    "name": name,
                    "model": "modelA",
                    "tg_ports": ["Eth0", "Eth1"],
                    "il_ports": list(IL_PORTS),
                    "slot": i + 1,
                    "group": "g{0}".format(k + 1),
                }
            )
            for j, il_port in enumerate(IL_PORTS):
                ic_links.append(
                    {
                        "a": {"dev": name, "port": il_port},
                        "b": {"dev": "ic-1", "port": k * 16 + i * 4 + j + 1},
                    }
                )
    return {
        "version": 1,
        "site": "ic-test",
        "generators": [{"name": "ixia-1", "type": "ixia", "ports": [1, 2]}],
        "fanouts": [{"name": "leaf-1", "role": "leaf", "port_count": 32}],
        "racks": [{"name": "rack-1", "duts": duts}],
        "links": [
            {"a": {"dev": "ixia-1", "port": 1}, "b": {"dev": "leaf-1", "port": 1}},
            {"a": {"dev": "leaf-1", "port": 11}, "b": {"dev": duts[0]["name"], "port": "Eth0"}},
        ],
        "interconnect": {
            "fanouts": [{"name": "ic-1", "port_count": 48, "rack": "rack-1"}],
            "links": ic_links,
        },
    }


def write_topology(tmp_path: Path, data: Dict, name: str = "topo.yaml") -> Path:
    """把拓樸 dict 寫成 YAML 檔並回傳路徑。"""
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def make_ic_topology(
    tmp_path: Path,
    groups: int = 1,
    mutate: Optional[Callable[[Dict], None]] = None,
) -> Topology:
    """組拓樸 dict（可選 ``mutate`` 就地修改）→ 寫 tmp YAML → load_topology。"""
    data = make_ic_data(groups)
    if mutate is not None:
        mutate(data)
    return load_topology(write_topology(tmp_path, data))
