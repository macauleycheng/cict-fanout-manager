# 拓樸資源管理系統設計提案

> 狀態：設計討論稿（尚未開始實作）
> 目標系統：管理機櫃上 DUT switch 與 IXIA/Trex packet generator 之間、經多層 fanout switch 的連線資源，偵測衝突、分配 VLAN、下發 fanout 設定，並把可用資源提供給 ATLAS 測試系統。

---

## 1. 名詞與定位

| 名詞 | 說明 |
|------|------|
| DUT | 待測 switch，機櫃上的受測設備 |
| Rack | 機櫃，固定 24 台 DUT = 6 款 × 4 台一組 |
| Fanout switch | 扇出交換機，48 port，可多層串接；使用 EC enterprise switch（支援 SNMP / SSH CLI） |
| Packet Generator | 打流來源：IXIA(4 port) 與 Trex(8 port) 同時存在 |
| 打流鏈路 | 每台 DUT 的 8 port → packet-generator 側 fanout |
| 互聯鏈路 | 每台 DUT 的 4 port → 共用互聯 fanout（同款 4 台互聯） |
| 邏輯路徑 | 一條 `generator port ↔ (多層 fanout) ↔ DUT port` 的端到端路徑 |
| ATLAS | 下游測試執行系統，消費本系統輸出的可用資源 |

**系統定位**：拓樸資料管理 + 資源分配/衝突檢查 + fanout VLAN 配置下發 + 對 ATLAS 的資源 API。
**設計參照**：SONiC PTF testbed —— 以拓樸檔描述接線，依測試需求把 DUT port 經 fanout 動態導向有限的 generator port。

---

## 2. 核心資源落差（為什麼需要動態分配）

單 rack：
- 每 rack 打流需求：24 DUT × 8 port = **192 port**

6 rack（全系統）：
- 打流需求：6 × 192 = **1152 個 DUT 打流 port**
- generator 實體：IXIA 4 + Trex 8 = **12 port**
- 比例約 **96 : 1**

結論：不可能靜態一次接滿。fanout 是一個大型 selector（選路矩陣），任一時刻只有 ≤12 條 DUT port 真正連到 generator。fanout 的工作是「能選中任意 12 條」，而不是「全部接通」。依測試需求動態把要用的 DUT port 經 fanout VLAN 導到空閒 generator port。

**使用前提（已與需求方確認）：**
- 可用 fanout 數量很多 → 以結構清楚、好排錯為優先，不做激進 oversubscription。
- 可接受「同一時間只有部分 rack 的 DUT 掛在 fanout 上」→ 不需讓 1152 port 物理上隨時全可達。

---

## 3. 衝突模型

| 類型 | 定義 | 處理 |
|------|------|------|
| 硬衝突 | 兩條邏輯路徑佔用**同一實體 port** | 禁止，分配失敗 |
| 軟衝突 | 兩條路徑共用某段 **fanout 間 trunk 鏈路** | 允許，但必須分配**不同 VLAN** 隔離 |

本階段不計算 trunk 頻寬（依指示先略過）。

---

## 4. Fanout 拓樸結構（6 Rack，Packet-Generator 側）

採三層 fanout 樹，以 rack 為單位、可彈性掛載。

```
            [ IXIA 4 + Trex 8 = 12 generator ports ]
                          │
                    ┌─────┴─────┐
                    │  Spine     │   1 台（選路核心）
                    └─────┬─────┘
       ┌──────────┬───────┼───────┬──────────┐
    [Agg-1]   [Agg-2]  [Agg-3]  ...        [Agg-6]
    每 rack 1 台（該 rack 的掛載閘門）
       │
   ┌───┴────────────┐
 [Leaf-1-1]...[Leaf-1-5]   每 rack 5 台 leaf，接滿該 rack 192 DUT port
```

### Port 分配總表

| 層 | 角色 | 每台 port 分配 | 台數 | 備註 |
|----|------|----------------|------|------|
| Leaf | 直接接 DUT 打流 port | 40 下行(DUT) + 8 上行(Agg) | 每 rack 5 → **30** | 每 rack 192 DUT port，固定接線一次到位 |
| Agg | 每 rack 匯聚 / 掛載閘門 | 40 下行(Leaf) + 上行(Spine) | 每 rack 1 → **6** | rack 掛載與否由 Agg↔Spine VLAN 控制 |
| Spine | 選路核心 / 接 generator | 6 下行(Agg) + 12 上行(Gen) = 18 | **1** | 18 < 48，不會成為瓶頸 |
| Generator | 打流來源 | — | IXIA 4 + Trex 8 = **12 port** | 同時最多 12 條活躍路徑 |

合計約 **37 台 fanout**（30 Leaf + 6 Agg + 1 Spine）。

### 關鍵設計意圖

1. **Leaf→DUT 線固定**：接一次就不動，拓樸檔穩定，對「人工接好線再填文件」流程最友善。
2. **掛載彈性在 Agg↔Spine 用 VLAN 控制**：換 rack 不需重插線，由系統下發 fanout VLAN。
3. **一次最多 12 條活躍路徑**（generator 上限），故上層不會壅塞；衝突管理聚焦共用 trunk 的 VLAN 隔離。
4. **重開機偵測**：系統須檢查 fanout 是否重啟（設定遺失），若是則重新下發當前活躍 VLAN。

### 活躍路徑讀法範例

```
ixia-1/1 → Spine:p37 → Spine:p1 → Agg-1:up → Agg-1:p3 → Leaf-1-1:up → Leaf-1-1:p5 → DUT-A1:Eth0
```
此路徑經過的每段 Spine↔Agg、Agg↔Leaf trunk 都配同一 VLAN（例如 VLAN 101）。

> 視覺化請見同目錄 `topology.html`（三層）、`topology-2tier.html`（兩層）。

### 4.1 部署策略：兩層起步，漸進長出三層

本系統以**三層為目標模型**設計，但**實際部署從兩層開始**，待 rack / DUT
增長後再加上 spine 層，升級為三層。關鍵要求：**從兩層到三層是平滑演進，
不需重建系統、不需改資料模型或演算法。**

| 階段 | 結構 | 時機 |
|------|------|------|
| 起步 | **兩層**：Core（Spine+Agg 合一）→ Leaf → DUT | rack / DUT 數量少時 |
| 成長 | **三層**：Spine → Agg → Leaf → DUT | rack / DUT 變多，Core port 接近上限時 |

**對程式架構的硬性約束（Phase 1 必須滿足）：**

- fanout 的「層」**不可寫死為 2 或 3**，必須建模為**通用多層結構**。兩層與
  三層只是同一資料模型在不同層數下的實例。
- 升級（長出 spine）在操作上等於：在拓樸 YAML 新增一層 spine 節點 + 調整
  Core 的上行接線，**資料模型與圖演算法不變**。
- 拓樸引擎（networkx）把 fanout 建成通用有向多層圖；路徑查找、衝突偵測一律
  走圖演算法，天然支援任意層數。

> 因此 Phase 1 的資料模型與引擎必須以「通用多層」為前提實作，並同時提供
> 兩層與三層兩份範例拓樸，證明同一套模型兩者都能正確載入與查路徑。

---

## 5. 資料模型（拓樸輸入文件）

採 **YAML**，用 **Pydantic** 驗證。

### 5.1 設備與 port 清單

```yaml
version: 1
site: lab-a

generators:
  - name: ixia-1
    type: ixia
    ports: [ixia-1/1, ixia-1/2, ixia-1/3, ixia-1/4]
  - name: trex-1
    type: trex
    ports: [trex-1/0, trex-1/1, trex-1/2, trex-1/3,
            trex-1/4, trex-1/5, trex-1/6, trex-1/7]

fanouts:
  - name: spine-1
    role: spine
    port_count: 48
    mgmt_ip: 10.0.0.1
    driver: ec            # EC enterprise switch
    access: ssh           # ssh | snmp
  - name: agg-1
    role: agg
    rack: rack-1
    port_count: 48
    mgmt_ip: 10.0.0.11
    driver: ec
    access: ssh
  - name: leaf-1-1
    role: leaf
    rack: rack-1
    port_count: 48
    mgmt_ip: 10.0.0.101
    driver: ec
    access: ssh

racks:
  - name: rack-1
    duts:
      - name: dut-modelA-1
        model: modelA
        slot: 1
        group: modelA        # 同款 4 台一組
        tg_ports:  [Eth0, Eth1, Eth2, Eth3, Eth4, Eth5, Eth6, Eth7]  # 8 打流
        il_ports:  [Eth8, Eth9, Eth10, Eth11]                         # 4 互聯
      # ... 其餘 23 台
```

### 5.2 接線（edge）清單

```yaml
links:
  # DUT 打流 port -> leaf fanout
  - a: {dev: dut-modelA-1, port: Eth0}
    b: {dev: leaf-1-1,     port: 5}
  # leaf -> agg（上行 trunk）
  - a: {dev: leaf-1-1, port: 41}
    b: {dev: agg-1,     port: 3}
  # agg -> spine（上行 trunk）
  - a: {dev: agg-1,   port: 41}
    b: {dev: spine-1, port: 1}
  # generator -> spine
  - a: {dev: ixia-1,  port: ixia-1/1}
    b: {dev: spine-1, port: 37}
  # DUT 互聯 port -> interconnect fanout（互聯側，規則待定義）
  - a: {dev: dut-modelA-1, port: Eth8}
    b: {dev: fanout-il-1,  port: 1}
```

> 接線用無向 edge；方向（上行/下行）由拓樸引擎依 role 推導。

---

## 6. 拓樸引擎（networkx）

- generator port、fanout port、DUT port 全部建成圖的節點，edge = 實體線 + 設備內部 port 連通關係。
- **路徑查找**：給定 `(generator_port, dut_port)`，用最短路徑找出經過的 fanout 鏈路。
- **硬衝突檢查**：維護「已佔用實體 port」集合，新路徑撞到即失敗。
- **軟衝突 / VLAN 分配**：對共用 trunk 的路徑，從 VLAN pool 配不重複 ID。

---

## 7. VLAN 分配與 fanout 配置下發

### 7.1 VLAN 分配
- 系統維護 VLAN pool（例如 100–3000，可設定）。
- 每條邏輯路徑（或每個 test session）分到一個 VLAN，套用到該路徑經過的 fanout trunk port。

### 7.2 配置下發（執行層）
- 抽象 `FanoutDriver` 介面：`apply_vlan(fanout, port, vlan)`、`clear_vlan(...)`、`read_config(...)`。
- 實作 `EcFanoutDriver`（EC enterprise switch，支援 SNMP / SSH CLI）；介面保留讓之後接其他廠牌。
- 下發採「期望狀態 → diff → 套用」，支援 dry-run 先輸出將執行的指令。
- **重開機偵測**：定期 / 按需 `read_config` 比對期望狀態，若 fanout 重啟導致設定遺失，重新下發當前活躍 VLAN。

> ⚠️ 下發是對實體設備的高風險動作：預設 dry-run，實際套用需明確確認。

---

## 8. 系統架構（Python / FastAPI）

```
topo-manager/
├── models/          # Pydantic schema（拓樸、資源、session）
├── loader/          # YAML 讀取 + 驗證
├── topology/        # networkx 圖引擎、路徑查找
├── allocator/       # 衝突偵測 + VLAN 分配
├── drivers/         # FanoutDriver 抽象 + EC 實作（SNMP / SSH）
├── api/             # FastAPI route
├── web/             # 前端（拓樸視覺化、資源狀態）
└── store/           # session/分配狀態（記憶體為主，不強制持久化）
```

- **FastAPI** 同時提供 Web UI 與 REST（自帶 OpenAPI 文件，方便 ATLAS 介接）。
- 狀態持久化：理論上不需要；重點放在偵測 fanout 重開機並重新配置。

---

## 9. 對 ATLAS 的介面

兩種併存：

### REST
- `GET  /api/resources/available` → 可用的 generator↔DUT 配對清單
- `POST /api/sessions` → 要求一組 `(generator_port, dut_port)` 分配，回傳分配結果 + 指定 VLAN
- `DELETE /api/sessions/{id}` → 釋放資源、清 VLAN
- `GET  /api/topology` → 完整拓樸（給視覺化）

### 檔案匯出
- `GET /api/resources/export?format=yaml` 匯出同等內容成檔，供 ATLAS 直接讀。

回傳 payload 範例：
```json
{
  "session_id": "s-001",
  "mappings": [
    {"generator": "ixia-1/1", "dut": "dut-modelA-1:Eth0", "vlan": 101,
     "path": ["ixia-1/1", "spine-1:37", "spine-1:1", "agg-1:41", "agg-1:3",
              "leaf-1-1:41", "leaf-1-1:5", "dut-modelA-1:Eth0"]}
  ],
  "status": "allocated"
}
```

---

## 10. 待辦 / 風險

- [x] 確認「動態 session 分配」模型 —— 已確認符合
- [x] fanout 存取方式 —— EC enterprise switch，SNMP / SSH CLI
- [x] 持久化策略 —— 不強制持久化，但須偵測 fanout 重開機並重配
- [ ] 互聯 fanout 的互聯規則（同款 4 台如何互聯）—— 需求方稍後提供想法
- [ ] 下發失敗的回滾機制
- [ ] Leaf port 分配微調（5 台 leaf，第 5 台僅用約 32 port）

---

## 11. 開發階段建議

1. **Phase 1**：資料模型 + loader + 拓樸圖（能讀檔、驗證、視覺化、查路徑）
2. **Phase 2**：衝突偵測 + VLAN 分配（純算，dry-run）
3. **Phase 3**：FastAPI + ATLAS 介面
4. **Phase 4**：fanout 配置下發（EC driver，SNMP / SSH）+ 重開機偵測
5. **Phase 5**：Web UI 視覺化
