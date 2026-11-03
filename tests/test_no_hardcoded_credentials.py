"""守門測試：tracked 原始碼不得寫死真實設備憑證（設計 §11.1、finding #10）。

對 ``git ls-files`` 列出的 tracked ``.py``/``.toml``/``.json`` 檔（排除
``*.example.json`` placeholder），逐檔 grep 真實 live 設備的 host 位址與帳密
字面值；一旦命中即 assert 失敗，防止 live 憑證（僅能由 env 餵給 live 測試）
意外進入版控。

歷史（HISTORY）：
  - 0.4.0  FEAT-001 初版。
  - 0.4.0  FEAT-003：本掃描檔本身必然含被掃字面（``_FORBIDDEN_LITERALS``），
           故將本檔自掃描清單排除，避免守門測試誤判自身。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

# tests/ → 專案根為其父目錄。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 不得出現於任何 tracked 原始碼的 live 設備憑證字面（僅由 env 餵給 live 測試）。
_FORBIDDEN_LITERALS = ["192.168.42.237", "admin"]

# 掃描的副檔名。
_SCANNED_SUFFIXES = {".py", ".toml", ".json"}


def _tracked_files() -> List[Path]:
    """以 git ls-files 列出 tracked 檔（相對專案根），回傳絕對路徑清單。"""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        _PROJECT_ROOT / line
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def test_no_hardcoded_live_credentials() -> None:
    """tracked .py/.toml/.json（排除 *.example.json）不含 live 憑證字面。"""
    offenders: List[str] = []

    # 本掃描檔自身必然含 _FORBIDDEN_LITERALS 字面，排除以免誤判。
    this_file = Path(__file__).resolve()

    for path in _tracked_files():
        if path.suffix not in _SCANNED_SUFFIXES:
            continue
        if path.name.endswith(".example.json"):
            continue
        if path.resolve() == this_file:
            continue
        if not path.exists():
            # tracked 但工作區暫缺（罕見）：跳過。
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for literal in _FORBIDDEN_LITERALS:
            if literal in text:
                offenders.append(
                    "{0}：命中禁用字面 '{1}'".format(
                        path.relative_to(_PROJECT_ROOT), literal
                    )
                )

    assert not offenders, "tracked 原始碼含 live 憑證字面：\n  " + "\n  ".join(
        offenders
    )
