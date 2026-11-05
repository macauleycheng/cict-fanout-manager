"""topo_manager 群組持久化模型（groups/models.py，群組設計 §資料模型）。

定義群組帳本內的資料結構（鏡像 ``interconnect/models.py`` 的 ``IcLedger``）：
  - ``GroupEntry``：一個群組（純標籤聚合，不持 session 物理資料）。
  - ``GroupLedger``：帳本頂層（version=1、kind="group"）。

NOTES：
  - 成員以 ``List[str]``（保序）而非 ``Set``：JSON 無原生 set，保序讓 ATLAS
    輸出的 pair list 具穩定順序（依加入順序），且 members 規模小（人工操作等
    級），加入前線性檢查重複即可。
  - ``session_id`` 的字串格式（``s-NNNN``）不在模型層以 ``Field(pattern=...)``
    強制（對齊 ``Session`` 的裁決：格式驗證集中在一處 parser）；群組層只把它
    當不透明字串鍵。
  - 不變量「一 session 至多一群」由 ``GroupRegistry``（業務層）擁有與強制，而
    非模型層（跨多個 ``GroupEntry`` 的不變量，單筆模型驗證無法表達）。

歷史（HISTORY）：
  - 0.7.0  群組功能初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field

# 群組帳本 schema 版本與種類（與打流側 version=2、互聯 version=1 帳本三方隔離）。
GROUP_SCHEMA_VERSION = 1
GROUP_LEDGER_KIND = "group"


class GroupEntry(BaseModel):
    """一個群組（純標籤聚合，不持 session 物理資料）。

    欄位：
      - name：群組名（使用者自選，如 "T1"），群組領域內全域唯一。
      - members：成員 session_id 清單（保序，形如 ["s-0001", "s-0003"]）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    members: List[str] = Field(default_factory=list)


class GroupLedger(BaseModel):
    """群組帳本頂層容器（對齊 ``IcLedger``）。

    欄位：
      - version：須等於 ``GROUP_SCHEMA_VERSION``。
      - kind：須等於 ``GROUP_LEDGER_KIND``。
      - groups：群組清單。
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    kind: str
    groups: List[GroupEntry]
