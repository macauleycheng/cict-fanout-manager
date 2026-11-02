"""topo_manager 套件：拓樸管理工具的公開 API 入口。

對外一律從 ``topo_manager`` 頂層匯入（設計 §2）。公開符號：

  - ``load_topology``：讀取並驗證拓樸 YAML，回傳 ``Topology``（§4、FR-2）。
  - ``TopologyGraph``：由 ``Topology`` 建成的有向多層圖，提供路徑查找與摘要
    （§5、FR-3）。
  - ``Topology``：已驗證的拓樸頂層資料模型（§3、FR-1）。
  - 錯誤家族：``TopologyError``（單一基底）與其子類，供呼叫端一次攔截可回報
    失敗（§9、FR-5.4）。

歷史（HISTORY）：
  - 0.1.0  FEAT-004 回填公開 API（load_topology、TopologyGraph、Topology
           與 errors 家族）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from topo_manager.errors import (
    PathLookupError,
    TopologyError,
    TopologyFileError,
    TopologyGraphError,
    TopologyParseError,
    TopologyReferenceError,
    TopologyValidationError,
)
from topo_manager.graph import TopologyGraph
from topo_manager.loader import load_topology
from topo_manager.models import Topology

__all__ = [
    "load_topology",
    "TopologyGraph",
    "Topology",
    "TopologyError",
    "TopologyFileError",
    "TopologyParseError",
    "TopologyValidationError",
    "TopologyReferenceError",
    "TopologyGraphError",
    "PathLookupError",
]
