"""topo_manager REST API 請求 / 回應模型（Phase 3，設計 §5）。

以 Pydantic v2 ``BaseModel`` 定義 API 自訂的請求 / 回應模型，作為 FastAPI
自動產生 OpenAPI schema 的來源（FR-5）。欄位命名以 Phase 2 ``Session`` 為
權威來源（直接對應），並對齊設計 §5 的語意裁決（需求 A-2）。

``extra="forbid"`` 的適用範圍（設計 §5 開頭）：本檔**新定義**的 API 自訂
模型（``AllocateRequest``/``SessionResponse``/``SessionListResponse`` 與
available 的清單模型）一律 ``ConfigDict(extra="forbid")``，以在 HTTP 邊界擋
掉請求多餘欄位並讓輸出 schema 收斂。錯誤模型（``ErrorDetail``/
``ErrorResponse``）為回應專用、不受此規則約束。``GET /topology`` 直接沿用既有
``topo_manager.models.Topology`` 當 ``response_model``（見 ``routes.py``），
不在本檔重新定義。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 API 請求 / 回應 / 錯誤模型（設計 §5、§6.1）。
  - 0.7.0  群組 REST（FEAT-002）：AllocateRequest 新增選填 group（空字串正規化
           為 None）；SessionResponse 新增選填 group（預設 None），from_session
           新增選填 group 參數帶入回應。既有欄位語意不變。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import Literal

from topo_manager.allocator import Session


class TrunkSegmentOut(BaseModel):
    """回應中的一段 trunk（對應 Phase 2 ``TrunkSegment``）。

    欄位：
      - a_node：trunk 近 generator 端節點 id（``"<dev>:<port>"``）。
      - b_node：trunk 近 DUT 端節點 id（``"<dev>:<port>"``）。
      - vlan：此 session 在該 trunk 上使用的 VLAN（全域唯一）。
    """

    model_config = ConfigDict(extra="forbid")

    a_node: str
    b_node: str
    vlan: int


class SessionResponse(BaseModel):
    """一筆 session 的 HTTP 回應（平鋪 Phase 2 ``Session`` 全欄位 + status）。

    欄位（設計 §5.3，對齊 Phase 2 ``Session``）：
      - session_id：session 識別碼（形如 ``s-0001``）。
      - vlan：本 session 佔用的 VLAN。
      - generator_port：分配時指定的 generator 端輸入。
      - dut_port：分配時指定的 DUT 端輸入（``dev:port``）。
      - path：find_path 回傳的節點 id 序列（首 generator、末 DUT）。
      - physical_ports：路徑全部實體節點 id（= path，保序）。
      - trunk_segments：路徑中的 fanout↔fanout trunk 段清單。
      - status：衍生常數，取值與來源 endpoint 一一對應（設計 §5.3）：
        ``"allocated"``（POST /sessions）、``"released"``（DELETE）、
        ``"active"``（GET /sessions 清單內每筆）。必填、不省略。
      - group：此 session 所屬群組名（FEAT-002）；不屬任何群組時為 None。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    vlan: int
    generator_port: str
    dut_port: str
    path: List[str]
    physical_ports: List[str]
    trunk_segments: List[TrunkSegmentOut]
    status: Literal["allocated", "released", "active"]
    group: Optional[str] = None

    @classmethod
    def from_session(
        cls,
        session: Session,
        status: Literal["allocated", "released", "active"],
        group: Optional[str] = None,
    ) -> "SessionResponse":
        """由 Phase 2 ``Session`` 與指定 status 建出回應模型（設計 §5.3）。

        參數：
          - session：Phase 2 分配引擎回傳的 ``Session``。
          - status：本次回應的狀態常數（由各 handler 依其 endpoint 設定）。
          - group：此 session 所屬群組名（FEAT-002）；不屬任何群組時為 None。

        回傳：
          - 欄位值與 ``session`` 等值、``status`` 為指定常數、``group`` 為指定
            群組名的 ``SessionResponse``。
        """
        return cls(
            session_id=session.session_id,
            vlan=session.vlan,
            generator_port=session.generator_port,
            dut_port=session.dut_port,
            path=list(session.path),
            physical_ports=list(session.physical_ports),
            trunk_segments=[
                TrunkSegmentOut(
                    a_node=segment.a_node,
                    b_node=segment.b_node,
                    vlan=segment.vlan,
                )
                for segment in session.trunk_segments
            ],
            status=status,
            group=group,
        )


class SessionListResponse(BaseModel):
    """``GET /sessions`` 的回應：active session 清單與其數量（設計 §5.5）。

    欄位：
      - sessions：active session 清單（依 session_id 升冪，每筆 status
        固定 ``"active"``）。
      - count：清單長度（便於 ATLAS 快速取總數）。
    """

    model_config = ConfigDict(extra="forbid")

    sessions: List[SessionResponse]
    count: int


class AllocateRequest(BaseModel):
    """``POST /sessions`` 的請求 body（設計 §5.3、§7）。

    欄位：
      - generator_port：generator 端，裸名或 ``dev:port``（同 CLI ``--from``），
        非空字串（``min_length=1``）。
      - dut_port：DUT 端，一律 ``dev:port``（同 CLI ``--to``），非空字串
        （``min_length=1``）。
      - group：選填的歸屬群組名（FEAT-002）；給定時 POST /sessions 會把新
        session 加入該群（不存在則自動建立）。空字串正規化為 None（視同未指定）。

    ``extra="forbid"``：多餘欄位一律拒絕（→ 400，設計 §7）。
    """

    model_config = ConfigDict(extra="forbid")

    generator_port: str = Field(min_length=1)
    dut_port: str = Field(min_length=1)
    group: Optional[str] = Field(default=None)

    @field_validator("group")
    @classmethod
    def _normalize_group(cls, value: Optional[str]) -> Optional[str]:
        """把空字串的 group 正規化為 None（視同未指定群組，FEAT-002）。

        參數：
          - value：請求帶入的 group（可能為 None、空字串或群組名）。

        回傳：
          - None（原為 None 或空字串）或原群組名。
        """
        if value is not None and value == "":
            return None
        return value


class GeneratorPortOut(BaseModel):
    """一個可用 generator port 的描述（設計 §5.2）。

    欄位：
      - node：port 節點 id（``"<generator>:<port>"``）。
      - generator：generator 裝置名。
      - port：generator port 名稱。
    """

    model_config = ConfigDict(extra="forbid")

    node: str
    generator: str
    port: str


class DutPortOut(BaseModel):
    """一個可用 DUT tg_port 的描述（設計 §5.2）。

    欄位：
      - node：port 節點 id（``"<dut>:<tg_port>"``）。
      - dut：DUT 裝置名。
      - port：DUT tg_port 名稱。
    """

    model_config = ConfigDict(extra="forbid")

    node: str
    dut: str
    port: str


class AvailableResourcesResponse(BaseModel):
    """``GET /resources/available`` 的回應（設計 §5.2）。

    欄位：
      - available_generator_ports：目前未被佔用的 generator port 清單。
      - available_dut_ports：目前未被佔用的 DUT tg_port 清單。
      - occupied_ports：目前被佔用的 access port → owner session_id 對照，
        供 ATLAS 參考。

    注意：「可用」僅表示該端點目前未被佔用，不保證任意 (gen, dut) 配對皆能
    成功分配（可能無連通路徑）；真正成敗由 ``POST /sessions`` 判定（設計 §5.2）。
    """

    model_config = ConfigDict(extra="forbid")

    available_generator_ports: List[GeneratorPortOut]
    available_dut_ports: List[DutPortOut]
    occupied_ports: Dict[str, str]


class ErrorDetail(BaseModel):
    """結構化錯誤的內層明細（設計 §6.1）。

    欄位：
      - type：錯誤分類字串（Phase 2 例外類別名，或 ``"ValidationError"`` /
        ``"InternalError"``）；供 ATLAS 程式化分支，非 traceback。
      - message：面向使用者的清楚訊息（不含 traceback、不外洩內部細節）。
    """

    type: str
    message: str


class ErrorResponse(BaseModel):
    """所有錯誤回應的統一外層 schema（設計 §6.1）。

    欄位：
      - error：錯誤明細（``ErrorDetail``）。
    """

    error: ErrorDetail
