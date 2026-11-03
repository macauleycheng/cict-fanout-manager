"""topo_manager REST API 設定與 Allocator factory（Phase 3，設計 §4）。

本模組承載 API 啟動設定（``Settings``）與建立 ``Allocator`` 的 factory
（``build_allocator``）。設定欄位與 CLI 預設對齊（FR-1.3/1.4），確保 API 與
CLI 指向同一帳本、同一 pool 時行為一致。設定 / 參數層的錯誤一律包成 Phase 2
既有的 ``ConfigError``（屬 ``TopologyError`` 家族），不自訂新例外、不冒
``ValueError`` 或 traceback。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 Settings（from_env）與 build_allocator
           factory（與 cli._build_allocator 等價，設計 §4.1/§4.4）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import os

from topo_manager.allocator import Allocator
from topo_manager.errors import ConfigError
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore

# 設定預設值（與 CLI 常數對齊，設計 §4.1）。
_DEFAULT_STATE_PATH = ".topo-state/allocations.json"
_DEFAULT_VLAN_MIN = 100
_DEFAULT_VLAN_MAX = 3000

# 環境變數名稱（設計 §4.1）。
_ENV_TOPOLOGY = "TOPO_MANAGER_TOPOLOGY"
_ENV_STATE = "TOPO_MANAGER_STATE"
_ENV_VLAN_MIN = "TOPO_MANAGER_VLAN_MIN"
_ENV_VLAN_MAX = "TOPO_MANAGER_VLAN_MAX"


class Settings:
    """API 啟動設定（由環境變數或顯式參數建立，設計 §4.1）。

    屬性：
      - topology_path：拓樸 YAML 路徑（無預設、必填）。
      - state_path：session ledger 路徑（預設 ``.topo-state/allocations.json``）。
      - vlan_min：VLAN pool 下界（預設 100）。
      - vlan_max：VLAN pool 上界（預設 3000）。
    """

    def __init__(
        self,
        topology_path: str,
        state_path: str = _DEFAULT_STATE_PATH,
        vlan_min: int = _DEFAULT_VLAN_MIN,
        vlan_max: int = _DEFAULT_VLAN_MAX,
    ) -> None:
        """以顯式參數建立設定（測試注入用，設計 §10.1）。

        參數：
          - topology_path：拓樸 YAML 路徑（必填）。
          - state_path：session ledger 路徑（預設 CLI 慣例路徑）。
          - vlan_min / vlan_max：VLAN pool 端點（預設 100 / 3000）。
        """
        self.topology_path = topology_path
        self.state_path = state_path
        self.vlan_min = vlan_min
        self.vlan_max = vlan_max

    @classmethod
    def from_env(cls) -> "Settings":
        """由環境變數讀取設定（供 uvicorn ``--factory``，設計 §4.1）。

        讀 ``TOPO_MANAGER_TOPOLOGY``（缺則丟 ``ConfigError``）、
        ``TOPO_MANAGER_STATE``（預設 ``.topo-state/allocations.json``）、
        ``TOPO_MANAGER_VLAN_MIN``（預設 100）、``TOPO_MANAGER_VLAN_MAX``
        （預設 3000）。VLAN 的 ``int()`` 轉換失敗亦丟 ``ConfigError``
        （不冒 ``ValueError``）。

        回傳：
          - 由環境變數組出的 ``Settings``。

        例外：
          - ConfigError：缺 ``TOPO_MANAGER_TOPOLOGY`` 或 VLAN 非整數。
        """
        topology_path = os.environ.get(_ENV_TOPOLOGY)
        if topology_path is None or len(topology_path) == 0:
            raise ConfigError(
                "缺少必要環境變數 {0}（拓樸 YAML 路徑）".format(_ENV_TOPOLOGY)
            )

        state_path = os.environ.get(_ENV_STATE, _DEFAULT_STATE_PATH)
        vlan_min = cls._parse_vlan_env(_ENV_VLAN_MIN, _DEFAULT_VLAN_MIN)
        vlan_max = cls._parse_vlan_env(_ENV_VLAN_MAX, _DEFAULT_VLAN_MAX)

        return cls(
            topology_path=topology_path,
            state_path=state_path,
            vlan_min=vlan_min,
            vlan_max=vlan_max,
        )

    @staticmethod
    def _parse_vlan_env(env_name: str, default: int) -> int:
        """讀取 VLAN 環境變數並轉 int；非整數丟 ``ConfigError``（設計 §4.1）。

        參數：
          - env_name：環境變數名稱。
          - default：未設定時的預設值。

        回傳：
          - 解析後的整數（未設定時回 default）。

        例外：
          - ConfigError：環境變數存在但非合法整數。
        """
        raw = os.environ.get(env_name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(
                "環境變數 {0} 非整數：{1}".format(env_name, raw)
            ) from exc


def build_allocator(settings: Settings) -> Allocator:
    """載入拓樸、建 ``JsonSessionStore`` 與 ``Allocator``（設計 §4.4）。

    與 ``cli._build_allocator`` 同語意（刻意複製該薄 glue 以避免 import cli
    造成耦合，屬薄層黏合而非演算法複製，NFR-2）。VLAN 範圍非法的
    ``ValueError`` 轉 ``ConfigError``（不冒 traceback）。

    參數：
      - settings：已建立的 ``Settings``。

    回傳：
      - 已還原既有 ledger 的 ``Allocator``。

    例外：
      - ConfigError：VLAN pool 設定非法（``Allocator`` 建構期 ``ValueError``）。
      - TopologyError 家族：載入 / 建圖 / ledger 還原失敗（原樣往外傳）。
    """
    topology = load_topology(settings.topology_path)
    store = JsonSessionStore(settings.state_path)
    try:
        return Allocator(
            topology,
            store,
            vlan_min=settings.vlan_min,
            vlan_max=settings.vlan_max,
        )
    except ValueError as exc:
        raise ConfigError("VLAN pool 設定非法：{0}".format(exc)) from exc
