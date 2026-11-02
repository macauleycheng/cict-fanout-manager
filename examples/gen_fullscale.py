"""滿規模三層範例拓樸產生器（gen_fullscale.py，設計 §4 滿規模）。

本腳本以純標準庫（stdlib）加 PyYAML 產生一份「滿規模」三層拓樸，寫到
``examples/topology-fullscale.yaml`` 並 check-in，讓測試不需在執行期跑本腳本。
規模取自設計 §4 滿規模：

  - 6 個 rack（A..F），每 rack 24 台 DUT，每台 DUT 8 個 tg_port
    （共 6 × 24 × 8 = 1152 個 DUT 打流 port）。
  - 1 台 spine、每 rack 1 台 agg（共 6 台 agg）、每 rack 5 台 leaf
    （共 30 台 leaf）。

接線遵守設計 §滿規模 的「嚴格分層樹、單一上行 / 單一下行、無同層直連」不變量，
確保 Phase 1 建圖不會因等距（同層）連線而丟 ``TopologyGraphError``，且每條
generator→DUT 最短路徑在實體 port 層級唯一：

  - spine-1：port 1..12 接 12 個 generator port（ixia-1/1..4、trex-1/0..7），
    port 13..18 各下行到 6 台 agg。
  - agg-r（每 rack 一台）：port 1 上行 spine，port 2..6 各下行到該 rack 的 5 台
    leaf。
  - leaf-r-l（每 rack 5 台）：port 1 上行 agg，其餘 port 接該 rack 的 DUT
    tg_port；該 rack 的 192 個 tg_port 平均分散到 5 台 leaf。

port_count 取 64（寬裕）：單台 leaf 最多承載 ceil(192 / 5) = 39 個 tg_port 加
1 個上行 = 40 個 port，遠低於 64；spine 需 18 個 port、agg 需 6 個。

決定性（determinism）：rack / dut / leaf / port 皆以固定迴圈順序產生，且不使用
任何隨機或時間來源，故重複執行會得到位元組等價的 YAML（AC1）。

用法：

    python examples/gen_fullscale.py

歷史（HISTORY）：
  - 0.2.0  FEAT-006 初版，依設計 §4 滿規模產生 6×24×8 三層拓樸並寫出
           examples/topology-fullscale.yaml。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import os
from typing import Dict, List

import yaml

# ---------------------------------------------------------------------------
# 滿規模常數（設計 §4 滿規模）。
# ---------------------------------------------------------------------------

RACK_COUNT = 6
DUT_PER_RACK = 24
TG_PORT_PER_DUT = 8
LEAF_PER_RACK = 5
FANOUT_PORT_COUNT = 64

# rack 代號 A..F，對應 DUT 命名 dut-A1..dut-F24。
RACK_LETTERS = [chr(ord("A") + index) for index in range(RACK_COUNT)]

# 12 個 generator port：ixia-1/1..4 與 trex-1/0..7。
IXIA_PORTS = ["ixia-1/{0}".format(index) for index in range(1, 5)]
TREX_PORTS = ["trex-1/{0}".format(index) for index in range(0, 8)]
GENERATOR_PORTS = IXIA_PORTS + TREX_PORTS

# spine 下行 agg 的起始 port（1..12 已給 generator，13 起下行 agg）。
SPINE_DOWN_PORT_BASE = 12

# agg port 1 上行 spine；2 起下行 leaf。
AGG_LEAF_PORT_BASE = 1

# leaf port 1 上行 agg；11 起接 DUT tg_port（與既有範例風格一致）。
LEAF_TG_PORT_BASE = 10


def _tg_port_name(index: int) -> str:
    """回傳第 index（0 起算）個 tg_port 名稱 ``Eth0``、``Eth1``…。"""
    return "Eth{0}".format(index)


def _build_generators() -> List[Dict[str, object]]:
    """建 generators 區塊（ixia-1 四 port、trex-1 八 port，設計 §4）。"""
    return [
        {"name": "ixia-1", "type": "ixia", "ports": list(IXIA_PORTS)},
        {"name": "trex-1", "type": "trex", "ports": list(TREX_PORTS)},
    ]


def _build_fanouts() -> List[Dict[str, object]]:
    """建 fanouts 區塊：1 spine、6 agg、30 leaf，皆 port_count=64（設計 §4）。"""
    fanouts: List[Dict[str, object]] = [
        {"name": "spine-1", "role": "spine", "port_count": FANOUT_PORT_COUNT}
    ]
    for rack_index in range(1, RACK_COUNT + 1):
        fanouts.append(
            {
                "name": "agg-{0}".format(rack_index),
                "role": "agg",
                "port_count": FANOUT_PORT_COUNT,
            }
        )
    for rack_index in range(1, RACK_COUNT + 1):
        for leaf_index in range(1, LEAF_PER_RACK + 1):
            fanouts.append(
                {
                    "name": "leaf-{0}-{1}".format(rack_index, leaf_index),
                    "role": "leaf",
                    "port_count": FANOUT_PORT_COUNT,
                }
            )
    return fanouts


def _build_racks() -> List[Dict[str, object]]:
    """建 racks 區塊：6 rack × 24 DUT × 8 tg_port（dut-A1..dut-F24，設計 §4）。"""
    racks: List[Dict[str, object]] = []
    for rack_index in range(1, RACK_COUNT + 1):
        letter = RACK_LETTERS[rack_index - 1]
        duts: List[Dict[str, object]] = []
        for dut_number in range(1, DUT_PER_RACK + 1):
            tg_ports = [
                _tg_port_name(port_index)
                for port_index in range(TG_PORT_PER_DUT)
            ]
            duts.append(
                {
                    "name": "dut-{0}{1}".format(letter, dut_number),
                    "model": "model{0}".format(letter),
                    "tg_ports": tg_ports,
                }
            )
        racks.append(
            {"name": "rack-{0}".format(rack_index), "duts": duts}
        )
    return racks


def _rack_tg_endpoints(rack_index: int) -> List[Dict[str, str]]:
    """回傳某 rack 全部 DUT tg_port 端點（保序：dut 1..24、tg 0..7）。

    參數：
      - rack_index：rack 序號（1 起算）。

    回傳：
      - 每個元素為 ``{"dev": <dut 名>, "port": <tg_port 名>}``，共
        ``DUT_PER_RACK * TG_PORT_PER_DUT`` 個，依 DUT 再依 tg_port 的固定順序。
    """
    letter = RACK_LETTERS[rack_index - 1]
    endpoints: List[Dict[str, str]] = []
    for dut_number in range(1, DUT_PER_RACK + 1):
        dut_name = "dut-{0}{1}".format(letter, dut_number)
        for port_index in range(TG_PORT_PER_DUT):
            endpoints.append(
                {"dev": dut_name, "port": _tg_port_name(port_index)}
            )
    return endpoints


def _build_links() -> List[Dict[str, Dict[str, object]]]:
    """建 links 區塊，依設計 §滿規模 的單一上行 / 單一下行樹狀接線。

    接線順序（固定、決定性）：
      1. generator → spine（12 條，spine port 1..12）。
      2. agg → spine（6 條，spine port 13..18）。
      3. leaf → agg（每 rack 5 條，agg port 2..6）。
      4. DUT tg_port → leaf（每 rack 192 條，平均分散到 5 台 leaf）。

    回傳：
      - link dict 清單，每筆為 ``{"a": {dev, port}, "b": {dev, port}}``。
    """
    links: List[Dict[str, Dict[str, object]]] = []

    # 1. generator → spine（spine port 1..12）。
    for offset, gen_port in enumerate(GENERATOR_PORTS):
        gen_dev = "ixia-1" if gen_port.startswith("ixia-1/") else "trex-1"
        links.append(
            {
                "a": {"dev": gen_dev, "port": gen_port},
                "b": {"dev": "spine-1", "port": offset + 1},
            }
        )

    # 2. agg → spine（spine port 13..18；agg port 1 上行）。
    for rack_index in range(1, RACK_COUNT + 1):
        links.append(
            {
                "a": {"dev": "agg-{0}".format(rack_index), "port": AGG_LEAF_PORT_BASE},
                "b": {"dev": "spine-1", "port": SPINE_DOWN_PORT_BASE + rack_index},
            }
        )

    # 3. leaf → agg（每 rack 5 條；agg port 2..6，leaf port 1 上行）。
    for rack_index in range(1, RACK_COUNT + 1):
        for leaf_index in range(1, LEAF_PER_RACK + 1):
            links.append(
                {
                    "a": {
                        "dev": "leaf-{0}-{1}".format(rack_index, leaf_index),
                        "port": 1,
                    },
                    "b": {
                        "dev": "agg-{0}".format(rack_index),
                        "port": AGG_LEAF_PORT_BASE + leaf_index,
                    },
                }
            )

    # 4. DUT tg_port → leaf（每 rack 192 條，平均分散到 5 台 leaf）。
    for rack_index in range(1, RACK_COUNT + 1):
        endpoints = _rack_tg_endpoints(rack_index)
        # 把該 rack 的 tg_port 以「連續分段」平均分到 5 台 leaf：前幾台多 1 個。
        total = len(endpoints)
        base = total // LEAF_PER_RACK
        remainder = total % LEAF_PER_RACK
        cursor = 0
        for leaf_index in range(1, LEAF_PER_RACK + 1):
            count = base + (1 if leaf_index <= remainder else 0)
            leaf_name = "leaf-{0}-{1}".format(rack_index, leaf_index)
            for local_index in range(count):
                endpoint = endpoints[cursor]
                cursor += 1
                leaf_port = LEAF_TG_PORT_BASE + local_index + 1
                links.append(
                    {
                        "a": {"dev": endpoint["dev"], "port": endpoint["port"]},
                        "b": {"dev": leaf_name, "port": leaf_port},
                    }
                )

    return links


def build_topology() -> Dict[str, object]:
    """組出完整的滿規模拓樸 dict（設計 §4 滿規模）。

    回傳：
      - 可直接 ``yaml.safe_dump`` 的拓樸 dict，鍵序為
        version / site / generators / fanouts / racks / links。
    """
    return {
        "version": 1,
        "site": "lab-fullscale",
        "generators": _build_generators(),
        "fanouts": _build_fanouts(),
        "racks": _build_racks(),
        "links": _build_links(),
    }


def main() -> None:
    """產生滿規模拓樸並寫出 examples/topology-fullscale.yaml（決定性，AC1）。

    以本檔所在目錄為輸出目錄，故不論從哪個 cwd 執行都寫到 examples/ 內。
    使用 ``sort_keys=False`` 保留我們安排的鍵序；``allow_unicode=True`` 讓
    site 等字串原樣輸出。
    """
    topology = build_topology()
    output_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(output_dir, "topology-fullscale.yaml")

    header = (
        "# 滿規模三層範例拓樸（設計 §4 滿規模）—— 由 examples/gen_fullscale.py 產生。\n"
        "#\n"
        "# 請勿手動編輯；如需調整規模或接線，改 gen_fullscale.py 後重跑：\n"
        "#     python examples/gen_fullscale.py\n"
        "#\n"
        "# 規模：6 rack × 24 DUT × 8 tg_port；1 spine / 6 agg / 30 leaf。\n"
        "# 接線：單一上行 / 單一下行樹狀結構，無同層直連（最短路徑唯一）。\n"
    )
    body = yaml.safe_dump(
        topology,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )

    with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(header)
        handle.write(body)

    print("已產生 {0}".format(output_path))


if __name__ == "__main__":
    main()
