"""設備下發 REST 的啟動期設定與 fail-fast 測試（tests/test_push_startup.py）。

涵蓋 ``Settings`` / ``from_env`` 對 ``TOPO_MANAGER_ENABLE_DEVICE_PUSH`` 與
``TOPO_MANAGER_API_TOKEN`` 的驗證，以及 ``topo-manager-api`` 進入點在
「啟用但沒有 token」時：非零 exit、stderr 單行、無 traceback。

歷史（HISTORY）：
  - 0.1.0  設備下發 REST 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from topo_manager.api import create_app
from topo_manager.api.deps import Settings
from topo_manager.errors import ConfigError


def test_enabled_without_token_raises_config_error() -> None:
    """啟用但 token 為 None 或空字串：ConfigError；有 token 則成功。"""
    with pytest.raises(ConfigError, match="TOPO_MANAGER_API_TOKEN"):
        Settings("t.yaml", enable_device_push=True)
    with pytest.raises(ConfigError, match="TOPO_MANAGER_API_TOKEN"):
        Settings("t.yaml", enable_device_push=True, api_token="")
    settings = Settings("t.yaml", enable_device_push=True, api_token="x")
    assert settings.enable_device_push is True


def test_disabled_by_default_needs_no_token() -> None:
    """預設關閉，不需要 token。"""
    settings = Settings("t.yaml")
    assert settings.enable_device_push is False
    assert settings.api_token is None
    assert settings.credentials_path is None


def _clean_env(monkeypatch: pytest.MonkeyPatch, topology: Path) -> None:
    for name in (
        "TOPO_MANAGER_ENABLE_DEVICE_PUSH",
        "TOPO_MANAGER_API_TOKEN",
        "TOPO_MANAGER_CREDENTIALS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOPO_MANAGER_TOPOLOGY", str(topology))


@pytest.mark.parametrize("value", [None, "", "0"])
def test_from_env_disabled_values(
    monkeypatch: pytest.MonkeyPatch, three_tier_path: Path, value
) -> None:
    """未設、空字串、'0' 都是關閉設備下發（不需要 token 即可啟動）。

    Design 1 起 ``api_token`` 不論是否啟用設備下發皆讀取（純帳本寫入端點以其
    閘控）：未設 token 時仍為 None（唯讀），設了 token 時即使設備下發關閉也會
    被讀入供帳本寫入閘門使用。
    """
    _clean_env(monkeypatch, three_tier_path)
    if value is not None:
        monkeypatch.setenv("TOPO_MANAGER_ENABLE_DEVICE_PUSH", value)
    settings = Settings.from_env()
    assert settings.enable_device_push is False
    # 未設 token → None（唯讀模式，寫入一律鎖住）。
    assert settings.api_token is None
    # 設備下發關閉時，token 仍會被讀取（供純帳本寫入端點閘控，Design 1）。
    monkeypatch.setenv("TOPO_MANAGER_API_TOKEN", "ledger-token")
    assert Settings.from_env().api_token == "ledger-token"


def test_from_env_enabled_reads_token_and_credentials(
    monkeypatch: pytest.MonkeyPatch, three_tier_path: Path
) -> None:
    """'1' 啟用，讀 token 與選填的 credentials 路徑。"""
    _clean_env(monkeypatch, three_tier_path)
    monkeypatch.setenv("TOPO_MANAGER_ENABLE_DEVICE_PUSH", "1")
    monkeypatch.setenv("TOPO_MANAGER_API_TOKEN", "tok")
    monkeypatch.setenv("TOPO_MANAGER_CREDENTIALS", "/tmp/some-creds.json")
    settings = Settings.from_env()
    assert settings.enable_device_push is True
    assert settings.api_token == "tok"
    assert settings.credentials_path == "/tmp/some-creds.json"


@pytest.mark.parametrize("value", ["true", "yes", "2", "on"])
def test_from_env_rejects_other_values(
    monkeypatch: pytest.MonkeyPatch, three_tier_path: Path, value: str
) -> None:
    """'true' 之類的值 fail-fast，不被默默當成關閉。"""
    _clean_env(monkeypatch, three_tier_path)
    monkeypatch.setenv("TOPO_MANAGER_ENABLE_DEVICE_PUSH", value)
    with pytest.raises(ConfigError, match="TOPO_MANAGER_ENABLE_DEVICE_PUSH"):
        Settings.from_env()


def test_create_app_fails_fast_without_token(
    monkeypatch: pytest.MonkeyPatch, three_tier_path: Path
) -> None:
    """create_app() 走環境變數：啟用卻沒有 token → ConfigError。"""
    _clean_env(monkeypatch, three_tier_path)
    monkeypatch.setenv("TOPO_MANAGER_ENABLE_DEVICE_PUSH", "1")
    with pytest.raises(ConfigError):
        create_app()


def test_server_entrypoint_fails_fast_one_line_no_traceback(three_tier_path: Path) -> None:
    """topo-manager-api 進入點：非零 exit、stderr 單行含變數名、無 Traceback。"""
    env = dict(os.environ)
    env.pop("TOPO_MANAGER_API_TOKEN", None)
    env["TOPO_MANAGER_TOPOLOGY"] = str(three_tier_path)
    env["TOPO_MANAGER_ENABLE_DEVICE_PUSH"] = "1"
    env["TOPO_MANAGER_HOST"] = "127.0.0.1"
    env["TOPO_MANAGER_PORT"] = "0"
    result = subprocess.run(
        [sys.executable, "-c", "from topo_manager.api.server import run; run()"],
        env=env,
        capture_output=True,
        timeout=60,
    )
    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode != 0
    assert "Traceback" not in stderr
    assert "TOPO_MANAGER_API_TOKEN" in stderr
    assert len(stderr.strip().splitlines()) == 1
