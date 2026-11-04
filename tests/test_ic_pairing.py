"""互聯配對表與 profile 展開測試（互聯設計 §3、§10）。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import itertools

import pytest

from topo_manager.errors import InterconnectRequestError
from topo_manager.interconnect.pairing import expand_profile, pair_port_indices

_TABLE = {
    (0, 1): (0, 0),
    (0, 2): (1, 0),
    (0, 3): (2, 0),
    (1, 2): (1, 1),
    (1, 3): (2, 1),
    (2, 3): (2, 2),
}


@pytest.mark.parametrize("pair,expected", sorted(_TABLE.items()))
def test_fixed_table(pair, expected) -> None:
    assert pair_port_indices(*pair) == expected
    assert pair_port_indices(pair[1], pair[0]) == (expected[1], expected[0])


def test_p3_never_used() -> None:
    used = {p for pair in _TABLE.values() for p in pair}
    assert 3 not in used


@pytest.mark.parametrize("args", [(1, 1), (-1, 2), (0, 4)])
def test_pair_invalid(args) -> None:
    with pytest.raises(InterconnectRequestError):
        pair_port_indices(*args)


@pytest.mark.parametrize("n,count", [(2, 1), (3, 3), (4, 6)])
def test_mesh_counts(n, count) -> None:
    assert len(expand_profile("mesh", n)) == count


@pytest.mark.parametrize("n,count", [(2, 1), (3, 2), (4, 3)])
def test_line_counts(n, count) -> None:
    pairs = expand_profile("line", n)
    assert len(pairs) == count
    assert pairs == [(k, k + 1) for k in range(n - 1)]


def test_ring_counts() -> None:
    assert len(expand_profile("ring", 3)) == 3
    assert len(expand_profile("ring", 4)) == 4
    assert expand_profile("ring", 4)[-1] == (0, 3)


def test_ring_two_rejected() -> None:
    with pytest.raises(InterconnectRequestError, match="ring 至少需要 3 台"):
        expand_profile("ring", 2)


def test_ring3_equals_mesh3_and_line2_equals_mesh2() -> None:
    assert set(expand_profile("ring", 3)) == set(expand_profile("mesh", 3))
    assert expand_profile("line", 2) == expand_profile("mesh", 2)


@pytest.mark.parametrize("n", [0, 1, 5])
def test_bad_count(n) -> None:
    for profile in ("mesh", "line", "ring"):
        with pytest.raises(InterconnectRequestError, match="需要 2..4 台"):
            expand_profile(profile, n)


def test_unknown_profile() -> None:
    with pytest.raises(InterconnectRequestError, match="未知 profile 'star'"):
        expand_profile("star", 3)


def test_pair_independent_of_selection() -> None:
    # 同一對 DUT 不論選了哪幾台或順序，p 序恆同（函式只吃組內名次）。
    for i, j in itertools.permutations(range(4), 2):
        assert pair_port_indices(i, j) == pair_port_indices(i, j)
        a, b = pair_port_indices(i, j)
        assert pair_port_indices(j, i) == (b, a)
