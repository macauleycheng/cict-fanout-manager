"""topo_manager 命令列入口（cli.py，設計 §6、§CLI、FR-5、FR-7）。

以標準庫 ``argparse`` 實作，不引入額外 CLI 相依。公開入口
``main(argv=None) -> int``：以子命令（subcommand）分派不同功能，每個子命令都
吃一個位置參數 ``topology.yaml``：

  - ``summary``：載入拓樸、建 ``TopologyGraph``、印人類可讀摘要（Phase 1 等價）。
  - ``path --from --to``：印一條 generator→DUT 路徑（Phase 1 等價）。
  - ``allocate --from --to [--state]``：建 Allocator，分配一筆 session，印出
    session_id、VLAN、路徑（以 ' → ' 連接）與各 trunk 段及其 VLAN。
  - ``release --session [--state]``：釋放一筆 session，印其摘要。
  - ``list [--state]``：列出目前所有 active session（無則印提示）。

``--state`` 預設 ``.topo-state/allocations.json``（相對於當前工作目錄）；跨行程
一致性需使用同一 cwd 或明確以 ``--state`` 指定同一狀態檔。

錯誤處理（FR-5.4）：捕捉 ``TopologyError`` 家族（含 Phase 2 新增的
HardConflictError / VlanPoolExhaustedError / SessionNotFoundError /
SessionStoreError，皆為其子類，自動一併攔截），印到 stderr 的單行清楚訊息
（``錯誤：{訊息}``）並回傳 1；不讓可回報的失敗冒出未處理 traceback。非預期例外
（程式 bug）不攔截，保留原始 traceback 以利除錯。

狀態不使用全域可變變數（對齊規範精神）：所有流程資料為函式區域變數，輸出經
``print`` 寫到 stdout、錯誤經 ``print(..., file=sys.stderr)`` 寫到 stderr。

歷史（HISTORY）：
  - 0.1.0  FEAT-004 初版，建立 argparse 介面、摘要排版、路徑輸出與
           TopologyError 家族的 stderr/exit code 錯誤處理（設計 §6）。
  - 0.2.0  FEAT-006 重構為子命令（summary/path/allocate/release/list），
           接上 Allocator 與 JsonSessionStore，新增 session 排版輔助與
           --state 選項（設計 §CLI）；移除舊的扁平介面。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

from topo_manager.allocator import Allocator, Session
from topo_manager.errors import TopologyError
from topo_manager.graph import TopologyGraph
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore

# --state 預設狀態檔路徑（相對於當前工作目錄，須列入 .gitignore）。
_DEFAULT_STATE_PATH = ".topo-state/allocations.json"


def _build_parser() -> argparse.ArgumentParser:
    """建立 CLI 的 argparse parser 與各子命令（設計 §CLI 介面）。

    回傳：
      - 已設定好 ``summary`` / ``path`` / ``allocate`` / ``release`` / ``list``
        五個子命令的 parser；子命令名記於 ``args.command``。
    """
    parser = argparse.ArgumentParser(
        prog="topo-manager",
        description="拓樸管理工具：載入拓樸 YAML、印摘要、查路徑，並管理 session 分配。",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.required = True

    # summary <topology.yaml>
    summary_parser = subparsers.add_parser(
        "summary", help="載入拓樸並印人類可讀摘要。"
    )
    _add_topology_arg(summary_parser)

    # path <topology.yaml> --from --to
    path_parser = subparsers.add_parser(
        "path", help="印一條 generator→DUT 實體路徑。"
    )
    _add_topology_arg(path_parser)
    _add_from_to_args(path_parser, required=True)

    # allocate <topology.yaml> --from --to [--state]
    allocate_parser = subparsers.add_parser(
        "allocate", help="分配一筆 session（含 VLAN 與路徑）並持久化。"
    )
    _add_topology_arg(allocate_parser)
    _add_from_to_args(allocate_parser, required=True)
    _add_state_arg(allocate_parser)

    # release <topology.yaml> --session [--state]
    release_parser = subparsers.add_parser(
        "release", help="釋放一筆既有 session。"
    )
    _add_topology_arg(release_parser)
    release_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        required=True,
        help="欲釋放的 session 識別碼（如 s-0001）。",
    )
    _add_state_arg(release_parser)

    # list <topology.yaml> [--state]
    list_parser = subparsers.add_parser(
        "list", help="列出目前所有 active session。"
    )
    _add_topology_arg(list_parser)
    _add_state_arg(list_parser)

    return parser


def _add_topology_arg(parser: argparse.ArgumentParser) -> None:
    """為子命令加上必填位置參數 ``topology.yaml``。"""
    parser.add_argument(
        "topology",
        metavar="topology.yaml",
        help="拓樸 YAML 檔路徑（必填）。",
    )


def _add_from_to_args(parser: argparse.ArgumentParser, required: bool) -> None:
    """為子命令加上 ``--from`` / ``--to`` 端點選項（path/allocate 用）。

    參數：
      - required：是否必填（path/allocate 皆需成對提供）。

    dest 用 ``from_port`` 避免與 Python 保留字 ``from`` 衝突。
    """
    parser.add_argument(
        "--from",
        dest="from_port",
        metavar="GEN_PORT",
        required=required,
        help="generator 端 port，可裸名（如 ixia-1/1）或 dev:port。",
    )
    parser.add_argument(
        "--to",
        dest="to_port",
        metavar="DUT_PORT",
        required=required,
        help="DUT 端點，一律 dev:port 形式（如 dut-A1:Eth0）。",
    )


def _add_state_arg(parser: argparse.ArgumentParser) -> None:
    """為子命令加上 ``--state`` 狀態檔選項（allocate/release/list 用）。

    預設相對於當前工作目錄；help 文字提醒跨行程一致性需同一 cwd 或明確路徑。
    """
    parser.add_argument(
        "--state",
        dest="state",
        metavar="STATE_JSON",
        default=_DEFAULT_STATE_PATH,
        help=(
            "session ledger 狀態檔路徑（預設 {0}，相對於當前工作目錄）；"
            "跨行程一致性需使用同一 cwd 或明確以 --state 指定同一檔。"
        ).format(_DEFAULT_STATE_PATH),
    )


def _format_summary(topology_site: str, yaml_path: str, summary: Dict[str, Any]) -> str:
    """把 ``TopologyGraph.summary()`` 的 dict 排版成人類可讀區塊（設計 §6）。

    參數：
      - topology_site：拓樸的 site 名稱，放在標題列。
      - yaml_path：輸入 YAML 路徑，放在標題列。
      - summary：``summary()`` 回傳的 dict（鍵見 §5.6.2）。

    回傳：
      - 多行字串（不含結尾換行）。``tiers`` 以 int key 升冪排版、
        ``nodes_by_role`` 以 role 名排序，皆取自實際資料不寫死。
    """
    nodes_by_role: Dict[str, int] = summary["nodes_by_role"]
    role_text = ", ".join(
        "{0}={1}".format(role, nodes_by_role[role])
        for role in sorted(nodes_by_role.keys())
    )

    tiers: Dict[int, int] = summary["tiers"]
    tiers_text = ", ".join(
        "{0}: {1}".format(level, tiers[level]) for level in sorted(tiers.keys())
    )

    lines: List[str] = [
        "拓樸摘要：{0}（site={1}）".format(yaml_path, topology_site),
        "  generator port 數：{0}".format(summary["generator_port_count"]),
        "  DUT 台數：{0}    DUT 打流 port 數：{1}".format(
            summary["dut_count"], summary["dut_port_count"]
        ),
        "  fanout 台數：{0}".format(summary["fanout_count"]),
        "  各層（role）節點數（台數）：{0}".format(role_text),
        "  各 tier 節點數（排除 #core）：{{{0}}}".format(tiers_text),
    ]
    return "\n".join(lines)


def _format_path(from_port: str, to_port: str, path: List[str]) -> str:
    """把 find_path 的節點序列排版成一條以 → 連接的路徑字串（設計 §6）。

    參數：
      - from_port / to_port：使用者輸入的查詢端點，放在標題列。
      - path：find_path 回傳、已清洗掉 #core 的節點 id 序列。

    回傳：
      - 兩行字串：標題列 + 以 ' → ' 連接的節點序列。
    """
    joined = " → ".join(path)
    return "路徑 {0} → {1}：\n  {2}".format(from_port, to_port, joined)


def _format_session(session: Session) -> str:
    """把一筆 ``Session`` 排版成人類可讀多行區塊（設計 §CLI）。

    參數：
      - session：要排版的 ``Session``。

    回傳：
      - 多行字串（不含結尾換行）：session_id、VLAN、generator→DUT、以 ' → '
        連接的路徑，以及各 trunk 段與其 VLAN；無 trunk 時明確標示。
    """
    lines: List[str] = [
        "session {0}".format(session.session_id),
        "  VLAN：{0}".format(session.vlan),
        "  端點：{0} → {1}".format(session.generator_port, session.dut_port),
        "  路徑：{0}".format(" → ".join(session.path)),
    ]
    if len(session.trunk_segments) > 0:
        lines.append("  trunk 段：")
        for segment in session.trunk_segments:
            lines.append(
                "    {0} → {1}（VLAN {2}）".format(
                    segment.a_node, segment.b_node, segment.vlan
                )
            )
    else:
        lines.append("  trunk 段：（無）")
    return "\n".join(lines)


def _format_session_list(sessions: List[Session]) -> str:
    """把 session 清單排版成人類可讀區塊（設計 §CLI）。

    參數：
      - sessions：``Allocator.list_sessions()`` 回傳、已排序的 session 清單。

    回傳：
      - 清單為空時回傳固定提示 '目前無 active session'；否則回傳標題列加上
        每筆 session 的 ``_format_session`` 區塊，以空行分隔。
    """
    if len(sessions) == 0:
        return "目前無 active session"

    blocks: List[str] = ["目前 active session 共 {0} 筆：".format(len(sessions))]
    for session in sessions:
        blocks.append(_format_session(session))
    return "\n\n".join(blocks)


def _build_allocator(topology_path: str, state_path: str) -> Allocator:
    """載入拓樸、建 JsonSessionStore 與 Allocator（allocate/release/list 共用）。

    參數：
      - topology_path：拓樸 YAML 檔路徑。
      - state_path：session ledger 狀態檔路徑（``--state``）。

    回傳：
      - 已還原既有 ledger 的 ``Allocator``。

    例外：
      - ``TopologyError`` 家族：載入 / 建圖 / ledger 還原失敗（由 main 攔截）。
    """
    topology = load_topology(topology_path)
    store = JsonSessionStore(state_path)
    return Allocator(topology, store)


def _cmd_summary(args: argparse.Namespace) -> int:
    """執行 ``summary`` 子命令：印拓樸摘要（Phase 1 等價）。"""
    topology = load_topology(args.topology)
    graph = TopologyGraph(topology)
    print(_format_summary(topology.site, args.topology, graph.summary()))
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    """執行 ``path`` 子命令：印 generator→DUT 路徑（Phase 1 等價）。"""
    topology = load_topology(args.topology)
    graph = TopologyGraph(topology)
    path = graph.find_path(args.from_port, args.to_port)
    print(_format_path(args.from_port, args.to_port, path))
    return 0


def _cmd_allocate(args: argparse.Namespace) -> int:
    """執行 ``allocate`` 子命令：分配一筆 session 並印其摘要（設計 §CLI）。"""
    allocator = _build_allocator(args.topology, args.state)
    session = allocator.allocate(args.from_port, args.to_port)
    print("已分配：")
    print(_format_session(session))
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    """執行 ``release`` 子命令：釋放一筆 session 並印其摘要（設計 §CLI）。"""
    allocator = _build_allocator(args.topology, args.state)
    session = allocator.release(args.session_id)
    print("已釋放：")
    print(_format_session(session))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """執行 ``list`` 子命令：列出目前所有 active session（設計 §CLI）。"""
    allocator = _build_allocator(args.topology, args.state)
    print(_format_session_list(allocator.list_sessions()))
    return 0


# 子命令名 → 處理函式的分派表（新增子命令只需在此註冊）。
_COMMANDS = {
    "summary": _cmd_summary,
    "path": _cmd_path,
    "allocate": _cmd_allocate,
    "release": _cmd_release,
    "list": _cmd_list,
}


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口：解析子命令並分派（設計 §CLI、FR-5）。

    參數：
      - argv：命令列參數清單（不含程式名）；None 時由 argparse 取
        ``sys.argv[1:]``。

    回傳：
      - int exit code：成功 0；捕捉到 ``TopologyError`` 家族（含 Phase 2 的
        HardConflictError / VlanPoolExhaustedError / SessionNotFoundError /
        SessionStoreError 與 Phase 1 的 PathLookupError）印 stderr 單行訊息後
        回傳 1。缺子命令或參數錯誤由 argparse 以 exit code 2 結束。

    例外：
      - 非預期例外（程式 bug）不攔截，保留原始 traceback。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    handler = _COMMANDS[args.command]

    try:
        return handler(args)
    except TopologyError as exc:
        # FR-5.4：可回報的失敗印單行清楚訊息到 stderr 並回傳 1；
        # 不讓 traceback 冒出。訊息可能多行（如驗證逐條），原樣輸出。
        print("錯誤：{0}".format(exc), file=sys.stderr)
        return 1
