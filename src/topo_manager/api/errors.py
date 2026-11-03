"""topo_manager REST API 例外→HTTP 狀態碼映射與 handler（Phase 3，設計 §6）。

本模組把 Phase 2 的 ``TopologyError`` 家族映射成 HTTP 狀態碼，並提供統一的
結構化錯誤回應（``ErrorResponse``）。所有 handler 回傳的 body 皆為固定 schema
（``type=exc.__class__.__name__``、``message=str(exc)``），**不含 traceback、
不外洩 Python 堆疊**（NFR-3）。以標準 ``logging``（logger ``topo_manager.api``）
記錄：可映射錯誤 WARNING、非預期 500 ERROR + exc_info（設計 §6.5）。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 status_for 映射表與三個 handler + 註冊函式
           （設計 §6.2/§6.3/§6.4/§6.5）。
  - 0.4.1  Phase 3 審查 Issue 1：請求期拓樸漂移（讀路徑 build_allocator 重讀
           YAML 失敗）五個環境性 TopologyError 子類映射成 503（而非 500），
           與啟動期 fail-fast / 寫路徑吃單例的語意分離。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import logging
from typing import Dict, Type

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from topo_manager.api.schemas import ErrorDetail, ErrorResponse
from topo_manager.errors import (
    ConfigError,
    HardConflictError,
    PathLookupError,
    SessionNotFoundError,
    SessionStoreError,
    TopologyError,
    TopologyFileError,
    TopologyGraphError,
    TopologyParseError,
    TopologyReferenceError,
    TopologyValidationError,
    VlanPoolExhaustedError,
)

# API 層 logger（標準 logging，不新增相依，設計 §6.5）。
_LOGGER = logging.getLogger("topo_manager.api")

# 例外類別 → HTTP 狀態碼的明確對照表（設計 §6.2）。查不到的 TopologyError
# 子類落 500（見 status_for）。
#
# 「請求期拓樸漂移」→ 503（Issue 1）：讀路徑（GET /sessions、
# GET /resources/available）每請求以 build_allocator 重讀 YAML 並建圖；啟動
# 後若 YAML 被改壞 / 刪除 / 權限變更，load_topology / 建圖會丟下列五個環境性
# TopologyError 子類。這類錯誤是「服務暫時不可用、使用者修檔後可重試」，語意
# 上屬 503 而非 500（500 的 InternalError 會誤導成程式 bug）。此五類在啟動期由
# fail-fast 攔下、寫路徑（吃啟動單例、不重讀 YAML）亦不會拋，故僅可能發生於
# 讀路徑的請求期拓樸漂移——把它們映射成 503 不影響寫路徑的 404/409/400 語意。
_STATUS_BY_EXC: Dict[Type[TopologyError], int] = {
    HardConflictError: 409,
    VlanPoolExhaustedError: 409,
    SessionNotFoundError: 404,
    PathLookupError: 404,
    SessionStoreError: 503,
    # 請求期拓樸載入 / 建圖失敗（環境性、可修檔重試）→ 503（Issue 1）。
    TopologyFileError: 503,
    TopologyParseError: 503,
    TopologyValidationError: 503,
    TopologyReferenceError: 503,
    TopologyGraphError: 503,
    ConfigError: 500,
}

# 非預期例外（程式 bug）對外的固定訊息（不外洩內部細節，設計 §6.4）。
_INTERNAL_ERROR_MESSAGE = "內部錯誤，請聯絡維運"


def status_for(exc: TopologyError) -> int:
    """以例外具體類別查 HTTP 狀態碼（設計 §6.2）。

    參數：
      - exc：``TopologyError`` 家族的例外實例。

    回傳：
      - 對照表命中的狀態碼；未命中的 ``TopologyError`` 子類落 500。
    """
    return _STATUS_BY_EXC.get(type(exc), 500)


def _error_json(status_code: int, error_type: str, message: str) -> JSONResponse:
    """以 ``ErrorResponse`` 形狀建 JSONResponse（統一結構化回應，設計 §6.1）。

    參數：
      - status_code：HTTP 狀態碼。
      - error_type：錯誤分類字串（例外類別名 / ValidationError / InternalError）。
      - message：面向使用者的訊息（不含 traceback）。

    回傳：
      - body 為 ``{"error": {"type": ..., "message": ...}}`` 的 JSONResponse。
    """
    payload = ErrorResponse(error=ErrorDetail(type=error_type, message=message))
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def topology_error_handler(request: Request, exc: TopologyError) -> JSONResponse:
    """``TopologyError`` 家族 handler：依具體子類映射狀態碼（設計 §6.2/§6.5）。

    參數：
      - request：進來的請求（未使用；FastAPI handler 簽章要求）。
      - exc：被攔截的 ``TopologyError``。

    回傳：
      - 結構化 ``ErrorResponse``（``type`` 為例外類別名、``message`` 為
        ``str(exc)``，不含 traceback）。
    """
    status_code = status_for(exc)
    error_type = exc.__class__.__name__
    _LOGGER.warning(
        "API 業務失敗：%s（%d）：%s", error_type, status_code, str(exc)
    )
    return _error_json(status_code, error_type, str(exc))


def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """請求驗證失敗 handler：統一回 400 + 結構化訊息（設計 §6.3）。

    把 FastAPI 預設的 422 ``RequestValidationError`` 覆寫為 400，body 走同一
    ``ErrorResponse`` schema（``type="ValidationError"``、``message`` 為彙整的
    欄位錯誤摘要）。此路徑不觸及 allocator（驗證在進 handler 前完成，AC-10）。

    參數：
      - request：進來的請求（未使用）。
      - exc：Pydantic / FastAPI 的 ``RequestValidationError``。

    回傳：
      - 400 + 結構化 ``ErrorResponse``。
    """
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        details.append("{0}：{1}".format(location, error.get("msg", "")))
    message = "請求驗證失敗：{0}".format("；".join(details))
    _LOGGER.warning("API 請求驗證失敗（400）：%s", message)
    return _error_json(400, "ValidationError", message)


def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """非預期例外 handler：回 500 固定訊息、不外洩 traceback（設計 §6.4/§6.5）。

    任何非 ``TopologyError``、非驗證錯的例外（程式 bug），回 500 且 body 為
    固定訊息的 ``ErrorResponse``（``type="InternalError"``）。完整堆疊只寫
    server 端 log（ERROR + exc_info），不入 HTTP body（NFR-3）。

    參數：
      - request：進來的請求（未使用）。
      - exc：被攔截的非預期例外。

    回傳：
      - 500 + 固定訊息的結構化 ``ErrorResponse``。
    """
    _LOGGER.error("API 非預期例外（500）", exc_info=exc)
    return _error_json(500, "InternalError", _INTERNAL_ERROR_MESSAGE)


def register_exception_handlers(app: FastAPI) -> None:
    """把三個 handler 掛到 app（設計 §6.2/§6.3/§6.4）。

    ``TopologyError`` 基底 handler 對所有子類生效；``RequestValidationError``
    覆寫為 400；``Exception`` fallback 保底 500。

    參數：
      - app：要註冊 handler 的 ``FastAPI`` 實例。
    """
    app.add_exception_handler(TopologyError, topology_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
