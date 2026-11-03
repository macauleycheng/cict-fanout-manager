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
  - 0.3.0  Phase 2 Hardening 新增 ConfigError（設定/參數非法，CLI 以既有
           except TopologyError 攔截）；SessionStoreError docstring 補充
           store 層讀寫 I/O 失敗亦屬此類。
  - 0.4.0  Phase 4 fanout SSH 下發層新增 6 個子類
           （DriverDependencyError / DriverConnectionError /
           DriverParseError / PortMappingError / ReadBackMismatchError /
           ApplyError），皆繼承 TopologyError，CLI 以既有 except TopologyError
           自動攔截（不改 cli.py）。

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

    store 層讀寫 I/O 失敗亦屬此類：權限不足、路徑為目錄或不可建立、磁碟
    滿、唯讀檔案系統、非 UTF-8 內容，一律包成本例外並保留 ``__cause__``
    （exception chaining），讓 CLI 以單一錯誤區塊回報、不冒 traceback。
    """


class ConfigError(TopologyError):
    """設定/參數非法時觸發（Phase 2 Hardening）。

    觸發條件：使用者或設定層提供的參數不合法，例如 CLI 的 VLAN pool 範圍
    （``--vlan-min``/``--vlan-max``）超出 802.1Q 可用範圍、或 ``list``（非
    ``--raw``）未提供必要的 topology.yaml。屬 ``TopologyError`` 家族，CLI 以
    既有 ``except TopologyError`` 攔截、印單一錯誤區塊、回非零、無 traceback。
    """


class DriverDependencyError(TopologyError):
    """fanout 下發層所需的選用相依（netmiko）未安裝時觸發（Phase 4，§9）。

    觸發條件：實際要建立 SSH 連線時 ``import netmiko`` 失敗（本專案將 netmiko
    延遲 import 於 driver 連線函式內，純邏輯/設定/CLI 路徑不觸發）。可復原性：
    使用者安裝選用相依（``pip install topo-manager[drivers]``）後即可重試。
    訊息應指明缺少 netmiko 與安裝方式，並保留 ``__cause__``（ImportError）。
    """


class DriverConnectionError(TopologyError):
    """對 fanout 設備建立/維持 SSH 連線失敗時觸發（Phase 4，§9）。

    觸發條件：netmiko 連線逾時、認證失敗、連線中斷或底層傳輸例外。可復原性：
    使用者修正連線設定（host/username/password/可達性）後可重試。訊息應含目標
    host 與底層原因，並保留 ``__cause__``。
    """


class DriverParseError(TopologyError):
    """解析設備 CLI 輸出（如 show vlan）失敗時觸發（Phase 4，§9）。

    觸發條件：設備回傳的文字不符預期格式、欄位缺漏或無法對映到 VlanState。
    可復原性：多屬設備型號/韌體差異，需調整解析器；屬可回報失敗而非程式 bug。
    訊息應含無法解析的指令與片段摘要。
    """


class PortMappingError(TopologyError):
    """拓樸 port 無法對映為合法設備介面名時觸發（Phase 4，§5、§9）。

    觸發條件：以 interface_template 套出的介面名不符設備介面命名規則
    （fail-closed：模組常數正則不符即拒絕，不臆測）。可復原性：使用者修正
    interface_template 或拓樸 port。訊息應含原始 port、套出的介面名與期望格式。
    """


class ReadBackMismatchError(TopologyError):
    """下發後回讀（read-back）驗證與期望狀態不符時觸發（Phase 4，§7.3、§9）。

    觸發條件：apply 後重新讀取設備 VLAN 狀態，期望的 (interface, vlan, tagged)
    成員仍有缺漏。可復原性：可重試下發或人工檢查設備；屬可回報失敗。訊息應
    逐條列出缺漏的 (interface, vlan) 成員。
    """


class ApplyError(TopologyError):
    """下發 VLAN 設定指令至設備失敗時觸發（Phase 4，§9）。

    觸發條件：送出設定指令時設備回報錯誤、拒絕或連線中斷，導致本批變更未能
    完整套用。可復原性：使用者修正設定/權限後可重試。訊息應含失敗的設備與
    底層原因，並保留 ``__cause__``。
    """
