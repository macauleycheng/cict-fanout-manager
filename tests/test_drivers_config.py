"""drivers/config.py 的 pytest 測試（設計 §6、FEAT-001）。

涵蓋：
  - env > JSON 檔 > 預設的優先序（env 覆寫 JSON 值、JSON 補預設）。
  - 連線用途缺 host/username/password 丟 ConfigError。
  - interface_template 缺 '{port}' 丟 ConfigError。
  - 選台：單台直接用、多台未指定 --fanout 丟 ConfigError、指定查無丟 ConfigError。

本模組不 import netmiko（純設定測試）。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from topo_manager.drivers.config import (
    FanoutConfig,
    load_fanout_config,
    select_fanout,
)
from topo_manager.errors import ConfigError


def _write_config(tmp_path: Path, data: dict) -> str:
    """把 config dict 寫成 JSON 檔並回傳其路徑字串（測試輔助）。"""
    path = tmp_path / "fanout-credentials.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_json_defaults_applied(tmp_path: Path) -> None:
    """JSON 僅給 host/username/password 時，device_type/template 套預設。"""
    config_path = _write_config(
        tmp_path,
        {
            "fanouts": {
                "sw-1": {
                    "host": "198.51.100.10",
                    "username": "u",
                    "password": "p",
                }
            }
        },
    )
    config = load_fanout_config(path=config_path, env={})
    entry = config.fanouts["sw-1"]
    assert entry.device_type == "cisco_ios"
    assert entry.interface_template == "Eth1/{port}"


def test_env_overrides_json(tmp_path: Path) -> None:
    """env（TOPO_FANOUT_*）覆寫 JSON 檔中的對應欄位。"""
    config_path = _write_config(
        tmp_path,
        {
            "fanouts": {
                "sw-1": {
                    "host": "198.51.100.10",
                    "username": "u",
                    "password": "p",
                }
            }
        },
    )
    env = {"TOPO_FANOUT_HOST": "203.0.113.5", "TOPO_FANOUT_USERNAME": "envuser"}
    config = load_fanout_config(path=config_path, env=env)
    entry = config.fanouts["sw-1"]
    assert entry.host == "203.0.113.5"
    assert entry.username == "envuser"
    assert entry.password == "p"


def test_missing_credential_raises(tmp_path: Path) -> None:
    """連線用途缺必填 password 時丟 ConfigError。"""
    config_path = _write_config(
        tmp_path,
        {"fanouts": {"sw-1": {"host": "198.51.100.10", "username": "u"}}},
    )
    with pytest.raises(ConfigError) as exc_info:
        load_fanout_config(path=config_path, env={})
    assert "password" in str(exc_info.value)


def test_template_missing_port_placeholder_raises(tmp_path: Path) -> None:
    """interface_template 缺 '{port}' 時丟 ConfigError。"""
    config_path = _write_config(
        tmp_path,
        {
            "fanouts": {
                "sw-1": {
                    "host": "198.51.100.10",
                    "username": "u",
                    "password": "p",
                    "interface_template": "Eth1/0",
                }
            }
        },
    )
    with pytest.raises(ConfigError) as exc_info:
        load_fanout_config(path=config_path, env={})
    assert "{port}" in str(exc_info.value)


def test_select_single_fanout(tmp_path: Path) -> None:
    """單台設定時 select_fanout 未指定也能直接選出該台。"""
    config_path = _write_config(
        tmp_path,
        {
            "fanouts": {
                "sw-1": {
                    "host": "198.51.100.10",
                    "username": "u",
                    "password": "p",
                }
            }
        },
    )
    config = load_fanout_config(path=config_path, env={})
    assert select_fanout(config, None) == "sw-1"


def test_select_multi_unspecified_raises(tmp_path: Path) -> None:
    """多台設定但未指定 --fanout 時 select_fanout 丟 ConfigError。"""
    config_path = _write_config(
        tmp_path,
        {
            "fanouts": {
                "sw-1": {"host": "198.51.100.1", "username": "u", "password": "p"},
                "sw-2": {"host": "198.51.100.2", "username": "u", "password": "p"},
            }
        },
    )
    config = load_fanout_config(path=config_path, env={})
    with pytest.raises(ConfigError):
        select_fanout(config, None)


def test_select_unknown_fanout_raises(tmp_path: Path) -> None:
    """指定的 fanout 查無時 select_fanout 丟 ConfigError。"""
    config_path = _write_config(
        tmp_path,
        {
            "fanouts": {
                "sw-1": {"host": "198.51.100.1", "username": "u", "password": "p"},
            }
        },
    )
    config = load_fanout_config(path=config_path, env={})
    with pytest.raises(ConfigError):
        select_fanout(config, "nope")
