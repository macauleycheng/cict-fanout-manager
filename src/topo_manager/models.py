"""topo_manager 資料模型（設計 §3、FR-1）。

以 Pydantic v2 ``BaseModel`` 定義拓樸 YAML 的結構與 schema 層驗證。
關鍵設計：

  - ``Fanout.role`` 為自由字串，不是固定列舉（§3.2）；模型不認識
    spine/agg/leaf/core 字面值，層與層的次序由建圖時的接線拓樸推導，
    以滿足「層數不可寫死」的通用多層約束（NFR-4）。
  - port 一律正規化為 ``str``（§3.3）：以 ``mode='before'`` 的
    field_validator 把 ``int`` 轉成 ``str``；其他型別不字串化，交由
    Pydantic 原生型別檢查報錯。
  - 所有模型 ``extra='forbid'``：YAML 多打未知欄位即報錯（FR-2.2）。
  - 同一裝置內 port 不重複於模型層檢查（§3.4）；跨裝置重名為合法。

歷史（HISTORY）：
  - 0.1.0  FEAT-001 初版，建立 Endpoint/Generator/Fanout/Dut/Rack/
           Link/Topology 模型與 §3.3/§3.4 驗證規則。
  - 0.2.0  互聯 fanout：新增 IcFanout、InterconnectSection；Topology 新增
           選填 interconnect 欄位（exclude=True，不影響序列化輸出）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _normalize_port_value(value: object) -> object:
    """把單一 port 值正規化：``int`` → ``str``，其餘型別原樣回傳。

    僅處理 ``int`` → ``str``；``float``/``list``/``dict`` 等不字串化，
    交由 Pydantic 原生型別檢查報 ValidationError（避免把 ``5.0`` 悄悄
    變成 ``"5.0"``）。注意 ``bool`` 是 ``int`` 子類，但 port 不預期出現
    bool，故不特別處理。
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


def _normalize_port_list(value: object) -> object:
    """把 port list 內每個 ``int`` 元素轉成 ``str``；非 list 原樣回傳。

    非 list 型別交由 Pydantic 原生檢查報錯，不在此強行轉換。
    """
    if isinstance(value, list):
        return [_normalize_port_value(item) for item in value]
    return value


def _ensure_no_duplicates(ports: List[str], field_name: str) -> List[str]:
    """確認同一裝置內 port 不重複；重複則丟 ValueError（含重複值）。

    由各模型的 field_validator 呼叫；Pydantic 會把 ValueError 包成
    ValidationError。跨裝置重名為合法，故此函式只檢查單一 list 內部。
    """
    seen = set()
    duplicates = []
    for port in ports:
        if port in seen:
            if port not in duplicates:
                duplicates.append(port)
        else:
            seen.add(port)
    if len(duplicates) > 0:
        raise ValueError(
            "{0} 內 port 重複：{1}".format(field_name, ", ".join(duplicates))
        )
    return ports


class Endpoint(BaseModel):
    """一條 link 的單一端點：指向某裝置的某個 port。

    欄位：
      - dev：裝置名（generator / fanout / dut 的 name），非空字串。
      - port：port 名稱，統一以 str 表示（int 於驗證前自動轉 str，§3.3）。
    """

    model_config = ConfigDict(extra="forbid")

    dev: str
    port: str

    @field_validator("port", mode="before")
    @classmethod
    def _coerce_port(cls, value: object) -> object:
        """int 轉 str（§3.3）；其他型別不字串化。"""
        return _normalize_port_value(value)


class Generator(BaseModel):
    """打流產生器（如 ixia、trex）。

    欄位：
      - name：裝置名，全域唯一（唯一性由 loader 層檢查）。
      - type：產生器型別字串（如 'ixia'、'trex'），Phase 1 不做語意分支。
      - ports：產生器 port 清單，至少 1 筆，同一 generator 內不重複。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    ports: List[str] = Field(min_length=1)

    @field_validator("ports", mode="before")
    @classmethod
    def _coerce_ports(cls, value: object) -> object:
        """list 內每個 int port 轉 str（§3.3）。"""
        return _normalize_port_list(value)

    @field_validator("ports")
    @classmethod
    def _unique_ports(cls, value: List[str]) -> List[str]:
        """同一 generator 內 port 不得重複（§3.4）。"""
        return _ensure_no_duplicates(value, "ports")


class Fanout(BaseModel):
    """扇出交換裝置（core/spine/agg/leaf 等，以 role 自由字串標示層）。

    欄位：
      - name：裝置名，全域唯一（唯一性由 loader 層檢查）。
      - role：層級標籤，自由字串（不限列舉，§3.2）；去空白後須非空。
      - port_count：實體 port 總數，須 > 0；合法 port 為 1..port_count。
      - rack：選填，所屬 rack 名（Phase 1 不使用）。
      - mgmt_ip / driver / access：選填，管理資訊（Phase 1 不使用）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    role: str
    port_count: int = Field(gt=0)
    rack: Optional[str] = None
    mgmt_ip: Optional[str] = None
    driver: Optional[str] = None
    access: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _role_non_empty(cls, value: str) -> str:
        """role 去除前後空白後不得為空字串（§3.4）。"""
        if len(value.strip()) == 0:
            raise ValueError("role 去除空白後不得為空")
        return value


class Dut(BaseModel):
    """受測裝置（Device Under Test）。

    欄位：
      - name：裝置名，跨所有 rack 全域唯一（唯一性由 loader 層檢查）。
      - model：型號字串。
      - tg_ports：打流 port 清單，至少 1 筆，同一 DUT 內不重複。
      - il_ports：互聯 port 清單，預設 []（Phase 1 不納入路徑圖）。
      - slot：選填，插槽編號（Phase 1 不使用）。
      - group：選填，分組標籤（Phase 1 不使用）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    model: str
    tg_ports: List[str] = Field(min_length=1)
    il_ports: List[str] = Field(default_factory=list)
    slot: Optional[int] = None
    group: Optional[str] = None

    @field_validator("tg_ports", "il_ports", mode="before")
    @classmethod
    def _coerce_ports(cls, value: object) -> object:
        """list 內每個 int port 轉 str（§3.3）。"""
        return _normalize_port_list(value)

    @field_validator("tg_ports")
    @classmethod
    def _unique_tg_ports(cls, value: List[str]) -> List[str]:
        """同一 DUT 內 tg_port 不得重複（§3.4）。"""
        return _ensure_no_duplicates(value, "tg_ports")


class Rack(BaseModel):
    """機櫃，內含若干 DUT。

    欄位：
      - name：rack 名（不納入 link 端點命名空間，但不得與裝置名衝突，
        由 loader 層檢查）。
      - duts：此 rack 內的 DUT 清單。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    duts: List[Dut]


class Link(BaseModel):
    """一條實體連線，連接兩個端點。

    欄位：
      - a：端點 A。
      - b：端點 B。
    """

    model_config = ConfigDict(extra="forbid")

    a: Endpoint
    b: Endpoint


class IcFanout(BaseModel):
    """互聯 fanout 設備（與打流側 fanout 完全分離，互聯設計 §2.2）。

    欄位：
      - name：裝置名，全域唯一（唯一性由 interconnect 驗證檢查）。
      - port_count：實體 port 總數，須 > 0；合法 port 為 1..port_count。
      - rack：選填，所屬 rack 名；有填則該 fanout 上所有組須屬此 rack。
      - mgmt_ip：選填，僅記錄。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    port_count: int = Field(gt=0)
    rack: Optional[str] = None
    mgmt_ip: Optional[str] = None


class InterconnectSection(BaseModel):
    """拓樸 YAML 頂層選填的 ``interconnect`` 區段（互聯設計 §2.2）。

    欄位：
      - fanouts：互聯 fanout 清單，至少 1 台。
      - links：互聯 link 清單，至少 1 條；a 端為 DUT il_port、b 端為互聯
        fanout port（方向由 interconnect 驗證檢查）。
    """

    model_config = ConfigDict(extra="forbid")

    fanouts: List[IcFanout] = Field(min_length=1)
    links: List[Link] = Field(min_length=1)


class Topology(BaseModel):
    """整份拓樸的頂層模型（設計 §3.1）。

    欄位：
      - version：拓樸格式版本（int）。
      - site：站點名稱字串。
      - generators：打流產生器清單。
      - fanouts：扇出裝置清單。
      - racks：機櫃清單。
      - links：實體連線清單（可為空 list，但鍵須存在）。
      - interconnect：選填互聯區段；預設 None，``exclude=True`` 使
        ``model_dump``/API 輸出與無此區段時完全相同。
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    site: str
    generators: List[Generator]
    fanouts: List[Fanout]
    racks: List[Rack]
    links: List[Link]
    interconnect: Optional[InterconnectSection] = Field(
        default=None, exclude=True
    )
