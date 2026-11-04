"""topo_manager REST API 子套件（Phase 3，設計 §3）。

本子套件在 Phase 1/2 之上疊一層薄層 FastAPI REST 介接，讓 ATLAS 能查詢可用
資源、要求 / 釋放 session、檢視完整拓樸。所有分配 / 衝突 / VLAN / 持久化演算法
一律沿用 Phase 2 的 ``Allocator`` 與 ``JsonSessionStore``；本層只負責
「HTTP 請求 ↔ Pydantic 模型 ↔ allocator 呼叫 ↔ 例外映射成 HTTP 狀態碼」。

模組切分（設計 §3）：

  - ``app``：``create_app(settings=None)`` factory 本體（唯一定義處）。
  - ``routes``：五個 endpoint 的 route handler 與可用資源重算輔助。
  - ``schemas``：請求 / 回應的 Pydantic 模型（OpenAPI schema 來源）。
  - ``deps``：設定（``Settings``）與 Allocator factory（相依注入）。
  - ``errors``：``TopologyError`` 家族 → HTTP 狀態碼的映射與 handler。
  - ``server``：``run()``（``topo-manager-api`` 入口，封裝 uvicorn）。
  - ``ic_routes`` / ``ic_schemas`` / ``ic_deps``：互聯 fanout 的帳本層端點、
    請求 / 回應模型與 allocator 建構輔助（不連任何設備）。

``create_app`` 於本 ``__init__`` 以 ``from topo_manager.api.app import
create_app`` re-export，使匯入字串 ``topo_manager.api:create_app`` 成立（供
uvicorn ``--factory`` 與測試 ``from topo_manager.api import create_app`` 共用，
設計 §3 單一歸屬裁決）。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 api/ 子套件並 re-export create_app（設計 §3）。
  - 0.5.0  互聯 REST：新增 ic_routes / ic_schemas / ic_deps。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from topo_manager.api.app import create_app

__all__ = ["create_app"]
