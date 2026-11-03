"""fanout 下發層核心資料型別與抽象驅動基底（base.py，設計 §4）。

本模組定義 Phase 4 fanout SSH 下發層的「純中繼資料型別」與 driver 抽象介面，
完全不依賴 netmiko（純標準庫），讓純邏輯 planner、設定載入與 CLI 路徑都能在
未安裝 netmiko 的環境中匯入與測試。

中繼資料型別採 ``@dataclass(frozen=True)``（非 pydantic）：這些是下發過程中的
不可變中繼值（非持久化、不入帳本），用 frozen dataclass 取得雜湊能力與輕量
值語意，與 allocator/store 的 pydantic 持久化模型刻意區隔（設計 §4、決策依
context key_patterns）。

  - PortVlanChange：一筆「在某 fanout 的某介面上對某 VLAN 做某動作」的計畫。
  - VlanMember：設備 VLAN 成員（介面 + VLAN + 是否 tagged）。
  - VlanState：某 fanout 回讀出的 VLAN 狀態快照（成員集合 + 既有 VLAN 集合）。

FanoutDriver 為 driver 抽象基底（``abc.ABC``），規範連線生命週期與讀寫介面；
具體實作（如 netmiko-based EcSshDriver）於 FEAT-002 提供，並以延遲 import
處理 netmiko 相依。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版，建立 PortVlanChange / VlanMember / VlanState 三個
           frozen dataclass 與 FanoutDriver 抽象基底（connect/disconnect/
           context manager/read_vlans/build_commands/apply_vlan/clear_vlan/
           read_show）。純標準庫，不 import netmiko。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import FrozenSet, List


@dataclass(frozen=True)
class PortVlanChange:
    """一筆待下發的 port/VLAN 變更計畫（不可變中繼值，設計 §4）。

    欄位：
      - fanout：目標 fanout 裝置名（str，對應連線設定的 key）。
      - interface：套用 interface_template 後的設備介面名（str，如
        ``Eth1/7``）。
      - vlan：要操作的 VLAN id（int，落在分配器的封閉池內）。
      - action：變更動作（str）；本階段僅使用 ``"add"``（新增 tagged 成員），
        ``"remove"`` 保留供清除流程使用。
      - topology_node：此變更來源的拓樸節點 id（str，``"<dev>:<port>"``），供
        追溯與輸出對照。
    """

    fanout: str
    interface: str
    vlan: int
    action: str
    topology_node: str


@dataclass(frozen=True)
class VlanMember:
    """設備上的一筆 VLAN 成員關係（不可變中繼值，設計 §4）。

    欄位：
      - vlan：VLAN id（int）。
      - interface：介面名（str，如 ``Eth1/7``）。
      - tagged：此成員是否為 tagged（trunk）成員（bool）；本階段下發一律要求
        tagged 成員，untagged 不視為等效（設計 §7.3、finding #5）。
    """

    vlan: int
    interface: str
    tagged: bool


@dataclass(frozen=True)
class VlanState:
    """某 fanout 回讀出的 VLAN 狀態快照（不可變中繼值，設計 §4）。

    欄位：
      - fanout：來源 fanout 裝置名（str）。
      - members：目前的 VLAN 成員集合（FrozenSet[VlanMember]）。
      - existing_vlans：設備上已存在的 VLAN id 集合（FrozenSet[int]），供判斷
        是否需先建立 VLAN。
    """

    fanout: str
    members: FrozenSet[VlanMember]
    existing_vlans: FrozenSet[int]


class FanoutDriver(abc.ABC):
    """fanout 設備驅動的抽象基底（設計 §4）。

    規範連線生命週期（connect/disconnect 與 context manager）與讀寫介面
    （read_vlans/build_commands/apply_vlan/clear_vlan/read_show）。具體實作
    （FEAT-002 的 netmiko-based driver）必須實作所有抽象方法；netmiko 相依一律
    於實作內延遲 import，確保本抽象基底可在未安裝 netmiko 時被匯入。

    狀態（如底層連線 handle）封裝於實例屬性，不使用全域可變狀態。
    """

    @abc.abstractmethod
    def connect(self) -> None:
        """建立與設備的連線。

        INPUT：無（連線參數於實作的建構子提供）。
        OUTPUT：無。
        RETURN：None。
        例外：DriverDependencyError（netmiko 未安裝）、DriverConnectionError
          （連線/認證失敗）。
        """

    @abc.abstractmethod
    def disconnect(self) -> None:
        """關閉與設備的連線（冪等；未連線時為 no-op）。

        INPUT：無。
        OUTPUT：無。
        RETURN：None。
        例外：不對外拋出（關閉失敗應被實作吞下或記錄，確保可安全重複呼叫）。
        """

    def __enter__(self) -> "FanoutDriver":
        """context manager 進入：建立連線並回傳自身。

        INPUT：無。
        OUTPUT：無。
        RETURN：已連線的 ``FanoutDriver`` 實例（自身）。
        例外：同 ``connect``。
        """
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        """context manager 離開：關閉連線，不吞沒臨界區內例外。

        INPUT：
          - exc_type / exc_value / traceback：由 runtime 傳入的例外資訊
            （正常離開時皆為 None）。
        OUTPUT：無。
        RETURN：False（不抑制臨界區例外，使其原樣往外傳）。
        例外：不額外拋出（disconnect 本身不對外拋）。
        """
        self.disconnect()
        return False

    @abc.abstractmethod
    def read_vlans(self) -> VlanState:
        """讀取並解析設備目前的 VLAN 狀態。

        INPUT：無。
        OUTPUT：無。
        RETURN：``VlanState``（本 fanout 的成員與既有 VLAN 快照）。
        例外：DriverConnectionError（連線失敗）、DriverParseError（輸出無法
          解析）。
        """

    @abc.abstractmethod
    def build_commands(self, changes: List[PortVlanChange]) -> List[str]:
        """由變更計畫產生設備設定指令（純邏輯，不送出）。

        INPUT：
          - changes：本 fanout 的 ``PortVlanChange`` 清單。
        OUTPUT：無。
        RETURN：依序要送出的設定指令字串清單（List[str]）。
        例外：不涉及 I/O，故不拋連線類例外。
        """

    @abc.abstractmethod
    def apply_vlan(self, changes: List[PortVlanChange]) -> None:
        """將變更計畫下發（apply）至設備。

        INPUT：
          - changes：本 fanout 的 ``PortVlanChange`` 清單。
        OUTPUT：無。
        RETURN：None。
        例外：DriverConnectionError（連線失敗）、ApplyError（設備拒絕/下發
          失敗）。
        """

    @abc.abstractmethod
    def clear_vlan(self, changes: List[PortVlanChange]) -> None:
        """清除（反向）先前下發的 VLAN 設定。

        INPUT：
          - changes：要清除的 ``PortVlanChange`` 清單。
        OUTPUT：無。
        RETURN：None。
        例外：DriverConnectionError（連線失敗）、ApplyError（清除失敗）。
        """

    @abc.abstractmethod
    def read_show(self, command: str) -> str:
        """送出一則唯讀 show 指令並回傳原始輸出（供診斷/回讀）。

        INPUT：
          - command：要送出的 show 指令字串（str）。
        OUTPUT：無。
        RETURN：設備回傳的原始文字輸出（str）。
        例外：DriverConnectionError（連線失敗）。
        """
