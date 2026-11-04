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
  - 0.4.1  Phase 3 審查：新增 faulty_write_live_server（寫路徑注入
           RuntimeError，Issue 3）fixture 與 _start_faulty_write_live_server
           輔助。

  - 0.5.0  互聯 REST：新增 ic_small_path / ic_api_client /
           ic_no_section_api_client（TestClient）與 ic_live_server /
           ic_no_section_live_server（主執行緒子行程 live-server，
           _start_ic_live_server 帶互聯帳本與 VLAN 池環境變數）。
  - 0.6.0  設備下發 REST：新增 write_fake_credentials（runtime 產生 tmp
           credentials，host 為合成的 fake-host-N，絕非真實 IP）、
           push_api_client（啟用設備下發的 TestClient，供請求驗證測試）與
           _start_push_live_server / push_live_server / ic_push_live_server
           （主執行緒 live-server，額外帶 TOPO_MANAGER_ENABLE_DEVICE_PUSH /
           TOPO_MANAGER_API_TOKEN / TOPO_MANAGER_CREDENTIALS，並以
           connection_factory 注入 FakeEcSwitch 讓下發端點在子行程內可離線
           驗證）。
Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple

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


# 供 faulty-write live-server 的子行程進入點：建 app（啟動期 fail-fast 合法）、
# 把啟動單例 allocator 的 allocate 換成注入非 TopologyError 的 RuntimeError，
# 以 uvicorn 在子行程主執行緒跑真 HTTP（Issue 3：寫路徑非預期例外→結構化 500、
# 鎖乾淨釋放）。以模組字串經 `python -c` 執行，避免在 fixture 行內塞大段腳本。
_FAULTY_WRITE_SERVER_SCRIPT = (
    "import os\n"
    "import uvicorn\n"
    "from topo_manager.api import create_app\n"
    "from topo_manager.api.deps import Settings\n"
    "settings = Settings(\n"
    "    topology_path=os.environ['TOPO_MANAGER_TOPOLOGY'],\n"
    "    state_path=os.environ['TOPO_MANAGER_STATE'],\n"
    "    vlan_min=100,\n"
    "    vlan_max=3000,\n"
    ")\n"
    "app = create_app(settings)\n"
    "\n"
    "\n"
    "def _boom(*_args, **_kwargs):\n"
    "    raise RuntimeError('注入的非預期寫路徑例外')\n"
    "\n"
    "\n"
    "# 只換 allocate（POST 寫路徑）：驗非 TopologyError 例外在真 server 下的行為。\n"
    "app.state.allocator.allocate = _boom\n"
    "uvicorn.run(\n"
    "    app,\n"
    "    host=os.environ['TOPO_MANAGER_HOST'],\n"
    "    port=int(os.environ['TOPO_MANAGER_PORT']),\n"
    ")\n"
)


def _start_faulty_write_live_server(
    topology_path: Path, tmp_path: Path
) -> Tuple[subprocess.Popen, _LiveServer]:
    """啟動「寫路徑注入 RuntimeError」的主執行緒 live-server（Issue 3）。

    與 ``_start_live_server`` 同樣以子行程擁有自己的主執行緒（signal 檔鎖可用），
    差別在進入點為 ``_FAULTY_WRITE_SERVER_SCRIPT``（把單例 allocate 換成拋
    RuntimeError）。讀端點（如 /openapi.json、/sessions）照常，供就緒輪詢與
    「鎖乾淨釋放」的後續驗證。
    """
    port = _free_port()
    state = tmp_path / "state" / "allocations.json"
    env = dict(os.environ)
    env["TOPO_MANAGER_TOPOLOGY"] = str(topology_path)
    env["TOPO_MANAGER_STATE"] = str(state)
    env["TOPO_MANAGER_HOST"] = "127.0.0.1"
    env["TOPO_MANAGER_PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "-c", _FAULTY_WRITE_SERVER_SCRIPT],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = "http://127.0.0.1:{0}".format(port)
    client = httpx.Client(base_url=base_url, timeout=10.0)

    deadline = time.time() + 15.0
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            output = (
                proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            )
            raise RuntimeError("faulty live-server 啟動失敗：\n{0}".format(output))
        try:
            response = client.get("/openapi.json")
            if response.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    if not ready:
        proc.terminate()
        raise RuntimeError("faulty live-server 等待就緒逾時")

    return proc, _LiveServer(base_url, state, client)


@pytest.fixture
def faulty_write_live_server(three_tier_path: Path, tmp_path: Path):
    """寫路徑注入 RuntimeError 的 3tier 主執行緒 live-server（Issue 3）。"""
    proc, server = _start_faulty_write_live_server(three_tier_path, tmp_path)
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

# ----------------------------------------------------------------------
# 互聯 REST 測試 fixtures（不連任何設備；狀態檔一律在 tmp_path）
# ----------------------------------------------------------------------
@pytest.fixture
def ic_small_path(examples_dir: Path) -> Path:
    """回傳互聯小規模範例拓樸（4 組 × 4 DUT）的絕對路徑。"""
    return examples_dir / "topology-interconnect-small.yaml"


@pytest.fixture
def ic_api_client(ic_small_path: Path, tmp_path: Path):
    """以互聯小拓樸建 app，回 (TestClient, 互聯帳本 Path)。

    僅供讀端點、plan、請求驗證與可 monkeypatch 的錯誤路徑使用（寫入端點的
    SIGALRM 取鎖需主執行緒，改用 ic_live_server）。
    """
    from fastapi.testclient import TestClient

    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    ic_state = tmp_path / "state" / "interconnect.json"
    settings = Settings(
        topology_path=str(ic_small_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        ic_state_path=str(ic_state),
    )
    return TestClient(create_app(settings)), ic_state


@pytest.fixture
def ic_no_section_api_client(three_tier_path: Path, tmp_path: Path):
    """以沒有 interconnect 區段的 3tier 拓樸建 app，回 TestClient。"""
    from fastapi.testclient import TestClient

    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        ic_state_path=str(tmp_path / "state" / "interconnect.json"),
    )
    return TestClient(create_app(settings))


def _start_ic_live_server(
    topology_path: Path,
    tmp_path: Path,
    ic_state: Path = None,
    extra_env: dict = None,
) -> Tuple[subprocess.Popen, _LiveServer]:
    """啟動帶互聯帳本環境變數的主執行緒 uvicorn 子行程，回 (proc, _LiveServer)。

    骨架同 ``_start_live_server``（不改動既有函式）；額外設定
    ``TOPO_MANAGER_IC_STATE``（預設 ``tmp_path/state/interconnect.json``）與
    ``extra_env``。回傳握把的 ``ic_state_path`` 屬性指向互聯帳本。
    """
    port = _free_port()
    state = tmp_path / "state" / "allocations.json"
    if ic_state is None:
        ic_state = tmp_path / "state" / "interconnect.json"
    env = dict(os.environ)
    env["TOPO_MANAGER_TOPOLOGY"] = str(topology_path)
    env["TOPO_MANAGER_STATE"] = str(state)
    env["TOPO_MANAGER_IC_STATE"] = str(ic_state)
    env["TOPO_MANAGER_HOST"] = "127.0.0.1"
    env["TOPO_MANAGER_PORT"] = str(port)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, "-c", "from topo_manager.api.server import run; run()"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = "http://127.0.0.1:{0}".format(port)
    client = httpx.Client(base_url=base_url, timeout=10.0)
    deadline = time.time() + 15.0
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            output = (
                proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            )
            raise RuntimeError("ic live-server 啟動失敗：\n{0}".format(output))
        try:
            response = client.get("/openapi.json")
            if response.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    if not ready:
        proc.terminate()
        raise RuntimeError("ic live-server 等待就緒逾時")
    server = _LiveServer(base_url, state, client)
    server.ic_state_path = ic_state
    server.topology_path = topology_path
    return proc, server


@pytest.fixture
def ic_live_server(ic_small_path: Path, tmp_path: Path):
    """互聯小拓樸的主執行緒 live-server fixture（互聯寫入端點整合測試）。"""
    proc, server = _start_ic_live_server(ic_small_path, tmp_path)
    try:
        yield server
    finally:
        _stop_live_server(proc, server)


@pytest.fixture
def ic_no_section_live_server(three_tier_path: Path, tmp_path: Path):
    """沒有互聯區段的 3tier 主執行緒 live-server fixture。"""
    proc, server = _start_ic_live_server(three_tier_path, tmp_path)
    try:
        yield server
    finally:
        _stop_live_server(proc, server)


# ----------------------------------------------------------------------
# 設備下發 REST 測試 fixtures（絕不連真實設備；host 一律是合成字串）
# ----------------------------------------------------------------------


def write_fake_credentials(tmp_path: Path, fanout_names: List[str]) -> Path:
    """寫 runtime 產生的 tmp credentials（host 為合成 ``fake-host-N``）。

    host／username／password 的值在執行時組字串，不含任何真實 IP 或密碼
    字面（絕不使用真實設備位址）；檔案只存於 ``tmp_path``，不入版控。
    """
    import json as _json

    fanouts = {
        name: {
            "host": "fake-host-{0}".format(index),
            "username": "fake-user-{0}".format(index),
            "password": "fake-pass-{0}".format(index),
        }
        for index, name in enumerate(fanout_names)
    }
    path = tmp_path / "fake-credentials.json"
    path.write_text(_json.dumps({"fanouts": fanouts}), encoding="utf-8")
    return path


@pytest.fixture
def push_api_client(three_tier_path: Path, tmp_path: Path):
    """啟用設備下發的打流 TestClient（僅供請求驗證／401／404 測試，不連線）。

    回 (TestClient, state_path, token)；fanout credentials 指向一個不存在的
    檔案（絕不可能被成功讀取而連線），用來斷言 401 發生在 credentials 載入
    之前。
    """
    from fastapi.testclient import TestClient

    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    state = tmp_path / "state" / "allocations.json"
    token = "test-token-xyz"
    settings = Settings(
        topology_path=str(three_tier_path),
        state_path=str(state),
        enable_device_push=True,
        api_token=token,
        credentials_path=str(tmp_path / "does-not-exist.json"),
    )
    return TestClient(create_app(settings)), state, token


@pytest.fixture
def ic_push_api_client(ic_small_path: Path, tmp_path: Path):
    """啟用設備下發的互聯 TestClient（僅供請求驗證測試，不連線）。"""
    from fastapi.testclient import TestClient

    from topo_manager.api import create_app
    from topo_manager.api.deps import Settings

    token = "test-token-xyz"
    settings = Settings(
        topology_path=str(ic_small_path),
        state_path=str(tmp_path / "state" / "allocations.json"),
        ic_state_path=str(tmp_path / "state" / "interconnect.json"),
        enable_device_push=True,
        api_token=token,
        credentials_path=str(tmp_path / "does-not-exist.json"),
    )
    return TestClient(create_app(settings)), token


# 子行程進入點：啟用設備下發、以假 credentials 與 FakeEcSwitch connection_factory
# 注入打流／互聯 driver（永不連真實設備），於主執行緒跑真 HTTP（SIGALRM 可用）。
_PUSH_SERVER_SCRIPT = (
    "import json, os\n"
    "import uvicorn\n"
    "from topo_manager.api import create_app\n"
    "from topo_manager.api.deps import Settings\n"
    "from ic_fakes import FakeSwitchFarm\n"
    "settings = Settings(\n"
    "    topology_path=os.environ['TOPO_MANAGER_TOPOLOGY'],\n"
    "    state_path=os.environ['TOPO_MANAGER_STATE'],\n"
    "    ic_state_path=os.environ.get('TOPO_MANAGER_IC_STATE'),\n"
    "    enable_device_push=True,\n"
    "    api_token=os.environ['TOPO_MANAGER_API_TOKEN'],\n"
    "    credentials_path=os.environ['TOPO_MANAGER_CREDENTIALS'],\n"
    ")\n"
    "mode = os.environ.get('PUSH_FAKE_MODE', 'ok')\n"
    "traffic_farm = FakeSwitchFarm('traffic', mode)\n"
    "ic_farm = FakeSwitchFarm('ic', mode)\n"
    "\n"
    "\n"
    "def _driver_factory(name, entry):\n"
    "    from topo_manager.drivers.ec_ssh import EcSshDriver\n"
    "    return EcSshDriver(\n"
    "        host=entry.host, username=entry.username, password=entry.password,\n"
    "        device_type=entry.device_type, interface_template=entry.interface_template,\n"
    "        connection_factory=traffic_farm.factory,\n"
    "    )\n"
    "\n"
    "\n"
    "def _ic_driver_factory(name, entry):\n"
    "    from topo_manager.drivers.ec_ssh_ic import EcSshInterconnectDriver\n"
    "    return EcSshInterconnectDriver(\n"
    "        host=entry.host, username=entry.username, password=entry.password,\n"
    "        device_type=entry.device_type, interface_template=entry.interface_template,\n"
    "        connection_factory=ic_farm.factory, force_access_mode=True,\n"
    "    )\n"
    "\n"
    "\n"
    "app = create_app(\n"
    "    settings, push_driver_factory=_driver_factory,\n"
    "    ic_push_driver_factory=_ic_driver_factory,\n"
    ")\n"
    "uvicorn.run(\n"
    "    app,\n"
    "    host=os.environ['TOPO_MANAGER_HOST'],\n"
    "    port=int(os.environ['TOPO_MANAGER_PORT']),\n"
    ")\n"
)


def _start_push_live_server(
    topology_path: Path,
    tmp_path: Path,
    fanout_names: List[str],
    mode: str = "ok",
) -> Tuple[subprocess.Popen, _LiveServer]:
    """啟動啟用設備下發、以 FakeSwitchFarm 注入假設備的主執行緒 live-server。

    ``mode``：ok | auth | timeout | readtimeout | drop（見 ic_fakes.FakeSwitchFarm）。
    """
    port = _free_port()
    state = tmp_path / "state" / "allocations.json"
    ic_state = tmp_path / "state" / "interconnect.json"
    credentials = write_fake_credentials(tmp_path, fanout_names)
    env = dict(os.environ)
    env["TOPO_MANAGER_TOPOLOGY"] = str(topology_path)
    env["TOPO_MANAGER_STATE"] = str(state)
    env["TOPO_MANAGER_IC_STATE"] = str(ic_state)
    env["TOPO_MANAGER_HOST"] = "127.0.0.1"
    env["TOPO_MANAGER_PORT"] = str(port)
    env["TOPO_MANAGER_ENABLE_DEVICE_PUSH"] = "1"
    env["TOPO_MANAGER_API_TOKEN"] = "test-token-xyz"
    env["TOPO_MANAGER_CREDENTIALS"] = str(credentials)
    env["PUSH_FAKE_MODE"] = mode
    # 讓子行程能 `from ic_fakes import FakeEcSwitch`（tests/ 目錄本身）。
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get(
        "PYTHONPATH", ""
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", _PUSH_SERVER_SCRIPT],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = "http://127.0.0.1:{0}".format(port)
    client = httpx.Client(base_url=base_url, timeout=10.0)
    deadline = time.time() + 15.0
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            output = (
                proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            )
            raise RuntimeError("push live-server 啟動失敗：\n{0}".format(output))
        try:
            response = client.get("/openapi.json")
            if response.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    if not ready:
        proc.terminate()
        raise RuntimeError("push live-server 等待就緒逾時")
    server = _LiveServer(base_url, state, client)
    server.ic_state_path = ic_state
    server.topology_path = topology_path
    server.token = "test-token-xyz"
    return proc, server


@pytest.fixture
def push_server_factory(tmp_path: Path):
    """回傳 ``start(topology_path, fanout_names, mode="ok") -> _LiveServer`` 的啟動器。

    fixture 結束時關閉所有已啟動的 live-server 子行程。
    """
    started = []

    def _start(topology_path: Path, fanout_names: List[str], mode: str = "ok") -> _LiveServer:
        proc, server = _start_push_live_server(topology_path, tmp_path, fanout_names, mode)
        started.append((proc, server))
        return server

    yield _start
    for proc, server in started:
        _stop_live_server(proc, server)


_TRAFFIC_FANOUTS = ["spine-1", "agg-1", "agg-2", "leaf-1-1", "leaf-2-1"]
_IC_FANOUTS = ["core-1", "leaf-1", "leaf-2", "ic-1-1", "ic-2-1"]


@pytest.fixture
def push_live_server(three_tier_path: Path, push_server_factory):
    """3tier 啟用設備下發的主執行緒 live-server（假設備，mode=ok）。"""
    return push_server_factory(three_tier_path, _TRAFFIC_FANOUTS)


@pytest.fixture
def ic_push_live_server(ic_small_path: Path, push_server_factory):
    """互聯小拓樸啟用設備下發的主執行緒 live-server（假設備，mode=ok）。"""
    return push_server_factory(ic_small_path, _IC_FANOUTS)
