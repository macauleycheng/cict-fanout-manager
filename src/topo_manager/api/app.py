"""topo_manager REST API 的 create_app factory（Phase 3，設計 §3、§4.3）。

``create_app(settings=None) -> FastAPI`` 是 app factory 的**唯一定義處**
（設計 §3 單一歸屬裁決）：``settings is None`` 時走 ``Settings.from_env()``
（供 uvicorn ``--factory``）；顯式傳入 ``settings`` 供測試注入。

啟動期 fail-fast（設計 §4.3）：factory 執行時立即 ``load_topology`` + 建單例
``Allocator``；若拓樸 YAML 缺失 / 不合法 / 建圖失敗 / ledger 還原不一致 /
VLAN 範圍非法，直接讓 factory 拋 ``TopologyError``（含子類），不延後到請求期。
settings / shared allocator / topology 存入 ``app.state`` 供 routes 取用。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 create_app factory（fail-fast、掛 router、
           註冊 exception handlers，設計 §3、§4.3、§5.6）。
  - 0.5.0  Web UI：於 include_router 之後呼叫 register_ui(app) 掛 /static 與
           GET /（僅用既有五個端點、不新增後端邏輯，設計 §1）。_API_VERSION
           維持 "0.4.0"（API 契約未變；UI 版本僅記於 HISTORY）。
  - 0.6.0  互聯 REST：新增 app.state.ic_graph（啟動時建立，無 interconnect
           區段時為 None，不影響既有端點）並掛 ic_routes.router；
           _API_VERSION 由 "0.4.0" 升為 "0.5.0"（OpenAPI 契約新增互聯端點，
           屬 semver minor；此 0.6.0 為本模組 HISTORY 標號，與 _API_VERSION
           是兩套計數）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from topo_manager.api import ic_routes, routes
from topo_manager.api.deps import Settings, build_allocator
from topo_manager.api.errors import register_exception_handlers
from topo_manager.api.ic_deps import build_ic_startup_graph
from topo_manager.api.ui import register_ui
from topo_manager.loader import load_topology

# app metadata（設計 §5.6）。
_API_TITLE = "topo-manager API"
_API_VERSION = "0.5.0"
_API_DESCRIPTION = (
    "topo-manager 的 REST 介接層：查詢可用資源、要求 / 釋放 session、檢視完整 "
    "拓樸，並提供互聯 fanout 的帳本層操作（建立 / 釋放 / 列出 / 計畫查詢）。 "
    "所有分配 / 衝突 / VLAN / 持久化演算法沿用既有 Allocator / "
    "InterconnectAllocator 與其 JsonSessionStore / IcSessionStore；本層為薄層 "
    "HTTP 包裝。互聯的設備下發（apply / park / 帶設備的 release / verify）僅限 "
    "CLI，REST 不提供、不連任何設備。"
)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """建立並回傳設定完成的 ``FastAPI`` app（唯一 factory，設計 §3、§4.3）。

    啟動期 fail-fast：立即載入拓樸並建單例 allocator；拓樸 / 設定 / ledger
    還原錯誤直接往外拋 ``TopologyError``（含子類），讓 uvicorn 啟動失敗。

    參數：
      - settings：顯式設定（測試注入）；``None`` 時走 ``Settings.from_env()``
        （供 uvicorn ``--factory``）。

    回傳：
      - 已掛 router、註冊 exception handler、且 ``app.state`` 含 settings /
        allocator / topology 的 ``FastAPI`` 實例。

    例外：
      - TopologyError 家族：啟動期拓樸 / 設定 / ledger 還原失敗（fail-fast）。
    """
    if settings is None:
        settings = Settings.from_env()

    # 啟動期 fail-fast：先載拓樸（供 /topology 純讀單例），再建共用 allocator。
    topology = load_topology(settings.topology_path)
    allocator = build_allocator(settings)

    app = FastAPI(
        title=_API_TITLE,
        version=_API_VERSION,
        description=_API_DESCRIPTION,
    )

    # 共用狀態存 app.state：讀路徑用 settings 重建、寫路徑共用 allocator、
    # /topology 用啟動單例 topology（設計 §4.2）。
    app.state.settings = settings
    app.state.topology = topology
    app.state.allocator = allocator
    # 互聯小圖：無 interconnect 區段時為 None（降級，app 照常啟動，既有端點
    # 完全不受影響）；啟動期不讀互聯帳本（互聯帳本故障不得拖垮打流側 API）。
    app.state.ic_graph = build_ic_startup_graph(settings, topology)

    app.include_router(routes.router)
    app.include_router(ic_routes.router)
    # Web UI 靜態服務：於 include_router 之後掛 /static 與 GET /（設計 §1.1/§1.4）。
    # register_ui 純靜態、不碰 app.state / allocator / topology，不參與 fail-fast。
    register_ui(app)
    register_exception_handlers(app)

    return app
