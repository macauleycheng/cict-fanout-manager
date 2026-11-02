"""topo_manager 模組執行入口（__main__.py，設計 §6）。

讓 ``python -m topo_manager ...`` 可直接執行：轉呼 ``cli.main`` 並以其回傳的
exit code 結束行程。

歷史（HISTORY）：
  - 0.1.0  FEAT-004 初版，建立 python -m topo_manager 執行入口（設計 §6）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import sys

from topo_manager.cli import main

if __name__ == "__main__":
    sys.exit(main())
