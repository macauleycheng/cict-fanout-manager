"""topo_manager 命令列入口（cli.py，設計 §6、FR-5）。

以標準庫 ``argparse`` 實作，不引入額外 CLI 相依。公開入口
``main(argv=None) -> int``：讀取拓樸 YAML、建 ``TopologyGraph``、印出人類可讀
摘要；若同時給了 ``--from`` / ``--to`` 則再印一條 generator→DUT 路徑。

錯誤處理（FR-5.4）：捕捉 ``TopologyError`` 家族（含 ``PathLookupError``），印
到 stderr 的單行清楚訊息並回傳 1；不讓可回報的失敗冒出未處理 traceback。非預期
例外（程式 bug）不攔截，保留原始 traceback 以利除錯。

狀態不使用全域可變變數（對齊規範精神）：所有流程資料為函式區域變數，輸出經
``print`` 寫到 stdout、錯誤經 ``print(..., file=sys.stderr)`` 寫到 stderr。

歷史（HISTORY）：
  - 0.1.0  FEAT-004 初版，建立 argparse 介面、摘要排版、路徑輸出與
           TopologyError 家族的 stderr/exit code 錯誤處理（設計 §6）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Optional

from topo_manager.errors import TopologyError
from topo_manager.graph import TopologyGraph
from topo_manager.loader import load_topology


def _build_parser() -> argparse.ArgumentParser:
    """建立 CLI 的 argparse parser（設計 §6 介面）。

    回傳：
      - 已設定好位置參數 ``topology`` 與選項 ``--from`` / ``--to`` 的 parser。
    """
    parser = argparse.ArgumentParser(
        prog="topo-manager",
        description="拓樸管理工具：載入拓樸 YAML、印摘要，並可查 generator→DUT 路徑。",
    )
    parser.add_argument(
        "topology",
        metavar="topology.yaml",
        help="拓樸 YAML 檔路徑（必填）。",
    )
    # dest 用 from_port，避免與 Python 保留字 from 衝突。
    parser.add_argument(
        "--from",
        dest="from_port",
        metavar="GEN_PORT",
        default=None,
        help="generator 端 port，可裸名（如 ixia-1/1）或 dev:port。",
    )
    parser.add_argument(
        "--to",
        dest="to_port",
        metavar="DUT_PORT",
        default=None,
        help="DUT 端點，一律 dev:port 形式（如 dut-A1:Eth0）。",
    )
    return parser


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


def main(argv: Optional[List[str]] = None) -> int:
    """CLI 主入口（設計 §6）。

    參數：
      - argv：命令列參數清單（不含程式名）；None 時由 argparse 取
        ``sys.argv[1:]``。

    回傳：
      - int exit code：成功 0；捕捉到 ``TopologyError`` 家族（含
        ``PathLookupError``）印 stderr 單行訊息後回傳 1。

    行為：
      1. 解析參數：``--from`` / ``--to`` 須成對；只給其一 → argparse 參數錯誤
         （SystemExit code 2）；皆不給 → 只印摘要。
      2. ``load_topology(path)`` → 建 ``TopologyGraph`` → 印摘要。
      3. 若給了成對端點 → 印 find_path 結果。

    例外：
      - 非預期例外（程式 bug）不攔截，保留原始 traceback。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --from / --to 須成對（§6）：只給其一視為參數錯誤（argparse exit code 2）。
    if (args.from_port is None) != (args.to_port is None):
        parser.error("--from 與 --to 須成對提供（兩者皆給才做路徑查找，皆不給則只印摘要）")

    try:
        topology = load_topology(args.topology)
        graph = TopologyGraph(topology)

        summary = graph.summary()
        print(_format_summary(topology.site, args.topology, summary))

        if args.from_port is not None and args.to_port is not None:
            path = graph.find_path(args.from_port, args.to_port)
            print(_format_path(args.from_port, args.to_port, path))
    except TopologyError as exc:
        # FR-5.4：可回報的失敗印單行清楚訊息到 stderr 並回傳 1；
        # 不讓 traceback 冒出。訊息可能多行（如驗證逐條），原樣輸出。
        print("錯誤：{0}".format(exc), file=sys.stderr)
        return 1

    return 0
