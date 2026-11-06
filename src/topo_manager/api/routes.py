"""topo_manager REST API 的五個 endpoint 與可用資源重算輔助（Phase 3，設計 §5）。

以 ``APIRouter`` 定義五個 endpoint：

  - ``GET /topology``：回啟動單例 topology（``response_model`` 為既有
    ``topo_manager.models.Topology``，§5.1）。
  - ``GET /resources/available``：每請求重建 allocator 取最新快照，回
    ``AvailableResourcesResponse``（§5.2）。
  - ``POST /sessions``：共用單例 allocator.allocate，201 + status
    ``"allocated"``（§5.3）。
  - ``DELETE /sessions/{session_id}``：共用單例 allocator.release，200 +
    status ``"released"``（§5.4）。
  - ``GET /sessions``：每請求重建 allocator.list_sessions，回
    ``SessionListResponse``（每筆 status ``"active"``，§5.5）。

app 狀態（settings、shared allocator、topology）以 ``request.app.state`` 取得
（於 ``app.create_app`` 啟動期建立）。寫路徑共用啟動單例 allocator（正確性由
Phase 2 ``store.locked()`` + 鎖內 reload 保證）；讀路徑每請求以 ``build_allocator``
重建以取最新快照（設計 §4.2）。

實作註記（對設計的必要微調，不改 Phase 2）：五個 endpoint 一律宣告為
``async def``。理由：Phase 2 ``store.locked()`` 以 ``signal.SIGALRM`` 實作取鎖
逾時，而 ``signal.signal`` 僅能在主執行緒呼叫；FastAPI 對同步（``def``）
handler 會丟到 threadpool 工作執行緒執行，導致鎖內 ``signal`` 拋
「signal only works in main thread」。``async def`` handler 由 ASGI 事件迴圈
直接在主執行緒執行，可正確取鎖。此為 API 層內的薄層調整（不動 Phase 1/2 的
``.py``、不改 API 契約與測試斷言），符合設計 §9「局部可逆調整」的精神。

可用資源判定只讀公開 ``Session``（``physical_ports`` 扣除
``trunk_segments[].a_node/b_node`` 得 access port），不碰 Allocator 私有屬性
（設計 §5.2 裁決 (a)）。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立五個 endpoint 與 compute_available 輔助
           （設計 §5）。
  - 0.7.0  群組 REST（FEAT-002）：post_session 於 allocate 後依 body.group 以
           單例 group_registry.add_member(auto_create=True) 歸群，並以
           group_of 回填回應的 group；list_sessions 每筆以 build_group_registry
           的 group_of 回填 group；delete_session 於 release 後以
           remove_member_any 解除群組歸屬（AC-9）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from typing import Dict, List, Set

from fastapi import APIRouter, Depends, Request, status

from topo_manager.allocator import Allocator, Session
from topo_manager.api.auth import require_ledger_write
from topo_manager.api.deps import Settings, build_allocator, build_group_registry
from topo_manager.api.schemas import (
    AvailableResourcesResponse,
    AllocateRequest,
    CapabilitiesResponse,
    DutPortOut,
    GeneratorPortOut,
    SessionListResponse,
    SessionResponse,
)
from topo_manager.models import Topology

router = APIRouter()


def _occupied_access_ports(sessions: List[Session]) -> Dict[str, str]:
    """由公開 ``Session`` 清單重算「access port → owner session_id」（設計 §5.2）。

    access port = ``physical_ports`` 扣除所有 trunk 段的兩端節點
    （``trunk_segments[].a_node/b_node``）。僅用公開欄位，不碰 Allocator 私有
    屬性（設計 §5.2 裁決 (a)）。

    參數：
      - sessions：``allocator.list_sessions()`` 回傳的公開 session 清單。

    回傳：
      - access port 節點 id → 擁有者 session_id 的對照 dict。
    """
    occupied: Dict[str, str] = {}
    for session in sessions:
        trunk_ends: Set[str] = set()
        for segment in session.trunk_segments:
            trunk_ends.add(segment.a_node)
            trunk_ends.add(segment.b_node)
        for node in session.physical_ports:
            if node not in trunk_ends:
                occupied[node] = session.session_id
    return occupied


def compute_available(
    topology: Topology, allocator: Allocator
) -> AvailableResourcesResponse:
    """重算可用 generator / DUT port 清單與被佔用 access port（設計 §5.2）。

    由 ``topology`` 宣告列出所有 generator port 節點 id（``"<gen>:<port>"``）與
    DUT tg_port 節點 id（``"<dut>:<tg_port>"``）；以 ``allocator.list_sessions()``
    的公開 ``Session`` 重算被佔用 access port 集合，端點不在佔用集合即為可用。

    參數：
      - topology：啟動單例 ``Topology``（宣告來源）。
      - allocator：取最新快照的 ``Allocator``（讀路徑每請求重建）。

    回傳：
      - ``AvailableResourcesResponse``（可用 gen/dut 清單 + occupied 對照）。
    """
    occupied = _occupied_access_ports(allocator.list_sessions())
    occupied_nodes = set(occupied.keys())

    available_generator_ports: List[GeneratorPortOut] = []
    for generator in topology.generators:
        for port in generator.ports:
            node = "{0}:{1}".format(generator.name, port)
            if node not in occupied_nodes:
                available_generator_ports.append(
                    GeneratorPortOut(node=node, generator=generator.name, port=port)
                )

    available_dut_ports: List[DutPortOut] = []
    for rack in topology.racks:
        for dut in rack.duts:
            for port in dut.tg_ports:
                node = "{0}:{1}".format(dut.name, port)
                if node not in occupied_nodes:
                    available_dut_ports.append(
                        DutPortOut(node=node, dut=dut.name, port=port)
                    )

    return AvailableResourcesResponse(
        available_generator_ports=available_generator_ports,
        available_dut_ports=available_dut_ports,
        occupied_ports=occupied,
    )


@router.get(
    "/topology",
    response_model=Topology,
    summary="取得完整拓樸（供視覺化重建）。",
)
async def get_topology(request: Request) -> Topology:
    """回傳啟動時載入的完整拓樸（設計 §5.1、AC-1）。

    純讀啟動單例 ``Topology``（啟動後不變、不碰 ledger、不建圖）。
    """
    return request.app.state.topology


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    summary="回報寫入能力（設備下發開關、是否需 token、是否已解鎖），供 Web UI 決策。",
)
async def get_capabilities(request: Request) -> CapabilitiesResponse:
    """回報 server 的寫入能力，供 Web UI 決定走哪組寫入端點與鎖狀態。

    純讀 ``app.state.settings``，不碰 ledger、不連設備：

    - device_push：``settings.enable_device_push`` 是否為真。
    - write_requires_token：恆為 true（純帳本寫入亦以 token 閘控）。
    - writes_unlocked：是否已設定 ``api_token``（未設即唯讀鎖住）。
    """
    settings: Settings = request.app.state.settings
    has_token = settings.api_token is not None and settings.api_token != ""
    return CapabilitiesResponse(
        device_push=settings.enable_device_push is True,
        write_requires_token=True,
        writes_unlocked=has_token,
    )


@router.get(
    "/resources/available",
    response_model=AvailableResourcesResponse,
    summary="取得可用 generator / DUT port 清單（排除已佔用）。",
)
async def get_available_resources(request: Request) -> AvailableResourcesResponse:
    """回傳排除已佔用資源後的可用 generator / DUT port（設計 §5.2、AC-2）。

    每請求以 factory 重建 allocator 取最新快照（讀路徑，設計 §4.2）。
    """
    settings: Settings = request.app.state.settings
    topology: Topology = request.app.state.topology
    allocator = build_allocator(settings)
    return compute_available(topology, allocator)


@router.post(
    "/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="分配一筆 session（含 VLAN 與路徑）。",
    dependencies=[Depends(require_ledger_write)],
)
async def post_session(request: Request, body: AllocateRequest) -> SessionResponse:
    """分配一筆新 session 並回其摘要（設計 §5.3、AC-3）。

    呼叫共用啟動單例 allocator.allocate（正確性由 Phase 2 鎖內交易保證）。
    若 body 帶 group，於分配後以單例 group_registry 歸群（不存在則自動建立，
    FEAT-002），並以 group_of 回填回應的 group 欄位。
    """
    allocator: Allocator = request.app.state.allocator
    session = allocator.allocate(body.generator_port, body.dut_port)
    if body.group is not None:
        request.app.state.group_registry.add_member(
            body.group, session.session_id, auto_create=True
        )
    group = request.app.state.group_registry.group_of(session.session_id)
    return SessionResponse.from_session(session, status="allocated", group=group)


@router.delete(
    "/sessions/{session_id}",
    response_model=SessionResponse,
    summary="釋放一筆既有 session（回被釋放 session 摘要）。",
    dependencies=[Depends(require_ledger_write)],
)
async def delete_session(request: Request, session_id: str) -> SessionResponse:
    """釋放一筆既有 session 並回其摘要（設計 §5.4、AC-6）。

    呼叫共用啟動單例 allocator.release；回 200 + status ``"released"``。
    釋放後以單例 group_registry.remove_member_any 解除其群組歸屬（AC-9，
    FEAT-002）。
    """
    allocator: Allocator = request.app.state.allocator
    session = allocator.release(session_id)
    request.app.state.group_registry.remove_member_any(session_id)
    return SessionResponse.from_session(session, status="released")


@router.get(
    "/sessions",
    response_model=SessionListResponse,
    summary="列出目前所有 active session。",
)
async def list_sessions(request: Request) -> SessionListResponse:
    """列出目前所有 active session（設計 §5.5、AC-5）。

    每請求以 factory 重建 allocator 取最新快照（讀路徑，設計 §4.2）；清單依
    session_id 升冪，每筆 status 固定 ``"active"``。
    """
    settings: Settings = request.app.state.settings
    allocator = build_allocator(settings)
    sessions = allocator.list_sessions()
    # 讀路徑每請求以 factory 重建 group registry 取最新群組快照（FEAT-002）。
    group_registry = build_group_registry(settings)
    responses = [
        SessionResponse.from_session(
            session,
            status="active",
            group=group_registry.group_of(session.session_id),
        )
        for session in sessions
    ]
    return SessionListResponse(sessions=responses, count=len(responses))
