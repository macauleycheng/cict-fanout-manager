"""topo_manager 自訂例外（設計 §9）。

所有對外錯誤皆繼承單一基底 ``TopologyError``，讓 CLI 邊界層能以一次
``except TopologyError`` 捕捉全部可回報的失敗（FR-5.4）。每種失敗都是
「明確分類、可回報、非致命」——library 層只負責丟出帶清楚訊息的例外，
由 CLI 決定輸出到 stderr 並回傳非零 exit code。非預期例外（程式 bug）
不在此體系內，保留原始 traceback 以利除錯。

歷史（HISTORY）：
  - 0.1.0  FEAT-001 初版，建立六個子類（檔案/解析/驗證/參照/建圖/
           路徑查找）。
  - 0.2.0  FEAT-005 Phase 2 新增 4 個分配/持久化錯誤子類
           （硬衝突/VLAN 耗盡/session 不存在/ledger 儲存）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations


class TopologyError(Exception):
    """所有拓樸相關錯誤的單一基底類別。

    CLI 以 ``except TopologyError`` 一次攔截所有可回報失敗（§9、FR-5.4）。
    不直接拋出此基底；一律使用下列具體子類以利分類。
    """


class TopologyFileError(TopologyError):
    """拓樸檔案不存在或不可讀時觸發。

    訊息應含出錯的路徑，供使用者修正後重試。
    """


class TopologyParseError(TopologyError):
    """YAML 解析失敗時觸發。

    觸發條件：YAML 語法錯誤（``yaml.YAMLError``）、檔案為空、或頂層
    不是 mapping（dict）。訊息應附 PyYAML 的問題描述與位置。
    """


class TopologyValidationError(TopologyError):
    """Pydantic schema 驗證失敗時觸發。

    觸發條件：缺少必填欄位、型別錯誤、違反欄位約束（含同一裝置內
    port 重複）、或出現未知欄位（``extra='forbid'``）。訊息應逐條列出
    欄位路徑與原因。
    """


class TopologyReferenceError(TopologyError):
    """參照完整性檢查失敗時觸發。

    觸發條件：裝置名重複（含 DUT 跨 rack 重名）、rack 名與裝置名衝突、
    link 引用不存在的裝置、fanout 端 port 非法或越界、或 port 不屬於
    該裝置。訊息應含出錯的 link 序號與相關名稱/值/範圍。
    """


class TopologyGraphError(TopologyError):
    """建圖階段偵測到不支援的拓樸結構時觸發。

    觸發條件：偵測到等距（同層）實體連線（以「排除該邊後兩端到
    generator 距離相等」判定，§5.3），Phase 1 不支援此類接線。訊息應
    指出該 link 兩端與其相等的距離值。
    """


class PathLookupError(TopologyError):
    """路徑查找失敗時觸發。

    觸發條件：來源或目的節點不存在、兩端之間無連通路徑、generator
    裸名解析不唯一、或 DUT 端點未以 ``dev:port`` 形式指定。訊息應含
    查詢端點與候選清單或失敗原因。
    """


class HardConflictError(TopologyError):
    """新路徑的 access port 已被其他使用中 session 佔用時觸發（FR-4）。

    硬衝突僅以 access port（實體路徑扣除所有 trunk 端節點後的集合）判定；
    trunk 端 port 可被多個 session 共用，不視為衝突。訊息應列出所有發生
    衝突的 access port 與其當前擁有者 session_id，供使用者釋放後重試。
    此檢查在任何狀態變更之前完成，確保分配失敗時不留副作用。
    """


class VlanPoolExhaustedError(TopologyError):
    """封閉 VLAN 池內所有值皆已被佔用、無法再分配時觸發（FR-3）。

    觸發條件：``range(vlan_min, vlan_max + 1)`` 內找不到任何未使用的 VLAN。
    訊息應含 VLAN 池範圍與目前已使用數量，供使用者擴充池或釋放既有
    session。
    """


class SessionNotFoundError(TopologyError):
    """釋放（release）時指定的 session_id 不存在時觸發（FR-4）。

    訊息應含查無的 session_id，供使用者確認輸入或改以 list 查詢現有
    session。
    """


class SessionStoreError(TopologyError):
    """ledger 持久化檔讀寫或還原一致性失敗時觸發（FR-5）。

    觸發條件：ledger 檔非合法 JSON、未通過 schema 驗證、版本不支援、或
    還原時偵測到一致性問題（同一 access port 被兩個 session 佔用、或
    VLAN 重複）。訊息應含檔案路徑與底層原因，供使用者修正或改用
    ``--state`` 指定正確的狀態檔。
    """
