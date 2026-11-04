"""topo_manager 互聯 REST API 請求 / 回應模型（api/ic_schemas.py）。

MODULE NAME：
  topo_manager.api.ic_schemas

PURPOSE：
  以 Pydantic v2 ``BaseModel`` 定義互聯 REST 端點的請求與回應模型，作為
  FastAPI 自動產生 OpenAPI schema 的來源。欄位命名對齊
  ``topo_manager.interconnect.models`` 的 ``IcSession`` / ``IcLink``。

NOTES：
  - 所有新模型 ``ConfigDict(extra="forbid")``（與 ``schemas.py`` 一致）。
  - ``phase`` / ``profile`` / ``kind`` 不用 ``Literal``，與
    ``interconnect/models.py`` 的取捨一致（值域由 allocator 還原層驗證，避免
    帳本異常值使回應序列化變成 500）。
  - 「二擇一」等請求語意檢查不放在 Pydantic validator，而在 ``ic_routes`` 以
    ``InterconnectRequestError`` 表達，使 ``error.type`` 一致。
  - 回應模型為純資料，不含憑證或連線物件。

HISTORY：
  - 0.1.0  互聯 REST 初版（帳本層）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import Literal

from topo_manager.interconnect.models import IcSession


# ======================================================================
# 請求
# ======================================================================
class IcPairIn(BaseModel):
    """一組明確 port 對（等同 CLI ``--pair a=b``）。

    欄位：
      - a：A 端，格式 ``<dut>:<port>``；port 為 il_port 名或 ``p0..p3``。
      - b：B 端，同上。
    """

    model_config = ConfigDict(extra="forbid")

    a: str = Field(min_length=1)
    b: str = Field(min_length=1)


class IcAllocateRequest(BaseModel):
    """``POST /interconnect/sessions`` 與 ``POST /interconnect/plan`` 的請求。

    欄位：
      - group：互聯組名（必填、非空）。
      - duts：選定 DUT（2..4 台，順序即選定順序），需搭配 ``profile``。
      - profile：``mesh`` | ``line`` | ``ring``（值域由 allocator 驗證）。
      - pairs：明確 port 對清單，不可與 ``profile`` 並用。

    ``duts`` 與 ``pairs`` 二擇一；此規則由 ``ic_routes`` 檢查並回 400。
    """

    model_config = ConfigDict(extra="forbid")

    group: str = Field(min_length=1)
    duts: Optional[List[str]] = None
    profile: Optional[str] = None
    pairs: Optional[List[IcPairIn]] = None

    @field_validator("duts")
    @classmethod
    def _strip_duts(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """DUT 名 strip；strip 後為空字串即拒絕（400 ValidationError）。"""
        if value is None:
            return None
        cleaned = [item.strip() for item in value]
        if any(len(item) == 0 for item in cleaned):
            raise ValueError("duts 的元素不可為空字串")
        return cleaned


# ======================================================================
# 回應：session
# ======================================================================
class IcLinkOut(BaseModel):
    """回應中的一條 link（VLAN 與兩端 DUT port / fanout 節點）。

    欄位：
      - vlan：此 link 專屬 VLAN。
      - a_dut / a_port / a_node：A 端 DUT 名、DUT 側 il_port、fanout 節點。
      - b_dut / b_port / b_node：B 端，同上。
      - kind：``paired``（profile 展開）或 ``explicit``（明確 port 對）。
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


class IcSessionResponse(BaseModel):
    """一筆互聯 session 的 HTTP 回應。

    欄位：
      - session_id：``ic-NNNN``（預覽為 ``(preview)``）。
      - group / fanout：所屬組與其互聯 fanout。
      - profile：``mesh`` | ``line`` | ``ring`` | ``explicit``。
      - duts：選定順序的 DUT 名。
      - phase：``allocated`` | ``releasing``。
      - links：所有 link。
      - status：``allocated``（POST）、``released``（DELETE）、``active``
        （GET 清單）、``preview``（plan 預覽）。
      - warning：僅 DELETE 填入（設備未 park 的提醒）。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    group: str
    fanout: str
    profile: str
    duts: List[str]
    phase: str
    links: List[IcLinkOut]
    status: Literal["allocated", "released", "active", "preview"]
    warning: Optional[str] = None

    @classmethod
    def from_ic_session(
        cls,
        session: IcSession,
        status: Literal["allocated", "released", "active", "preview"],
        warning: Optional[str] = None,
    ) -> "IcSessionResponse":
        """由 ``IcSession`` 建出回應模型。

        參數：
          - session：互聯 allocator 回傳的 ``IcSession``。
          - status：本次回應的狀態常數（由各 handler 依端點設定）。
          - warning：選填警告文字。

        回傳：
          - 欄位與 ``session`` 等值的 ``IcSessionResponse``。
        """
        return cls(
            session_id=session.session_id,
            group=session.group,
            fanout=session.fanout,
            profile=session.profile,
            duts=list(session.duts),
            phase=session.phase,
            links=[
                IcLinkOut(
                    vlan=link.vlan,
                    a_dut=link.a_dut,
                    a_port=link.a_port,
                    a_node=link.a_node,
                    b_dut=link.b_dut,
                    b_port=link.b_port,
                    b_node=link.b_node,
                    kind=link.kind,
                )
                for link in session.links
            ],
            status=status,
            warning=warning,
        )


class IcSessionListResponse(BaseModel):
    """``GET /interconnect/sessions`` 的回應。

    欄位：
      - sessions：所有互聯 session（依 session_id 升冪，status 固定 ``active``）。
      - count：清單長度。
    """

    model_config = ConfigDict(extra="forbid")

    sessions: List[IcSessionResponse]
    count: int


# ======================================================================
# 回應：summary
# ======================================================================
class IcFanoutUsageOut(BaseModel):
    """互聯 fanout 的 port 使用（對映 ``IcGraph.summary()`` 的 used / total）。

    欄位：
      - name：互聯 fanout 名。
      - wired_ports：已接線 port 數（summary 的 ``used``）。
      - port_count：總 port 數（summary 的 ``total``）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    wired_ports: int
    port_count: int


class IcSummaryDutOut(BaseModel):
    """組內一台 DUT。

    欄位：
      - label：組內名次標籤 ``d0``..``d3``。
      - name：DUT 名。
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    name: str


class IcSummaryGroupOut(BaseModel):
    """一個互聯組。

    欄位：
      - name：組名。
      - rack：所屬 rack。
      - fanout：該組使用的互聯 fanout。
      - duts：組內 DUT（d0..d3）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    rack: str
    fanout: str
    duts: List[IcSummaryDutOut]


class IcSummaryResponse(BaseModel):
    """``GET /interconnect/summary`` 的回應（對應 CLI ``interconnect summary``）。

    欄位：
      - fanout_count / group_count / dut_count / link_count：總數。
      - fanouts：各互聯 fanout 的 port 使用（依名稱排序）。
      - groups：各組與組內 DUT。
    """

    model_config = ConfigDict(extra="forbid")

    fanout_count: int
    group_count: int
    dut_count: int
    link_count: int
    fanouts: List[IcFanoutUsageOut]
    groups: List[IcSummaryGroupOut]


# ======================================================================
# 回應：available
# ======================================================================
class IcPortStateOut(BaseModel):
    """一個互聯 port 的佔用狀態。

    欄位：
      - p_index：邏輯序 0..3。
      - il_port：DUT 側 port 名。
      - node：fanout 節點 ``<fanout>:<n>``。
      - occupied_by：佔用的 session_id；``None`` 表未佔用。
    """

    model_config = ConfigDict(extra="forbid")

    p_index: int
    il_port: str
    node: str
    occupied_by: Optional[str]


class IcAvailDutOut(BaseModel):
    """可用資源回應中的一台 DUT。

    欄位：
      - label / name：組內名次標籤與 DUT 名。
      - ports：p0..p3 的佔用狀態。
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    name: str
    ports: List[IcPortStateOut]


class IcAvailLinkOut(BaseModel):
    """固定配對表中兩端 port 皆未佔用的 link。

    欄位：
      - a_dut / a_port / b_dut / b_port：兩端 DUT 與 il_port。
    """

    model_config = ConfigDict(extra="forbid")

    a_dut: str
    a_port: str
    b_dut: str
    b_port: str


class IcAvailGroupOut(BaseModel):
    """可用資源回應中的一個組。

    欄位：
      - name / fanout：組名與互聯 fanout。
      - duts：DUT 與其 port 佔用狀態。
      - buildable_links：固定配對表中目前可建的 link。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    fanout: str
    duts: List[IcAvailDutOut]
    buildable_links: List[IcAvailLinkOut]


class IcAvailableResponse(BaseModel):
    """``GET /interconnect/resources/available`` 的回應。

    「可建」僅表示 port 目前未被佔用，不保證 VLAN 池仍有空位；真正成敗由
    ``POST /interconnect/sessions`` 判定。

    欄位：
      - groups：各組的 port 佔用與可建 link。
    """

    model_config = ConfigDict(extra="forbid")

    groups: List[IcAvailGroupOut]


# ======================================================================
# 回應：plan
# ======================================================================
class IcFanoutPlanOut(BaseModel):
    """單一互聯 fanout 的指令計畫。

    欄位：
      - fanout：互聯 fanout 名。
      - batches：依序送出的指令批次。
    """

    model_config = ConfigDict(extra="forbid")

    fanout: str
    batches: List[List[str]]


class IcPlanResponse(BaseModel):
    """計畫查詢的回應（純計算，不連線、不入帳）。

    欄位：
      - session_id：既有 session 的 id；預覽為 ``(preview)``，park 為 ``(park)``。
      - action：``apply`` | ``release`` | ``park``。
      - vlans：涉及的 VLAN（升冪）。
      - fanouts：各 fanout 的指令批次。
      - notes：備註。
      - preview：僅 ``POST /interconnect/plan`` 填入，預覽的 link 與示意 VLAN。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    action: Literal["apply", "release", "park"]
    vlans: List[int]
    fanouts: List[IcFanoutPlanOut]
    notes: List[str]
    preview: Optional[IcSessionResponse] = None

    @classmethod
    def from_plan_result(
        cls, plan: object, preview: Optional[IcSessionResponse] = None
    ) -> "IcPlanResponse":
        """由 deployer 的 ``IcPlanResult`` 建出回應（不含 ``configured``）。

        參數：
          - plan：``IcPlanResult``（具 session_id / action / vlans / fanouts / notes）。
          - preview：選填，預覽 session 回應。

        回傳：
          - ``IcPlanResponse``。
        """
        return cls(
            session_id=plan.session_id,
            action=plan.action,
            vlans=list(plan.vlans),
            fanouts=[
                IcFanoutPlanOut(
                    fanout=item.fanout,
                    batches=[list(batch) for batch in item.batches],
                )
                for item in plan.fanouts
            ],
            notes=list(plan.notes),
            preview=preview,
        )
