"""topo_manager REST API 與 CLI 跨行程一致性測試（Phase 3 審查 Issue 5）。

設計宣稱 API 與 CLI 並行同一帳本靠 Phase 2 檔鎖（``<state>.lock`` 的
``fcntl.flock`` + SIGALRM 逾時）保證一致：API 的 POST/DELETE 與 CLI 的
``allocate``/``release`` 互斥序列化，鎖內重讀最新帳本再決策。原審查指出此賣點
「設計有敘述、測試無覆蓋」。本模組補上多行程交錯寫入的回歸守門：

  - ``test_interleaved_writes_ledger_consistent``：API（live-server 子行程）與
    CLI（子行程）指向**同一** state 交錯分配多筆 session，最終讀回磁碟帳本，
    斷言 session_id / VLAN / access port 皆無重複（跨行程檔鎖的核心不變量）。
  - ``test_cli_holds_lock_api_waits``：CLI 子行程於鎖內長駐（以 release 一個
    不存在 session 之前無法人工延長，改以同時發動多筆並行寫入觀測序列化結果），
    驗證交錯期間無任何一筆落空或重號。

測試以子行程（API live-server + CLI subprocess）執行，不依賴真實設備，亦不標
``live``（不觸 Phase 4 drivers）。harness 的主執行緒 live-server 機制與
``_LiveServer`` 握把於 conftest.py 定義（見 test_api.py 模組 docstring）。

歷史（HISTORY）：
  - 0.4.1  Phase 3 審查 Issue 5：新增 API↔CLI 跨行程交錯寫入一致性測試。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Set, Tuple


def _read_ledger(state_path: Path) -> dict:
    """讀回磁碟 ledger JSON（跨行程寫入後的最終一致狀態）。"""
    with state_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _cli_allocate(
    topology_path: Path, state_path: Path, gen_port: str, dut_node: str
) -> Tuple[int, str]:
    """以子行程跑 CLI ``allocate`` 指向同一 state，回 (returncode, 合併輸出)。

    與 production CLI 完全同路徑（擁有自己的主執行緒，signal 檔鎖可用）；成功
    回 0，可回報失敗（如硬衝突）印 stderr 單行、回 1（不冒 traceback）。
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "topo_manager",
            "allocate",
            str(topology_path),
            "--from",
            gen_port,
            "--to",
            dut_node,
            "--state",
            str(state_path),
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _api_allocate(client, gen_port: str, dut_node: str):
    """以 API POST /sessions 指向同一 state 分配一筆（回 httpx.Response）。"""
    return client.post(
        "/sessions",
        json={"generator_port": gen_port, "dut_port": dut_node},
    )


def _access_ports(session: dict) -> Set[str]:
    """由帳本 session dict 推導 access port 集合（physical_ports − trunk 端）。"""
    trunk_ends: Set[str] = set()
    for segment in session.get("trunk_segments", []):
        trunk_ends.add(segment["a_node"])
        trunk_ends.add(segment["b_node"])
    return {node for node in session["physical_ports"] if node not in trunk_ends}


def _assert_ledger_invariants(ledger: dict, expected_count: int) -> None:
    """斷言帳本跨行程不變量：筆數、session_id / VLAN / access port 皆不重複。"""
    sessions = ledger["sessions"]
    assert len(sessions) == expected_count

    session_ids = [s["session_id"] for s in sessions]
    vlans = [s["vlan"] for s in sessions]
    assert len(set(session_ids)) == len(session_ids), "session_id 重複"
    assert len(set(vlans)) == len(vlans), "VLAN 重複"

    all_access: List[str] = []
    for session in sessions:
        all_access.extend(_access_ports(session))
    assert len(set(all_access)) == len(all_access), "access port 被兩個 session 佔用"


def test_interleaved_writes_ledger_consistent(
    live_server, three_tier_path: Path
) -> None:
    """API 與 CLI 交錯分配同一 state → 帳本序號/VLAN/access port 不重複（Issue 5）。

    對同一 state：CLI 子行程與 API POST 交錯各分配若干不同 (gen, dut) 配對。
    檔鎖序列化保證每筆鎖內重讀最新帳本再決策，故最終帳本應含全部 4 筆且無
    任何重複號/重複 VLAN/重複 access port。
    """
    client = live_server.client
    state_path = live_server.state_path

    # 四組互不衝突的端點（走不同 DUT/gen，確保各自可成功分配）。
    api_pairs = [
        ("ixia-1/1", "dut-A1:Eth0"),
        ("trex-1/0", "dut-A2:Eth0"),
    ]
    cli_pairs = [
        ("ixia-1/2", "dut-B1:Eth0"),
        ("trex-1/1", "dut-B2:Eth0"),
    ]

    # 交錯：API→CLI→API→CLI，每步前一步落鎖釋放後才進行（嚴格序列化驗證）。
    r1 = _api_allocate(client, *api_pairs[0])
    assert r1.status_code == 201, r1.text

    rc, out = _cli_allocate(three_tier_path, state_path, *cli_pairs[0])
    assert rc == 0, out

    r2 = _api_allocate(client, *api_pairs[1])
    assert r2.status_code == 201, r2.text

    rc, out = _cli_allocate(three_tier_path, state_path, *cli_pairs[1])
    assert rc == 0, out

    ledger = _read_ledger(state_path)
    _assert_ledger_invariants(ledger, expected_count=4)

    # API 讀端點每請求重建、鎖內取最新快照，應看到全部 4 筆（含 CLI 寫入的）。
    listing = client.get("/sessions").json()
    assert listing["count"] == 4


def test_concurrent_writes_serialized_no_dup(
    live_server, three_tier_path: Path
) -> None:
    """同時發動 API 與 CLI 並行寫入 → 檔鎖序列化、無重號/重 VLAN（Issue 5）。

    以 thread pool 同時觸發兩筆 API POST 與兩筆 CLI allocate（互不衝突的端點），
    製造真實的取鎖競爭。檔鎖保證彼此互斥序列化：全部成功、帳本 4 筆且
    session_id / VLAN / access port 皆不重複（即使取鎖競爭下亦不重號）。
    """
    client = live_server.client
    state_path = live_server.state_path

    api_pairs = [
        ("ixia-1/1", "dut-A1:Eth0"),
        ("trex-1/0", "dut-A2:Eth0"),
    ]
    cli_pairs = [
        ("ixia-1/2", "dut-B1:Eth0"),
        ("trex-1/1", "dut-B2:Eth0"),
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for gen_port, dut_node in api_pairs:
            futures.append(
                pool.submit(_api_allocate, client, gen_port, dut_node)
            )
        for gen_port, dut_node in cli_pairs:
            futures.append(
                pool.submit(
                    _cli_allocate, three_tier_path, state_path, gen_port, dut_node
                )
            )
        results = [future.result() for future in futures]

    # 前兩筆為 API Response、後兩筆為 CLI (rc, out)。
    api_results = results[:2]
    cli_results = results[2:]
    for response in api_results:
        assert response.status_code == 201, response.text
    for rc, out in cli_results:
        assert rc == 0, out

    ledger = _read_ledger(state_path)
    _assert_ledger_invariants(ledger, expected_count=4)
