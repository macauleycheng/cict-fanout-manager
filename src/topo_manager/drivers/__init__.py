"""topo_manager.drivers 子套件：fanout SSH 下發層（Phase 4）。

本子套件提供「把分配器算出的 session 下發到實體 fanout 設備」所需的純邏輯、
driver 介面與具體實作：

  - base：核心中繼資料型別（PortVlanChange / VlanMember / VlanState）與
    driver 抽象基底 ``FanoutDriver``。
  - planner：純邏輯 map/plan/diff（map_interface / plan_changes /
    diff_expected_vs_actual）。
  - config：fanout 連線設定載入與憑證管理（FanoutEntry / FanoutConfig /
    load_fanout_config / select_fanout）。
  - ec_ssh：``FanoutDriver`` 的 netmiko-based 具體實作 ``EcSshDriver``。
  - deployer：下發編排層 ``Deployer`` 與其結果中繼型別（FanoutPlan /
    PlanResult / VerifyResult）。

本子套件的公開符號集中於本模組的 ``__all__`` 匯出，呼叫端可直接
``from topo_manager.drivers import <symbol>``。此層刻意不進入 ``topo_manager``
頂層的既有公開匯入路徑（context：drivers 不進既有匯入路徑）。

netmiko 相依僅於 ``ec_ssh`` 的 ``connect()`` 內延遲 import：``ec_ssh`` 模組層
不 import netmiko，故本 ``__init__`` 匯入 ``EcSshDriver`` 在未安裝 netmiko 的
環境亦不會觸發 ``ImportError``（FEAT-003 驗收項）。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版，匯出 base 的中繼資料型別與 FanoutDriver 抽象基底。
  - 0.4.0  FEAT-003：補齊公開符號匯出——加入具體 driver ``EcSshDriver``、
           編排層 ``Deployer`` 與其結果型別（FanoutPlan/PlanResult/
           VerifyResult）、設定型別與載入器（FanoutEntry/FanoutConfig/
           load_fanout_config/select_fanout）、planner 純邏輯函式
           （map_interface/plan_changes/diff_expected_vs_actual）。確認匯入
           ec_ssh 於未裝 netmiko 時不觸發 ImportError（僅 connect() 內延遲
           import netmiko）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from topo_manager.drivers.base import (
    FanoutDriver,
    PortVlanChange,
    VlanMember,
    VlanState,
)
from topo_manager.drivers.config import (
    FanoutConfig,
    FanoutEntry,
    load_fanout_config,
    select_fanout,
)
from topo_manager.drivers.deployer import (
    Deployer,
    FanoutPlan,
    PlanResult,
    VerifyResult,
)
from topo_manager.drivers.ec_ssh import EcSshDriver
from topo_manager.drivers.planner import (
    diff_expected_vs_actual,
    map_interface,
    plan_changes,
)

__all__ = [
    # base：抽象基底與中繼資料型別
    "FanoutDriver",
    "PortVlanChange",
    "VlanMember",
    "VlanState",
    # ec_ssh：具體 driver
    "EcSshDriver",
    # deployer：編排層與結果型別
    "Deployer",
    "FanoutPlan",
    "PlanResult",
    "VerifyResult",
    # config：設定型別與載入/選台
    "FanoutConfig",
    "FanoutEntry",
    "load_fanout_config",
    "select_fanout",
    # planner：純邏輯 map/plan/diff
    "map_interface",
    "plan_changes",
    "diff_expected_vs_actual",
]
