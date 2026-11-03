"""CLI 子命令整合測試（設計 §CLI、§6、FR-5、FR-7、AC14/AC15）。

以 main([...]) 搭配 capsys 驗證子命令行為：
  - allocate 印出 VLAN 與路徑；另一個 list 呼叫看到同一 ledger（同一 tmp 狀態
    檔）；release 後 list 反映移除（AC14）。
  - 錯誤情境回傳非零 exit code、stderr 僅單行且無 traceback（AC15）。
  - summary <yaml> 與 path <yaml> --from --to 的輸出與 Phase 1 等價（FR-7.7 回歸，
    補足舊 repo 無 CLI 測試的缺口）。

歷史（HISTORY）：
  - 0.2.0  FEAT-006 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from topo_manager.cli import (
    _format_path,
    _format_summary,
    main,
)
from topo_manager.graph import TopologyGraph
from topo_manager.loader import load_topology


def _state_arg(tmp_path: Path) -> str:
    """回傳 tmp_path 下的狀態檔路徑字串（供 --state 使用）。"""
    return str(tmp_path / "state" / "allocations.json")


def test_allocate_prints_vlan_and_path(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """allocate 子命令成功並印出 VLAN 與路徑（AC14）。"""
    state = _state_arg(tmp_path)
    code = main(
        [
            "allocate",
            str(three_tier_path),
            "--from",
            "ixia-1/1",
            "--to",
            "dut-A1:Eth0",
            "--state",
            state,
        ]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "s-0001" in out
    assert "VLAN：100" in out
    # 路徑含首節點與末節點。
    assert "ixia-1:ixia-1/1" in out
    assert "dut-A1:Eth0" in out


def test_list_sees_same_ledger_then_release_reflects_removal(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """同一狀態檔下 list 看到既有 session；release 後 list 反映移除（AC14）。"""
    state = _state_arg(tmp_path)
    topo = str(three_tier_path)

    assert main(["allocate", topo, "--from", "ixia-1/1", "--to", "dut-A1:Eth0", "--state", state]) == 0
    capsys.readouterr()

    # 另一個 list 呼叫（獨立建 Allocator）看到同一 ledger。
    assert main(["list", topo, "--state", state]) == 0
    list_out = capsys.readouterr().out
    assert "s-0001" in list_out
    assert "共 1 筆" in list_out

    # release 後再 list，session 消失。
    assert main(["release", topo, "--session", "s-0001", "--state", state]) == 0
    capsys.readouterr()

    assert main(["list", topo, "--state", state]) == 0
    after_out = capsys.readouterr().out
    assert "s-0001" not in after_out
    assert "目前無 active session" in after_out


def test_error_case_returns_nonzero_single_stderr_line(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """錯誤情境回傳非零、stderr 單行且無 traceback（AC15）。"""
    state = _state_arg(tmp_path)
    # 釋放不存在的 session → SessionNotFoundError → exit 1。
    code = main(["release", str(three_tier_path), "--session", "s-9999", "--state", state])
    assert code == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    # 單行錯誤訊息、以「錯誤：」開頭、含查無的 session_id，且不含 traceback。
    assert err.startswith("錯誤：")
    assert err.rstrip("\n").count("\n") == 0
    assert "s-9999" in err
    assert "Traceback" not in err


def test_allocate_hard_conflict_returns_nonzero(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """重複分配同一資源觸發硬衝突，CLI 回傳非零且 stderr 單行（AC15）。"""
    state = _state_arg(tmp_path)
    topo = str(three_tier_path)
    assert main(["allocate", topo, "--from", "ixia-1/1", "--to", "dut-A1:Eth0", "--state", state]) == 0
    capsys.readouterr()

    code = main(["allocate", topo, "--from", "ixia-1/1", "--to", "dut-A1:Eth0", "--state", state])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("錯誤：")
    assert "Traceback" not in captured.err
    # 本階段 AC13（原 AC15 裁決）：硬衝突為單一「多行」錯誤區塊（標題列 +
    # 至少一個衝突 port 行），不含 traceback。
    assert captured.err.rstrip("\n").count("\n") >= 1


def test_store_io_failure_cli_nonzero_no_traceback(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--state 指向不可建立路徑（檔案當中間目錄）→ 非零、stderr 含路徑、無 traceback（#4）。"""
    a_file = tmp_path / "afile"
    a_file.write_text("x", encoding="utf-8")
    bad_state = str(a_file / "nope" / "allocations.json")
    code = main(
        [
            "allocate", str(three_tier_path), "--from", "ixia-1/1",
            "--to", "dut-A1:Eth0", "--state", bad_state,
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("錯誤：")
    assert str(a_file) in captured.err
    assert "Traceback" not in captured.err


def test_list_raw_dumps_bad_ledger(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """版本不符/malformed id 帳本下：正常 list 非零，list --raw 回 0 並含原始 id（#12、AC11）。"""
    state = _state_arg(tmp_path)
    state_path = Path(state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # 版本不符（1）且 malformed id 的帳本。
    state_path.write_text(
        json.dumps(
            {"version": 1, "sessions": [{"session_id": "manual-session"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    topo = str(three_tier_path)

    # 正常 list（非 --raw）會因版本不符而非零。
    assert main(["list", topo, "--state", state]) == 1
    capsys.readouterr()

    # list --raw 回 0 且 stdout 含原始 malformed id。
    assert main(["list", "--raw", "--state", state]) == 0
    out = capsys.readouterr().out
    assert "manual-session" in out


def test_list_raw_works_without_topology_or_with_bad_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """list --raw 繞過拓樸：缺 YAML 或壞 YAML 皆回 0 並 dump 帳本（#12）。"""
    state = _state_arg(tmp_path)
    state_path = Path(state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": 2, "next_seq": 1, "sessions": []}),
        encoding="utf-8",
    )

    # 完全不給 topology。
    assert main(["list", "--raw", "--state", state]) == 0
    out = capsys.readouterr().out
    assert '"version"' in out

    # 給一個不存在的 YAML 也不影響 --raw。
    assert main(["list", "nonexistent.yaml", "--raw", "--state", state]) == 0
    assert '"sessions"' in capsys.readouterr().out


def test_list_non_raw_missing_topology_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """list（非 --raw）未給 topology → ConfigError、非零、無 traceback（#12）。"""
    state = _state_arg(tmp_path)
    code = main(["list", "--state", state])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("錯誤：")
    assert "Traceback" not in captured.err


def test_vlan_range_cli_config_error(
    three_tier_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--vlan-max 9999 超出 802.1Q → ConfigError 攔截、非零、無 traceback（#13、AC12）。"""
    state = _state_arg(tmp_path)
    code = main(
        [
            "allocate", str(three_tier_path), "--from", "ixia-1/1",
            "--to", "dut-A1:Eth0", "--state", state, "--vlan-max", "9999",
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("錯誤：")
    assert "Traceback" not in captured.err


def test_summary_subcommand_equivalent_to_phase1(
    three_tier_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """summary 子命令輸出與 Phase 1 _format_summary 等價（FR-7.7 回歸）。"""
    topo = str(three_tier_path)
    assert main(["summary", topo]) == 0
    out = capsys.readouterr().out.rstrip("\n")

    topology = load_topology(three_tier_path)
    graph = TopologyGraph(topology)
    expected = _format_summary(topology.site, topo, graph.summary())
    assert out == expected


def test_path_subcommand_equivalent_to_phase1(
    three_tier_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """path 子命令輸出與 Phase 1 _format_path 等價（FR-7.7 回歸）。"""
    topo = str(three_tier_path)
    assert main(["path", topo, "--from", "ixia-1/1", "--to", "dut-A1:Eth0"]) == 0
    out = capsys.readouterr().out.rstrip("\n")

    graph = TopologyGraph(load_topology(three_tier_path))
    path = graph.find_path("ixia-1/1", "dut-A1:Eth0")
    expected = _format_path("ixia-1/1", "dut-A1:Eth0", path)
    assert out == expected
