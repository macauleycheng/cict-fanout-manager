"""pytest 共用 fixtures（設計 §11）。

提供範例與測試資料檔的實際路徑 fixture，供各測試模組共用，避免在測試中硬寫
相對路徑。路徑一律以本檔所在位置推導，確保不論從哪個工作目錄執行 pytest 都
能定位到專案的 examples/ 與 tests/data/。

歷史（HISTORY）：
  - 0.1.0  FEAT-002 初版，建立 examples_dir / two_tier_path /
           three_tier_path / invalid_path fixtures。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# tests/conftest.py → 專案根為其父目錄的父目錄。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def examples_dir() -> Path:
    """回傳 examples/ 目錄的絕對路徑。"""
    return _PROJECT_ROOT / "examples"


@pytest.fixture
def two_tier_path(examples_dir: Path) -> Path:
    """回傳兩層範例 YAML 的絕對路徑。"""
    return examples_dir / "topology-2tier.yaml"


@pytest.fixture
def three_tier_path(examples_dir: Path) -> Path:
    """回傳三層範例 YAML 的絕對路徑。"""
    return examples_dir / "topology-3tier.yaml"


@pytest.fixture
def invalid_path() -> Path:
    """回傳故意錯誤的 YAML（tests/data/invalid-topology.yaml）的絕對路徑。"""
    return _PROJECT_ROOT / "tests" / "data" / "invalid-topology.yaml"
