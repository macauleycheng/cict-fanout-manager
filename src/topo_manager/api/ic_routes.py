"""topo_manager 互聯 REST 的八個 endpoint（api/ic_routes.py）。

MODULE NAME：
  topo_manager.api.ic_routes

PURPOSE：
  以 ``APIRouter`` 定義互聯「帳本層」端點，語意與 ``interconnect/cli.py`` 一致：

  - ``GET  /interconnect/summary``                     ← ``summary``
  - ``GET  /interconnect/resources/available``         ← 各組 port 佔用與可建 link
  - ``GET  /interconnect/sessions``                    ← ``list``
  - ``POST /interconnect/sessions``                    ← ``allocate``
  - ``DELETE /interconnect/sessions/{session_id}``     ← ``release --ledger-only``
  - ``POST /interconnect/plan``                        ← ``plan --group ...``（預覽）
  - ``GET  /interconnect/sessions/{session_id}/plan``  ← ``plan --session [--release]``
  - ``GET  /interconnect/plan/park``                   ← ``plan --park``

NOTES：
  - 薄層：只做 HTTP、Pydantic 模型、呼叫既有 ``InterconnectAllocator`` /
    ``InterconnectDeployer.plan_*`` 與例外映射；配對、衝突、VLAN 邏輯一律沿用
    allocator，不重寫、不繞過跨行程檔鎖。
  - 所有 handler 皆為 ``async def`` 且不使用同步 ``Depends``、不使用
    threadpool：``store.locked()`` 以 ``signal.SIGALRM`` 實作取鎖逾時，而
    ``signal.signal`` 只能在主執行緒呼叫，``async def`` handler 由事件迴圈在
    主執行緒執行。
  - REST **不連任何設備**：不提供 apply / park / 帶設備的 release / verify。
    plan 端點以空連線設定與一個一旦被呼叫就拋錯的 driver 工廠建 deployer，
    不讀取連線設定檔或憑證，指令文字僅由 fanout port 編號與 VLAN 決定。
  - DELETE 為 ledger-only，不取設備鎖（``.device.lock``）；回應帶 ``warning``。
  - 拓樸沒有 interconnect 區段時，每個 handler 第一行即丟
    ``InterconnectNotConfiguredError``（404）。

HISTORY：
  - 0.1.0  互聯 REST 初版（帳本層八個端點）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query, Request, status
from typing_extensions import Literal

from topo_manager.api.auth import require_ledger_write
from topo_manager.api.deps import Settings
from topo_manager.api.ic_deps import build_ic_allocator
from topo_manager.api.ic_schemas import (
    IcAllocateRequest,
    IcAvailableResponse,
    IcAvailDutOut,
    IcAvailGroupOut,
    IcAvailLinkOut,
    IcFanoutUsageOut,
    IcPlanResponse,
    IcPortStateOut,
    IcSessionListResponse,
    IcSessionResponse,
    IcSummaryDutOut,
    IcSummaryGroupOut,
    IcSummaryResponse,
)
from topo_manager.api.schemas import ErrorResponse
from topo_manager.errors import (
    InterconnectNotConfiguredError,
    InterconnectRequestError,
)
from topo_manager.interconnect.allocator import InterconnectAllocator
from topo_manager.interconnect.layout import IcGraph
from topo_manager.interconnect.models import IcSession
from topo_manager.interconnect.pairing import pair_port_indices
from topo_manager.models import Topology

# 各端點共用的錯誤回應文件（OpenAPI 完整 schema）。
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "請求不合法"},
    404: {"model": ErrorResponse, "description": "找不到 session，或拓樸沒有互聯區段"},
    409: {"model": ErrorResponse, "description": "port 衝突或 VLAN 池不足"},
    503: {"model": ErrorResponse, "description": "互聯帳本 I/O 或取鎖故障"},
}

# DELETE 的固定警告（與 CLI --ledger-only 同義）。
_RELEASE_WARNING = "僅自帳本釋放，設備未 park；殘留設定需以 CLI 'interconnect park --apply' 清理。"

router = APIRouter(prefix="/interconnect", tags=["interconnect"])


# ======================================================================
# 輔助
# ======================================================================
def _require_ic(request: Request) -> Tuple[Settings, Topology]:
    """確認拓樸有互聯區段並取出設定與拓樸（非 ``Depends``，維持主執行緒）。

    參數：
      - request：進來的請求（取 ``app.state``）。

    回傳：
      - (settings, topology)。

    例外：
      - InterconnectNotConfiguredError：拓樸沒有 interconnect 區段（404）。
    """
    if request.app.state.ic_graph is None:
        raise InterconnectNotConfiguredError(
            "此拓樸沒有 interconnect 區段，無法使用互聯端點"
        )
    return request.app.state.settings, request.app.state.topology


def _check_request_mode(body: IcAllocateRequest) -> None:
    """二擇一檢查，逐條對應 ``cli._check_request_mode``。

    以 ``is None`` 判斷：``duts: []``、``pairs: []`` 視為「有提供」，再由
    allocator 以台數或「至少一組 port 對」拒絕（與 CLI 一致）。

    例外：
      - InterconnectRequestError：二擇一違反、duts 缺 profile、pairs 與 profile 並用。
    """
    has_duts = body.duts is not None
    has_pairs = body.pairs is not None
    if has_duts == has_pairs:
        raise InterconnectRequestError("請擇一提供 duts（搭配 profile）或 pairs")
    if has_duts and body.profile is None:
        raise InterconnectRequestError("duts 需搭配 profile（mesh | line | ring）")
    if has_pairs and body.profile is not None:
        raise InterconnectRequestError("pairs（明確 port 對）不可與 profile 並用")


def _pairs_of(body: IcAllocateRequest) -> List[Tuple[str, str]]:
    """把請求的 pairs 轉成 allocator 要的 ``(a, b)`` 元組清單（兩端去頭尾空白）。"""
    return [(item.a.strip(), item.b.strip()) for item in (body.pairs or [])]


def _raise_no_driver(*_args: object, **_kwargs: object) -> object:
    """plan 用 deployer 的 driver 工廠：REST 層不應建立 driver，被呼叫即拋錯。"""
    raise RuntimeError("REST 層不應建立 driver")


def _build_plan_deployer(settings: Settings):
    """建立只供純計算 plan 使用的 deployer（延遲 import drivers）。

    不傳 store / allocator、連線設定為空、driver 工廠一旦被呼叫就拋錯，故
    不可能連線，也不讀取連線設定檔或憑證。

    參數：
      - settings：API 設定（取互聯 VLAN 池）。

    回傳：
      - ``InterconnectDeployer``。
    """
    from topo_manager.drivers.config import FanoutConfig
    from topo_manager.drivers.ic_deployer import InterconnectDeployer

    return InterconnectDeployer(
        _raise_no_driver,
        FanoutConfig(fanouts={}),
        vlan_min=settings.ic_vlan_min,
        vlan_max=settings.ic_vlan_max,
        force_access_mode=True,
    )


def compute_ic_available(graph: IcGraph, owned: Dict[str, str]) -> IcAvailableResponse:
    """由互聯小圖與 port 擁有者快照計算各組的佔用與可建 link。

    可建 link 只用固定配對表（``pair_port_indices``）判斷兩端 port 是否皆未
    佔用；p3 與其他明確 port 對的可用性由 ``ports[].occupied_by`` 讀出。

    參數：
      - graph：互聯小圖。
      - owned：fanout 節點 → 佔用的 session_id。

    回傳：
      - ``IcAvailableResponse``。
    """
    groups: List[IcAvailGroupOut] = []
    for group in graph.groups.values():
        duts: List[IcAvailDutOut] = []
        for dut in group.duts:
            duts.append(
                IcAvailDutOut(
                    label="d{0}".format(dut.index),
                    name=dut.name,
                    ports=[
                        IcPortStateOut(
                            p_index=port.p_index,
                            il_port=port.il_port,
                            node=port.node,
                            occupied_by=owned.get(port.node),
                        )
                        for port in dut.ports
                    ],
                )
            )
        links: List[IcAvailLinkOut] = []
        for pos_a, dut_a in enumerate(group.duts):
            for dut_b in group.duts[pos_a + 1:]:
                idx_a, idx_b = pair_port_indices(dut_a.index, dut_b.index)
                port_a = dut_a.ports[idx_a]
                port_b = dut_b.ports[idx_b]
                if port_a.node in owned or port_b.node in owned:
                    continue
                links.append(
                    IcAvailLinkOut(
                        a_dut=dut_a.name,
                        a_port=port_a.il_port,
                        b_dut=dut_b.name,
                        b_port=port_b.il_port,
                    )
                )
        groups.append(
            IcAvailGroupOut(
                name=group.name, fanout=group.fanout, duts=duts, buildable_links=links
            )
        )
    return IcAvailableResponse(groups=groups)


def _preview_session(allocator: InterconnectAllocator, body: IcAllocateRequest) -> IcSession:
    """依請求模式呼叫 allocator 預覽（不取鎖、不入帳、不佔池）。"""
    _check_request_mode(body)
    if body.duts is not None:
        return allocator.preview_profile(body.group, list(body.duts), body.profile)
    return allocator.preview_pairs(body.group, _pairs_of(body))


# ======================================================================
# 讀端點
# ======================================================================
@router.get(
    "/summary",
    response_model=IcSummaryResponse,
    summary="互聯摘要：組、組內 DUT（d0..d3）與各互聯 fanout 的 port 使用。",
    responses=_ERROR_RESPONSES,
)
async def get_ic_summary(request: Request) -> IcSummaryResponse:
    """回傳互聯小圖摘要（不碰帳本，帳本故障時仍可用）。"""
    _require_ic(request)
    data = request.app.state.ic_graph.summary()
    return IcSummaryResponse(
        fanout_count=data["fanout_count"],
        group_count=data["group_count"],
        dut_count=data["dut_count"],
        link_count=data["link_count"],
        fanouts=[
            IcFanoutUsageOut(
                name=name,
                wired_ports=data["fanouts"][name]["used"],
                port_count=data["fanouts"][name]["total"],
            )
            for name in sorted(data["fanouts"])
        ],
        groups=[
            IcSummaryGroupOut(
                name=group["name"],
                rack=group["rack"],
                fanout=group["fanout"],
                duts=[
                    IcSummaryDutOut(label="d{0}".format(index), name=name)
                    for index, name in enumerate(group["duts"])
                ],
            )
            for group in data["groups"]
        ],
    )


@router.get(
    "/resources/available",
    response_model=IcAvailableResponse,
    summary="目前可用的互聯資源：各組 port 佔用與固定配對表中可建的 link。",
    responses=_ERROR_RESPONSES,
)
async def get_ic_available(request: Request) -> IcAvailableResponse:
    """回傳各組目前哪些互聯 port 仍未佔用、哪些固定配對 link 可建。

    「可建」只看 port，不保證 VLAN 池仍有空位；真正成敗由建立端點判定。
    """
    settings, topology = _require_ic(request)
    allocator = build_ic_allocator(settings, topology)
    return compute_ic_available(allocator.graph, allocator.owned_nodes())


@router.get(
    "/sessions",
    response_model=IcSessionListResponse,
    summary="列出目前所有互聯 session。",
    responses=_ERROR_RESPONSES,
)
async def list_ic_sessions(request: Request) -> IcSessionListResponse:
    """列出所有互聯 session（依 session_id 升冪，status 固定 active）。"""
    settings, topology = _require_ic(request)
    allocator = build_ic_allocator(settings, topology)
    items = [
        IcSessionResponse.from_ic_session(session, status="active")
        for session in allocator.list_sessions()
    ]
    return IcSessionListResponse(sessions=items, count=len(items))


# ======================================================================
# 寫端點（帳本層）
# ======================================================================
@router.post(
    "/sessions",
    response_model=IcSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="建立互聯 session（duts + profile，或明確 port 對；二選一）。",
    responses=_ERROR_RESPONSES,
    dependencies=[Depends(require_ledger_write)],
)
async def create_ic_session(request: Request, body: IcAllocateRequest) -> IcSessionResponse:
    """建立互聯 session 並回每條 link 的 VLAN 與兩端 port（只動帳本，不連設備）。

    port 衝突與 VLAN 唯一由 allocator 在 ``store.locked()`` 內判定。
    """
    settings, topology = _require_ic(request)
    _check_request_mode(body)
    allocator = build_ic_allocator(settings, topology)
    if body.duts is not None:
        session = allocator.allocate_profile(body.group, list(body.duts), body.profile)
    else:
        session = allocator.allocate_pairs(body.group, _pairs_of(body))
    return IcSessionResponse.from_ic_session(session, status="allocated")


@router.delete(
    "/sessions/{session_id}",
    response_model=IcSessionResponse,
    summary="釋放互聯 session（僅帳本層，等同 release --ledger-only；不連設備）。",
    responses=_ERROR_RESPONSES,
    dependencies=[Depends(require_ledger_write)],
)
async def delete_ic_session(request: Request, session_id: str) -> IcSessionResponse:
    """自帳本移除 session（任何 phase 皆可）；設備未 park，回應帶 warning。"""
    settings, topology = _require_ic(request)
    allocator = build_ic_allocator(settings, topology)
    session = allocator.release_now(session_id)
    return IcSessionResponse.from_ic_session(
        session, status="released", warning=_RELEASE_WARNING
    )


# ======================================================================
# plan（純計算，不連線、不入帳）
# ======================================================================
@router.post(
    "/plan",
    response_model=IcPlanResponse,
    summary="預覽：若建立此請求並下發會下哪些指令（無副作用，不入帳、不佔池）。",
    responses=_ERROR_RESPONSES,
)
async def preview_ic_plan(request: Request, body: IcAllocateRequest) -> IcPlanResponse:
    """預覽請求的 link 與啟用指令。使用 POST 只為攜帶 body，沒有任何副作用。

    VLAN 為示意值（自池下界連續編號），不檢查 port 衝突與 VLAN 耗盡。
    """
    settings, topology = _require_ic(request)
    _check_request_mode(body)
    allocator = build_ic_allocator(settings, topology)
    session = _preview_session(allocator, body)
    plan = _build_plan_deployer(settings).plan_enable(session)
    return IcPlanResponse.from_plan_result(
        plan, preview=IcSessionResponse.from_ic_session(session, status="preview")
    )


@router.get(
    "/sessions/{session_id}/plan",
    response_model=IcPlanResponse,
    summary="既有 session 的啟用（apply）或釋放（release）指令計畫。",
    responses=_ERROR_RESPONSES,
)
async def get_ic_session_plan(
    request: Request,
    session_id: str,
    action: Literal["apply", "release"] = "apply",
) -> IcPlanResponse:
    """回傳既有 session 的指令計畫（純計算，不連線）。"""
    settings, topology = _require_ic(request)
    allocator = build_ic_allocator(settings, topology)
    session = allocator.get_session(session_id)
    deployer = _build_plan_deployer(settings)
    plan = deployer.plan_release(session) if action == "release" else deployer.plan_enable(session)
    return IcPlanResponse.from_plan_result(plan)


@router.get(
    "/plan/park",
    response_model=IcPlanResponse,
    summary="park 靜態 baseline 指令計畫（可用 fanout / ports 縮小範圍）。",
    responses=_ERROR_RESPONSES,
)
async def get_ic_park_plan(
    request: Request,
    fanout: Optional[str] = None,
    ports: Optional[List[int]] = Query(None),
) -> IcPlanResponse:
    """回傳 park 計畫（純計算，不連線）；被 session 持有的 port 不列入。"""
    settings, topology = _require_ic(request)
    allocator = build_ic_allocator(settings, topology)
    plan = _build_plan_deployer(settings).plan_park(
        allocator.graph,
        [fanout] if fanout else None,
        ports,
        owned=allocator.owned_nodes(),
    )
    return IcPlanResponse.from_plan_result(plan)
