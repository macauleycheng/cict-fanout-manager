# topo-manager（拓樸管理工具）Phase 1

拓樸管理 CLI 工具。讀取描述打流環境的拓樸 YAML（generator、fanout、rack/DUT
與實體連線），驗證其正確性後建成有向多層圖，印出摘要，並可查詢任一
generator port 到任一 DUT port 的實體路徑。

本階段（Phase 1）為命令列骨架，聚焦「載入 → 驗證 → 建圖 → 摘要 / 路徑查找」。

## 安裝

需 Python 3.8+。於專案根目錄以可編輯模式安裝（含測試相依）：

```bash
pip install -e ".[dev]"
```

相依：`pydantic`（2.5~2.10）、`networkx`（3.1，Python 3.8 可用的最高版）、
`PyYAML`；測試用 `pytest`。

## 執行

```bash
python -m topo_manager <topology.yaml> [--from GEN_PORT --to DUT_PORT]
```

- 位置參數 `<topology.yaml>`：拓樸 YAML 檔路徑（必填）。
- `--from GEN_PORT`：generator 端 port，可裸名（如 `ixia-1/1`）或 `dev:port`。
- `--to DUT_PORT`：DUT 端點，**一律 `dev:port` 形式**（如 `dut-A1:Eth0`）；
  因 tg_port 名稱會跨 DUT 重複，裸名無法唯一指定。
- `--from` 與 `--to` **須成對提供**：兩者皆給才做路徑查找；只給其一會得到參數
  錯誤；皆不給則只印摘要。

載入或路徑查找失敗時，會在 stderr 印一行清楚的錯誤訊息並以非零 exit code 結束
（不會冒出 Python traceback）。

### 兩層範例（Core → Leaf → DUT）

```bash
python -m topo_manager examples/topology-2tier.yaml --from ixia-1/1 --to dut-A1:Eth0
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
python -m topo_manager examples/topology-3tier.yaml --from ixia-1/1 --to dut-A1:Eth0
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
