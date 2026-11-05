"""topo_manager 設備下發 REST 的請求／回應模型（api/push_schemas.py）。

MODULE NAME：
  topo_manager.api.push_schemas

PURPOSE：
  定義打流側與互聯側下發端點的 Pydantic 模型（皆 ``extra="forbid"``）：
  逐 fanout 的結果（``FanoutPushResult``）與 read-back 摘要（``ReadbackSummary``）、
  各端點的回應，以及 ``POST /fanouts/setup`` 的請求 body。

NOTES：
  - 錯誤回應沿用 ``api.schemas.ErrorResponse``，本檔不重複定義。
  - 409（apply／release／park 的 read-back 不符）的不符清單放在錯誤 message，
    為多行文字，不是 JSON 陣列；要結構化資料請在失敗後呼叫 verify 端點。

HISTORY：
  - 0.1.0  設備下發 REST 初版。
  - 0.2.0  FEAT-002 合併端點：新增 token 守護的「allocate-and-apply /
           release-and-remove」請求與回應模型。打流側
           ``AllocateAndApplyRequest`` / ``AllocateAndApplyResponse``（回應內嵌
           ``schemas.SessionResponse``，不重複頂層 session_id/vlan）、
           ``ReleaseAndRemoveResponse``（session 已清，僅帶 session_id）；互聯側
           ``IcAllocateAndApplyResponse``（內嵌 ``ic_schemas.IcSessionResponse``
           與內嵌 device 模型）。互聯 allocate 沿用既有 ``IcAllocateRequest``，
           本檔不重複定義。皆 ``extra="forbid"``。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict
from typing_extensions import Literal

from topo_manager.api.ic_schemas import IcSessionResponse
from topo_manager.api.schemas import SessionResponse


class ReadbackSummary(BaseModel):
    """單一 fanout 的 read-back 摘要。

    欄位：
      - verified：是否已通過 apply 後的 read-back（verify 端點為目前是否相符）。
      - checked：檢查的項目數（打流：變更數；互聯：port 數）。
      - missing：不符項目；apply 成功時恆為空，verify 時可非空。
    """

    model_config = ConfigDict(extra="forbid")

    verified: bool
    checked: int
    missing: List[str]


class FanoutPushResult(BaseModel):
    """單一 fanout 的下發結果。

    欄位：
      - fanout：fanout 裝置名。
      - ok：該 fanout 是否成功／相符。
      - commands：下發的指令（verify 為空）。
      - ports：本 fanout 涉及的 port 編號。
      - readback：read-back 摘要；無實際寫入時可為 None。
    """

    model_config = ConfigDict(extra="forbid")

    fanout: str
    ok: bool
    commands: List[str]
    ports: List[int]
    readback: Optional[ReadbackSummary] = None


class SessionApplyResponse(BaseModel):
    """打流 ``POST /sessions/{id}/apply`` 回應。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    vlan: int
    status: Literal["applied"]
    fanouts: List[FanoutPushResult]


class SessionVerifyResponse(BaseModel):
    """打流 ``GET /sessions/{id}/verify`` 回應（不符仍是 HTTP 200，``ok=false``）。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    ok: bool
    fanouts: List[FanoutPushResult]


class SetupRequest(BaseModel):
    """``POST /fanouts/setup`` 請求 body（選填；省略 fanouts＝全部）。"""

    model_config = ConfigDict(extra="forbid")

    fanouts: Optional[List[str]] = None


class SetupFanoutResult(BaseModel):
    """單一 fanout 的全域 tunnel setup 結果。"""

    model_config = ConfigDict(extra="forbid")

    fanout: str
    ok: bool
    tunnel_status: Optional[bool] = None
    commands: List[str]


class SetupPushResponse(BaseModel):
    """``POST /fanouts/setup`` 回應（成功路徑恆 ``ok=true``；讀回非 Enabled 為 409）。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    fanouts: List[SetupFanoutResult]


class IcPushResponse(BaseModel):
    """互聯 apply／release-device 回應。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    action: Literal["apply", "release"]
    vlans: List[int]
    fanouts: List[FanoutPushResult]
    notes: List[str]


class SkippedPort(BaseModel):
    """park 跳過的 port（被某 session 持有）。"""

    model_config = ConfigDict(extra="forbid")

    fanout: str
    port: int
    session_id: str


class IcParkPushResponse(BaseModel):
    """互聯 ``POST /interconnect/park`` 回應。"""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    fanouts: List[FanoutPushResult]
    parked: Dict[str, List[int]]
    skipped: List[SkippedPort]
    warnings: List[str]


class IcVerifyPushResponse(BaseModel):
    """互聯 verify 回應：``problems`` 為扁平字串清單（不解析），不符仍是 HTTP 200。"""

    model_config = ConfigDict(extra="forbid")

    target: str
    ok: bool
    fanouts: List[str]
    problems: List[str]


# ======================================================================
# FEAT-002 合併端點（token 守護的 allocate-and-apply / release-and-remove）
# ======================================================================
class AllocateAndApplyRequest(BaseModel):
    """打流 ``POST /sessions/allocate-and-apply`` 請求 body。

    欄位（對齊 ``schemas.AllocateRequest``）：
      - generator_port：generator 端輸入（裸名或 ``dev:port``）。
      - dut_port：DUT 端輸入（``dev:port``）。

    注意：此 body 於 handler 內手動解析（見 push_routes 模組 NOTES），故這裡
    不設 ``min_length``；空字串交由 allocator 的 find_path 判為 PathLookupError。
    """

    model_config = ConfigDict(extra="forbid")

    generator_port: str
    dut_port: str


class AllocateAndApplyResponse(BaseModel):
    """打流 ``POST /sessions/allocate-and-apply`` 回應（先入帳本、再下發）。

    欄位：
      - status：固定 ``allocated-and-applied``。
      - session：剛建立的 session（內嵌 ``SessionResponse``，status=allocated）；
        session_id / vlan 皆在 ``session.*`` 之內，不於頂層重複。
      - fanouts：逐 fanout 的下發結果（僅列有變更者）。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["allocated-and-applied"]
    session: SessionResponse
    fanouts: List[FanoutPushResult]


class ReleaseAndRemoveResponse(BaseModel):
    """打流 ``POST /sessions/{id}/release-and-remove`` 回應（先撤設備、再清帳本）。

    欄位：
      - status：固定 ``released-and-removed``。
      - session_id：已釋放的 session_id（session 本體已清，不內嵌）。
      - fanouts：逐 fanout 的撤除結果。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["released-and-removed"]
    session_id: str
    fanouts: List[FanoutPushResult]


class IcAllocateAndApplyDevice(BaseModel):
    """互聯 allocate-and-apply 回應的 device 區塊（下發後的設備面結果）。

    欄位：
      - vlans：本次涉及的 VLAN（升冪）。
      - fanouts：逐 fanout 的下發結果。
      - notes：apply 回讀備註（如 Link Status）。
    """

    model_config = ConfigDict(extra="forbid")

    vlans: List[int]
    fanouts: List[FanoutPushResult]
    notes: List[str]


class IcAllocateAndApplyResponse(BaseModel):
    """互聯 ``POST /interconnect/sessions/allocate-and-apply`` 回應。

    欄位：
      - status：固定 ``allocated-and-applied``。
      - session：剛建立的互聯 session（內嵌 ``IcSessionResponse``，
        status=allocated）。
      - device：下發後的設備面結果（vlans / fanouts / notes）。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["allocated-and-applied"]
    session: IcSessionResponse
    device: IcAllocateAndApplyDevice
