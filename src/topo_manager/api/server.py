"""topo_manager REST API 的 uvicorn 啟動入口（Phase 3，設計 §6.3、§11）。

``run()`` 封裝 ``uvicorn.run("topo_manager.api:create_app", factory=True, ...)``
作為 ``topo-manager-api`` 的 ``[project.scripts]`` 入口（設計 §3 單一歸屬：
``server.py`` 只放 ``run()``，不定義 ``create_app``）。host/port 由環境變數
``TOPO_MANAGER_HOST``（預設 ``0.0.0.0``）/``TOPO_MANAGER_PORT``（預設 ``8000``）
覆寫。

啟動期 fail-fast 的 ``TopologyError``（含 ``ConfigError``）於此捕捉，印單行
stderr 訊息並以非零 exit code 結束（不冒 traceback，設計 §4.3），語意與 CLI
一致。

歷史（HISTORY）：
  - 0.4.0  Phase 3 初版，建立 run()（uvicorn factory 入口 + fail-fast
           stderr/exit，設計 §6.3）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import os
import sys

import uvicorn

from topo_manager.errors import ConfigError, TopologyError

# host/port 環境變數與預設（設計 §6.3）。
_ENV_HOST = "TOPO_MANAGER_HOST"
_ENV_PORT = "TOPO_MANAGER_PORT"
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8000


def run() -> None:
    """啟動 uvicorn 服務 topo-manager API（``topo-manager-api`` 入口，設計 §6.3）。

    以 ``factory=True`` 指向 ``topo_manager.api:create_app``（啟動期由
    ``Settings.from_env()`` 建 app）。host/port 由 ``TOPO_MANAGER_HOST`` /
    ``TOPO_MANAGER_PORT`` 覆寫；port 非整數時以 ``ConfigError`` 走 fail-fast。
    啟動期 ``TopologyError`` 捕捉後印單行 stderr、以非零 exit code 結束。

    例外：
      - SystemExit：啟動期 fail-fast（印 stderr 單行後以 exit code 1 結束）。
    """
    host = os.environ.get(_ENV_HOST, _DEFAULT_HOST)
    try:
        port = _resolve_port()
        uvicorn.run(
            "topo_manager.api:create_app",
            factory=True,
            host=host,
            port=port,
        )
    except TopologyError as exc:
        # 啟動期 fail-fast：單行 stderr + 非零 exit（不冒 traceback，設計 §4.3）。
        print("錯誤：{0}".format(exc), file=sys.stderr)
        sys.exit(1)


def _resolve_port() -> int:
    """解析 ``TOPO_MANAGER_PORT``（預設 8000）；非整數丟 ``ConfigError``。

    回傳：
      - 解析後的 port 整數（未設定時回預設 8000）。

    例外：
      - ConfigError：環境變數存在但非合法整數（走 fail-fast，無 traceback）。
    """
    raw = os.environ.get(_ENV_PORT)
    if raw is None:
        return _DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            "環境變數 {0} 非整數：{1}".format(_ENV_PORT, raw)
        ) from exc
