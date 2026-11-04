"""互聯 fanout 範例拓樸產生器（gen_interconnect.py，互聯設計 §9）。

本腳本以純標準庫（stdlib）加 PyYAML 產生兩份含 ``interconnect`` 區段的範例
拓樸並 check-in，讓測試不需在執行期跑本腳本：

  - ``topology-interconnect-small.yaml``：小規模，自帶精簡打流側（1 個 ixia-1
    四 port、core-1、每 rack 一台 leaf）；2 rack × 每 rack 2 組 × 4 DUT
    （共 16 DUT），每 rack 一台互聯 fanout ``ic-<r>-1``（48 port，承載 2 組
    = 32 port，示範未滿載）。
  - ``topology-interconnect-fullscale.yaml``：滿規模，打流側直接重用
    ``gen_fullscale.build_topology()``（6 rack × 24 DUT）；每 rack 6 組、2 台
    互聯 fanout（``ic-<r>-1`` 承載組 1..3、``ic-<r>-2`` 承載組 4..6），共 12
    台互聯 fanout、36 組、144 DUT、576 條互聯 link，每台互聯 fanout 48 port
    全部用滿。

互聯接線規則：組 k（1 起算）在其互聯 fanout 內的位置 ``s = (k - 1) % 3``，
組內 DUT 索引 ``i``（0..3）的第 ``j`` 個 il_port（Eth8..Eth11）接 fanout port
``s * 16 + i * 4 + j + 1``。

決定性：全部以固定迴圈順序產生，不使用隨機或時間來源，重複執行得到位元組等價
的 YAML（測試以重新生成後逐位元組比對 check-in 檔）。

用法：

    python examples/gen_interconnect.py

HISTORY：
  - 0.1.0  互聯 fanout 初版：小規模與滿規模兩份範例，main 末端自查規模。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import copy
import os
import sys
from typing import Dict, List

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gen_fullscale  # noqa: E402  (需先把本目錄加入 sys.path)

# ---------------------------------------------------------------------------
# 常數（互聯設計 §9）。
# ---------------------------------------------------------------------------

IL_PORTS = ["Eth8", "Eth9", "Eth10", "Eth11"]
GROUP_SIZE = 4
GROUPS_PER_FANOUT = 3
PORTS_PER_GROUP = GROUP_SIZE * len(IL_PORTS)
IC_FANOUT_PORT_COUNT = 48

SMALL_RACKS = 2
SMALL_GROUPS_PER_RACK = 2
SMALL_TG_PORTS = ["Eth0", "Eth1"]
SMALL_LEAF_PORT_COUNT = 32
SMALL_CORE_PORT_COUNT = 24
SMALL_LEAF_TG_PORT_BASE = 10

SMALL_FILE = "topology-interconnect-small.yaml"
FULLSCALE_FILE = "topology-interconnect-fullscale.yaml"

SMALL_HEADER = (
    "# 互聯 fanout 小規模範例拓樸（互聯設計 §9）—— 由 examples/gen_interconnect.py 產生。\n"
    "#\n"
    "# 請勿手動編輯；如需調整，改 gen_interconnect.py 後重跑：\n"
    "#     python examples/gen_interconnect.py\n"
    "#\n"
    "# 規模：2 rack × 2 組 × 4 DUT（16 DUT）；每 rack 一台互聯 fanout（48 port，承載\n"
    "# 32 port，未滿載）。打流側為精簡樹狀（1 core / 每 rack 1 leaf）。\n"
)

FULLSCALE_HEADER = (
    "# 互聯 fanout 滿規模範例拓樸（互聯設計 §9）—— 由 examples/gen_interconnect.py 產生。\n"
    "#\n"
    "# 請勿手動編輯；如需調整，改 gen_interconnect.py 後重跑：\n"
    "#     python examples/gen_interconnect.py\n"
    "#\n"
    "# 規模：打流側同 topology-fullscale.yaml（6 rack × 24 DUT）；互聯側 12 台互聯\n"
    "# fanout、36 組、144 DUT、576 條 link，每台互聯 fanout 48 port 全部用滿。\n"
)


def _rack_letter(rack_index: int) -> str:
    """rack 序號（1 起算）→ 代號 A、B、…。"""
    return chr(ord("A") + rack_index - 1)


def _ic_fanout_name(rack_index: int, fanout_index: int) -> str:
    """互聯 fanout 名 ``ic-<rack>-<序>``。"""
    return "ic-{0}-{1}".format(rack_index, fanout_index)


def _annotate_group_duts(
    duts: List[Dict[str, object]], rack_index: int, group_count: int
) -> List[Dict[str, object]]:
    """為 rack 內前 ``group_count * 4`` 台 DUT 補 il_ports/slot/group，並回傳互聯 link。

    參數：
      - duts：該 rack 的 DUT dict 清單（就地補欄位）。
      - rack_index：rack 序號（1 起算）。
      - group_count：該 rack 要建立的組數（每 4 台一組，依序）。

    回傳：
      - 互聯 link 清單（a=DUT il_port、b=互聯 fanout port）。
    """
    letter = _rack_letter(rack_index)
    links: List[Dict[str, object]] = []
    for group_number in range(1, group_count + 1):
        fanout_index = (group_number - 1) // GROUPS_PER_FANOUT + 1
        position = (group_number - 1) % GROUPS_PER_FANOUT
        fanout_name = _ic_fanout_name(rack_index, fanout_index)
        for member in range(GROUP_SIZE):
            dut = duts[(group_number - 1) * GROUP_SIZE + member]
            dut["il_ports"] = list(IL_PORTS)
            dut["slot"] = member + 1
            dut["group"] = "{0}-g{1}".format(letter, group_number)
            for port_index, il_port in enumerate(IL_PORTS):
                links.append(
                    {
                        "a": {"dev": dut["name"], "port": il_port},
                        "b": {
                            "dev": fanout_name,
                            "port": position * PORTS_PER_GROUP
                            + member * len(IL_PORTS)
                            + port_index
                            + 1,
                        },
                    }
                )
    return links


def build_fullscale() -> Dict[str, object]:
    """組出滿規模互聯拓樸 dict（打流側重用 gen_fullscale.build_topology）。"""
    topology = copy.deepcopy(gen_fullscale.build_topology())
    topology["site"] = "lab-interconnect-fullscale"
    ic_fanouts: List[Dict[str, object]] = []
    ic_links: List[Dict[str, object]] = []
    for rack_index, rack in enumerate(topology["racks"], start=1):
        groups = len(rack["duts"]) // GROUP_SIZE
        ic_links.extend(_annotate_group_duts(rack["duts"], rack_index, groups))
        fanout_count = (groups + GROUPS_PER_FANOUT - 1) // GROUPS_PER_FANOUT
        for fanout_index in range(1, fanout_count + 1):
            ic_fanouts.append(
                {
                    "name": _ic_fanout_name(rack_index, fanout_index),
                    "port_count": IC_FANOUT_PORT_COUNT,
                    "rack": rack["name"],
                }
            )
    topology["interconnect"] = {"fanouts": ic_fanouts, "links": ic_links}
    return topology


def build_small() -> Dict[str, object]:
    """組出小規模互聯拓樸 dict（自帶精簡打流側，樹狀單上行）。"""
    racks: List[Dict[str, object]] = []
    fanouts: List[Dict[str, object]] = [
        {"name": "core-1", "role": "core", "port_count": SMALL_CORE_PORT_COUNT}
    ]
    links: List[Dict[str, object]] = []
    ic_fanouts: List[Dict[str, object]] = []
    ic_links: List[Dict[str, object]] = []
    gen_ports = ["ixia-1/{0}".format(index) for index in range(1, 5)]
    for offset, gen_port in enumerate(gen_ports):
        links.append(
            {
                "a": {"dev": "ixia-1", "port": gen_port},
                "b": {"dev": "core-1", "port": offset + 1},
            }
        )
    dut_per_rack = SMALL_GROUPS_PER_RACK * GROUP_SIZE
    for rack_index in range(1, SMALL_RACKS + 1):
        letter = _rack_letter(rack_index)
        leaf_name = "leaf-{0}".format(rack_index)
        fanouts.append(
            {"name": leaf_name, "role": "leaf", "port_count": SMALL_LEAF_PORT_COUNT}
        )
        links.append(
            {
                "a": {"dev": leaf_name, "port": 1},
                "b": {"dev": "core-1", "port": len(gen_ports) + rack_index},
            }
        )
        duts: List[Dict[str, object]] = []
        for dut_number in range(1, dut_per_rack + 1):
            duts.append(
                {
                    "name": "dut-{0}{1}".format(letter, dut_number),
                    "model": "model{0}".format(letter),
                    "tg_ports": list(SMALL_TG_PORTS),
                }
            )
        leaf_port = SMALL_LEAF_TG_PORT_BASE
        for dut in duts:
            for tg_port in SMALL_TG_PORTS:
                leaf_port += 1
                links.append(
                    {
                        "a": {"dev": dut["name"], "port": tg_port},
                        "b": {"dev": leaf_name, "port": leaf_port},
                    }
                )
        ic_links.extend(_annotate_group_duts(duts, rack_index, SMALL_GROUPS_PER_RACK))
        ic_fanouts.append(
            {
                "name": _ic_fanout_name(rack_index, 1),
                "port_count": IC_FANOUT_PORT_COUNT,
                "rack": "rack-{0}".format(rack_index),
            }
        )
        racks.append({"name": "rack-{0}".format(rack_index), "duts": duts})
    return {
        "version": 1,
        "site": "lab-interconnect-small",
        "generators": [{"name": "ixia-1", "type": "ixia", "ports": gen_ports}],
        "fanouts": fanouts,
        "racks": racks,
        "links": links,
        "interconnect": {"fanouts": ic_fanouts, "links": ic_links},
    }


def render(topology: Dict[str, object], header: str) -> str:
    """把拓樸 dict 轉成含標頭註解的 YAML 文字（決定性）。"""
    body = yaml.safe_dump(
        topology, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return header + body


def check_fullscale(topology: Dict[str, object]) -> None:
    """規模自查：12 台互聯 fanout、36 組、144 DUT、576 link、每台 48 port 滿載。"""
    section = topology["interconnect"]
    assert len(section["fanouts"]) == 12, "互聯 fanout 應為 12 台"
    groups = set()
    duts = set()
    for rack in topology["racks"]:
        for dut in rack["duts"]:
            if "group" in dut:
                groups.add(dut["group"])
                duts.add(dut["name"])
    assert len(groups) == 36, "組數應為 36"
    assert len(duts) == 144, "互聯 DUT 應為 144 台"
    assert len(section["links"]) == 576, "互聯 link 應為 576 條"
    used: Dict[str, set] = {}
    for link in section["links"]:
        used.setdefault(link["b"]["dev"], set()).add(link["b"]["port"])
    assert len(used) == 12, "每台互聯 fanout 都應有 link"
    for name, ports in used.items():
        assert ports == set(range(1, IC_FANOUT_PORT_COUNT + 1)), (
            "{0} 應 48 port 全部用滿".format(name)
        )


def main() -> None:
    """產生兩份互聯範例並寫到 examples/（以本檔所在目錄為輸出目錄）。"""
    output_dir = os.path.dirname(os.path.abspath(__file__))
    small = build_small()
    fullscale = build_fullscale()
    for file_name, topology, header in (
        (SMALL_FILE, small, SMALL_HEADER),
        (FULLSCALE_FILE, fullscale, FULLSCALE_HEADER),
    ):
        output_path = os.path.join(output_dir, file_name)
        with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render(topology, header))
        print("已產生 {0}".format(output_path))
    check_fullscale(fullscale)


if __name__ == "__main__":
    main()
