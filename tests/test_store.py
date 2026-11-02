"""store.py 的 pytest 測試（設計 §持久化、FR-5）。

涵蓋 save/load round-trip（AC7 store 面）、原子寫入經 tempfile + os.replace
（AC8）、ledger 帶 version、版本不支援 → SessionStoreError（AC10）、非法
JSON → SessionStoreError（AC11）、缺檔 → []（FR-5.6）。以 tmp_path 操作真實
檔案。

歷史（HISTORY）：
  - 0.2.0  FEAT-005 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from topo_manager.allocator import Session, TrunkSegment
from topo_manager.errors import SessionStoreError
from topo_manager.store import SCHEMA_VERSION, JsonSessionStore


def _sample_sessions() -> list:
    """產生一組含 trunk 的測試 session（store 測試輔助）。"""
    return [
        Session(
            session_id="s-0001",
            vlan=100,
            generator_port="ixia-1/1",
            dut_port="dut-A1:Eth0",
            path=["ixia-1:ixia-1/1", "core-1:1", "core-1:7", "leaf-1-1:1",
                  "leaf-1-1:11", "dut-A1:Eth0"],
            physical_ports=["ixia-1:ixia-1/1", "core-1:1", "core-1:7",
                            "leaf-1-1:1", "leaf-1-1:11", "dut-A1:Eth0"],
            access_ports=["ixia-1:ixia-1/1", "core-1:1", "leaf-1-1:11",
                          "dut-A1:Eth0"],
            trunk_segments=[
                TrunkSegment(a_node="core-1:7", b_node="leaf-1-1:1", vlan=100),
            ],
        ),
        Session(
            session_id="s-0002",
            vlan=101,
            generator_port="ixia-1/2",
            dut_port="dut-A2:Eth0",
            path=["ixia-1:ixia-1/2", "core-1:2", "core-1:7", "leaf-1-1:1",
                  "leaf-1-1:13", "dut-A2:Eth0"],
            physical_ports=["ixia-1:ixia-1/2", "core-1:2", "core-1:7",
                            "leaf-1-1:1", "leaf-1-1:13", "dut-A2:Eth0"],
            access_ports=["ixia-1:ixia-1/2", "core-1:2", "leaf-1-1:13",
                          "dut-A2:Eth0"],
            trunk_segments=[
                TrunkSegment(a_node="core-1:7", b_node="leaf-1-1:1", vlan=101),
            ],
        ),
    ]


def test_save_load_round_trip(tmp_path: Path) -> None:
    """save 後 load 回來逐欄位完全相同（AC7 store 面）。"""
    store = JsonSessionStore(tmp_path / "state" / "allocations.json")
    sessions = _sample_sessions()
    store.save(sessions)

    loaded = store.load()
    assert len(loaded) == len(sessions)
    for original, restored in zip(sessions, loaded):
        assert restored.model_dump() == original.model_dump()


def test_save_goes_through_tempfile_and_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """原子寫入必經 os.replace；replace 前目標檔內容保持不變（AC8）。"""
    import topo_manager.store as store_module

    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)

    # 先寫入一版舊內容，作為「replace 前不應被改動」的基準。
    store.save(_sample_sessions()[:1])
    old_content = path.read_text(encoding="utf-8")

    calls = {"count": 0}
    real_replace = store_module.os.replace

    def _spy_replace(src, dst):
        """確認 replace 被呼叫，且呼叫前目標檔仍是舊內容（原子性）。"""
        calls["count"] += 1
        assert Path(dst) == path
        assert path.read_text(encoding="utf-8") == old_content
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", _spy_replace)

    store.save(_sample_sessions())
    assert calls["count"] == 1
    # replace 之後目標檔已更新為新內容。
    assert len(store.load()) == 2


def test_ledger_carries_version(tmp_path: Path) -> None:
    """寫出的 ledger 檔頂層帶正確的 version 欄位。"""
    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)
    store.save(_sample_sessions())

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == SCHEMA_VERSION


def test_unsupported_version_raises(tmp_path: Path) -> None:
    """ledger version 不等於 SCHEMA_VERSION 時 load 丟 SessionStoreError（AC10）。"""
    path = tmp_path / "state" / "allocations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": SCHEMA_VERSION + 99, "sessions": []}),
        encoding="utf-8",
    )
    store = JsonSessionStore(path)
    with pytest.raises(SessionStoreError) as exc_info:
        store.load()
    assert str(path) in str(exc_info.value)


def test_invalid_json_raises(tmp_path: Path) -> None:
    """ledger 檔非合法 JSON 時 load 丟 SessionStoreError（AC11）。"""
    path = tmp_path / "state" / "allocations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json ", encoding="utf-8")
    store = JsonSessionStore(path)
    with pytest.raises(SessionStoreError) as exc_info:
        store.load()
    assert str(path) in str(exc_info.value)


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    """ledger 檔不存在時 load 回傳空 list（FR-5.6）。"""
    store = JsonSessionStore(tmp_path / "nope" / "allocations.json")
    assert store.load() == []
