"""topo_manager 互聯側設備下發 REST 的四個 endpoint（api/ic_push_routes.py）。

MODULE NAME：
  topo_manager.api.ic_push_routes

PURPOSE：
  以 ``APIRouter(prefix="/interconnect")`` 定義會連線並寫入互聯 fanout 的端點，
  語意對應 ``interconnect/cli.py``：

  - ``POST /interconnect/sessions/{session_id}/apply``          ← ``apply --apply``
  - ``GET  /interconnect/sessions/{session_id}/verify``         ← ``verify --session``
  - ``POST /interconnect/park``                                 ← ``park --apply``
  - ``POST /interconnect/sessions/{session_id}/release-device`` ← ``release --apply``

NOTES：
  - 既有 ``DELETE /interconnect/sessions/{id}`` 仍是 ledger-only，本檔不改動它。
  - 閘門順序：``require_device_push`` → ``require_api_token`` → 無互聯區段 404 →
    參數驗證 → 帳本 → SSH。所有 handler 為 ``async def``，不含 body 參數。
  - 設備鎖（``device_locked()``，SIGALRM）與重讀帳本由 ``InterconnectDeployer``
    負責，REST 不重複實作；handler 必須在主執行緒執行，故不可進 threadpool。
  - verify 不符仍是 HTTP 200 且 ``ok=false``；``problems`` 為扁平字串清單。
  - ``drivers.*`` 於函式內延遲 import。

HISTORY：
  - 0.1.0  設備下發 REST 初版（互聯側四個端點）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request

from topo_manager.api.auth import require_api_token, require_device_push
from topo_manager.api.ic_deps import build_ic_allocator
from topo_manager.api.ic_routes import _require_ic
from topo_manager.api.push_common import (
    driver_factory_of,
    load_config,
    ports_by_fanout,
    translate_device_errors,
)
from topo_manager.api.push_schemas import (
    FanoutPushResult,
    IcParkPushResponse,
    IcPushResponse,
    IcVerifyPushResponse,
    ReadbackSummary,
    SkippedPort,
)
from topo_manager.api.schemas import ErrorResponse

# 各端點共用的錯誤回應文件。
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "請求不合法或 session 狀態不允許"},
    401: {"model": ErrorResponse, "description": "X-API-Token 缺漏或錯誤"},
    404: {
        "model": ErrorResponse,
        "description": "下發未啟用、session 不存在，或拓樸沒有互聯區段",
    },
    409: {"model": ErrorResponse, "description": "read-back 與期望不符"},
    500: {"model": ErrorResponse, "description": "credentials 缺漏或服務配置錯誤"},
    502: {"model": ErrorResponse, "description": "連不上或認證失敗、設備拒絕指令"},
    503: {"model": ErrorResponse, "description": "互聯帳本 I/O 或取鎖故障"},
    504: {"model": ErrorResponse, "description": "SSH 逾時"},
}

router = APIRouter(
    prefix="/interconnect",
    tags=["interconnect-device-push"],
    dependencies=[Depends(require_device_push), Depends(require_api_token)],
)


def _build_deployer(request: Request, settings: object, allocator: object, config: object):
    """建互聯 deployer（store 與 allocator 指向同一帳本；延遲 import drivers）。"""
    from topo_manager.drivers.ic_deployer import InterconnectDeployer
    from topo_manager.interconnect.store import IcSessionStore

    return InterconnectDeployer(
        driver_factory_of(request, interconnect=True),
        config,
        store=IcSessionStore(settings.ic_state_path),
        allocator=allocator,
        vlan_min=settings.ic_vlan_min,
        vlan_max=settings.ic_vlan_max,
        force_access_mode=True,
    )


def _push_results(plan: object, session: object) -> List[FanoutPushResult]:
    """把 ``IcPlanResult.fanouts`` 轉成逐 fanout 結果（port 由 session links 解析）。"""
    ports = ports_by_fanout(session)
    results: List[FanoutPushResult] = []
    for item in plan.fanouts:
        numbers = ports.get(item.fanout, [])
        results.append(
            FanoutPushResult(
                fanout=item.fanout,
                ok=True,
                commands=list(item.commands),
                ports=numbers,
                readback=ReadbackSummary(verified=True, checked=len(numbers), missing=[]),
            )
        )
    return results


def _run_session_action(request: Request, session_id: str, action: str) -> IcPushResponse:
    """apply／release 共用流程：取 session → 建 deployer → 設備動作 → 轉回應。"""
    settings, topology = _require_ic(request)
    allocator = build_ic_allocator(settings, topology)
    session = allocator.get_session(session_id)
    deployer = _build_deployer(request, settings, allocator, load_config(settings))
    with translate_device_errors():
        if action == "apply":
            plan = deployer.apply(session, apply=True)
        else:
            plan = deployer.release(session, apply=True)
    return IcPushResponse(
        session_id=session.session_id,
        action=action,
        vlans=list(plan.vlans),
        fanouts=_push_results(plan, session),
        notes=list(plan.notes),
    )


@router.post(
    "/sessions/{session_id}/apply",
    response_model=IcPushResponse,
    summary="啟用互聯 session 並 read-back（對應 CLI interconnect apply --apply）。",
    responses=_ERROR_RESPONSES,
)
async def apply_ic_session(request: Request, session_id: str) -> IcPushResponse:
    """同步啟用 session（兩階段：設定驗證通過後才 no shutdown）。"""
    return _run_session_action(request, session_id, "apply")


@router.post(
    "/sessions/{session_id}/release-device",
    response_model=IcPushResponse,
    summary="連設備釋放互聯 session（對應 CLI interconnect release --apply）。",
    responses=_ERROR_RESPONSES,
)
async def release_ic_session_device(request: Request, session_id: str) -> IcPushResponse:
    """兩階段釋放：port 退回 parking、清 VLAN、讀回確認後才從帳本移除。

    中途失敗時 session 停在 releasing，重跑同一端點即可。
    """
    return _run_session_action(request, session_id, "release")


@router.get(
    "/sessions/{session_id}/verify",
    response_model=IcVerifyPushResponse,
    summary="唯讀查核互聯 session 的設備狀態（對應 CLI interconnect verify --session）。",
    responses=_ERROR_RESPONSES,
)
async def verify_ic_session(request: Request, session_id: str) -> IcVerifyPushResponse:
    """唯讀查核；不符仍回 HTTP 200 且 ``ok=false``，``problems`` 為扁平字串清單。"""
    settings, topology = _require_ic(request)
    allocator = build_ic_allocator(settings, topology)
    session = allocator.get_session(session_id)
    deployer = _build_deployer(request, settings, allocator, load_config(settings))
    with translate_device_errors():
        result = deployer.verify_session(session)
    return IcVerifyPushResponse(
        target=result.target,
        ok=result.ok,
        fanouts=sorted(ports_by_fanout(session)),
        problems=list(result.problems),
    )


@router.post(
    "/park",
    response_model=IcParkPushResponse,
    summary="把互聯 port 還原為 parking（對應 CLI interconnect park --apply）。",
    responses=_ERROR_RESPONSES,
)
async def park_ic_ports(
    request: Request,
    fanout: Optional[str] = None,
    ports: Optional[List[int]] = Query(None),
) -> IcParkPushResponse:
    """park 互聯區段 port；被 session 持有的 port 跳過並列在 ``skipped``。"""
    settings, topology = _require_ic(request)
    allocator = build_ic_allocator(settings, topology)
    deployer = _build_deployer(request, settings, allocator, load_config(settings))
    with translate_device_errors():
        result = deployer.park(
            allocator.graph,
            None,
            fanouts=[fanout] if fanout else None,
            ports=ports,
            apply=True,
        )
    results = [
        FanoutPushResult(
            fanout=item.fanout,
            ok=True,
            commands=list(item.commands),
            ports=list(result.parked.get(item.fanout, [])),
            readback=ReadbackSummary(
                verified=True,
                checked=len(result.parked.get(item.fanout, [])),
                missing=[],
            ),
        )
        for item in result.fanouts
    ]
    return IcParkPushResponse(
        applied=result.applied,
        fanouts=results,
        parked={name: list(numbers) for name, numbers in result.parked.items()},
        skipped=[
            SkippedPort(fanout=name, port=number, session_id=owner)
            for name, number, owner in result.skipped
        ],
        warnings=list(result.warnings),
    )
