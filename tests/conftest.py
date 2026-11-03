"""pytest 共用 fixtures（設計 §11）。

提供範例與測試資料檔的實際路徑 fixture，供各測試模組共用，避免在測試中硬寫
相對路徑。路徑一律以本檔所在位置推導，確保不論從哪個工作目錄執行 pytest 都
能定位到專案的 examples/ 與 tests/data/。

歷史（HISTORY）：
  - 0.1.0  FEAT-002 初版，建立 examples_dir / two_tier_path /
           three_tier_path / invalid_path fixtures。
  - 0.4.0  Phase 3：新增 REST API 測試共用 fixtures（api_client TestClient、
           主執行緒 live-server 子行程 live_server / fullscale_live_server 與
           _LiveServer 握把）；供 test_api.py 與 test_api_errors.py 共用。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Tuple

import httpx
import pytest

# tests/conftest.py → 專案根為其父目錄的父目錄。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def examples_dir() -> Path:
    """回傳 examples/ 目錄的絕對路徑。"""
    return _PROJECT_ROOT / "examples"


@pytest.fixture
def two_tier_path(examples_dir: Path) -> Path:
    """回傳兩層範例 YAML 的絕對路徑。"""
    return examples_dir / "topology-2tier.yaml"


@pytest.fixture
def three_tier_path(examples_dir: Path) -> Path:
    """回傳三層範例 YAML 的絕對路徑。"""
    return examples_dir / "topology-3tier.yaml"


@pytest.fixture
def invalid_path() -> Path:
    """回傳故意錯誤的 YAML（tests/data/invalid-topology.yaml）的絕對路徑。"""
    return _PROJECT_ROOT / "tests" / "data" / "invalid-topology.yaml"


# ----------------------------------------------------------------------
# Phase 3 REST API 測試 fixtures（設計 §10；harness 機制說明見 test_api.py）
# ----------------------------------------------------------------------


@pytest.fixture
def api_client(three_tier_path: Path, tmp_path: Path):
    """以 3tier + tmp_path 設定建 app，回 (TestClient, state_path)（設計 §10.1）。

    僅供**唯讀端點**、請求驗證與 fail-fast 測試使用（寫入端點改用
    live_server，因 Phase 2 signal-based 檔鎖無法在 TestClient 的 worker
    thread 執行）。
    """
    from fastapi.testclient import TestClient

    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    state = tmp_path / "state" / "allocations.json"
    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(state),
        vlan_min=100,
        vlan_max=3000,
    )
    return TestClient(create_app(settings)), state


class _LiveServer:
    """已啟動的主執行緒 uvicorn live-server 握把（供寫入端點整合測試）。

    屬性：
      - base_url：伺服器根 URL（``http://127.0.0.1:<port>``）。
      - state_path：ledger 狀態檔的 ``Path``（供測試讀回驗持久化）。
      - client：已設定 base_url 的 ``httpx.Client``。
    """

    def __init__(self, base_url: str, state_path: Path, client: "httpx.Client") -> None:
        self.base_url = base_url
        self.state_path = state_path
        self.client = client


def _free_port() -> int:
    """取得一個目前可用的本機 TCP port（避免固定 port 衝突）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_live_server(
    topology_path: Path, tmp_path: Path
) -> Tuple[subprocess.Popen, _LiveServer]:
    """啟動主執行緒 uvicorn 子行程並等待就緒，回 (proc, _LiveServer)。

    以子行程執行 ``topo_manager.api.server.run()``（擁有自己的主執行緒事件
    迴圈），與 production 完全一致；signal-based 檔鎖因此可正常運作。
    """
    port = _free_port()
    state = tmp_path / "state" / "allocations.json"
    env = dict(os.environ)
    env["TOPO_MANAGER_TOPOLOGY"] = str(topology_path)
    env["TOPO_MANAGER_STATE"] = str(state)
    env["TOPO_MANAGER_HOST"] = "127.0.0.1"
    env["TOPO_MANAGER_PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "-c", "from topo_manager.api.server import run; run()"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = "http://127.0.0.1:{0}".format(port)
    client = httpx.Client(base_url=base_url, timeout=10.0)

    # 等待就緒：輪詢 /openapi.json 直到可連線（最多約 15 秒）。
    deadline = time.time() + 15.0
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            output = (
                proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            )
            raise RuntimeError("live-server 啟動失敗：\n{0}".format(output))
        try:
            response = client.get("/openapi.json")
            if response.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    if not ready:
        proc.terminate()
        raise RuntimeError("live-server 等待就緒逾時")

    return proc, _LiveServer(base_url, state, client)


def _stop_live_server(proc: subprocess.Popen, server: _LiveServer) -> None:
    """關閉 live-server 子行程與 httpx client（fixture teardown 共用）。"""
    server.client.close()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def live_server(three_tier_path: Path, tmp_path: Path):
    """3tier 主執行緒 live-server fixture（供寫入端點整合測試）。"""
    proc, server = _start_live_server(three_tier_path, tmp_path)
    try:
        yield server
    finally:
        _stop_live_server(proc, server)


@pytest.fixture
def fullscale_live_server(examples_dir: Path, tmp_path: Path):
    """fullscale 主執行緒 live-server fixture（AC-15 煙測用）。"""
    topology_path = examples_dir / "topology-fullscale.yaml"
    proc, server = _start_live_server(topology_path, tmp_path)
    try:
        yield server
    finally:
        _stop_live_server(proc, server)
