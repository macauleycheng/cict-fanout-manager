"""topo_manager.drivers 子套件：fanout SSH 下發層（Phase 4）。

本子套件提供「把分配器算出的 session 下發到實體 fanout 設備」所需的純邏輯與
driver 介面：

  - base：核心中繼資料型別（PortVlanChange / VlanMember / VlanState）與
    driver 抽象基底 ``FanoutDriver``。
  - planner：純邏輯 map/plan/diff（map_interface / plan_changes /
    diff_expected_vs_actual）。
  - config：fanout 連線設定載入與憑證管理（FanoutEntry / FanoutConfig /
    load_fanout_config / select_fanout）。

本層刻意不進入 ``topo_manager`` 頂層的既有公開匯入路徑（context：drivers 不進
既有匯入路徑），呼叫端一律自 ``topo_manager.drivers.<module>`` 匯入。base/
planner/config 皆不 import netmiko；netmiko 相依僅於具體 driver（FEAT-002）
連線函式內延遲 import。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版，匯出 base 的中繼資料型別與 FanoutDriver 抽象基底。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from topo_manager.drivers.base import (
    FanoutDriver,
    PortVlanChange,
    VlanMember,
    VlanState,
)

__all__ = [
    "FanoutDriver",
    "PortVlanChange",
    "VlanMember",
    "VlanState",
]
