"""topo_manager 群組 REST 請求 / 回應模型（api/group_schemas.py，FEAT-002）。

以 Pydantic v2 ``BaseModel`` 定義群組端點的請求 / 回應模型，作為 FastAPI
自動產生 OpenAPI schema 的來源。所有本檔新定義的模型一律
``ConfigDict(extra="forbid")``，在 HTTP 邊界擋掉多餘欄位並讓輸出 schema 收斂。

群組 name 不得含 ``'/'``：name 會出現在路徑參數（如 ``/groups/{name}``）內，
含 ``'/'`` 會破壞路由語意。body 內的 name 於欄位層以 ``field_validator`` 拒絕
（拋出使其變成 400 ValidationError）；路徑參數帶 ``'/'`` 則由 FastAPI 路由
自然不匹配而回 404（兩者語意分離，見 group_routes）。

歷史（HISTORY）：
  - 0.7.0  群組 REST（FEAT-002）初版：建立 CreateGroupRequest /
           AddMemberRequest / GroupResponse / GroupListResponse。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, ConfigDict, Field, field_validator

from topo_manager.groups.models import GroupEntry


class CreateGroupRequest(BaseModel):
    """``POST /groups`` 的請求 body（FEAT-002）。

    欄位：
      - name：群組名（非空字串，``min_length=1``）；不得含 ``'/'``（會破壞
        路徑語意），違反時欄位驗證失敗→ 400。

    ``extra="forbid"``：多餘欄位一律拒絕（→ 400）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _reject_slash(cls, value: str) -> str:
        """拒絕含 ``'/'`` 的群組名（拋出→變成 400 ValidationError）。

        參數：
          - value：欲驗證的群組名。

        回傳：
          - 原字串（未含 ``'/'``）。

        例外：
          - ValueError：name 含 ``'/'``（FastAPI 轉為 400 ValidationError）。
        """
        if "/" in value:
            raise ValueError("群組名不得含 '/'")
        return value


class AddMemberRequest(BaseModel):
    """``POST /groups/{name}/members`` 的請求 body（FEAT-002）。

    欄位：
      - session_id：欲加入的 session 識別碼（非空字串，``min_length=1``；
        群組層當作不透明字串鍵）。

    ``extra="forbid"``：多餘欄位一律拒絕（→ 400）。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)


class GroupResponse(BaseModel):
    """一個群組的 HTTP 回應（對應 ``GroupEntry`` + 成員數，FEAT-002）。

    欄位：
      - name：群組名。
      - members：成員 session_id 清單（保序，依加入順序）。
      - count：成員數（= len(members)，便於前端顯示）。

    ``extra="forbid"``：輸出 schema 收斂。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    members: List[str]
    count: int

    @classmethod
    def from_entry(cls, entry: GroupEntry) -> "GroupResponse":
        """由業務層 ``GroupEntry`` 建出回應模型（FEAT-002）。

        參數：
          - entry：``GroupRegistry`` 回傳的 ``GroupEntry``。

        回傳：
          - 欄位值與 ``entry`` 等值、``count`` 為成員數的 ``GroupResponse``。
        """
        members = list(entry.members)
        return cls(name=entry.name, members=members, count=len(members))


class GroupListResponse(BaseModel):
    """``GET /groups`` 的回應：群組清單與其數量（FEAT-002）。

    欄位：
      - groups：群組清單（每筆為 ``GroupResponse``）。
      - count：清單長度。

    ``extra="forbid"``：輸出 schema 收斂。
    """

    model_config = ConfigDict(extra="forbid")

    groups: List[GroupResponse]
    count: int
