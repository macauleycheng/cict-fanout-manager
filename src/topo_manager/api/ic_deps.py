"""topo_manager 互聯 REST 的 allocator 建構輔助（api/ic_deps.py）。

MODULE NAME：
  topo_manager.api.ic_deps

PURPOSE：
  提供互聯端點使用的 allocator 建構函式：

  - ``build_ic_startup_graph``：啟動時驗證互聯 VLAN 池並取得互聯小圖；拓樸
    沒有 interconnect 區段時回 ``None``（降級，app 照常啟動）。
  - ``build_ic_allocator``：每請求新建 ``InterconnectAllocator``（讀、寫皆然）。

NOTES：
  - 啟動期**不讀互聯帳本**：互聯帳本故障不得拖垮打流側 API / Web UI 的啟動，
    錯誤改在互聯端點請求時以 503 回報。
  - 每請求新建 allocator，建構時會讀一次帳本；帳本故障只影響該請求，修好後
    下一請求即恢復。寫入方法在鎖內重載，正確性由 ``store.locked()`` 保證。
  - 使用啟動時已驗證的 ``app.state.topology``，不重讀 YAML。
  - VLAN 池非法的 ``ValueError`` 轉 ``ConfigError``（不冒 traceback）。

HISTORY：
  - 0.1.0  互聯 REST 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Optional

from topo_manager.api.deps import Settings
from topo_manager.errors import ConfigError
from topo_manager.interconnect.allocator import InterconnectAllocator
from topo_manager.interconnect.layout import IcGraph
from topo_manager.interconnect.store import IcLoaded, IcSessionStore
from topo_manager.models import Topology


class _EmptyIcStore:
    """啟動期驗證用的空 store：``load()`` 恆回空帳本，不讀檔、不取鎖。"""

    def load(self) -> IcLoaded:
        """回傳空容器（無 session、無序號高水位）。"""
        return IcLoaded(next_seq=None, sessions=[])


def build_ic_startup_graph(
    settings: Settings, topology: Topology
) -> Optional[IcGraph]:
    """啟動期驗證互聯 VLAN 池並取得互聯小圖；無互聯區段回 ``None``。

    以不讀檔的空 store 建一次 allocator，藉 allocator 自身的檢查驗證
    ``ic_vlan_min`` / ``ic_vlan_max``（不複製驗證規則），並取得其 ``graph``。

    參數：
      - settings：API 設定。
      - topology：啟動時載入並驗證過的拓樸。

    回傳：
      - 互聯小圖；拓樸沒有 interconnect 區段時為 ``None``。

    例外：
      - ConfigError：拓樸有互聯區段但互聯 VLAN 池設定非法。
    """
    if topology.interconnect is None:
        return None
    try:
        allocator = InterconnectAllocator(
            topology,
            _EmptyIcStore(),
            vlan_min=settings.ic_vlan_min,
            vlan_max=settings.ic_vlan_max,
        )
    except ValueError as exc:
        raise ConfigError("互聯 VLAN 池設定非法：{0}".format(exc)) from exc
    return allocator.graph


def build_ic_allocator(settings: Settings, topology: Topology) -> InterconnectAllocator:
    """每請求新建互聯 allocator（建構時讀一次互聯帳本）。

    參數：
      - settings：API 設定（取 ``ic_state_path`` 與互聯 VLAN 池）。
      - topology：啟動時載入的拓樸（須含 interconnect 區段）。

    回傳：
      - 已還原帳本的 ``InterconnectAllocator``。

    例外：
      - ConfigError：互聯 VLAN 池非法。
      - SessionStoreError：帳本 I/O、格式或還原一致性失敗（REST 對應 503）。
    """
    store = IcSessionStore(settings.ic_state_path)
    try:
        return InterconnectAllocator(
            topology,
            store,
            vlan_min=settings.ic_vlan_min,
            vlan_max=settings.ic_vlan_max,
        )
    except ValueError as exc:
        raise ConfigError("互聯 VLAN 池設定非法：{0}".format(exc)) from exc
