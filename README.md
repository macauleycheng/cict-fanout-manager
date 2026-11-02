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
`PyYAML`；測試用 `pytest`。

## 執行

CLI 採「子命令（subcommand）」介面，每個子命令都吃一個位置參數 `<topology.yaml>`：

```bash
python -m topo_manager <command> <topology.yaml> [選項]
```

| 子命令 | 用途 | 主要選項 |
| --- | --- | --- |
| `summary` | 載入拓樸並印人類可讀摘要 | （無） |
| `path` | 印一條 generator→DUT 實體路徑 | `--from`、`--to`（須成對） |
| `allocate` | 分配一筆 session（含 VLAN 與路徑）並持久化 | `--from`、`--to`、`--state` |
| `release` | 釋放一筆既有 session | `--session`、`--state` |
| `list` | 列出目前所有 active session | `--state` |

選項說明：

- `--from GEN_PORT`：generator 端 port，可裸名（如 `ixia-1/1`）或 `dev:port`。
- `--to DUT_PORT`：DUT 端點，**一律 `dev:port` 形式**（如 `dut-A1:Eth0`）；
  因 tg_port 名稱會跨 DUT 重複，裸名無法唯一指定。`path` / `allocate` 兩者皆須提供。
- `--session SESSION_ID`：`release` 欲釋放的 session 識別碼（如 `s-0001`）。
- `--state STATE_JSON`：session ledger 狀態檔路徑，預設
  `.topo-state/allocations.json`（相對於**當前工作目錄**）。跨行程一致性需使用
  同一 cwd，或一致地以 `--state` 明確指定同一狀態檔。

載入、路徑查找或分配失敗時，會在 stderr 印一行清楚的錯誤訊息並以非零 exit code
結束（不會冒出 Python traceback）。

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
