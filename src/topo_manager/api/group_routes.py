"""topo_manager 群組 REST 的七個 endpoint（api/group_routes.py，FEAT-002）。

MODULE NAME：
  topo_manager.api.group_routes

PURPOSE：
  以 ``APIRouter`` 定義群組「標籤聚合」端點（CRUD + ATLAS v2 輸出）：

  - ``POST   /groups``                        ← 建立空群組（201）
  - ``GET    /groups``                        ← 列出所有群組
  - ``GET    /groups/{name}``                 ← 取回單一群組（404 if absent）
  - ``DELETE /groups/{name}``                 ← 刪除群組
  - ``POST   /groups/{name}/members``         ← 加入成員（auto_create=False）
  - ``DELETE /groups/{name}/members/{session_id}`` ← 移除成員
  - ``GET    /groups/{name}/atlas``           ← ATLAS v2 輸出

NOTES：
  - 讀寫分工（對齊 routes.py 的 build_allocator）：寫路徑共用啟動期單例
    ``request.app.state.group_registry``（正確性由 GroupStore.locked() + 鎖內
    reload 保證）；讀路徑（list／get／atlas）每請求以 ``build_group_registry``
    重建以取最新帳本快照。
  - 所有 handler 皆為 ``async def``：GroupStore.locked() 以 signal.SIGALRM 實作
    取鎖逾時，``signal.signal`` 只能在主執行緒呼叫；async handler 由事件迴圈在
    主執行緒執行，寫路徑取鎖才不會拋「signal only works in main thread」。
  - atlas handler：取群組成員（群組不存在→404），以 build_allocator 逐 member
    解析 traffic session；成員的 session 已不存在（SessionNotFoundError）時
    **刻意略過**（不視為硬錯誤——群組是純標籤，session 可能已被他途釋放），
    再把啟動單例 topology 與解析到的 session 餵給 build_atlas_v2，直接回傳
    dict（省略 response_model，ATLAS v2 頂層 key 為群組名、動態結構）。

HISTORY：
  - 0.7.0  群組 REST（FEAT-002）初版：建立七個群組端點。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request, status

from topo_manager.allocator import Allocator, Session
from topo_manager.api.deps import (
    Settings,
    build_allocator,
    build_group_registry,
)
from topo_manager.api.group_schemas import (
    AddMemberRequest,
    CreateGroupRequest,
    GroupListResponse,
    GroupResponse,
)
from topo_manager.groups.atlas import build_atlas_v2
from topo_manager.groups.registry import GroupRegistry
from topo_manager.errors import SessionNotFoundError
from topo_manager.models import Topology

router = APIRouter()


@router.post(
    "/groups",
    response_model=GroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="建立一個空群組。",
)
async def create_group(request: Request, body: CreateGroupRequest) -> GroupResponse:
    """建立一個空群組並回其摘要（201；已存在→409，FEAT-002）。

    呼叫啟動單例 group_registry.create_group（正確性由鎖內交易保證）。
    """
    registry: GroupRegistry = request.app.state.group_registry
    entry = registry.create_group(body.name)
    return GroupResponse.from_entry(entry)


@router.get(
    "/groups",
    response_model=GroupListResponse,
    summary="列出目前所有群組。",
)
async def list_groups(request: Request) -> GroupListResponse:
    """列出目前所有群組（讀路徑，每請求重建取最新快照，FEAT-002）。"""
    settings: Settings = request.app.state.settings
    registry = build_group_registry(settings)
    entries = registry.list_groups()
    responses = [GroupResponse.from_entry(entry) for entry in entries]
    return GroupListResponse(groups=responses, count=len(responses))


@router.get(
    "/groups/{name}",
    response_model=GroupResponse,
    summary="取回單一群組（查無回 404）。",
)
async def get_group(request: Request, name: str) -> GroupResponse:
    """取回單一群組（讀路徑；查無→404，FEAT-002）。"""
    settings: Settings = request.app.state.settings
    registry = build_group_registry(settings)
    entry = registry.get_group(name)
    return GroupResponse.from_entry(entry)


@router.delete(
    "/groups/{name}",
    response_model=GroupResponse,
    summary="刪除一個群組（回被刪除群組摘要）。",
)
async def delete_group(request: Request, name: str) -> GroupResponse:
    """刪除一個群組並回其摘要（查無→404，FEAT-002）。

    呼叫啟動單例 group_registry.delete_group（鎖內交易）。
    """
    registry: GroupRegistry = request.app.state.group_registry
    entry = registry.delete_group(name)
    return GroupResponse.from_entry(entry)


@router.post(
    "/groups/{name}/members",
    response_model=GroupResponse,
    summary="把一個 session 加入群組（群組須已存在）。",
)
async def add_member(
    request: Request, name: str, body: AddMemberRequest
) -> GroupResponse:
    """把 session 加入既有群組並回其摘要（FEAT-002）。

    群組不存在→404（auto_create=False）；session 已屬他群→409。
    """
    registry: GroupRegistry = request.app.state.group_registry
    entry = registry.add_member(name, body.session_id, auto_create=False)
    return GroupResponse.from_entry(entry)


@router.delete(
    "/groups/{name}/members/{session_id}",
    response_model=GroupResponse,
    summary="把一個 session 自群組移除（回群組摘要）。",
)
async def remove_member(
    request: Request, name: str, session_id: str
) -> GroupResponse:
    """把 session 自指定群組移除並回其摘要（查無群組→404，FEAT-002）。

    呼叫啟動單例 group_registry.remove_member（鎖內交易；session 不在該群為
    no-op）。
    """
    registry: GroupRegistry = request.app.state.group_registry
    entry = registry.remove_member(name, session_id)
    return GroupResponse.from_entry(entry)


@router.get(
    "/groups/{name}/atlas",
    summary="以 ATLAS v2 格式輸出群組（頂層 key 為群組名）。",
)
async def get_group_atlas(request: Request, name: str) -> Dict[str, Any]:
    """以 ATLAS v2 格式輸出群組（讀路徑；群組查無→404，FEAT-002）。

    每請求以 build_group_registry 取群組成員、以 build_allocator 逐 member 解析
    traffic session（已不存在的成員刻意略過），再餵 topology + sessions 給
    build_atlas_v2。回傳動態結構 dict（省略 response_model）；空群組回
    ``{name: []}``。
    """
    settings: Settings = request.app.state.settings
    topology: Topology = request.app.state.topology

    registry = build_group_registry(settings)
    entry = registry.get_group(name)

    allocator: Allocator = build_allocator(settings)
    member_sessions: List[Session] = []
    for session_id in entry.members:
        try:
            member_sessions.append(allocator.get_session(session_id))
        except SessionNotFoundError:
            # 成員的 session 已被他途釋放：群組純標籤，刻意略過，不視為硬錯誤。
            continue

    return build_atlas_v2(entry.name, member_sessions, topology)
