"""topo_manager 互聯持久化模型（interconnect/models.py，互聯設計 §4.3）。

定義帳本內的資料結構：
  - ``IcLink``：一條邏輯 link（專屬 VLAN、兩端 DUT port 與 fanout 節點）。
  - ``IcSession``：一個互聯 session（組內選定 DUT + profile 的所有 link）。
  - ``IcLedger``：帳本頂層（version=1、kind="interconnect"）。

NOTES：
  - ``phase``/``profile``/``kind`` 的值域不用 pydantic Literal，由 allocator
    還原層驗證，使錯誤統一走 ``SessionStoreError`` 並帶修復指引。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

# 互聯帳本 schema 版本與種類（與打流側 version=2 帳本區隔）。
IC_SCHEMA_VERSION = 1
IC_LEDGER_KIND = "interconnect"


class IcLink(BaseModel):
    """一條邏輯 link。

    欄位：
      - vlan：此 link 專屬 VLAN（互聯領域內全域唯一）。
      - a_dut / a_port / a_node：A 端 DUT 名、DUT 側 il_port 名、fanout 節點
        ``"<fanout>:<n>"``。
      - b_dut / b_port / b_node：B 端，同上。
      - kind："paired"（profile 展開）或 "explicit"（明確 port 對）。
    """

    model_config = ConfigDict(extra="forbid")

    vlan: int
    a_dut: str
    a_port: str
    a_node: str
    b_dut: str
    b_port: str
    b_node: str
    kind: str


class IcSession(BaseModel):
    """一個互聯 session。

    欄位：
      - session_id：``ic-NNNN``（格式由 allocator 還原層嚴格驗證）。
      - group / fanout：所屬組與其互聯 fanout。
      - profile："mesh" | "line" | "ring" | "explicit"。
      - duts：選定順序的 DUT 名（explicit 為出現順序去重）。
      - phase："allocated" | "releasing"。
      - links：此 session 的所有 link。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    group: str
    fanout: str
    profile: str
    duts: List[str]
    phase: str
    links: List[IcLink]


class IcLedger(BaseModel):
    """互聯帳本頂層容器。

    欄位：
      - version：須等於 ``IC_SCHEMA_VERSION``。
      - kind：須等於 ``IC_LEDGER_KIND``。
      - next_seq：序號高水位（可選，>= 1）。
      - sessions：session 清單。
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    kind: str
    next_seq: Optional[int] = Field(default=None, ge=1)
    sessions: List[IcSession]
