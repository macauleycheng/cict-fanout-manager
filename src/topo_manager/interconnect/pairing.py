"""topo_manager 互聯配對表與 profile 展開（interconnect/pairing.py，互聯設計 §3）。

純函式模組（無 I/O、無狀態）：
  - ``pair_port_indices(i, j)``：組內 d_i 與 d_j 互連時各自使用的 p 序。
    規則：DUT i 對鄰居 j 所用 port 序 = j 在 i 的「其他組員（升冪）」中的名次。
    固定表：d0-d1 p0/p0、d0-d2 p1/p0、d0-d3 p2/p0、d1-d2 p1/p1、d1-d3 p2/p1、
    d2-d3 p2/p2；p3 不在表內，保留給明確 port 對。
  - ``expand_profile(profile, n)``：mesh/line/ring 展開成位置對。

NOTES：
  - 配對只由兩台 DUT 的組內名次決定，與選了哪幾台、選定順序無關。
  - ``expand_profile`` 輸出的每個位置對 (a, b) 恆滿足 a < b（A 端為選定順序中
    靠前者）；ring 的收尾邊以 (0, n-1) 表示。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版：pair_port_indices、expand_profile。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

from typing import List, Tuple

from topo_manager.errors import InterconnectRequestError

# 允許的 profile 名稱。
IC_PROFILES = ("mesh", "line", "ring")
# 每組 DUT 台數上限/下限（profile 展開用）。
_MIN_DUTS = 2
_MAX_DUTS = 4


def pair_port_indices(i: int, j: int) -> Tuple[int, int]:
    """回傳 (i 端 p 序, j 端 p 序)；i != j 且皆在 0..3，否則丟 InterconnectRequestError。"""
    if i == j or not (0 <= i <= 3) or not (0 <= j <= 3):
        raise InterconnectRequestError(
            "組內 DUT 名次須為 0..3 且兩者不同（實得 {0}, {1}）".format(i, j)
        )
    if i > j:
        j_port, i_port = pair_port_indices(j, i)
        return i_port, j_port
    return (j - 1, i)


def expand_profile(profile: str, n: int) -> List[Tuple[int, int]]:
    """把 profile 展開成位置對清單（位置為選定順序 0..n-1，皆 a < b）。

    參數：
      - profile：mesh / line / ring。
      - n：DUT 台數，2..4。
    例外：
      - InterconnectRequestError：profile 未知、n 超出 2..4、ring 且 n==2。
    """
    if profile not in IC_PROFILES:
        raise InterconnectRequestError(
            "未知 profile '{0}'（允許：{1}）".format(profile, ", ".join(IC_PROFILES))
        )
    if not (_MIN_DUTS <= n <= _MAX_DUTS):
        raise InterconnectRequestError(
            "profile '{0}' 需要 {1}..{2} 台 DUT（實得 {3} 台）".format(
                profile, _MIN_DUTS, _MAX_DUTS, n
            )
        )
    if profile == "mesh":
        return [(a, b) for a in range(n) for b in range(a + 1, n)]
    pairs = [(k, k + 1) for k in range(n - 1)]
    if profile == "ring":
        if n == 2:
            raise InterconnectRequestError("ring 至少需要 3 台 DUT（實得 2 台）")
        pairs.append((0, n - 1))
    return pairs
