"""topo_manager 打流側設備下發 REST 的三個 endpoint（api/push_routes.py）。

MODULE NAME：
  topo_manager.api.push_routes

PURPOSE：
  以 ``APIRouter`` 定義會連線並寫入 fanout 的打流側端點，語意對應 ``cli.py``：

  - ``POST /sessions/{session_id}/apply``  ← ``apply --apply``
  - ``GET  /sessions/{session_id}/verify`` ← ``verify``（唯讀）
  - ``POST /fanouts/setup``                ← ``setup --apply``

NOTES：
  - router 層 dependencies 先後套用 ``require_device_push``（未啟用 404）與
    ``require_api_token``（401），早於 query／body 驗證、帳本與 SSH。
  - 所有 handler 為 ``async def`` 且不含 Pydantic body 參數：FastAPI 會在解析
    dependencies 之前先讀並驗證 body 參數，會使「壞 body＋壞 token」得到 422／400
    而非 401。``/fanouts/setup`` 因此在 handler 內手動解析 body。
  - 同步在請求內完成 SSH，期間事件迴圈被阻塞（見 README liveness 章節）。
  - session 以每請求新建的 allocator 讀帳本（非啟動單例快照），確保找得到
    server 啟動後才由其他行程寫入的 session，也不會對已釋放的 session 下發。
  - ``drivers.*`` 於函式內延遲 import。

HISTORY：
  - 0.1.0  設備下發 REST 初版（打流側三個端點）。
  - 0.2.0  FEAT-002 合併端點：新增 token 守護的
           ``POST /sessions/allocate-and-apply``（順序 LEDGER→DEVICE，設備失敗
           以 Allocator.release 回捲帳本）與
           ``POST /sessions/{id}/release-and-remove``（順序 DEVICE→LEDGER，設備
           失敗不清帳本）。兩者皆 async def、手動解析 body、沿用既有
           _build_deployer / _load_session_and_config / _ports_of 與 apply_session
           的 FanoutPushResult 形狀；帳本操作用 build_allocator。
  - 0.3.0  群組 REST（FEAT-002）：release-and-remove 於帳本釋放成功後呼叫
           app.state.group_registry.remove_member_any 解除群組歸屬（AC-9），
           與 delete_session 同語意。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends, Request

from topo_manager.api.auth import require_api_token, require_device_push
from topo_manager.api.deps import build_allocator
from topo_manager.api.push_common import (
    driver_factory_of,
    interface_port,
    load_config,
    require_credentials_for,
    translate_device_errors,
)
from topo_manager.api.push_schemas import (
    AllocateAndApplyRequest,
    AllocateAndApplyResponse,
    FanoutPushResult,
    ReadbackSummary,
    ReleaseAndRemoveResponse,
    SessionApplyResponse,
    SessionVerifyResponse,
    SetupFanoutResult,
    SetupPushResponse,
    SetupRequest,
)
from topo_manager.api.schemas import ErrorResponse, SessionResponse
from topo_manager.errors import (
    DevicePushRequestError,
    ReadBackMismatchError,
    SessionStoreError,
)

# 各端點共用的錯誤回應文件。
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "請求不合法（setup body、未知 fanout）"},
    401: {"model": ErrorResponse, "description": "X-API-Token 缺漏或錯誤"},
    404: {"model": ErrorResponse, "description": "下發未啟用，或 session 不存在"},
    409: {"model": ErrorResponse, "description": "read-back 與期望不符"},
    500: {"model": ErrorResponse, "description": "credentials 缺漏或服務配置錯誤"},
    502: {"model": ErrorResponse, "description": "連不上或認證失敗、設備拒絕指令"},
    503: {"model": ErrorResponse, "description": "帳本或拓樸暫時不可用"},
    504: {"model": ErrorResponse, "description": "SSH 逾時"},
}

router = APIRouter(
    tags=["device-push"],
    dependencies=[Depends(require_device_push), Depends(require_api_token)],
)


def _build_deployer(request: Request, config: object) -> object:
    """以注入或預設的 driver 工廠建 ``Deployer``（延遲 import drivers）。"""
    from topo_manager.drivers.deployer import Deployer

    return Deployer(driver_factory_of(request, interconnect=False), config)


def _load_session_and_config(request: Request, session_id: str):
    """取 session（每請求重讀帳本）、載入 credentials 並核對涵蓋；回 (session, config)。

    例外：
      - SessionNotFoundError：session 不存在（404）。
      - ConfigError：credentials 載入失敗或缺 fanout（500）。
    """
    settings = request.app.state.settings
    topology = request.app.state.topology
    session = build_allocator(settings).get_session(session_id)
    config = load_config(settings)
    require_credentials_for(session, config, {fanout.name for fanout in topology.fanouts})
    return session, config


def _ports_of(changes: List[object]) -> List[int]:
    """由變更的介面名解出 port 編號（排序去重；解不到者略過）。"""
    numbers = {interface_port(change.interface) for change in changes}
    return sorted(number for number in numbers if number is not None)


@router.post(
    "/sessions/{session_id}/apply",
    response_model=SessionApplyResponse,
    summary="把既有 session 下發到 fanout 並 read-back（對應 CLI apply --apply）。",
    responses=_ERROR_RESPONSES,
)
async def apply_session(request: Request, session_id: str) -> SessionApplyResponse:
    """同步下發 session 的 VLAN／tunnel 設定；成功代表每台有變更的 fanout 已通過 read-back。

    只列出有變更的 fanout；全部已滿足（冪等重送）時 ``fanouts`` 為空。
    """
    session, config = _load_session_and_config(request, session_id)
    deployer = _build_deployer(request, config)
    with translate_device_errors():
        plan = deployer.apply(session, apply=True)
    results = [
        FanoutPushResult(
            fanout=item.fanout,
            ok=True,
            commands=list(item.commands),
            ports=_ports_of(item.changes),
            readback=ReadbackSummary(verified=True, checked=len(item.changes), missing=[]),
        )
        for item in plan.fanouts
        if item.changes
    ]
    return SessionApplyResponse(
        session_id=plan.session_id, vlan=plan.vlan, status="applied", fanouts=results
    )


@router.get(
    "/sessions/{session_id}/verify",
    response_model=SessionVerifyResponse,
    summary="唯讀查核 session 是否已在 fanout 上達成（對應 CLI verify）。",
    responses=_ERROR_RESPONSES,
)
async def verify_session(request: Request, session_id: str) -> SessionVerifyResponse:
    """唯讀讀回並比對；不符仍回 HTTP 200 且 ``ok=false``（GET 回報現況）。"""
    session, config = _load_session_and_config(request, session_id)
    deployer = _build_deployer(request, config)
    with translate_device_errors():
        verify = deployer.verify(session)
        plan = deployer.plan(session)
    results: List[FanoutPushResult] = []
    for item in plan.fanouts:
        if not item.changes:
            continue
        missing = verify.missing_by_fanout.get(item.fanout, [])
        results.append(
            FanoutPushResult(
                fanout=item.fanout,
                ok=not missing,
                commands=[],
                ports=_ports_of(item.changes),
                readback=ReadbackSummary(
                    verified=not missing,
                    checked=len(item.changes),
                    missing=[
                        "{0} VLAN {1}".format(change.interface, change.vlan)
                        for change in missing
                    ],
                ),
            )
        )
    return SessionVerifyResponse(
        session_id=verify.session_id, ok=verify.ok, fanouts=results
    )


def _parse_setup_body(raw: bytes) -> SetupRequest:
    """手動解析 setup 的 body；空 body＝全部 fanout。

    例外：
      - DevicePushRequestError：非合法 JSON、不符 schema，或 ``fanouts`` 為空陣列。
    """
    if len(raw.strip()) == 0:
        return SetupRequest()
    try:
        body = SetupRequest.model_validate(json.loads(raw))
    except ValueError as exc:
        raise DevicePushRequestError("請求 body 不合法：{0}".format(exc)) from exc
    if body.fanouts is not None and len(body.fanouts) == 0:
        raise DevicePushRequestError("fanouts 不可為空陣列；要操作全部請省略 fanouts")
    return body


@router.post(
    "/fanouts/setup",
    response_model=SetupPushResponse,
    summary="全域 dot1q-tunnel setup 並讀回狀態（對應 CLI setup --apply）。",
    responses=_ERROR_RESPONSES,
    openapi_extra={
        "requestBody": {
            "required": False,
            "content": {"application/json": {"schema": SetupRequest.model_json_schema()}},
        }
    },
)
async def setup_fanouts(request: Request) -> SetupPushResponse:
    """對指定（或全部）fanout 下發全域 tunnel 總開關；讀回不是 Enabled 即 409。

    body 為選填的 ``{"fanouts": ["name", ...]}``，於 handler 內手動解析（見模組 NOTES）。
    """
    body = _parse_setup_body(await request.body())
    config = load_config(request.app.state.settings)
    available = sorted(config.fanouts)
    if body.fanouts is None:
        names = available
    else:
        names = list(dict.fromkeys(body.fanouts))
        unknown = [name for name in names if name not in config.fanouts]
        if unknown:
            raise DevicePushRequestError(
                "找不到 fanout：{0}（可用：{1}）".format(
                    ", ".join(unknown), ", ".join(available)
                )
            )
    deployer = _build_deployer(request, config)
    with translate_device_errors():
        results = deployer.setup_tunnel(names, apply=True)
    bad = [item for item in results if item.status is not True]
    if bad:
        lines = [
            "fanout {0}：{1}".format(
                item.fanout, "Disabled" if item.status is False else "未知"
            )
            for item in bad
        ]
        raise ReadBackMismatchError(
            "全域 dot1q-tunnel 讀回未啟用：\n  " + "\n  ".join(lines)
        )
    return SetupPushResponse(
        ok=True,
        fanouts=[
            SetupFanoutResult(
                fanout=item.fanout,
                ok=True,
                tunnel_status=True,
                commands=list(item.commands),
            )
            for item in results
        ],
    )


def _fanout_results_of(plan: object) -> List[FanoutPushResult]:
    """把 ``PlanResult`` 轉成逐 fanout 結果（僅列有變更者，與 apply_session 一致）。

    成功路徑下每個有變更的 fanout 都已通過 read-back，故 ``verified=True`` 且
    ``missing=[]``。
    """
    return [
        FanoutPushResult(
            fanout=item.fanout,
            ok=True,
            commands=list(item.commands),
            ports=_ports_of(item.changes),
            readback=ReadbackSummary(verified=True, checked=len(item.changes), missing=[]),
        )
        for item in plan.fanouts
        if item.changes
    ]


def _parse_allocate_body(raw: bytes) -> AllocateAndApplyRequest:
    """手動解析 allocate-and-apply 的 body（壞 body → DevicePushRequestError 400）。

    例外：
      - DevicePushRequestError：非合法 JSON 或不符 schema。
    """
    try:
        return AllocateAndApplyRequest.model_validate(json.loads(raw))
    except ValueError as exc:
        raise DevicePushRequestError("請求 body 不合法：{0}".format(exc)) from exc


@router.post(
    "/sessions/allocate-and-apply",
    response_model=AllocateAndApplyResponse,
    summary="分配一筆 session 並立即下發（先入帳本、再下發；設備失敗回捲帳本）。",
    responses=_ERROR_RESPONSES,
)
async def allocate_and_apply(request: Request) -> AllocateAndApplyResponse:
    """合併動作：Allocator.allocate → Deployer.apply（順序 LEDGER → DEVICE）。

    (1) 帳本分配：失敗（硬衝突／VLAN 耗盡／查無路徑／帳本 I/O）直接回結構化
    錯誤，無副作用可回捲。(2) 載 credentials、核對涵蓋、建 deployer、apply。
    (3) 成功回 200；(4) 設備失敗先 ``Allocator.release`` 回捲帳本，再原樣重拋
    原設備錯誤；若回捲本身失敗，另拋點名孤兒 session 與修復動作的結構化錯誤。
    """
    body = _parse_allocate_body(await request.body())
    settings = request.app.state.settings
    topology = request.app.state.topology

    # (1) LEDGER：分配（失敗無副作用，結構化錯誤原樣往外傳）。
    allocator = build_allocator(settings)
    session = allocator.allocate(body.generator_port, body.dut_port)

    # (2) DEVICE：載 credentials、核對涵蓋、建 deployer、apply。credentials 缺漏
    # 的 ConfigError 與設備失敗同屬「已入帳本但下發未成」，一併回捲。
    try:
        config = load_config(settings)
        require_credentials_for(
            session, config, {fanout.name for fanout in topology.fanouts}
        )
        deployer = _build_deployer(request, config)
        with translate_device_errors():
            plan = deployer.apply(session, apply=True)
    except Exception as device_exc:  # noqa: BLE001 — 下發失敗後回捲帳本再重拋。
        # (4) 設備失敗：回捲帳本（釋放剛建的 session），再原樣重拋原設備錯誤。
        try:
            build_allocator(settings).release(session.session_id)
        except Exception as rollback_exc:  # noqa: BLE001
            raise SessionStoreError(
                "下發失敗後帳本回捲亦失敗，session {0} 已成為孤兒；請執行 "
                "release-and-remove 或 CLI release 清理（原回捲錯誤：{1}）".format(
                    session.session_id, rollback_exc
                )
            ) from rollback_exc
        raise device_exc

    # (3) 成功：200，內嵌 session（status=allocated）與逐 fanout 結果。
    return AllocateAndApplyResponse(
        status="allocated-and-applied",
        session=SessionResponse.from_session(session, "allocated"),
        fanouts=_fanout_results_of(plan),
    )


@router.post(
    "/sessions/{session_id}/release-and-remove",
    response_model=ReleaseAndRemoveResponse,
    summary="連設備撤除一筆 session 並自帳本釋放（先撤設備、再清帳本）。",
    responses=_ERROR_RESPONSES,
)
async def release_and_remove(request: Request, session_id: str) -> ReleaseAndRemoveResponse:
    """合併動作：Deployer.remove → Allocator.release（順序 DEVICE → LEDGER）。

    (1) 取 session（查無 404）、載 credentials、建 deployer、remove。(2) 設備
    失敗（502／504／409）不清帳本、原樣重拋（session 留著，remove 回讀只檢查
    缺席，重試冪等安全）。(3) 成功才 ``Allocator.release``；若設備已拆後帳本
    釋放失敗，另拋點名仍在的 session 與「重試安全」的結構化錯誤。(4) 回 200。
    """
    settings = request.app.state.settings

    # (1) DEVICE：取 session（查無 404）、核對 credentials、撤除。
    session, config = _load_session_and_config(request, session_id)
    deployer = _build_deployer(request, config)
    with translate_device_errors():
        plan = deployer.remove(session, apply=True)

    # (3) LEDGER：設備已拆，才自帳本釋放；釋放失敗點名 session 並說明重試安全。
    try:
        build_allocator(settings).release(session_id)
    except Exception as rollback_exc:  # noqa: BLE001
        raise SessionStoreError(
            "設備已撤除但帳本釋放失敗，session {0} 仍在帳本中；重試本端點安全"
            "（設備已無設定，remove 回讀會直接通過）。原錯誤：{1}".format(
                session_id, rollback_exc
            )
        ) from rollback_exc

    # 帳本釋放成功後解除群組歸屬（AC-9，FEAT-002）：與 delete_session 同語意，
    # 用啟動單例 group_registry.remove_member_any（鎖內交易；不屬任何群為 no-op）。
    request.app.state.group_registry.remove_member_any(session_id)

    # (4) 成功：200，回已釋放的 session_id 與逐 fanout 撤除結果。
    return ReleaseAndRemoveResponse(
        status="released-and-removed",
        session_id=session_id,
        fanouts=_fanout_results_of(plan),
    )
