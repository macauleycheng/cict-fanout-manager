"""topo_manager REST API 的 Web UI 靜態服務掛載（設計 §1）。

``register_ui(app: FastAPI) -> None`` 把 Web UI 的靜態資產掛到既有 app，
形狀對齊 ``register_exception_handlers(app)``：純靜態服務，不讀 ``app.state``、
不碰 allocator / topology，故不參與啟動期 fail-fast、不會拋 ``TopologyError``。

做兩件事（設計 §1.2）：

  1. 掛 ``/static`` 靜態目錄：``StaticFiles(directory=..., check_dir=False)``。
     目錄以模組檔位置推導（``Path(__file__).resolve().parent / "static"``），
     不依賴 process cwd，故不論 uvicorn 從哪啟動都服得到。``check_dir=False``
     為必要（設計 §1.2 / 審查 Finding 2）：starlette 0.37.2 的 ``StaticFiles``
     預設 ``check_dir=True`` 會在建構時 ``os.path.isdir`` 檢查、目錄缺失即丟
     ``RuntimeError``；因本掛載在 fail-fast 的 ``create_app`` 內建構，缺資產
     不應升級成「整個啟動失敗」。關掉 check_dir 後，缺資產退化為每請求 404
     （static 層），與「static 404 非業務錯誤」一致。三個靜態檔仍入版控並以
     package-data 出貨，正常安裝必有。

  2. 掛根路由 ``GET /``：``include_in_schema=False``（不污染 OpenAPI，保五個
     業務路徑斷言乾淨，設計 §1.3），回 ``FileResponse(index.html)``。

非干擾保證（設計 §1.4）：``register_ui`` 於 ``include_router(routes.router)``
之後呼叫；``/`` 與 ``/static/*`` 與五個業務路徑無前綴碰撞；starlette 依註冊
順序配對、業務路由先勝。主執行緒 / signal 檔鎖部署契約不受影響（不新增執行緒
/ middleware、不動寫路徑取鎖）。

歷史（HISTORY）：
  - 0.5.0  Web UI 初版：新增 register_ui，掛 /static 靜態目錄（check_dir=False）
           與 GET /（FileResponse index.html，include_in_schema=False），僅用
           既有五個端點、不新增後端邏輯（設計 §1）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

# 靜態資產目錄：以本模組檔位置推導，與 process cwd 無關（設計 §1.2）。
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"


def register_ui(app: FastAPI) -> None:
    """把 Web UI 靜態服務掛到 app（設計 §1.2；形狀對齊 register_exception_handlers）。

    掛一個 ``/static`` 靜態目錄（``check_dir=False``，設計 §1.2 / Finding 2）
    與一個 ``GET /`` 根路由（``include_in_schema=False``，回 index.html）。純靜態
    服務：不讀 ``app.state``、不碰 allocator / topology，故不參與 fail-fast。

    參數：
      - app：要掛 UI 的 ``FastAPI`` 實例（應已 include 業務 router，設計 §1.4）。
    """
    # /static 靜態掛載：check_dir=False 讓缺資產退化為每請求 404 而非啟動失敗。
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR), check_dir=False),
        name="static",
    )

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        """回傳 Web UI 的 index.html（純靜態、不碰 allocator / topology，設計 §1.2）。

        宣告為 ``async def`` 與既有 handler 一致（實際不 await）；不觸及 signal
        檔鎖（根路由永不碰 allocator / store）。

        回傳：
          - ``text/html`` 的 ``FileResponse``（串流 index.html，設計 §1.2）。
        """
        return FileResponse(str(_INDEX_HTML), media_type="text/html")
