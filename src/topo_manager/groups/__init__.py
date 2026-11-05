"""topo_manager 群組子套件（groups/，群組設計 Part 2）。

本子套件提供「群組」功能的後端核心：把多個既有單對 traffic session（``s-NNNN``）
聚合到使用者命名的 group（如 ``T1``）之下，並以 ATLAS version 2 格式輸出。群組
純做標籤聚合與 CRUD，不碰裝置、VLAN、下發（push）。

模組組成：
  - ``models``：``GroupEntry`` / ``GroupLedger`` 持久化模型。
  - ``store``：``GroupStore``（子類化 ``JsonSessionStore``）。
  - ``registry``：``GroupRegistry`` 業務層（CRUD + 成員維護）。
  - ``atlas``：``build_atlas_v2`` ATLAS v2 builder（純函式，無 I/O）。

NOTES：
  - 刻意鏡像 interconnect 子套件（models/store），並子類化 ``JsonSessionStore``
    重用原子寫入與跨行程鎖；帳本以 version+kind 標記與其他帳本隔離。

歷史（HISTORY）：
  - 0.7.0  群組功能初版：建立 groups 子套件（models/store/registry/atlas）。

Copyright (c) 2026 topo-manager。
"""
