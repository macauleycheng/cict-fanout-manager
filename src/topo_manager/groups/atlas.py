"""topo_manager ATLAS v2 輸出 builder（groups/atlas.py，群組設計 §ATLAS Builder）。

純函式、無 I/O、可單元測試：把一群已解析的 traffic ``Session``（每個為一
generator-port ↔ 一 DUT-port 配對）聚合成 ATLAS version 2 shape —— group name 為
JSON 頂層唯一 key，其值為 pair 物件清單，順序＝傳入 ``member_sessions`` 的順序
（即群組 members 的加入順序）。

每個 pair 物件：
  ``{ "<DUT key>": { "<PX>": "<unit>/<port>" }, "TG": { "<slot>": "<slot>/<port>" } }``

TG 側（generator_port，允許裸名或 ``dev:port``）：
  - raw_gen = 含 ':' 時取首個 ':' 之後段，否則原樣。
  - 套 ``_TG_PORT_RE``（``ixia-1/1``、``trex-1/0`` 皆涵蓋）；slot=group(1)、
    port=group(2)，輸出 ``{ "<slot>": "<slot>/<port>" }``。不符丟 AtlasFormatError。

DUT 側（dut_port 一律 ``dev:port``）：
  - dev = 首個 ':' 之前；port_raw = 首個 ':' 之後（tg_ports 的原始宣告字串）。
  - ATLAS key = ``dev.replace("-", "").upper()``（``dut-A1`` → ``DUTA1``）。
  - PA/PB/PC 標籤：依該 DUT 的 ``tg_ports`` 宣告順序決定（權威來源，先於 value
    轉換）；以 port_raw 為索引鍵在 tg_ports 內找位置 i，標籤 = ``"P"+chr('A'+i)``。
    查無 DUT／port_raw 不在 tg_ports／i>=26 皆丟 AtlasFormatError。
  - value（僅套在輸出）：對 port_raw 套 ``_DUT_PORT_RE``；unit 群組選填，裸
    ``EthN`` → unit 預設 "1"（``Eth0``→``1/0``、``Eth5``→``1/5``），帶 unit 的
    ``Eth<unit>/<port>``（含選填空白 ``Eth 2/5``）→ ``2/5``。不符丟 AtlasFormatError。

空群組回 ``{ group_name: [] }``。

歷史（HISTORY）：
  - 0.7.0  群組功能初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import re
from typing import Dict, List

from topo_manager.allocator import Session
from topo_manager.errors import AtlasFormatError
from topo_manager.models import Dut, Topology

# TG 側 generator port 裸名正則（涵蓋 ixia-1/1、trex-1/0）：slot=group(1)、
# port=group(2)。
_TG_PORT_RE = re.compile(r"^[A-Za-z]+-(\d+)/(\d+)$")

# DUT port 裸段正則：unit 群組選填（裸 EthN → unit 預設 "1"）；含選填空白。
_DUT_PORT_RE = re.compile(r"^Eth\s*(?:(\d+)/)?(\d+)$")

# 單字母 PA/PB/PC… 標籤空間上限（超出本期不支援）。
_MAX_LABEL_INDEX = 26


def _find_dut(topology: Topology, dev: str) -> "Dut":
    """在 ``topology.racks[].duts[]`` 以 name==dev 找 DUT；查無回 None。"""
    for rack in topology.racks:
        for dut in rack.duts:
            if dut.name == dev:
                return dut
    return None


def _tg_side(generator_port: str) -> Dict[str, str]:
    """把 generator_port 解析成 TG 側 ``{ "<slot>": "<slot>/<port>" }``。

    例外：
      - AtlasFormatError：裸名不符 ``_TG_PORT_RE``。
    """
    if ":" in generator_port:
        raw_gen = generator_port.split(":", 1)[1]
    else:
        raw_gen = generator_port
    match = _TG_PORT_RE.match(raw_gen)
    if match is None:
        raise AtlasFormatError(
            "ATLAS TG port 格式非法（期望 ^[A-Za-z]+-\\d+/\\d+$）：{0}".format(
                generator_port
            )
        )
    slot = match.group(1)
    port = match.group(2)
    return {slot: "{0}/{1}".format(slot, port)}


def _dut_value(port_raw: str) -> str:
    """把 DUT port 裸段轉成 value ``<unit>/<port>``（裸 EthN → unit 預設 "1"）。

    例外：
      - AtlasFormatError：port_raw 不符 ``_DUT_PORT_RE``。
    """
    match = _DUT_PORT_RE.match(port_raw)
    if match is None:
        raise AtlasFormatError(
            "ATLAS DUT port 格式非法（期望 ^Eth\\s*(?:\\d+/)?\\d+$）：{0}".format(
                port_raw
            )
        )
    unit = match.group(1) if match.group(1) is not None else "1"
    port = match.group(2)
    return "{0}/{1}".format(unit, port)


def _dut_side(dut_port: str, topology: Topology) -> Dict[str, Dict[str, str]]:
    """把 dut_port 解析成 DUT 側 ``{ "<DUT key>": { "<PX>": "<unit>/<port>" } }``。

    例外：
      - AtlasFormatError：查無 DUT、port_raw 不在 tg_ports、索引 >= 26、或
        port_raw value 轉換失敗。
    """
    dev = dut_port.split(":", 1)[0]
    port_raw = dut_port.split(":", 1)[1]
    key = dev.replace("-", "").upper()

    dut = _find_dut(topology, dev)
    if dut is None or port_raw not in dut.tg_ports:
        raise AtlasFormatError(
            "ATLAS DUT port 無法對映（dut_port={0}）：查無 DUT 或 port 不在其 "
            "tg_ports".format(dut_port)
        )
    index = dut.tg_ports.index(port_raw)
    if index >= _MAX_LABEL_INDEX:
        raise AtlasFormatError(
            "ATLAS DUT port 索引 {0} 超出單字母標籤空間（>= {1}），本期不支援："
            "dut_port={2}".format(index, _MAX_LABEL_INDEX, dut_port)
        )
    label = "P" + chr(ord("A") + index)
    value = _dut_value(port_raw)
    return {key: {label: value}}


def build_atlas_v2(
    group_name: str,
    member_sessions: List[Session],
    topology: Topology,
) -> Dict[str, List[Dict[str, Dict[str, str]]]]:
    """把一群 traffic session 聚合成 ATLAS v2 shape（FR-G7）。

    參數：
      - group_name：群組名，為輸出 JSON 頂層唯一 key。
      - member_sessions：已解析的成員 ``Session`` 清單（由 route 自 Allocator
        取得），保序＝群組 members 的加入順序。
      - topology：啟動期單例 ``Topology``，提供 DUT 的 ``tg_ports`` 排序。

    回傳：
      - ``{ group_name: [pair, ...] }``；空群組回 ``{ group_name: [] }``。

    例外：
      - AtlasFormatError：任一 session 的 TG/DUT 側無法由 session/拓樸正確組出。
    """
    pairs: List[Dict[str, Dict[str, str]]] = []
    for session in member_sessions:
        pair: Dict[str, Dict[str, str]] = {}
        pair.update(_dut_side(session.dut_port, topology))
        pair["TG"] = _tg_side(session.generator_port)
        pairs.append(pair)
    return {group_name: pairs}
