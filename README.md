# topo-manager（拓樸管理工具）

拓樸管理 CLI 工具。讀取描述打流環境的拓樸 YAML（generator、fanout、rack/DUT
與實體連線），驗證其正確性後建成有向多層圖，印出摘要，查詢任一 generator port
到任一 DUT port 的實體路徑，並管理「generator→DUT」的 session 分配（含全域唯一
VLAN 與 JSON 持久化）。

Phase 1 聚焦「載入 → 驗證 → 建圖 → 摘要 / 路徑查找」；Phase 2 在其上加入 session
分配引擎（硬衝突偵測、全域唯一 VLAN、斷電可還原的 JSON ledger）並把 CLI 重構成
子命令。

## 安裝

需 Python 3.8+。於專案根目錄以可編輯模式安裝（含測試相依）：

```bash
pip install -e ".[dev]"
```

相依：`pydantic`（2.5~2.10）、`networkx`（3.1，Python 3.8 可用的最高版）、
`PyYAML`、`fastapi`（0.110.x，Phase 3 REST API）、`uvicorn`（0.23~0.33，ASGI
server）；測試用 `pytest` 與 `httpx`（REST 測試）。

## 執行

CLI 採「子命令（subcommand）」介面，每個子命令都吃一個位置參數 `<topology.yaml>`：

```bash
python -m topo_manager <command> <topology.yaml> [選項]
```

| 子命令 | 用途 | 主要選項 |
| --- | --- | --- |
| `summary` | 載入拓樸並印人類可讀摘要 | （無） |
| `path` | 印一條 generator→DUT 實體路徑 | `--from`、`--to`（須成對） |
| `allocate` | 分配一筆 session（含 VLAN 與路徑）並持久化 | `--from`、`--to`、`--state`、`--vlan-min`、`--vlan-max` |
| `release` | 釋放一筆既有 session | `--session`、`--state`、`--vlan-min`、`--vlan-max` |
| `list` | 列出目前所有 active session（或 `--raw` 唯讀 dump） | `--state`、`--raw`、`--vlan-min`、`--vlan-max`；`<topology.yaml>` 在 `--raw` 時可省略 |

選項說明：

- `--from GEN_PORT`：generator 端 port，可裸名（如 `ixia-1/1`）或 `dev:port`。
- `--to DUT_PORT`：DUT 端點，**一律 `dev:port` 形式**（如 `dut-A1:Eth0`）；
  因 tg_port 名稱會跨 DUT 重複，裸名無法唯一指定。`path` / `allocate` 兩者皆須提供。
- `--session SESSION_ID`：`release` 欲釋放的 session 識別碼（如 `s-0001`）。
- `--state STATE_JSON`：session ledger 狀態檔路徑，預設
  `.topo-state/allocations.json`（相對於**當前工作目錄**）。跨行程一致性需使用
  同一 cwd，或一致地以 `--state` 明確指定同一狀態檔。
- `--vlan-min VLAN_MIN` / `--vlan-max VLAN_MAX`：VLAN pool 端點（預設
  `100`/`3000`），供 `allocate`/`release`/`list` 共用。範圍須落在 802.1Q 可用
  區間 `2..4094`（排除保留的 0/1/4095）；非法範圍（如 `--vlan-max 9999`）會被
  明確拒絕、以非零 exit code 結束、不冒 traceback。
- `--raw`（僅 `list`）：唯讀 dump ledger 的原始 JSON，**繞過拓樸與還原檢查**。
  此模式完全不讀取 `topology.yaml`（可省略該位置參數），因此即使帳本版本不符、
  內容不一致，或拓樸 YAML 缺失/損壞，仍能原樣檢視帳本以便診斷修復。

載入、路徑查找或分配失敗時，會在 stderr 印清楚的錯誤訊息並以非零 exit code
結束（不會冒出 Python traceback）。這包含 `--state` 指向**不可建立/不可讀/唯讀/
磁碟滿**等 I/O 失敗——一律以單一錯誤區塊回報、不冒 traceback。

### 舊介面對照（Phase 1 → Phase 2）

Phase 1 的扁平介面 `python -m topo_manager <yaml> [--from --to]` 已改為子命令：

| Phase 1（舊） | Phase 2（新） |
| --- | --- |
| `python -m topo_manager <yaml>` | `python -m topo_manager summary <yaml>` |
| `python -m topo_manager <yaml> --from A --to B` | `python -m topo_manager path <yaml> --from A --to B` |

### 兩層範例（Core → Leaf → DUT）

```bash
python -m topo_manager summary examples/topology-2tier.yaml
python -m topo_manager path examples/topology-2tier.yaml --from ixia-1/1 --to dut-A1:Eth0
```

實際輸出：

```
拓樸摘要：examples/topology-2tier.yaml（site=lab-a）
  generator port 數：6
  DUT 台數：8    DUT 打流 port 數：16
  fanout 台數：3
  各層（role）節點數（台數）：core=1, leaf=2
  各 tier 節點數（排除 #core）：{0: 6, 1: 8, 2: 18, 3: 16}
路徑 ixia-1/1 → dut-A1:Eth0：
  ixia-1:ixia-1/1 → core-1:1 → core-1:7 → leaf-1-1:1 → leaf-1-1:11 → dut-A1:Eth0
```

### 三層範例（Spine → Agg → Leaf → DUT）

```bash
python -m topo_manager summary examples/topology-3tier.yaml
python -m topo_manager path examples/topology-3tier.yaml --from ixia-1/1 --to dut-A1:Eth0
```

實際輸出：

```
拓樸摘要：examples/topology-3tier.yaml（site=lab-a）
  generator port 數：6
  DUT 台數：8    DUT 打流 port 數：16
  fanout 台數：5
  各層（role）節點數（台數）：agg=2, leaf=2, spine=1
  各 tier 節點數（排除 #core）：{0: 6, 1: 8, 2: 4, 3: 18, 4: 16}
路徑 ixia-1/1 → dut-A1:Eth0：
  ixia-1:ixia-1/1 → spine-1:1 → spine-1:7 → agg-1:1 → agg-1:2 → leaf-1-1:1 → leaf-1-1:11 → dut-A1:Eth0
```

兩份範例共用完全相同的 rack/DUT 區塊；三層只是把兩層的 `core` 拆成
`spine + agg`，模型與演算法完全不變。

### 滿規模三層範例（6 rack × 24 DUT × 8 tg_port）

`examples/topology-fullscale.yaml` 由 `examples/gen_fullscale.py` 產生並 check-in
（勿手動編輯；重跑該腳本會得到位元組等價的輸出）：

```bash
python examples/gen_fullscale.py
```

## session 分配（Phase 2）

`allocate` 依最短路徑建立一筆 session，挑一個全域唯一的 VLAN，並把狀態寫到
`--state` 指定的 JSON ledger（預設 `.topo-state/allocations.json`）。`list` 讀同一
ledger 列出現有 session，`release` 釋放指定 session。硬衝突（同一 access port
被兩筆 session 佔用）會被擋下並列出擁有者；共用 trunk 的多筆 session 合法，各自
使用不同 VLAN。

```bash
# 分配一筆 session（印出 session_id、VLAN、路徑與 trunk 段）
python -m topo_manager allocate examples/topology-3tier.yaml \
    --from ixia-1/1 --to dut-A1:Eth0 --state .topo-state/allocations.json

# 列出目前所有 active session（讀同一狀態檔）
python -m topo_manager list examples/topology-3tier.yaml \
    --state .topo-state/allocations.json

# 釋放指定 session
python -m topo_manager release examples/topology-3tier.yaml \
    --session s-0001 --state .topo-state/allocations.json
```

`allocate` 實際輸出：

```
已分配：
session s-0001
  VLAN：100
  端點：ixia-1/1 → dut-A1:Eth0
  路徑：ixia-1:ixia-1/1 → spine-1:1 → spine-1:7 → agg-1:1 → agg-1:2 → leaf-1-1:1 → leaf-1-1:11 → dut-A1:Eth0
  trunk 段：
    spine-1:7 → agg-1:1（VLAN 100）
    agg-1:2 → leaf-1-1:1（VLAN 100）
```

### 帳本格式（ledger）

狀態檔是一份 JSON ledger，頂層有三個 key：

- `version`：ledger 的 **schema 版本**（整數，目前為 `2`）。load 時以此 key 與
  程式內建版本比對，不符即明確拒絕。
- `next_seq`：session 序號的**高水位**（整數）。序號只增不減——釋放最高序號的
  session 後重啟，下一筆分配不會重用已用過的序號。
- `sessions`：目前 active 的 session 陣列（各含 `session_id`、`vlan`、端點、
  `path`、`physical_ports` 與 `trunk_segments`）。

> `access_ports` 不入帳本：它是由 `physical_ports` 扣除 trunk 端節點即時重算的
> 衍生值（單一真相來源），不受帳本竄改影響。

跨行程併發安全：`allocate`/`release` 以同目錄的 lock 檔（`<state>.lock`）取跨
行程排他鎖，鎖內重新讀取最新帳本再決策、save，確保多行程同時分配不會算出重複的
序號或 VLAN。寫入採「tempfile + fsync + 原子換名 + 父目錄 fsync」，斷電時帳本不是
舊內容就是新完整內容，不會半截損毀。

### 唯讀檢視原始帳本（`list --raw`）

當帳本版本不符、內容不一致，或拓樸 YAML 缺失/損壞導致正常 `list` 無法進行時，用
`--raw` 直接 dump 原始 JSON（不碰拓樸、不做還原檢查）以便診斷：

```bash
# 原樣 dump 帳本（可省略 topology.yaml）
python -m topo_manager list --raw --state .topo-state/allocations.json
```

## Phase 3：REST API

Phase 3 在 Phase 1/2 之上疊一層**薄層 FastAPI REST 介接**，讓外部系統（如
ATLAS）能以 HTTP 查詢可用資源、要求 / 釋放 session、檢視完整拓樸。所有分配 /
硬衝突 / VLAN / 持久化演算法一律**沿用 Phase 2 的 `Allocator` 與
`JsonSessionStore`**；API 層只負責「HTTP ↔ Pydantic 模型 ↔ allocator 呼叫 ↔
例外映射成 HTTP 狀態碼」，不重寫任何演算法、不繞過檔鎖。

### 安裝

```bash
pip install -e ".[dev]"   # 現在含 fastapi / uvicorn / httpx
```

### 設定（環境變數）

| 環境變數 | 預設 | 意義 |
| --- | --- | --- |
| `TOPO_MANAGER_TOPOLOGY` | （無預設，**必填**） | 拓樸 YAML 路徑 |
| `TOPO_MANAGER_STATE` | `.topo-state/allocations.json` | session ledger 路徑 |
| `TOPO_MANAGER_VLAN_MIN` | `100` | VLAN pool 下界 |
| `TOPO_MANAGER_VLAN_MAX` | `3000` | VLAN pool 上界 |
| `TOPO_MANAGER_HOST` | `0.0.0.0` | `topo-manager-api` 綁定 host |
| `TOPO_MANAGER_PORT` | `8000` | `topo-manager-api` 綁定 port |

缺 `TOPO_MANAGER_TOPOLOGY`、拓樸不合法、或 VLAN 範圍非法時，app 於**啟動期即
fail-fast**（印單行 stderr、非零 exit code，不冒 traceback），而非每個請求回 500。

### 啟動（兩種等價方式）

```bash
export TOPO_MANAGER_TOPOLOGY=examples/topology-3tier.yaml
export TOPO_MANAGER_STATE=.topo-state/allocations.json

# 方式 A：uvicorn 直接指向 app factory（讀環境變數建 app）
uvicorn "topo_manager.api:create_app" --factory --host 0.0.0.0 --port 8000

# 方式 B：便利入口（封裝 uvicorn.run，讀環境變數）
topo-manager-api
```

> **部署注意（主執行緒限制）**：Phase 2 的檔鎖以 `signal.SIGALRM` 實作取鎖
> 逾時，而 `signal` 只能在**主執行緒**設定。因此 app 的 ASGI 事件迴圈必須跑在
> 主執行緒（uvicorn 單行程預設即如此）。請**勿**以「把事件迴圈放到背景執行緒」
> 的多執行緒 worker 模式部署，否則寫入端點（POST/DELETE）取鎖時會失敗。需要
> 多核時以多行程（如多個 uvicorn/gunicorn worker process，各自擁有主執行緒）
> 水平擴充即可，跨行程一致性由 Phase 2 檔鎖保證。

### 文件（OpenAPI）

啟動後：

- Swagger UI：`http://<host>:8000/docs`
- OpenAPI schema：`http://<host>:8000/openapi.json`

### 端點與 curl 範例

```bash
# 1) 取得完整拓樸（供視覺化重建）
curl -s localhost:8000/topology

# 2) 取得可用 generator / DUT port 清單（排除已被 active session 佔用）
curl -s localhost:8000/resources/available

# 3) 分配一筆 session（201 + session 摘要，status=allocated）
curl -s -X POST localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"}'

# 4) 列出目前所有 active session
curl -s localhost:8000/sessions

# 5) 釋放一筆 session（200 + 被釋放 session 摘要，status=released）
curl -s -X DELETE localhost:8000/sessions/s-0001
```

硬衝突範例（對同一 `(generator_port, dut_port)` 重複分配，access port 被佔用）
會回 **409** 與結構化錯誤：

```bash
curl -s -X POST localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"}'
# => HTTP 409
# {"error": {"type": "HardConflictError", "message": "硬衝突：下列 access port ..."}}
```

錯誤回應一律為統一結構 `{"error": {"type": ..., "message": ...}}`，**不含
traceback**。狀態碼映射：硬衝突 / VLAN 耗盡 → 409、session 找不到 / 無路徑 →
404、ledger I/O / 取鎖故障 → 503、請求輸入錯（缺欄位 / 空字串 / 多餘欄位）→
400、非預期錯 → 500（固定訊息）。

### 回應欄位：access port 如何推導

`SessionResponse` 平鋪 Phase 2 `Session` 的欄位（`session_id`、`vlan`、端點、
`path`、`physical_ports`、`trunk_segments`）外加頂層 `status`。與帳本一致，
回應**不**另帶 `access_ports`；若需要，可由
`physical_ports − {所有 trunk_segments 的 a_node/b_node}` 自行推導。

### 與 CLI 的一致性

API 與 CLI 指向**同一** `--state` / `TOPO_MANAGER_STATE` 時，透過 Phase 2 的
檔鎖（`<state>.lock`）保證並行一致：API 的 POST/DELETE 與 CLI 的
`allocate`/`release` 互斥序列化，鎖內重讀最新帳本再決策。API 層**不繞過
檔鎖**、不自建記憶體快取代替 allocator 決策。

## 通用多層約束（設計重點）

工具不把「層數」寫死，也不靠 `role` 字串判斷方向或層級：

- `fanout.role`（`core` / `spine` / `agg` / `leaf` 等）只是自由字串標籤與摘要
  分組鍵，不參與任何方向或層級判斷邏輯。
- 邊的**方向**由接線拓樸相對於 generator 的距離動態推導；**tier 層級**另由
  「收縮同一台 fanout 的所有 port 為超節點」的收縮圖多源 BFS 計算。兩者分離。
- 因此同一套演算法可自然處理兩層、三層乃至更多層（上方 `tiers` 鍵集合分別為
  `{0,1,2,3}` 與 `{0,1,2,3,4}`），新增一層只需在 YAML 多描述一層連線。

節點 id 一律為 `<裝置>:<port>`；每台 fanout 另有一個虛擬中樞 `<裝置>:#core`
做設備內部連通，摘要與路徑輸出都會排除它。
