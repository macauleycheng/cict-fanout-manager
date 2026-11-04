"""互聯 fanout 子命令群（interconnect/cli.py，互聯設計 §8）。

MODULE NAME：
  topo_manager.interconnect.cli

PURPOSE：
  實作 ``topo-manager interconnect <子命令>``：

  - ``summary``：印互聯小圖摘要（fanout 數、組數、DUT 數、link 數、各 fanout
    已接 port、各組 d0..d3）。
  - ``allocate``：以 profile（``--duts`` + ``--profile``）或明確 port 對
    （``--pair``）分配並持久化。
  - ``release``：預設 dry-run；``--apply`` 兩階段釋放；``--ledger-only`` 只改帳本。
  - ``list``：列 active session；``--raw`` 唯讀 dump 原始帳本（不需拓樸）。
  - ``plan``：純算指令清單（不連線、不入帳）。
  - ``park``（別名 ``setup``）：把互聯 port 還原為 parking，預設 dry-run。
  - ``apply``：啟用一個 session，預設 dry-run。
  - ``verify``：唯讀回讀，有 problems 回 exit 1。

NOTES：
  - 本模組由 ``cli.py`` 的 ``_cmd_interconnect`` 延遲 import；drivers 相關模組
    再於各 handler 內延遲 import，故未安裝 netmiko 時 summary／allocate／
    release --ledger-only／list／plan 皆可使用。
  - 所有失敗皆為 ``TopologyError`` 家族，由 ``cli.main`` 的 ``except
    TopologyError`` 統一印「錯誤：」並回 1，不冒 traceback。argparse 的互斥與
    必填錯誤維持 exit 2。
  - dry-run 為預設；只有 ``--apply`` 才連線寫設備。dry-run 與 plan 找不到連線
    設定時不報錯，改以空設定計算（計畫中標示未設定連線）。
  - 狀態不使用全域可變變數；``_make_driver_factory`` 為可替換的模組函式，測試
    以它注入假設備。

HISTORY：
  - 0.1.0  互聯 fanout 初版：summary/allocate/release/list/plan/park/apply/verify。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import argparse
import json
from typing import Callable, Dict, List, Optional, Tuple

from topo_manager.errors import ConfigError, InterconnectRequestError
from topo_manager.interconnect.allocator import InterconnectAllocator
from topo_manager.interconnect.layout import IcGraph, build_ic_graph
from topo_manager.interconnect.models import IcSession
from topo_manager.interconnect.store import IcSessionStore
from topo_manager.loader import load_topology

# 預設值（與打流側 CLI 一致的 VLAN 池；互聯帳本另開檔）。
_DEFAULT_VLAN_MIN = 100
_DEFAULT_VLAN_MAX = 3000
_DEFAULT_STATE_PATH = ".topo-state/interconnect.json"


# ======================================================================
# parser 註冊
# ======================================================================
def _add_topology_arg(parser: argparse.ArgumentParser, required: bool = True) -> None:
    """加上位置參數 ``topology.yaml``（required=False 時為 nargs='?'）。"""
    if required:
        parser.add_argument("topology", metavar="topology.yaml", help="拓樸 YAML 檔路徑（必填）。")
    else:
        parser.add_argument(
            "topology",
            metavar="topology.yaml",
            nargs="?",
            default=None,
            help="拓樸 YAML 檔路徑（--raw 時可省略）。",
        )


def _add_state_arg(parser: argparse.ArgumentParser) -> None:
    """加上 ``--state`` 互聯帳本路徑。"""
    parser.add_argument(
        "--state",
        dest="state",
        metavar="STATE_JSON",
        default=_DEFAULT_STATE_PATH,
        help="互聯帳本路徑（預設 {0}，與打流側帳本分開）。".format(_DEFAULT_STATE_PATH),
    )


def _add_vlan_range_args(parser: argparse.ArgumentParser) -> None:
    """加上 ``--vlan-min`` / ``--vlan-max``。"""
    parser.add_argument(
        "--vlan-min",
        dest="vlan_min",
        metavar="VLAN_MIN",
        type=int,
        default=_DEFAULT_VLAN_MIN,
        help="VLAN 池下界（預設 {0}，須落在 2..4094）。".format(_DEFAULT_VLAN_MIN),
    )
    parser.add_argument(
        "--vlan-max",
        dest="vlan_max",
        metavar="VLAN_MAX",
        type=int,
        default=_DEFAULT_VLAN_MAX,
        help="VLAN 池上界（預設 {0}，須落在 2..4094）。".format(_DEFAULT_VLAN_MAX),
    )


def _add_device_args(parser: argparse.ArgumentParser) -> None:
    """加上 ``--credentials`` 連線設定檔與 ``--fanout`` 選台。"""
    parser.add_argument(
        "--credentials",
        dest="credentials",
        metavar="CREDENTIALS_JSON",
        default=None,
        help="fanout 連線設定檔（JSON，以互聯 fanout 名為 key）；未指定走預設檔名與 env。",
    )
    parser.add_argument(
        "--fanout",
        dest="fanout",
        metavar="FANOUT_NAME",
        default=None,
        help="只處理指定的互聯 fanout 裝置名。",
    )


def _add_apply_flag(parser: argparse.ArgumentParser) -> None:
    """加上 ``--apply``（預設 dry-run）。"""
    parser.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="實際連線並寫入設備（預設 dry-run，不連線）。",
    )


def _add_request_args(parser: argparse.ArgumentParser, required: bool) -> None:
    """加上 ``--group`` / ``--duts`` / ``--profile`` / ``--pair``（allocate 與 plan 共用）。

    ``--duts`` 與 ``--pair`` 以 argparse 互斥群組處理；required=True 時二擇一必填。
    """
    parser.add_argument(
        "--group",
        dest="group",
        metavar="GROUP",
        required=required,
        default=None,
        help="互聯組名（如 A-g1）。",
    )
    source = parser.add_mutually_exclusive_group(required=required)
    source.add_argument(
        "--duts",
        dest="duts",
        metavar="DUT1,DUT2[,...]",
        default=None,
        help="選定的 DUT（逗號分隔，2..4 台，順序即選定順序）；需搭配 --profile。",
    )
    source.add_argument(
        "--pair",
        dest="pairs",
        metavar="DUT_A:PORT=DUT_B:PORT",
        action="append",
        default=None,
        help="明確 port 對（可重複），如 dut-A1:p3=dut-A2:p3；PORT 為 il_port 名或 p0..p3。",
    )
    parser.add_argument(
        "--profile",
        dest="profile",
        metavar="PROFILE",
        default=None,
        help="連線方式：mesh | line | ring（與 --duts 並用）。",
    )


def register_interconnect_parsers(parser: argparse.ArgumentParser) -> None:
    """在 ``interconnect`` 子命令 parser 下註冊巢狀子命令（dest='ic_command'）。"""
    subparsers = parser.add_subparsers(dest="ic_command", metavar="ic_command")
    subparsers.required = True

    summary_parser = subparsers.add_parser("summary", help="印互聯小圖摘要。")
    _add_topology_arg(summary_parser)

    allocate_parser = subparsers.add_parser(
        "allocate", help="分配一個互聯 session（profile 或明確 port 對）並持久化。"
    )
    _add_topology_arg(allocate_parser)
    _add_request_args(allocate_parser, required=True)
    _add_state_arg(allocate_parser)
    _add_vlan_range_args(allocate_parser)

    release_parser = subparsers.add_parser(
        "release", help="釋放互聯 session（預設 dry-run；--apply 兩階段、--ledger-only 只改帳本）。"
    )
    _add_topology_arg(release_parser)
    release_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        required=True,
        help="欲釋放的 session 識別碼（如 ic-0001）。",
    )
    mode = release_parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="連線 park 並清 VLAN 後才自帳本移除（失敗停在 releasing，可重跑）。",
    )
    mode.add_argument(
        "--ledger-only",
        dest="ledger_only",
        action="store_true",
        help="不連線，只自帳本移除（設備殘留需另行 park）。",
    )
    _add_state_arg(release_parser)
    _add_vlan_range_args(release_parser)
    _add_device_args(release_parser)

    list_parser = subparsers.add_parser(
        "list", help="列出 active 互聯 session（--raw 唯讀 dump 原始帳本）。"
    )
    _add_topology_arg(list_parser, required=False)
    _add_state_arg(list_parser)
    _add_vlan_range_args(list_parser)
    list_parser.add_argument(
        "--raw",
        dest="raw",
        action="store_true",
        help="唯讀 dump 原始帳本 JSON（不需拓樸、不做一致性檢查）。",
    )

    plan_parser = subparsers.add_parser(
        "plan", help="純算指令清單（不連線、不入帳、不佔池）。"
    )
    _add_topology_arg(plan_parser)
    plan_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        default=None,
        help="既有 session 的啟用計畫（加 --release 則為釋放計畫）。",
    )
    plan_parser.add_argument(
        "--release",
        dest="release",
        action="store_true",
        help="與 --session 並用：印釋放計畫。",
    )
    plan_parser.add_argument(
        "--park",
        dest="park",
        action="store_true",
        help="印 park 靜態 baseline（可搭配 --fanout / --ports）。",
    )
    plan_parser.add_argument(
        "--ports",
        dest="ports",
        metavar="1,2,3",
        default=None,
        help="縮小 park 範圍的 fanout port 編號（逗號分隔）。",
    )
    _add_request_args(plan_parser, required=False)
    _add_state_arg(plan_parser)
    _add_vlan_range_args(plan_parser)
    _add_device_args(plan_parser)

    park_parser = subparsers.add_parser(
        "park",
        aliases=["setup"],
        help="把互聯區段 port 還原為 parking（預設 dry-run，--apply 才寫入）。",
    )
    _add_topology_arg(park_parser)
    park_parser.add_argument(
        "--ports",
        dest="ports",
        metavar="1,2,3",
        default=None,
        help="縮小範圍的 fanout port 編號（逗號分隔）。",
    )
    _add_state_arg(park_parser)
    _add_vlan_range_args(park_parser)
    _add_device_args(park_parser)
    _add_apply_flag(park_parser)

    apply_parser = subparsers.add_parser(
        "apply", help="啟用一個互聯 session（預設 dry-run，--apply 才寫入並讀回）。"
    )
    _add_topology_arg(apply_parser)
    apply_parser.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        required=True,
        help="目標 session 識別碼（如 ic-0001）。",
    )
    _add_state_arg(apply_parser)
    _add_vlan_range_args(apply_parser)
    _add_device_args(apply_parser)
    _add_apply_flag(apply_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="唯讀回讀查核；有 problems 回 exit 1。"
    )
    _add_topology_arg(verify_parser)
    target = verify_parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        default=None,
        help="查核該 session 處於啟用狀態。",
    )
    target.add_argument(
        "--parked",
        dest="parked",
        action="store_true",
        help="查核所有未被持有的互聯 port 皆為 parking。",
    )
    _add_state_arg(verify_parser)
    _add_vlan_range_args(verify_parser)
    _add_device_args(verify_parser)


# ======================================================================
# 共用輔助
# ======================================================================
def _build_allocator(args: argparse.Namespace) -> InterconnectAllocator:
    """載入拓樸、建互聯帳本與 allocator；VLAN 範圍非法轉 ConfigError。"""
    topology = load_topology(args.topology)
    store = IcSessionStore(args.state)
    try:
        return InterconnectAllocator(
            topology, store, vlan_min=args.vlan_min, vlan_max=args.vlan_max
        )
    except ValueError as exc:
        raise ConfigError("VLAN 池設定非法：{0}".format(exc)) from exc


def _make_driver_factory(force_access_mode: bool) -> Callable[[str, object], object]:
    """回傳 ``(fanout 名, FanoutEntry) -> EcSshInterconnectDriver`` 工廠（延遲 import）。

    netmiko 仍於 driver 的 ``connect()`` 內才 import。測試以替換本函式注入假設備。
    """
    from topo_manager.drivers.ec_ssh_ic import EcSshInterconnectDriver

    def _factory(name: str, entry: object) -> object:
        """以單台連線設定建立互聯 driver。"""
        return EcSshInterconnectDriver(
            host=entry.host,
            username=entry.username,
            password=entry.password,
            device_type=entry.device_type,
            interface_template=entry.interface_template,
            force_access_mode=force_access_mode,
        )

    return _factory


def _load_config(credentials: Optional[str], strict: bool) -> object:
    """載入連線設定；strict=False（dry-run／plan）時失敗改回空設定。"""
    from topo_manager.drivers.config import FanoutConfig, load_fanout_config

    try:
        return load_fanout_config(path=credentials)
    except ConfigError:
        if strict:
            raise
        return FanoutConfig(fanouts={})


def _build_deployer(allocator: InterconnectAllocator, args: argparse.Namespace, strict: bool):
    """建 InterconnectDeployer（drivers 延遲 import）。"""
    from topo_manager.drivers.ic_deployer import InterconnectDeployer

    config = _load_config(args.credentials, strict)
    return InterconnectDeployer(
        _make_driver_factory(True),
        config,
        store=IcSessionStore(args.state),
        allocator=allocator,
        vlan_min=args.vlan_min,
        vlan_max=args.vlan_max,
        force_access_mode=True,
    )


def _parse_ports(text: Optional[str]) -> Optional[List[int]]:
    """解析 ``--ports 1,2,3``；非整數丟 ConfigError。"""
    if text is None:
        return None
    ports: List[int] = []
    for token in text.split(","):
        token = token.strip()
        if not (token.isascii() and token.isdigit()):
            raise ConfigError("--ports 須為逗號分隔的整數（實得 '{0}'）".format(text))
        ports.append(int(token))
    return ports


def _fanout_filter(args: argparse.Namespace) -> Optional[List[str]]:
    """``--fanout`` 轉為清單；未指定回 None。"""
    return [args.fanout] if args.fanout else None


def _parse_duts(text: str) -> List[str]:
    """解析 ``--duts a,b,c``。"""
    return [item.strip() for item in text.split(",") if item.strip()]


def _parse_pairs(raw_pairs: List[str]) -> List[Tuple[str, str]]:
    """解析 ``--pair a:p3=b:p3``；格式錯丟 InterconnectRequestError。"""
    pairs: List[Tuple[str, str]] = []
    for raw in raw_pairs:
        left, sep, right = raw.partition("=")
        if sep != "=" or ":" not in left or ":" not in right or not left or not right:
            raise InterconnectRequestError(
                "--pair '{0}' 格式錯誤（須為 dutA:port=dutB:port）".format(raw)
            )
        pairs.append((left.strip(), right.strip()))
    return pairs


def _check_request_mode(args: argparse.Namespace) -> None:
    """profile 模式與明確 port 對模式的二次檢查（互斥、二擇一必填）。"""
    has_duts = args.duts is not None
    has_pairs = args.pairs is not None
    if has_duts == has_pairs:
        raise InterconnectRequestError("請擇一提供 --duts（搭配 --profile）或 --pair")
    if has_duts and args.profile is None:
        raise InterconnectRequestError("--duts 需搭配 --profile（mesh | line | ring）")
    if has_pairs and args.profile is not None:
        raise InterconnectRequestError("--pair（明確 port 對）不可與 --profile 並用")


# ======================================================================
# 排版
# ======================================================================
def _port_label(graph: IcGraph, group_name: str, dut: str, il_port: str, node: str) -> str:
    """``d0:p0(Eth8)@ic-1:1`` 形式的端點標籤；找不到組內名次時退回 DUT 名。"""
    group = graph.groups.get(group_name)
    if group is not None:
        for member in group.duts:
            if member.name == dut:
                for port in member.ports:
                    if port.il_port == il_port:
                        return "d{0}:p{1}({2})@{3}".format(
                            member.index, port.p_index, il_port, node
                        )
    return "{0}:{1}@{2}".format(dut, il_port, node)


def _format_session(graph: IcGraph, session: IcSession) -> str:
    """把 ``IcSession`` 排版成多行區塊。"""
    group = graph.groups.get(session.group)
    lines: List[str] = [
        "互聯 session {0}".format(session.session_id),
        "  組：{0}（fanout {1}，phase {2}）".format(
            session.group, session.fanout, session.phase
        ),
        "  profile：{0}".format(session.profile),
    ]
    if group is not None:
        lines.append(
            "  DUT：{0}".format(", ".join("d{0}={1}".format(d.index, d.name) for d in group.duts))
        )
    lines.append("  選定順序：{0}".format(", ".join(session.duts)))
    lines.append("  link（{0} 條）：".format(len(session.links)))
    for link in session.links:
        lines.append(
            "    {0} <-> {1}  VLAN {2}".format(
                _port_label(graph, session.group, link.a_dut, link.a_port, link.a_node),
                _port_label(graph, session.group, link.b_dut, link.b_port, link.b_node),
                link.vlan,
            )
        )
    return "\n".join(lines)


def _format_summary(topology_path: str, summary: dict) -> str:
    """把 ``IcGraph.summary()`` 排版成人類可讀區塊。"""
    lines: List[str] = [
        "互聯摘要：{0}".format(topology_path),
        "  互聯 fanout 台數：{0}".format(summary["fanout_count"]),
        "  組數：{0}    DUT 數：{1}    link 數：{2}".format(
            summary["group_count"], summary["dut_count"], summary["link_count"]
        ),
        "  各互聯 fanout 已接 port / 總 port：",
    ]
    for name in sorted(summary["fanouts"]):
        info = summary["fanouts"][name]
        lines.append("    {0}：{1}/{2}".format(name, info["used"], info["total"]))
    lines.append("  各組：")
    for group in summary["groups"]:
        lines.append(
            "    {0}（rack {1}，fanout {2}）：{3}".format(
                group["name"],
                group["rack"],
                group["fanout"],
                ", ".join("d{0}={1}".format(i, n) for i, n in enumerate(group["duts"])),
            )
        )
    return "\n".join(lines)


def _print_fanout_plans(plan_fanouts: List[object]) -> None:
    """印每台互聯 fanout 的指令（``$`` 縮排）。"""
    for fanout_plan in plan_fanouts:
        state = "已設定連線" if fanout_plan.configured else "未設定連線"
        if not fanout_plan.commands:
            print("  fanout {0}（{1}）：（無指令）".format(fanout_plan.fanout, state))
            continue
        print("  fanout {0}（{1}）：".format(fanout_plan.fanout, state))
        for command in fanout_plan.commands:
            print("      $ {0}".format(command))


def _print_plan(plan: object, dry_run: bool) -> None:
    """印 ``IcPlanResult``。"""
    print(
        "互聯下發計畫（{0}，動作 {1}，VLAN {2}）{3}".format(
            plan.session_id,
            plan.action,
            ", ".join(str(v) for v in plan.vlans) if plan.vlans else "（無）",
            "（DRY-RUN，未連線、未寫入）" if dry_run else "",
        )
    )
    _print_fanout_plans(plan.fanouts)
    for note in plan.notes:
        print("  備註：{0}".format(note))


def _print_park(result: object) -> None:
    """印 ``IcParkResult``。"""
    print(
        "互聯 park{0}".format("（已寫入）" if result.applied else "（DRY-RUN，未連線、未寫入）")
    )
    _print_fanout_plans(result.fanouts)
    for fanout in sorted(result.parked):
        print("  已 park：{0} port {1}".format(fanout, ", ".join(str(n) for n in result.parked[fanout])))
    for fanout, port, owner in result.skipped:
        print("  跳過（被 {0} 持有）：{1}:{2}".format(owner, fanout, port))
    for warning in result.warnings:
        print("  警告：{0}".format(warning))


def _print_problems(result: object) -> int:
    """印 ``IcVerifyResult``；有 problems 回 1，否則 0。"""
    if result.ok:
        print("verify：{0} 設備狀態與期望相符。".format(result.target))
        return 0
    print("verify：{0} 發現 {1} 項問題：".format(result.target, len(result.problems)))
    for problem in result.problems:
        print("  - {0}".format(problem))
    return 1


# ======================================================================
# handlers
# ======================================================================
def _cmd_summary(args: argparse.Namespace) -> int:
    """``interconnect summary``：印互聯小圖摘要。"""
    topology = load_topology(args.topology)
    graph = build_ic_graph(topology)
    print(_format_summary(args.topology, graph.summary()))
    return 0


def _cmd_allocate(args: argparse.Namespace) -> int:
    """``interconnect allocate``：分配並持久化一個互聯 session。"""
    _check_request_mode(args)
    allocator = _build_allocator(args)
    if args.duts is not None:
        session = allocator.allocate_profile(args.group, _parse_duts(args.duts), args.profile)
    else:
        session = allocator.allocate_pairs(args.group, _parse_pairs(args.pairs))
    print("已分配：")
    print(_format_session(allocator.graph, session))
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    """``interconnect release``：dry-run／``--apply`` 兩階段／``--ledger-only``。"""
    allocator = _build_allocator(args)
    session = allocator.get_session(args.session_id)
    if args.ledger_only:
        released = allocator.release_now(args.session_id)
        print("已自帳本釋放：")
        print(_format_session(allocator.graph, released))
        print("警告：設備未 park，殘留需以 'interconnect park --apply' 清理。")
        return 0
    deployer = _build_deployer(allocator, args, strict=args.apply)
    plan = deployer.release(session, apply=args.apply)
    _print_plan(plan, dry_run=not args.apply)
    if args.apply:
        print("已釋放：port 已 park、VLAN 定義已清除、session 已自帳本移除。")
    else:
        print("（dry-run：不會改帳本；加 --apply 才標記 releasing 並下發，--ledger-only 只改帳本）")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """``interconnect list``：列 active session；``--raw`` 唯讀 dump 原始帳本。"""
    if args.raw:
        print(json.dumps(IcSessionStore(args.state).load_raw(), ensure_ascii=False, indent=2))
        return 0
    if args.topology is None:
        raise ConfigError("interconnect list（非 --raw 模式）需提供 topology.yaml")
    allocator = _build_allocator(args)
    sessions = allocator.list_sessions()
    if not sessions:
        print("目前無 active 互聯 session")
        return 0
    print("目前 active 互聯 session 共 {0} 筆：".format(len(sessions)))
    for session in sessions:
        print()
        print(_format_session(allocator.graph, session))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    """``interconnect plan``：純算指令清單，不連線、不入帳。"""
    has_request = args.group is not None or args.duts is not None or args.pairs is not None
    modes = [args.session_id is not None, args.park, has_request]
    if sum(1 for item in modes if item) != 1:
        raise ConfigError(
            "plan 請擇一：--session ID [--release]、--park [--fanout F] [--ports ...]、"
            "或 --group G 搭配 --duts/--profile 或 --pair"
        )
    if args.release and args.session_id is None:
        raise ConfigError("--release 須與 --session 並用")
    allocator = _build_allocator(args)
    deployer = _build_deployer(allocator, args, strict=False)
    if args.park:
        plan = deployer.plan_park(
            allocator.graph,
            _fanout_filter(args),
            _parse_ports(args.ports),
            owned=allocator.owned_nodes(),
        )
        _print_plan(plan, dry_run=True)
        return 0
    if args.session_id is not None:
        session = allocator.get_session(args.session_id)
        plan = deployer.plan_release(session) if args.release else deployer.plan_enable(session)
    else:
        if args.group is None:
            raise InterconnectRequestError("預覽需要 --group")
        _check_request_mode(args)
        if args.duts is not None:
            session = allocator.preview_profile(args.group, _parse_duts(args.duts), args.profile)
        else:
            session = allocator.preview_pairs(args.group, _parse_pairs(args.pairs))
        print("預覽（不佔池、不入帳；VLAN 為示意值）：")
        print(_format_session(allocator.graph, session))
        plan = deployer.plan_enable(session)
    _print_plan(plan, dry_run=True)
    return 0


def _cmd_park(args: argparse.Namespace) -> int:
    """``interconnect park``（別名 ``setup``）：還原為 parking，預設 dry-run。"""
    allocator = _build_allocator(args)
    deployer = _build_deployer(allocator, args, strict=args.apply)
    result = deployer.park(
        allocator.graph,
        allocator.owned_nodes(),
        fanouts=_fanout_filter(args),
        ports=_parse_ports(args.ports),
        apply=args.apply,
    )
    _print_park(result)
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    """``interconnect apply``：啟用 session，預設 dry-run。"""
    allocator = _build_allocator(args)
    session = allocator.get_session(args.session_id)
    deployer = _build_deployer(allocator, args, strict=args.apply)
    plan = deployer.apply(session, apply=args.apply)
    _print_plan(plan, dry_run=not args.apply)
    if args.apply:
        print("已套用至設備並通過回讀驗證。")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """``interconnect verify``：唯讀回讀；有 problems 回 1。"""
    allocator = _build_allocator(args)
    deployer = _build_deployer(allocator, args, strict=True)
    if args.session_id is not None:
        result = deployer.verify_session(allocator.get_session(args.session_id))
    else:
        result = deployer.verify_parked(
            allocator.graph, allocator.owned_nodes(), fanouts=_fanout_filter(args)
        )
    return _print_problems(result)


# 子命令名 → handler（含別名 setup）。
_HANDLERS: Dict[str, Callable[[argparse.Namespace], int]] = {
    "summary": _cmd_summary,
    "allocate": _cmd_allocate,
    "release": _cmd_release,
    "list": _cmd_list,
    "plan": _cmd_plan,
    "park": _cmd_park,
    "setup": _cmd_park,
    "apply": _cmd_apply,
    "verify": _cmd_verify,
}


def dispatch(args: argparse.Namespace) -> int:
    """依 ``args.ic_command`` 分派到對應 handler，回傳 exit code。"""
    return _HANDLERS[args.ic_command](args)
