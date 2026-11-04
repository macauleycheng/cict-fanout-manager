"""topo_manager 互聯 fanout（interconnect）子套件。

提供 DUT 與 DUT 之間經由獨立互聯 fanout 連線的驗證、配對、分配與持久化。
本檔刻意不 import 任何子模組，避免與 ``loader`` 互相 import 造成循環相依；
使用者請直接 import 子模組（如 ``topo_manager.interconnect.layout``）。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版，建立子套件。

Copyright (c) 2026 topo-manager。
"""
