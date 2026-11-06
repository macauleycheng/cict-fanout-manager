"""topo_manager REST API 設定與 Allocator factory（Phase 3，設計 §4）。

本模組承載 API 啟動設定（``Settings``）與建立 ``Allocator`` 的 factory
（``build_allocator``）。設定欄位與 CLI 預設對齊（FR-1.3/1.4），確保 API 與
CLI 指向同一帳本、同一 pool 時行為一致。設定 / 參數層的錯誤一律包成 Phase 2
既有的 ``ConfigError``（屬 ``TopologyError`` 家族），不自訂新例外、不冒
``ValueError`` 或 traceback。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 Settings（from_env）與 build_allocator
           factory（與 cli._build_allocator 等價，設計 §4.1/§4.4）。
  - 0.5.0  互聯 REST：Settings 新增選填的 ic_state_path / ic_vlan_min /
           ic_vlan_max 與環境變數 TOPO_MANAGER_IC_STATE /
           TOPO_MANAGER_IC_VLAN_MIN / TOPO_MANAGER_IC_VLAN_MAX（既有欄位與
           環境變數語意不變）。
  - 0.6.0  設備下發 REST：Settings 新增 enable_device_push / api_token /
           credentials_path 與環境變數 TOPO_MANAGER_ENABLE_DEVICE_PUSH /
           TOPO_MANAGER_API_TOKEN / TOPO_MANAGER_CREDENTIALS；啟用卻無 token
           時 __init__ 丟 ConfigError（啟動期 fail-fast）。預設全部關閉，既有
           行為不變。
  - 0.7.0  群組 REST（FEAT-002）：Settings 新增選填的 group_state_path 與環境
           變數 TOPO_MANAGER_GROUP_STATE（預設 .topo-state/groups.json）；新增
           build_group_registry factory 建 GroupStore + GroupRegistry。既有
           欄位與行為不變（群組帳本與打流／互聯帳本三方隔離）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import os
from typing import Optional

from topo_manager.allocator import Allocator
from topo_manager.errors import ConfigError
from topo_manager.groups.registry import GroupRegistry
from topo_manager.groups.store import GroupStore
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore

# 設定預設值（與 CLI 常數對齊，設計 §4.1）。
_DEFAULT_STATE_PATH = ".topo-state/allocations.json"
_DEFAULT_VLAN_MIN = 100
_DEFAULT_VLAN_MAX = 3000
# 互聯側預設值（與 interconnect/cli.py 對齊；互聯帳本與打流側帳本分開）。
_DEFAULT_IC_STATE_PATH = ".topo-state/interconnect.json"
_DEFAULT_IC_VLAN_MIN = 100
_DEFAULT_IC_VLAN_MAX = 3000

# 環境變數名稱（設計 §4.1）。
_ENV_TOPOLOGY = "TOPO_MANAGER_TOPOLOGY"
_ENV_STATE = "TOPO_MANAGER_STATE"
_ENV_VLAN_MIN = "TOPO_MANAGER_VLAN_MIN"
_ENV_VLAN_MAX = "TOPO_MANAGER_VLAN_MAX"
_ENV_IC_STATE = "TOPO_MANAGER_IC_STATE"
_ENV_IC_VLAN_MIN = "TOPO_MANAGER_IC_VLAN_MIN"
_ENV_IC_VLAN_MAX = "TOPO_MANAGER_IC_VLAN_MAX"
# 設備下發 REST（預設關閉）。
_ENV_ENABLE_DEVICE_PUSH = "TOPO_MANAGER_ENABLE_DEVICE_PUSH"
_ENV_API_TOKEN = "TOPO_MANAGER_API_TOKEN"
_ENV_CREDENTIALS = "TOPO_MANAGER_CREDENTIALS"
# 群組 REST（FEAT-002）：群組帳本路徑與其環境變數（與其他帳本隔離）。
_DEFAULT_GROUP_STATE_PATH = ".topo-state/groups.json"
_ENV_GROUP_STATE = "TOPO_MANAGER_GROUP_STATE"


class Settings:
    """API 啟動設定（由環境變數或顯式參數建立，設計 §4.1）。

    屬性：
      - topology_path：拓樸 YAML 路徑（無預設、必填）。
      - state_path：session ledger 路徑（預設 ``.topo-state/allocations.json``）。
      - vlan_min：VLAN pool 下界（預設 100）。
      - vlan_max：VLAN pool 上界（預設 3000）。
      - ic_state_path：互聯帳本路徑（預設 ``.topo-state/interconnect.json``）。
      - ic_vlan_min / ic_vlan_max：互聯 VLAN pool（預設 100 / 3000）。
      - enable_device_push：是否啟用設備下發端點（預設 False）。
      - api_token：下發端點的共用密鑰（啟用時必填；不得出現在 log／訊息）。
      - credentials_path：fanout 連線設定檔路徑（None＝與 CLI 相同預設）。
      - group_state_path：群組帳本路徑（預設 ``.topo-state/groups.json``）。
    """

    def __init__(
        self,
        topology_path: str,
        state_path: str = _DEFAULT_STATE_PATH,
        vlan_min: int = _DEFAULT_VLAN_MIN,
        vlan_max: int = _DEFAULT_VLAN_MAX,
        ic_state_path: str = _DEFAULT_IC_STATE_PATH,
        ic_vlan_min: int = _DEFAULT_IC_VLAN_MIN,
        ic_vlan_max: int = _DEFAULT_IC_VLAN_MAX,
        enable_device_push: bool = False,
        api_token: Optional[str] = None,
        credentials_path: Optional[str] = None,
        group_state_path: str = _DEFAULT_GROUP_STATE_PATH,
    ) -> None:
        """以顯式參數建立設定（測試注入用，設計 §10.1）。

        參數：
          - topology_path：拓樸 YAML 路徑（必填）。
          - state_path：session ledger 路徑（預設 CLI 慣例路徑）。
          - vlan_min / vlan_max：VLAN pool 端點（預設 100 / 3000）。
          - ic_state_path：互聯帳本路徑（預設 CLI 慣例路徑）。
          - ic_vlan_min / ic_vlan_max：互聯 VLAN pool 端點（預設 100 / 3000）。
          - enable_device_push：是否啟用設備下發端點（預設 False）。
          - api_token：下發端點共用密鑰（啟用時須為非空字串）。
          - credentials_path：fanout 連線設定檔路徑（None 走 CLI 預設）。
          - group_state_path：群組帳本路徑（預設 CLI 慣例路徑）。

        例外：
          - ConfigError：啟用設備下發但 api_token 未設或為空字串。
        """
        self.topology_path = topology_path
        self.state_path = state_path
        self.vlan_min = vlan_min
        self.vlan_max = vlan_max
        self.ic_state_path = ic_state_path
        self.ic_vlan_min = ic_vlan_min
        self.ic_vlan_max = ic_vlan_max
        self.enable_device_push = enable_device_push
        self.api_token = api_token
        self.credentials_path = credentials_path
        self.group_state_path = group_state_path
        self._validate_device_push(enable_device_push, api_token)

    @staticmethod
    def _validate_device_push(enable: bool, token: Optional[str]) -> None:
        """啟用設備下發卻沒有 token 時 fail-fast（不印出任何 token 內容）。

        參數：
          - enable：是否啟用。
          - token：共用密鑰。

        例外：
          - ConfigError：enable 為真且 token 為 None 或空字串。
        """
        if enable and (token is None or token == ""):
            raise ConfigError(
                "{0}=1 但未設定 {1}；拒絕啟動".format(
                    _ENV_ENABLE_DEVICE_PUSH, _ENV_API_TOKEN
                )
            )

    @classmethod
    def from_env(cls) -> "Settings":
        """由環境變數讀取設定（供 uvicorn ``--factory``，設計 §4.1）。

        讀 ``TOPO_MANAGER_TOPOLOGY``（缺則丟 ``ConfigError``）、
        ``TOPO_MANAGER_STATE``（預設 ``.topo-state/allocations.json``）、
        ``TOPO_MANAGER_VLAN_MIN``（預設 100）、``TOPO_MANAGER_VLAN_MAX``
        （預設 3000）。VLAN 的 ``int()`` 轉換失敗亦丟 ``ConfigError``
        （不冒 ``ValueError``）。互聯側另讀 ``TOPO_MANAGER_IC_STATE``（預設
        ``.topo-state/interconnect.json``）、``TOPO_MANAGER_IC_VLAN_MIN`` /
        ``TOPO_MANAGER_IC_VLAN_MAX``（預設 100 / 3000）。群組側另讀
        ``TOPO_MANAGER_GROUP_STATE``（預設 ``.topo-state/groups.json``）。

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
        ic_state_path = os.environ.get(_ENV_IC_STATE, _DEFAULT_IC_STATE_PATH)
        ic_vlan_min = cls._parse_vlan_env(_ENV_IC_VLAN_MIN, _DEFAULT_IC_VLAN_MIN)
        ic_vlan_max = cls._parse_vlan_env(_ENV_IC_VLAN_MAX, _DEFAULT_IC_VLAN_MAX)

        enable_device_push = cls._parse_enable_env()
        # api_token 不論是否啟用設備下發皆讀取：純帳本寫入端點
        # （POST /sessions、DELETE /sessions/{id}）在未啟用下發時仍以此 token
        # 閘控（有設 token 才解鎖寫入；未設則唯讀，見 auth.require_ledger_write）。
        # 空字串正規化為 None（視同未設定）。
        api_token = os.environ.get(_ENV_API_TOKEN)
        if api_token == "":
            api_token = None
        # credentials 僅設備下發用，未啟用時忽略（不影響既有行為）。
        credentials_path = (
            os.environ.get(_ENV_CREDENTIALS) if enable_device_push else None
        )
        if credentials_path == "":
            credentials_path = None

        group_state_path = os.environ.get(
            _ENV_GROUP_STATE, _DEFAULT_GROUP_STATE_PATH
        )

        return cls(
            topology_path=topology_path,
            state_path=state_path,
            vlan_min=vlan_min,
            vlan_max=vlan_max,
            ic_state_path=ic_state_path,
            ic_vlan_min=ic_vlan_min,
            ic_vlan_max=ic_vlan_max,
            enable_device_push=enable_device_push,
            api_token=api_token,
            credentials_path=credentials_path,
            group_state_path=group_state_path,
        )

    @staticmethod
    def _parse_enable_env() -> bool:
        """讀取 ``TOPO_MANAGER_ENABLE_DEVICE_PUSH``：'1' 啟用；未設／空／'0' 關閉。

        其他值（如 'true'）丟 ``ConfigError``，避免被默默當成關閉而使維運誤以為已啟用。

        回傳：
          - 是否啟用設備下發。

        例外：
          - ConfigError：值不是 '1'、'0' 或空。
        """
        raw = os.environ.get(_ENV_ENABLE_DEVICE_PUSH)
        if raw is None or raw == "" or raw == "0":
            return False
        if raw == "1":
            return True
        raise ConfigError(
            "環境變數 {0} 只接受 '1'（啟用）或 '0'／未設（關閉），實得 '{1}'".format(
                _ENV_ENABLE_DEVICE_PUSH, raw
            )
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


def build_group_registry(settings: Settings) -> GroupRegistry:
    """以 ``settings.group_state_path`` 建 ``GroupStore`` + ``GroupRegistry``（FEAT-002）。

    讀路徑（list／get／atlas）每請求以本 factory 重建以取最新帳本快照；寫路徑
    則共用啟動期單例（``app.state.group_registry``），正確性由 ``GroupStore``
    的 ``locked()`` + 鎖內 reload 保證（對齊 ``build_allocator`` 的讀寫分工）。

    參數：
      - settings：已建立的 ``Settings``（提供 group_state_path）。

    回傳：
      - 已還原既有群組帳本的 ``GroupRegistry``。

    例外：
      - SessionStoreError：群組帳本讀取／驗證失敗，或還原期不變量被違反
        （由 ``GroupRegistry.__init__`` 往外傳）。
    """
    store = GroupStore(settings.group_state_path)
    return GroupRegistry(store)
