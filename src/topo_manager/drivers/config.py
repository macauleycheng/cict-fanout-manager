"""fanout 連線設定載入與憑證管理（config.py，設計 §6）。

本模組負責把 fanout 設備的連線設定（host/username/password/device_type/
interface_template）以「env > JSON 檔 > 內建預設」的優先序載入，並做守門驗證。
完全不 import netmiko（純標準庫 + pydantic 資料型別），讓設定路徑可在未安裝
netmiko 的環境測試。

憑證不寫死於任何 tracked 原始碼：真實憑證僅由 env 或使用者自備（已 gitignore）
的 JSON 檔提供；版控內只放 ``fanout-credentials.example.json`` placeholder。

優先序（每一欄位獨立，env 覆寫 JSON、JSON 覆寫預設）：
  - env：``TOPO_FANOUT_<KEY>``（單台，無 fanout 名時）；多台以 JSON 檔為主。
  - JSON 檔：``load_fanout_config(path=...)`` 指定，或預設檔名。
  - 預設：device_type='cisco_ios'、interface_template='Eth1/{port}'；
    host/username/password 無預設（連線用途缺任一即 ConfigError）。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版，建立 FanoutEntry / FanoutConfig 資料型別、
           load_fanout_config（env > JSON > 預設、守門驗證）與選台輔助
           select_fanout（單台直接用、多台未指定/查無丟 ConfigError）。不
           import netmiko。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Mapping, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from topo_manager.errors import ConfigError

# 內建預設（設計 §6）：僅 device_type 與 interface_template 有預設；
# host/username/password 無預設（連線用途必須由 env 或 JSON 提供）。
_DEFAULT_DEVICE_TYPE = "cisco_ios"
_DEFAULT_INTERFACE_TEMPLATE = "Eth1/{port}"

# 單台（無 fanout 名）env 覆寫前綴與欄位對照（設計 §6）。
_ENV_PREFIX = "TOPO_FANOUT_"
_ENV_FIELDS = {
    "HOST": "host",
    "USERNAME": "username",
    "PASSWORD": "password",
    "DEVICE_TYPE": "device_type",
    "INTERFACE_TEMPLATE": "interface_template",
}

# 預設 JSON 設定檔名（相對於當前工作目錄；已列入 .gitignore）。
_DEFAULT_CONFIG_FILENAME = "fanout-credentials.json"


class FanoutEntry(BaseModel):
    """單台 fanout 的連線設定（設計 §6）。

    欄位：
      - host：設備可達位址（str，連線用途必填）。
      - username：登入帳號（str，連線用途必填）。
      - password：登入密碼（str，連線用途必填）。
      - device_type：netmiko device_type（str），預設 ``'cisco_ios'``。
      - interface_template：port→介面名樣板（str，須含 ``'{port}'``），預設
        ``'Eth1/{port}'``。
    """

    model_config = ConfigDict(extra="forbid")

    host: str
    username: str
    password: str
    device_type: str = _DEFAULT_DEVICE_TYPE
    interface_template: str = _DEFAULT_INTERFACE_TEMPLATE


class FanoutConfig(BaseModel):
    """多台 fanout 的連線設定集合（設計 §6）。

    欄位：
      - fanouts：Dict[fanout 裝置名, FanoutEntry]。
    """

    model_config = ConfigDict(extra="forbid")

    fanouts: Dict[str, FanoutEntry]


def _read_json_file(path: Path) -> dict:
    """讀取並解析 JSON 設定檔（設計 §6）。

    INPUT：
      - path：設定檔路徑（Path）。

    OUTPUT：無。

    RETURN：
      - 解析後的 dict（頂層須為 object）。

    例外：
      - ConfigError：讀檔 I/O 失敗、非 UTF-8、非合法 JSON、或頂層非 object
        （含路徑與原因，保留 ``__cause__``）。
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            "fanout 設定檔非合法 UTF-8（{0}）：{1}".format(path, exc)
        ) from exc
    except OSError as exc:
        raise ConfigError(
            "無法讀取 fanout 設定檔（{0}）：{1}".format(path, exc)
        ) from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "fanout 設定檔非合法 JSON（{0}）：{1}（line {2}, column {3}）".format(
                path, exc.msg, exc.lineno, exc.colno
            )
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(
            "fanout 設定檔頂層須為 JSON object（{0}）：實得 {1}".format(
                path, type(data).__name__
            )
        )
    return data


def _normalize_raw(data: dict) -> Dict[str, dict]:
    """把 JSON 設定正規化為 ``{fanout 名: {欄位: 值}}``（設計 §6）。

    支援兩種頂層格式：含 ``"fanouts"`` 鍵（值為 mapping）者取其值；否則視整個
    頂層 object 的每個鍵為一台 fanout。

    INPUT：
      - data：已解析的頂層 dict。

    OUTPUT：無。

    RETURN：
      - ``{fanout 名: 原始欄位 dict}``。

    例外：
      - ConfigError：``fanouts`` 值或單台項目非 object。
    """
    if "fanouts" in data:
        fanouts = data["fanouts"]
        if not isinstance(fanouts, dict):
            raise ConfigError(
                "fanout 設定的 'fanouts' 須為 object：實得 {0}".format(
                    type(fanouts).__name__
                )
            )
    else:
        fanouts = data

    normalized: Dict[str, dict] = {}
    for name, entry in fanouts.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                "fanout 設定項目 '{0}' 須為 object：實得 {1}".format(
                    name, type(entry).__name__
                )
            )
        normalized[name] = dict(entry)
    return normalized


def _env_overrides(env: Mapping[str, str]) -> Dict[str, str]:
    """由 env 擷取單台覆寫欄位（``TOPO_FANOUT_<KEY>``，設計 §6）。

    INPUT：
      - env：環境變數映射。

    OUTPUT：無。

    RETURN：
      - ``{欄位名: 值}``（僅含實際存在於 env 的欄位）。
    """
    overrides: Dict[str, str] = {}
    for suffix, field in _ENV_FIELDS.items():
        key = _ENV_PREFIX + suffix
        if key in env:
            overrides[field] = env[key]
    return overrides


def _validate_entry(name: str, fields: dict) -> FanoutEntry:
    """套預設、驗證並建構單台 ``FanoutEntry``（設計 §6）。

    INPUT：
      - name：fanout 裝置名（str），供錯誤訊息標示。
      - fields：合併 env/JSON/預設後的欄位 dict。

    OUTPUT：無。

    RETURN：
      - 驗證通過的 ``FanoutEntry``。

    例外：
      - ConfigError：連線用途缺 host/username/password、interface_template
        缺 ``'{port}'``、或 pydantic schema 驗證失敗。
    """
    for required in ("host", "username", "password"):
        value = fields.get(required)
        if value is None or value == "":
            raise ConfigError(
                "fanout '{0}' 的連線設定缺少必填欄位 '{1}'（需由 env 或設定檔"
                "提供）".format(name, required)
            )

    template = fields.get("interface_template", _DEFAULT_INTERFACE_TEMPLATE)
    if "{port}" not in template:
        raise ConfigError(
            "fanout '{0}' 的 interface_template 必須含 '{{port}}' 佔位："
            "實得 '{1}'".format(name, template)
        )

    try:
        return FanoutEntry(**fields)
    except ValidationError as exc:
        raise ConfigError(
            "fanout '{0}' 的連線設定非法：{1}".format(name, exc)
        ) from exc


def load_fanout_config(
    path: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> FanoutConfig:
    """載入並驗證 fanout 連線設定（env > JSON 檔 > 預設，設計 §6）。

    優先序：對每個欄位，env（``TOPO_FANOUT_<KEY>``）覆寫 JSON 檔值、JSON 檔值
    覆寫內建預設。單台 env 覆寫套用於設定檔中的每一台 fanout；若設定檔不存在
    但 env 提供了完整連線欄位，則以 env 合成單台（名為 ``"default"``）。

    INPUT：
      - path：JSON 設定檔路徑（str）；None 時用預設檔名
        ``fanout-credentials.json``（檔不存在時不報錯，改走 env/預設）。
      - env：環境變數映射；None 時用 ``os.environ``。

    OUTPUT：無。

    RETURN：
      - 驗證通過的 ``FanoutConfig``（至少含一台，否則 ConfigError）。

    例外：
      - ConfigError：設定檔格式錯、連線用途缺必填欄位、interface_template 缺
        ``'{port}'``、或最終無任何可用 fanout（無檔且 env 不足以合成單台）。
    """
    effective_env = env if env is not None else os.environ
    overrides = _env_overrides(effective_env)

    # 解析 JSON 來源（指定 path 必須存在；預設檔名則容許不存在）。
    raw: Dict[str, dict] = {}
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise ConfigError(
                "指定的 fanout 設定檔不存在：{0}".format(config_path)
            )
        raw = _normalize_raw(_read_json_file(config_path))
    else:
        default_path = Path(_DEFAULT_CONFIG_FILENAME)
        if default_path.exists():
            raw = _normalize_raw(_read_json_file(default_path))

    # 無設定檔內容時，嘗試以 env 合成單台（需 env 提供完整連線欄位）。
    if not raw and overrides:
        raw = {"default": {}}

    if not raw:
        raise ConfigError(
            "找不到任何 fanout 連線設定：請提供設定檔（{0}）或以 env "
            "'{1}<FIELD>' 指定連線欄位".format(
                _DEFAULT_CONFIG_FILENAME, _ENV_PREFIX
            )
        )

    entries: Dict[str, FanoutEntry] = {}
    for name, json_fields in raw.items():
        # 欄位優先序：JSON 值為底、env 覆寫其上（預設於 _validate_entry 補）。
        merged = dict(json_fields)
        merged.update(overrides)
        entries[name] = _validate_entry(name, merged)

    return FanoutConfig(fanouts=entries)


def select_fanout(
    config: FanoutConfig, fanout: Optional[str] = None
) -> str:
    """從設定中選出一台 fanout（單台直接用、多台須指定，設計 §6）。

    INPUT：
      - config：已載入的 ``FanoutConfig``。
      - fanout：使用者指定的 fanout 名（str，對應 ``--fanout``）；None 表未指定。

    OUTPUT：無。

    RETURN：
      - 選定的 fanout 裝置名（str）。

    例外：
      - ConfigError：無任何 fanout、多台但未指定 ``--fanout``、或指定名查無。
    """
    names = list(config.fanouts.keys())
    if not names:
        raise ConfigError("fanout 連線設定為空，無可選用的設備")

    if fanout is None:
        if len(names) == 1:
            return names[0]
        raise ConfigError(
            "設定含多台 fanout（{0}），請以 --fanout 指定其一".format(
                ", ".join(sorted(names))
            )
        )

    if fanout not in config.fanouts:
        raise ConfigError(
            "指定的 fanout '{0}' 不在連線設定中（可用：{1}）".format(
                fanout, ", ".join(sorted(names))
            )
        )
    return fanout
