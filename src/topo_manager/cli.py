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
  - 0.3.0  Phase 2 Hardening：list 加 --raw（唯讀 dump，繞過拓樸）且
           topology 改選填（nargs="?"）；allocate/release/list 加
           --vlan-min/--vlan-max；_build_allocator 把 ValueError 轉
           ConfigError 並傳 vlan 範圍；新增 _print_raw_ledger。
  - 0.4.0  Phase 4 fanout 下發層：新增 plan/probe/apply/verify 四子命令與其
           handler（_cmd_plan/_cmd_probe/_cmd_apply/_cmd_verify），於 _COMMANDS
           分派表註冊；新增 --credentials/--fanout/--apply 選項輔助；drivers
           模組（config/ec_ssh/deployer）一律於 handler 內延遲 import，確保
           summary/path/allocate/release/list 與未裝 netmiko 時完全不受影響。
           既有 5 子命令、_build_allocator、main() 的 except TopologyError 不動。
  - 0.5.0  Phase 4 QinQ/dot1q-tunnel 擴充（決策 8、9）：新增 ``setup`` 子命令
           （全域 dot1q-tunnel system-tunnel-control 冪等下發，不吃 topology.yaml；
           --fanout 指定分支復用 select_fanout、未指定時遍歷全部）與 _cmd_setup
           handler，於 _COMMANDS 註冊；_print_plan_result 每個 change 加印其
           role（邊緣/互聯）；_print_verify_result 診斷字串改中性詞（期望設定）。
           既有 9 子命令、_build_allocator、main() 的 except 不動。
  - 0.6.0  互聯 fanout：新增 ``interconnect`` 子命令群（巢狀 summary/allocate/
           release/list/plan/park/apply/verify，由 interconnect.cli 註冊與
           分派，lazy import）與 _cmd_interconnect；既有子命令與輸出不變。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from topo_manager.allocator import Allocator, Session
from topo_manager.errors import ConfigError, TopologyError
from topo_manager.graph import TopologyGraph
from topo_manager.loader import load_topology
from topo_manager.store import JsonSessionStore

# 預設 VLAN pool 端點（CLI --vlan-min/--vlan-max，#13）。
_DEFAULT_VLAN_MIN = 100
_DEFAULT_VLAN_MAX = 3000

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

    # allocate <topology.yaml> --from --to [--state] [--vlan-min/--vlan-max]
    allocate_parser = subparsers.add_parser(
        "allocate", help="分配一筆 session（含 VLAN 與路徑）並持久化。"
    )
    _add_topology_arg(allocate_parser)
    _add_from_to_args(allocate_parser, required=True)
    _add_state_arg(allocate_parser)
    _add_vlan_range_args(allocate_parser)

    # release <topology.yaml> --session [--state] [--vlan-min/--vlan-max]
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
    _add_vlan_range_args(release_parser)

    # list [topology.yaml] [--state] [--raw] [--vlan-min/--vlan-max]
    list_parser = subparsers.add_parser(
        "list", help="列出目前所有 active session（--raw 唯讀 dump 原始帳本）。"
    )
    _add_topology_arg(list_parser, required=False)
    _add_state_arg(list_parser)
    list_parser.add_argument(
        "--raw",
        dest="raw",
        action="store_true",
        help="唯讀 dump 原始 ledger JSON（繞過拓樸與還原檢查，壞帳本亦可檢視）。",
    )
    _add_vlan_range_args(list_parser)

    # plan <topology.yaml> --session/--from/--to [--state][--credentials][--fanout]
    plan_parser = subparsers.add_parser(
        "plan", help="純算 fanout 下發計畫（不連線）；--session 取既有、--from/--to 預覽。"
    )
    _add_topology_arg(plan_parser)
    _add_session_or_endpoints_args(plan_parser)
    _add_state_arg(plan_parser)
    _add_vlan_range_args(plan_parser)
    _add_credentials_arg(plan_parser)
    _add_fanout_arg(plan_parser)

    # probe [--credentials][--fanout]
    probe_parser = subparsers.add_parser(
        "probe", help="唯讀連線 fanout 並印 show version/vlan（診斷用）。"
    )
    _add_credentials_arg(probe_parser)
    _add_fanout_arg(probe_parser)

    # apply <topology.yaml> --session [--state][--credentials][--fanout][--apply]
    apply_parser = subparsers.add_parser(
        "apply", help="下發 session 至 fanout（預設 dry-run，--apply 才寫入）。"
    )
    _add_topology_arg(apply_parser)
    _add_session_arg(apply_parser)
    _add_state_arg(apply_parser)
    _add_vlan_range_args(apply_parser)
    _add_credentials_arg(apply_parser)
    _add_fanout_arg(apply_parser)
    _add_apply_flag(apply_parser)

    # verify <topology.yaml> --session [--state][--credentials][--fanout][--apply]
    verify_parser = subparsers.add_parser(
        "verify", help="回讀驗證 session 是否已下發；--apply 時重套缺漏（重開機偵測）。"
    )
    _add_topology_arg(verify_parser)
    _add_session_arg(verify_parser)
    _add_state_arg(verify_parser)
    _add_vlan_range_args(verify_parser)
    _add_credentials_arg(verify_parser)
    _add_fanout_arg(verify_parser)
    _add_apply_flag(verify_parser)

    # setup [--credentials][--fanout][--apply]
    setup_parser = subparsers.add_parser(
        "setup",
        help="全域下發 dot1q-tunnel system-tunnel-control（預設 dry-run，--apply 才寫入）。",
    )
    _add_credentials_arg(setup_parser)
    _add_fanout_arg(setup_parser)
    _add_apply_flag(setup_parser)

    # interconnect <子命令>（互聯 fanout；巢狀子命令由 interconnect.cli 註冊）
    ic_parser = subparsers.add_parser(
        "interconnect", help="互聯 fanout（DUT 與 DUT 之間）管理。"
    )
    from topo_manager.interconnect.cli import register_interconnect_parsers

    register_interconnect_parsers(ic_parser)

    return parser


def _add_topology_arg(parser: argparse.ArgumentParser, required: bool = True) -> None:
    """為子命令加上位置參數 ``topology.yaml``。

    參數：
      - required：True（預設）時為必填位置參數；False 時以 ``nargs="?"``
        設為選填（預設 None）——供 ``list --raw`` 可完全不提供拓樸（#12）。
    """
    if required:
        parser.add_argument(
            "topology",
            metavar="topology.yaml",
            help="拓樸 YAML 檔路徑（必填）。",
        )
    else:
        parser.add_argument(
            "topology",
            metavar="topology.yaml",
            nargs="?",
            default=None,
            help="拓樸 YAML 檔路徑（--raw 時可省略；非 --raw 時必填）。",
        )


def _add_vlan_range_args(parser: argparse.ArgumentParser) -> None:
    """為子命令加上 ``--vlan-min`` / ``--vlan-max`` 選項（#13）。

    三個子命令（allocate/release/list）共用同一組選項與預設（100/3000），
    使對同一 state 的 pool 範圍一致。非法範圍由 ``_build_allocator`` 轉
    ``ConfigError``（CLI 無 traceback）。
    """
    parser.add_argument(
        "--vlan-min",
        dest="vlan_min",
        metavar="VLAN_MIN",
        type=int,
        default=_DEFAULT_VLAN_MIN,
        help="VLAN pool 下界（預設 {0}，須落在 802.1Q 2..4094）。".format(
            _DEFAULT_VLAN_MIN
        ),
    )
    parser.add_argument(
        "--vlan-max",
        dest="vlan_max",
        metavar="VLAN_MAX",
        type=int,
        default=_DEFAULT_VLAN_MAX,
        help="VLAN pool 上界（預設 {0}，須落在 802.1Q 2..4094）。".format(
            _DEFAULT_VLAN_MAX
        ),
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


def _add_session_arg(parser: argparse.ArgumentParser) -> None:
    """為子命令加上必填 ``--session`` 選項（apply/verify 用）。

    apply/verify 一律針對一筆既有 session 操作（經 allocator.get_session 取回），
    故 ``--session`` 為必填。
    """
    parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        required=True,
        help="目標 session 識別碼（如 s-0001，經帳本取回）。",
    )


def _add_session_or_endpoints_args(parser: argparse.ArgumentParser) -> None:
    """為 ``plan`` 加上互斥的 ``--session`` 或 ``--from/--to``（擇一）。

    ``--session`` 走 ``get_session`` 取既有 session；``--from/--to`` 走
    ``derive_session`` 純推導預覽（不落帳）。兩者互斥且須擇一提供，由 handler
    驗證（argparse 層僅收選項，缺漏/互斥語意於 ``_cmd_plan`` 以 ConfigError 回報）。
    """
    parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        default=None,
        help="既有 session 識別碼（如 s-0001）；與 --from/--to 擇一。",
    )
    parser.add_argument(
        "--from",
        dest="from_port",
        metavar="GEN_PORT",
        default=None,
        help="generator 端 port（預覽用，與 --session 擇一）。",
    )
    parser.add_argument(
        "--to",
        dest="to_port",
        metavar="DUT_PORT",
        default=None,
        help="DUT 端點 dev:port（預覽用，與 --session 擇一）。",
    )


def _add_credentials_arg(parser: argparse.ArgumentParser) -> None:
    """為下發層子命令加上 ``--credentials`` 連線設定檔選項。

    對應 ``load_fanout_config(path=...)``；None（未指定）時由設定層走預設檔名
    與 env 覆寫（細節見 drivers/config.py）。憑證檔已列入 .gitignore，不入版控。
    """
    parser.add_argument(
        "--credentials",
        dest="credentials",
        metavar="CREDENTIALS_JSON",
        default=None,
        help="fanout 連線設定檔路徑（JSON）；未指定時走預設檔名與 env 覆寫。",
    )


def _add_fanout_arg(parser: argparse.ArgumentParser) -> None:
    """為下發層子命令加上 ``--fanout`` 選台選項（多台時必指定其一）。"""
    parser.add_argument(
        "--fanout",
        dest="fanout",
        metavar="FANOUT_NAME",
        default=None,
        help="指定要操作的 fanout 裝置名（設定含多台時必填）。",
    )


def _add_apply_flag(parser: argparse.ArgumentParser) -> None:
    """為 apply/verify 加上 ``--apply`` 旗標（store_true，預設 False=dry-run）。"""
    parser.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="實際連線並寫入設備（預設 dry-run，不寫入）。",
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


def _print_raw_ledger(raw: dict) -> None:
    """原樣 pretty-dump ledger JSON（不做任何還原/一致性詮釋，#12）。

    UTF-8 友善（``ensure_ascii=False``）、2 空格縮排，印到 stdout。
    """
    print(json.dumps(raw, ensure_ascii=False, indent=2))


def _build_allocator(
    topology_path: str, state_path: str, vlan_min: int, vlan_max: int
) -> Allocator:
    """載入拓樸、建 JsonSessionStore 與 Allocator（allocate/release/list 共用）。

    參數：
      - topology_path：拓樸 YAML 檔路徑。
      - state_path：session ledger 狀態檔路徑（``--state``）。
      - vlan_min / vlan_max：VLAN pool 端點（``--vlan-min``/``--vlan-max``）。

    回傳：
      - 已還原既有 ledger 的 ``Allocator``。

    例外：
      - ``ConfigError``：VLAN pool 範圍非法（把 ``Allocator`` 建構期的
        ``ValueError`` 轉為 ``TopologyError`` 家族，CLI 無 traceback，#13）。
      - ``TopologyError`` 家族：載入 / 建圖 / ledger 還原失敗（由 main 攔截）。
    """
    topology = load_topology(topology_path)
    store = JsonSessionStore(state_path)
    try:
        return Allocator(topology, store, vlan_min=vlan_min, vlan_max=vlan_max)
    except ValueError as exc:
        raise ConfigError("VLAN pool 設定非法：{0}".format(exc)) from exc


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
    allocator = _build_allocator(
        args.topology, args.state, args.vlan_min, args.vlan_max
    )
    session = allocator.allocate(args.from_port, args.to_port)
    print("已分配：")
    print(_format_session(session))
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    """執行 ``release`` 子命令：釋放一筆 session 並印其摘要（設計 §CLI）。"""
    allocator = _build_allocator(
        args.topology, args.state, args.vlan_min, args.vlan_max
    )
    session = allocator.release(args.session_id)
    print("已釋放：")
    print(_format_session(session))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """執行 ``list`` 子命令：列出 active session，或 ``--raw`` 唯讀 dump（§CLI、#12）。

    ``--raw`` 分支完全不碰拓樸（不建 Allocator、不呼叫 load_topology），直接讀
    ledger 原始 JSON 並 dump，確保壞帳本/缺 YAML 時仍可檢視。非 raw 模式需提供
    topology.yaml，否則丟 ``ConfigError``（無 traceback）。
    """
    if args.raw:
        store = JsonSessionStore(args.state)
        _print_raw_ledger(store.load_raw())
        return 0
    if args.topology is None:
        raise ConfigError("list（非 --raw 模式）需提供 topology.yaml")
    allocator = _build_allocator(
        args.topology, args.state, args.vlan_min, args.vlan_max
    )
    print(_format_session_list(allocator.list_sessions()))
    return 0


def _resolve_session_for_plan(args: argparse.Namespace):
    """為 ``plan`` 取回或推導目標 session（--session 取既有、--from/--to 預覽）。

    參數：
      - args：已解析的命名空間（具 topology/state/vlan_min/vlan_max 與
        session_id/from_port/to_port）。

    回傳：
      - 一筆 ``Session``（既有或 (preview) 臨時）。

    例外：
      - ConfigError：同時給或都不給（--session 與 --from/--to 須擇一）、或
        --from/--to 只給其一。
      - TopologyError 家族：載入/建圖/還原/查無 session/路徑查找失敗。
    """
    has_session = args.session_id is not None
    has_endpoints = args.from_port is not None or args.to_port is not None

    if has_session and has_endpoints:
        raise ConfigError("plan：--session 與 --from/--to 互斥，請擇一")
    if not has_session and not has_endpoints:
        raise ConfigError("plan：請提供 --session 或成對的 --from/--to")

    allocator = _build_allocator(
        args.topology, args.state, args.vlan_min, args.vlan_max
    )
    if has_session:
        return allocator.get_session(args.session_id)

    if args.from_port is None or args.to_port is None:
        raise ConfigError("plan：--from 與 --to 須成對提供")
    return allocator.derive_session(args.from_port, args.to_port)


def _build_deployer(credentials: Optional[str]):
    """延遲 import drivers，載入連線設定並建 Deployer（下發層 handler 共用）。

    drivers 子套件（config/ec_ssh/deployer）於此延遲 import：確保非下發層子命令
    與未安裝 netmiko 的環境完全不觸發 drivers 匯入。driver_factory 以
    ``FanoutEntry`` 建 ``EcSshDriver``（netmiko 仍於其 connect() 內延遲 import）。

    參數：
      - credentials：連線設定檔路徑（``--credentials``）；None 走預設/env。

    回傳：
      - ``(Deployer, FanoutConfig)`` tuple。

    例外：
      - ConfigError：連線設定載入/驗證失敗（由 load_fanout_config 丟出）。
    """
    from topo_manager.drivers.config import load_fanout_config
    from topo_manager.drivers.deployer import Deployer
    from topo_manager.drivers.ec_ssh import EcSshDriver

    config = load_fanout_config(path=credentials)

    def _factory(name: str, entry) -> "object":
        """以單台 FanoutEntry 建立 EcSshDriver（供 Deployer 下發各 fanout）。"""
        return EcSshDriver(
            host=entry.host,
            username=entry.username,
            password=entry.password,
            device_type=entry.device_type,
            interface_template=entry.interface_template,
        )

    return Deployer(_factory, config), config


def _print_plan_result(plan_result: "object", *, dry_run: bool) -> None:
    """排版並印 ``PlanResult``：fanout→interface→VLAN→指令（設計 §CLI）。

    參數：
      - plan_result：``deployer.PlanResult``。
      - dry_run：True 時標示為 DRY-RUN（不寫入）。
    """
    header = "下發計畫（session {0}，VLAN {1}）{2}".format(
        plan_result.session_id,
        plan_result.vlan,
        "（DRY-RUN，未寫入）" if dry_run else "",
    )
    print(header)
    for fanout_plan in plan_result.fanouts:
        if not fanout_plan.changes:
            print("  fanout {0}：（無變更）".format(fanout_plan.fanout))
            continue
        print("  fanout {0}：".format(fanout_plan.fanout))
        for change in fanout_plan.changes:
            role_label = (
                "邊緣" if getattr(change, "role", None) == _edge_role() else "互聯"
            )
            print(
                "    介面 {0} ← VLAN {1}（{2}）".format(
                    change.interface, change.vlan, role_label
                )
            )
        for command in fanout_plan.commands:
            print("      $ {0}".format(command))


def _edge_role():
    """延遲取用 ``PortRole_E.EDGE``（drivers 於 handler 內才 import，§CLI）。

    _print_plan_result 於下發層 handler 路徑被呼叫（此時 drivers 已可 import）；
    於此延遲 import 以維持「非下發層子命令不觸發 drivers 匯入」的設計不變量。
    """
    from topo_manager.drivers.base import PortRole_E

    return PortRole_E.EDGE


def _cmd_plan(args: argparse.Namespace) -> int:
    """執行 ``plan`` 子命令：純算下發計畫並印（絕不連線，設計 §CLI）。"""
    session = _resolve_session_for_plan(args)
    deployer, _config = _build_deployer(args.credentials)
    plan_result = deployer.plan(session)
    _print_plan_result(plan_result, dry_run=True)
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    """執行 ``probe`` 子命令：唯讀連線並印 show version/vlan（設計 §CLI）。"""
    from topo_manager.drivers.config import load_fanout_config, select_fanout
    from topo_manager.drivers.ec_ssh import EcSshDriver

    config = load_fanout_config(path=args.credentials)
    fanout_name = select_fanout(config, args.fanout)
    entry = config.fanouts[fanout_name]
    driver = EcSshDriver(
        host=entry.host,
        username=entry.username,
        password=entry.password,
        device_type=entry.device_type,
        interface_template=entry.interface_template,
    )
    driver.connect()
    try:
        print("fanout {0} show version：".format(fanout_name))
        print(driver.read_show("show version"))
        print("fanout {0} show vlan：".format(fanout_name))
        print(driver.read_show("show vlan"))
    finally:
        driver.disconnect()
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    """執行 ``apply`` 子命令：下發 session（預設 dry-run，--apply 才寫入）。"""
    allocator = _build_allocator(
        args.topology, args.state, args.vlan_min, args.vlan_max
    )
    session = allocator.get_session(args.session_id)
    deployer, _config = _build_deployer(args.credentials)
    plan_result = deployer.apply(session, apply=args.apply)
    _print_plan_result(plan_result, dry_run=not args.apply)
    if args.apply:
        print("已套用（apply）至設備並通過回讀驗證。")
    return 0


def _print_verify_result(verify_result: "object", *, reapplied: bool) -> None:
    """排版並印 ``VerifyResult``（缺漏/全部達成，設計 §CLI）。"""
    if verify_result.ok:
        print(
            "verify：session {0} 全部期望設定已達成{1}。".format(
                verify_result.session_id,
                "（重套後驗證通過）" if reapplied else "",
            )
        )
        return
    print("verify：session {0} 仍有缺漏：".format(verify_result.session_id))
    for fanout_name, missing in verify_result.missing_by_fanout.items():
        items = ", ".join(
            "({0}, VLAN {1})".format(change.interface, change.vlan)
            for change in missing
        )
        print("  fanout {0}：{1}".format(fanout_name, items))


def _cmd_verify(args: argparse.Namespace) -> int:
    """執行 ``verify`` 子命令：回讀驗證；--apply 時重套缺漏（重開機偵測）。"""
    allocator = _build_allocator(
        args.topology, args.state, args.vlan_min, args.vlan_max
    )
    session = allocator.get_session(args.session_id)
    deployer, _config = _build_deployer(args.credentials)
    if args.apply:
        result = deployer.detect_and_reapply(session, apply=True)
    else:
        result = deployer.verify(session)
    _print_verify_result(result, reapplied=args.apply)
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    """執行 ``setup`` 子命令：全域 dot1q-tunnel setup（決策 8）。

    不吃 topology.yaml（全域 setup 與拓樸/session 無關）。``--fanout`` 指定時復用
    ``select_fanout`` 做存在性驗證（查無丟 ConfigError）、僅取該台；未指定時
    遍歷 config.fanouts 全部。呼叫 ``deployer.setup_tunnel`` 並印每台「將下發
    （dry-run）/ 已下發 + 回讀狀態（apply）」。
    """
    from topo_manager.drivers.config import select_fanout

    deployer, config = _build_deployer(args.credentials)
    if args.fanout is not None:
        fanout_names = [select_fanout(config, args.fanout)]
    else:
        fanout_names = list(config.fanouts.keys())

    results = deployer.setup_tunnel(fanout_names, apply=args.apply)

    header = "全域 dot1q-tunnel setup{0}".format(
        "（DRY-RUN，未寫入）" if not args.apply else ""
    )
    print(header)
    for result in results:
        if args.apply:
            if result.status is True:
                status_text = "已下發，回讀狀態：Enabled"
            elif result.status is False:
                status_text = "已下發，回讀狀態：Disabled"
            else:
                status_text = "已下發，回讀狀態：未知"
        else:
            status_text = "將下發"
        print("  fanout {0}：{1}".format(result.fanout, status_text))
        for command in result.commands:
            print("      $ {0}".format(command))
    return 0


def _cmd_interconnect(args: argparse.Namespace) -> int:
    """執行 ``interconnect`` 子命令群：延遲 import 並交給 interconnect.cli 分派。"""
    from topo_manager.interconnect.cli import dispatch

    return dispatch(args)


# 子命令名 → 處理函式的分派表（新增子命令只需在此註冊）。
_COMMANDS = {
    "summary": _cmd_summary,
    "path": _cmd_path,
    "allocate": _cmd_allocate,
    "release": _cmd_release,
    "list": _cmd_list,
    "plan": _cmd_plan,
    "probe": _cmd_probe,
    "apply": _cmd_apply,
    "verify": _cmd_verify,
    "setup": _cmd_setup,
    "interconnect": _cmd_interconnect,
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
