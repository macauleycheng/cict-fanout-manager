"""store.py 的 pytest 測試（設計 §持久化、FR-5）。

涵蓋 save/load round-trip（AC7 store 面）、原子寫入經 tempfile + os.replace
（AC8）、ledger 帶 version、版本不支援 → SessionStoreError（AC10）、非法
JSON → SessionStoreError（AC11）、缺檔 → 空 LoadedLedger（FR-5.6）。以
tmp_path 操作真實檔案。

Phase 2 Hardening 另涵蓋：I/O 失敗/非 UTF-8 → SessionStoreError（chaining）、
_ensure_dir_durable 逐層 fsync 與冪等、save fsync/replace 失敗回捲與 temp
cleanup、save 回捲兩情境（N#4）、load_raw 缺檔/壞 JSON、locked 取鎖/逾時。

歷史（HISTORY）：
  - 0.2.0  FEAT-005 初版。
  - 0.3.0  Phase 2 Hardening：更新既有斷言（access_ports 移除、save 補
           next_seq、load() 回 LoadedLedger）；新增 I/O/durability/回捲/
           load_raw/locked 測試。
  - 0.3.1  新增 locked 臨界區 _ensure_dir_durable 的 OSError 包裝回歸測試
           （Issue #1）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import json
import os
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
            trunk_segments=[
                TrunkSegment(a_node="core-1:7", b_node="leaf-1-1:1", vlan=101),
            ],
        ),
    ]


def test_save_load_round_trip(tmp_path: Path) -> None:
    """save 後 load 回來逐欄位完全相同（AC7 store 面）。"""
    store = JsonSessionStore(tmp_path / "state" / "allocations.json")
    sessions = _sample_sessions()
    store.save(sessions, 3)

    loaded = store.load()
    assert len(loaded.sessions) == len(sessions)
    for original, restored in zip(sessions, loaded.sessions):
        assert restored.model_dump() == original.model_dump()


def test_save_goes_through_tempfile_and_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """原子寫入必經 os.replace；replace 前目標檔內容保持不變（AC8）。"""
    import topo_manager.store as store_module

    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)

    # 先寫入一版舊內容，作為「replace 前不應被改動」的基準。
    store.save(_sample_sessions()[:1], 2)
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

    store.save(_sample_sessions(), 3)
    assert calls["count"] == 1
    # replace 之後目標檔已更新為新內容。
    assert len(store.load().sessions) == 2


def test_ledger_carries_version(tmp_path: Path) -> None:
    """寫出的 ledger 檔頂層帶正確的 version 欄位。"""
    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)
    store.save(_sample_sessions(), 3)

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
    """ledger 檔不存在時 load 回傳空 LoadedLedger（FR-5.6）。"""
    store = JsonSessionStore(tmp_path / "nope" / "allocations.json")
    loaded = store.load()
    assert loaded.sessions == []
    assert loaded.next_seq is None


# ----------------------------------------------------------------------
# Phase 2 Hardening：I/O 失敗（#4）
# ----------------------------------------------------------------------


def test_load_io_failure_wrapped_with_cause(tmp_path: Path) -> None:
    """load 讀檔 I/O 失敗 → SessionStoreError 且 __cause__ 非 None（AC5）。

    以目錄當作 ledger 路徑（存在但 read_text 丟 IsADirectoryError，OSError
    子類）觸發讀檔失敗。
    """
    a_dir = tmp_path / "state" / "allocations.json"
    a_dir.mkdir(parents=True)
    store = JsonSessionStore(a_dir)
    with pytest.raises(SessionStoreError) as exc_info:
        store.load()
    assert exc_info.value.__cause__ is not None


def test_load_non_utf8_raises(tmp_path: Path) -> None:
    """load 遇非 UTF-8 內容 → SessionStoreError（chaining）。"""
    path = tmp_path / "state" / "allocations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00bad")
    store = JsonSessionStore(path)
    with pytest.raises(SessionStoreError) as exc_info:
        store.load()
    assert str(path) in str(exc_info.value)
    assert exc_info.value.__cause__ is not None


def test_save_io_failure_notadirectory(tmp_path: Path) -> None:
    """save 到不可建立路徑（檔案當中間目錄）→ SessionStoreError（chaining）。"""
    a_file = tmp_path / "afile"
    a_file.write_text("x", encoding="utf-8")
    store = JsonSessionStore(a_file / "nope" / "allocations.json")
    with pytest.raises(SessionStoreError) as exc_info:
        store.save(_sample_sessions(), 3)
    assert exc_info.value.__cause__ is not None


# ----------------------------------------------------------------------
# Phase 2 Hardening：durability（#3）
# ----------------------------------------------------------------------


def test_ensure_dir_durable_fsyncs_new_dir_parents(
    tmp_path: Path, monkeypatch
) -> None:
    """_ensure_dir_durable 對新建目錄的父逐層 fsync；已存在為冪等 no-op。"""
    path = tmp_path / "a" / "b" / "allocations.json"
    store = JsonSessionStore(path)

    fsynced: list = []
    real_fsync_dir = store._fsync_dir

    def _wrap(dir_path):
        fsynced.append(Path(dir_path))
        return real_fsync_dir(dir_path)

    monkeypatch.setattr(store, "_fsync_dir", _wrap)

    # 首次建立：a 與 b 皆不存在，應對新建目錄的父各 fsync 一次。
    store._ensure_dir_durable(path.parent)
    assert (tmp_path / "a" / "b").is_dir()
    assert len(fsynced) >= 2

    # 第二次：目錄已存在 → 冪等 no-op，不再新增 fsync 呼叫。
    fsynced.clear()
    store._ensure_dir_durable(path.parent)
    assert fsynced == []


def test_save_fsyncs_parent_dir(tmp_path: Path, monkeypatch) -> None:
    """save 對尚不存在的狀態目錄建立後，ledger 父目錄被 _fsync_dir 呼叫。"""
    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)

    fsynced: list = []
    real_fsync_dir = store._fsync_dir

    def _wrap(dir_path):
        fsynced.append(Path(dir_path))
        return real_fsync_dir(dir_path)

    monkeypatch.setattr(store, "_fsync_dir", _wrap)
    store.save(_sample_sessions(), 3)
    assert path.parent in fsynced


def test_save_replace_failure_rolls_back_and_cleans_temp(
    tmp_path: Path, monkeypatch
) -> None:
    """save 中 os.replace 丟 OSError → SessionStoreError（chaining）、temp 已清。"""
    import topo_manager.store as store_module

    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store_module.os, "replace", _boom)
    with pytest.raises(SessionStoreError) as exc_info:
        store.save(_sample_sessions(), 3)
    assert exc_info.value.__cause__ is not None

    # 殘留 temp 檔已被清理。
    leftovers = list(path.parent.glob(".allocations-*.tmp"))
    assert leftovers == []


def test_save_rollback_preserves_existing_ledger_bytes(tmp_path: Path, monkeypatch) -> None:
    """N#4 情境一：已有既有帳本，save 失敗前後 bytes 相等。"""
    import topo_manager.store as store_module

    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)
    store.save(_sample_sessions()[:1], 2)
    before = path.read_bytes()

    def _boom(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(store_module.os, "replace", _boom)
    with pytest.raises(SessionStoreError):
        store.save(_sample_sessions(), 3)
    assert path.read_bytes() == before


def test_save_rollback_first_save_leaves_no_file(tmp_path: Path, monkeypatch) -> None:
    """N#4 情境二：首筆即 save 失敗，目標檔不存在。"""
    import topo_manager.store as store_module

    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)

    def _boom(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(store_module.os, "replace", _boom)
    with pytest.raises(SessionStoreError):
        store.save(_sample_sessions(), 3)
    assert not path.exists()


# ----------------------------------------------------------------------
# Phase 2 Hardening：load_raw（#12）
# ----------------------------------------------------------------------


def test_load_raw_missing_returns_empty_structure(tmp_path: Path) -> None:
    """load_raw 對缺檔回空結構，不報錯。"""
    store = JsonSessionStore(tmp_path / "nope" / "allocations.json")
    assert store.load_raw() == {
        "version": None,
        "next_seq": None,
        "sessions": [],
    }


def test_load_raw_malformed_json_raises(tmp_path: Path) -> None:
    """load_raw 對非法 JSON 丟 SessionStoreError。"""
    path = tmp_path / "state" / "allocations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json ", encoding="utf-8")
    store = JsonSessionStore(path)
    with pytest.raises(SessionStoreError):
        store.load_raw()


def test_load_raw_returns_original_even_for_bad_version(tmp_path: Path) -> None:
    """load_raw 不做版本/schema 檢查，壞版本帳本仍原樣回傳。"""
    path = tmp_path / "state" / "allocations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": 1, "sessions": [{"session_id": "manual-session"}]}
        ),
        encoding="utf-8",
    )
    store = JsonSessionStore(path)
    raw = store.load_raw()
    assert raw["version"] == 1
    assert raw["sessions"][0]["session_id"] == "manual-session"


# ----------------------------------------------------------------------
# Phase 2 Hardening：locked（#1）
# ----------------------------------------------------------------------


def test_locked_basic_acquire_release(tmp_path: Path) -> None:
    """locked 基本取鎖/釋鎖不拋例外，且建立 lock 檔與狀態目錄。"""
    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)
    with store.locked():
        assert path.parent.is_dir()
    assert Path(str(path) + ".lock").exists()


def test_locked_timeout_raises(tmp_path: Path, monkeypatch) -> None:
    """flock 阻塞時 SIGALRM 逾時 → SessionStoreError（chaining）。"""
    import topo_manager.store as store_module

    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)

    def _block(fd, op):
        # 模擬阻塞：等待 SIGALRM 打斷（handler raise _LockTimeout）。
        import time

        time.sleep(5)

    monkeypatch.setattr(store_module.fcntl, "flock", _block)
    with pytest.raises(SessionStoreError) as exc_info:
        with store.locked(timeout=1):
            pass
    assert "逾時" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None


def test_locked_critical_section_ensure_dir_oserror_wrapped(
    tmp_path: Path, monkeypatch
) -> None:
    """locked 臨界區 _ensure_dir_durable 丟 OSError → SessionStoreError（Issue #1）。

    注入方式讓臨界區（取鎖成功、alarm(0) 之後、yield 之前）的
    _ensure_dir_durable 呼叫 raise OSError，確認得到 SessionStoreError（保留
    chaining）而非裸 OSError 穿透——後者會讓 CLI 的 except TopologyError 攔不到
    而冒 traceback。
    """
    path = tmp_path / "state" / "allocations.json"
    store = JsonSessionStore(path)

    def _boom(dir_path):
        raise OSError("simulated dir fsync failure in critical section")

    monkeypatch.setattr(store, "_ensure_dir_durable", _boom)
    with pytest.raises(SessionStoreError) as exc_info:
        with store.locked():
            pass
    # 必須是 SessionStoreError（TopologyError 家族），且保留 chaining。
    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(path.parent) in str(exc_info.value)
