"""topo_manager 設備下發 REST 的兩道閘門（api/auth.py）。

MODULE NAME：
  topo_manager.api.auth

PURPOSE：
  提供套在下發 router（``dependencies=[...]``）上的兩個 dependency：

  - ``require_device_push``：功能未啟用 → ``DevicePushDisabledError``（404）。
  - ``require_api_token``：``X-API-Token`` 缺漏或與 ``TOPO_MANAGER_API_TOKEN``
    不符 → ``ApiTokenError``（401）。

NOTES：
  - 兩者皆為 ``async def``：同步 dependency 會被丟進 threadpool，違反
    「SIGALRM 檔鎖只能在主執行緒」的前提。
  - 順序即 router dependencies 的列表順序：先判斷啟用（未啟用連 token 都不比對），
    再驗 token；兩者都早於 query／body 驗證、帳本與 SSH。
  - token 以 UTF-8 bytes 傳入 ``hmac.compare_digest``（str 版遇非 ASCII 會
    ``TypeError``，造成 500）。缺漏與錯誤訊息相同，且不含任何 token 內容。
  - 單一共用密鑰，無使用者身分、無速率限制；token 長度不視為機密。

HISTORY：
  - 0.1.0  設備下發 REST 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import hmac

from fastapi import Request

from topo_manager.errors import (
    ApiTokenError,
    DevicePushDisabledError,
    WriteLockedError,
)

# 驗證用 header 名稱。
_TOKEN_HEADER = "X-API-Token"


async def require_device_push(request: Request) -> None:
    """確認設備下發功能已啟用；未啟用丟 ``DevicePushDisabledError``（404）。

    參數：
      - request：進來的請求（取 ``app.state.settings``）。

    例外：
      - DevicePushDisabledError：``settings.enable_device_push`` 不是 True。
    """
    if request.app.state.settings.enable_device_push is not True:
        raise DevicePushDisabledError(
            "設備下發端點未啟用（需設定 TOPO_MANAGER_ENABLE_DEVICE_PUSH=1）"
        )


async def require_api_token(request: Request) -> None:
    """驗證 ``X-API-Token``；缺漏或不符丟 ``ApiTokenError``（401）。

    啟動期已保證啟用時 ``api_token`` 非空。比較使用 ``hmac.compare_digest``
    （定時間比較），輸入以 UTF-8 bytes 比較。

    參數：
      - request：進來的請求（取 header 與 ``app.state.settings``）。

    例外：
      - ApiTokenError：header 缺漏或與設定的 token 不符。
    """
    expected = request.app.state.settings.api_token
    provided = request.headers.get(_TOKEN_HEADER)
    if provided is None:
        raise ApiTokenError("缺少或錯誤的 X-API-Token")
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise ApiTokenError("缺少或錯誤的 X-API-Token")


async def require_ledger_write(request: Request) -> None:
    """純帳本寫入端點的 token 閘門（``POST /sessions``／``DELETE /sessions/{id}``）。

    與設備下發的 ``require_api_token`` 不同，純帳本寫入不依賴
    ``TOPO_MANAGER_ENABLE_DEVICE_PUSH``：

    - 未設定 ``TOPO_MANAGER_API_TOKEN``（``api_token`` 為 None／空字串）→ 視為
      唯讀模式，一律鎖住寫入，丟 ``WriteLockedError``（403）；此時不比對 header。
    - 有設定 token → 驗 ``X-API-Token``，缺漏或不符丟 ``ApiTokenError``（401）。

    比較使用 ``hmac.compare_digest``（定時間比較），輸入以 UTF-8 bytes 比較；
    錯誤訊息不含任何 token 內容。

    參數：
      - request：進來的請求（取 header 與 ``app.state.settings``）。

    例外：
      - WriteLockedError：未設定 token（唯讀模式，寫入未解鎖）。
      - ApiTokenError：有設定 token 但 header 缺漏或不符。
    """
    expected = request.app.state.settings.api_token
    if expected is None or expected == "":
        raise WriteLockedError(
            "寫入未解鎖：未設定 TOPO_MANAGER_API_TOKEN（唯讀模式）"
        )
    provided = request.headers.get(_TOKEN_HEADER)
    if provided is None:
        raise ApiTokenError("缺少或錯誤的 X-API-Token")
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise ApiTokenError("缺少或錯誤的 X-API-Token")
