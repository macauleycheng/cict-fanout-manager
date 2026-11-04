"""設備下發 REST 存取控制模組測試（tests/test_push_auth.py）。

直接以替身 Request 呼叫 ``require_device_push`` / ``require_api_token``，並對
原始碼做靜態檢查：必須使用 ``hmac.compare_digest`` 且以 UTF-8 bytes 比較。

歷史（HISTORY）：
  - 0.1.0  設備下發 REST 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from topo_manager.api import auth
from topo_manager.errors import ApiTokenError, DevicePushDisabledError


def _request(enabled: bool = True, token: str = "secret", headers: dict = None):
    """組出具 app.state.settings 與 headers 的最小替身 Request。"""
    settings = SimpleNamespace(enable_device_push=enabled, api_token=token)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    return SimpleNamespace(app=app, headers=headers or {})


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_dependencies_are_async() -> None:
    """兩個 dependency 都是 async def（同步版會進 threadpool，破壞主執行緒前提）。"""
    assert inspect.iscoroutinefunction(auth.require_device_push)
    assert inspect.iscoroutinefunction(auth.require_api_token)


def test_source_uses_hmac_compare_digest_on_utf8_bytes() -> None:
    """原始碼層確認：使用 hmac.compare_digest，且比較對象為 UTF-8 bytes。"""
    source = inspect.getsource(auth)
    assert "hmac.compare_digest" in source
    assert '.encode("utf-8")' in source
    assert "import hmac" in source


def test_missing_token_raises() -> None:
    with pytest.raises(ApiTokenError):
        _run(auth.require_api_token(_request()))


def test_wrong_token_raises_same_message_as_missing() -> None:
    with pytest.raises(ApiTokenError) as missing:
        _run(auth.require_api_token(_request()))
    with pytest.raises(ApiTokenError) as wrong:
        _run(auth.require_api_token(_request(headers={"X-API-Token": "nope"})))
    assert str(missing.value) == str(wrong.value)
    assert "secret" not in str(wrong.value)


def test_correct_token_passes() -> None:
    assert _run(auth.require_api_token(_request(headers={"X-API-Token": "secret"}))) is None


def test_non_ascii_token_does_not_raise_type_error() -> None:
    """非 ASCII（含設定值）都不會 TypeError：不符 → ApiTokenError，相符 → 通過。"""
    with pytest.raises(ApiTokenError):
        _run(auth.require_api_token(_request(headers={"X-API-Token": "密鑰"})))
    request = _request(token="密鑰", headers={"X-API-Token": "密鑰"})
    assert _run(auth.require_api_token(request)) is None


@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_raises(enabled) -> None:
    with pytest.raises(DevicePushDisabledError):
        _run(auth.require_device_push(_request(enabled=enabled)))


def test_enabled_is_noop() -> None:
    assert _run(auth.require_device_push(_request(enabled=True))) is None
