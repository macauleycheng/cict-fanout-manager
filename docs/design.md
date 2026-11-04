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

### 互聯 REST（帳本層，API 0.5.0）
互聯功能（§12）同樣提供 REST，與打流側對等，但**只做帳本層**：不連任何設備。
設備下發（apply、park、verify、帶設備的 release）僅限 CLI，這是刻意的範圍界線。
所有端點掛在 `/interconnect` 之下，為 `InterconnectAllocator` 的薄層（HTTP、
Pydantic、例外映射），不重寫配對、衝突與 VLAN 邏輯：
- `GET    /interconnect/summary` → 組、組內 DUT 編號（d0..d3）、各互聯 fanout 的 port 使用
- `GET    /interconnect/resources/available` → 每組目前未佔用的 il_port 與可建的 link
- `GET    /interconnect/sessions` → 列出 active 互聯 session
- `POST   /interconnect/sessions` → 建立 session；請求為 `group` 加「`duts` + `profile`」
  或「明確 `pairs`」二選一（語意同 CLI 的 `--group/--duts/--profile/--pair`）；
  回應含 `session_id`、每條 link 的 VLAN 與兩端 port
- `DELETE /interconnect/sessions/{session_id}` → 釋放，等同 `release --ledger-only`（不連設備）
- `POST   /interconnect/plan`、`GET /interconnect/sessions/{session_id}/plan`、
  `GET /interconnect/plan/park` → 純計算「若下發會下哪些指令」，不連線、不入帳

設計重點：
- **帳本與環境變數**：互聯帳本獨立於打流側。`TOPO_MANAGER_IC_STATE`（預設
  `.topo-state/interconnect.json`）、`TOPO_MANAGER_IC_VLAN_MIN` / `TOPO_MANAGER_IC_VLAN_MAX`
  （預設 100 / 3000）。API 與 CLI 指向同一檔案時，靠既有檔鎖保持一致。
- **主執行緒**：寫入端點與打流側相同，宣告為 `async def` 在主執行緒事件迴圈執行
  （`store.locked()` 用 SIGALRM 取鎖，只能在主執行緒）。allocator 每請求新建，並在鎖內
  重載帳本。取捨：取鎖等待期間（最長一個逾時，預設 10 秒）事件迴圈被卡住，連打流側
  與唯讀端點也無法回應；擴充請用多行程，不可用多執行緒 worker。
- **拓樸沒有 interconnect 區段**：app 照常啟動，既有端點不受影響；互聯端點回 404
  （`InterconnectNotConfiguredError`，說明此拓樸沒有互聯區段），不回 500。
- **錯誤映射**（沿用 `{"error": {"type", "message"}}`）：

| 例外 | HTTP |
| --- | --- |
| `InterconnectRequestError`（跨組、未知 DUT、2 台 ring、重複 port、二選一違反）、請求驗證失敗 | 400 |
| `InterconnectConflictError`（port 衝突）、`VlanPoolExhaustedError` | 409 |
| `InterconnectSessionNotFoundError`、`InterconnectNotConfiguredError` | 404 |
| `SessionStoreError`（帳本 I/O、取鎖逾時） | 503 |
| 其他非預期例外 | 500（固定訊息，不含 traceback） |

  `InterconnectLayoutError`（拓樸互聯區段不合法）**刻意不映射**：載入時已 fail-fast，
  請求期理論上不會觸發；若觸發則落 500，避免改變打流側拓樸漂移時的既有映射。
  `status_for` 以精確型別查表，故各子類皆顯式列出。
- **版本**：新增端點屬 API 契約擴充，`_API_VERSION` 由 0.4.0 升為 0.5.0。

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
- [x] 互聯 fanout 的互聯規則（同款 4 台如何互聯）—— 已決：見 §12（方向 1：802.1Q access VLAN，每條 link 一個 VLAN）
- [ ] 下發失敗的回滾機制
- [ ] Leaf port 分配微調（5 台 leaf，第 5 台僅用約 32 port）

---

## 11. 開發階段建議

1. **Phase 1**：資料模型 + loader + 拓樸圖（能讀檔、驗證、視覺化、查路徑）
2. **Phase 2**：衝突偵測 + VLAN 分配（純算，dry-run）
3. **Phase 3**：FastAPI + ATLAS 介面
4. **Phase 4**：fanout 配置下發（EC driver，SNMP / SSH）+ 重開機偵測
5. **Phase 5**：Web UI 視覺化

---

## 12. 互聯 fanout 設計

### 12.1 定位

互聯 fanout 讓同一組（4 台）DUT 之間彼此連線（mesh / line / ring），與打流側
完全分離：使用另一批專屬的互聯 fanout，不與打流 fanout 共用。一台互聯 fanout
可承載多組 DUT（一組用 16 port，48 port 承載 3 組），達到共享。

隔離方式採方向 1：**802.1Q access VLAN，每條 link 一個專屬 VLAN**。link 兩端的
fanout port 以 untagged 加入該 VLAN 並設為 native VLAN，其他 port 不在其中。
互聯功能有獨立的圖、帳本與子命令，既有打流側命令、輸出與帳本格式不變。

### 12.2 YAML 區段與欄位

頂層選填 `interconnect:`，內含 `fanouts`（名稱、port_count、rack）與 `links`
（DUT 的 `il_ports` ↔ 互聯 fanout port）。參與互聯的 DUT 多三個欄位：`group`
（同組標籤）、`slot`（1..4，決定組內名次 d0..d3）、`il_ports`（4 個互聯 port，
依序為 p0..p3）。載入時對該區段獨立驗證：每個 il_port 恰接一個 fanout port、
fanout port 不重複使用、同組 DUT 需同 rack 且接在同一台互聯 fanout。

### 12.3 獨立小圖

載入驗證後建立 `IcGraph`，與打流側 `TopologyGraph` 零共用節點與程式碼；同一
DUT 名會同時出現在兩張圖但彼此不引用。節點為 `<dut>:<il_port>` 與
`<fanout>:<port>`，每條 link 一條邊。

### 12.4 配對表與 profile

組內固定配對：DUT i 對鄰居 j 所用 port 序 = j 在 i 的其他組員（升冪）中的名次。
對 i < j，i 端用 p(j-1)，j 端用 p(i)。

| 配對 | 第一台 port | 第二台 port |
| --- | --- | --- |
| d0-d1 | p0 | p0 |
| d0-d2 | p1 | p0 |
| d0-d3 | p2 | p0 |
| d1-d2 | p1 | p1 |
| d1-d3 | p2 | p1 |
| d2-d3 | p2 | p2 |

p3 不在表內，保留給明確 port 對（`--pair`）。配對只由兩台 DUT 的組內名次決定，
與選了哪幾台、選定順序無關。profile 對選定的 2..4 台展開：`mesh` 為所有
配對、`line` 為相鄰配對、`ring` 為 line 加首尾（2 台 ring 拒絕，3 台 ring 等同
mesh）。

### 12.5 allocator 與衝突

衝突只看 fanout port（硬衝突鍵 `<fanout>:<port>`）。同組可同時存在多個 port
互不重疊的 session，例如 `{d0,d1}` 與 `{d2,d3}`。四台 ring 若按 d0,d1,d2,d3
選會用到 d0-d1 的 port，與 `{d0,d1}` 衝突；按 d0,d2,d1,d3 選則 port 不重疊，
可共存。DUT 層級的獨占由 ATLAS 預約負責，本系統不加限制。每條 link 從 VLAN 池
（預設 100..3000）取一個專屬 VLAN，池耗盡時拒絕。allocator 在鎖內「重載 → 決策
→ 儲存」，失敗時回捲。

### 12.6 持久化與鎖

互聯 session 存在獨立檔案 `interconnect.json`（預設
`.topo-state/interconnect.json`），schema 與打流側帳本區隔（`kind=interconnect`），
不影響既有帳本。載入時以當前拓樸重新驗證（偵測接線漂移、配對表被改、VLAN 重複、
port 被兩個 session 佔用）。鎖分兩種：帳本鎖序列化帳本更新；設備鎖序列化所有
會寫設備的動作，鎖序固定為設備鎖（外）→ 帳本鎖（內）。

### 12.7 設備下發與 parking

- **parking**：閒置 port 為管理面 shutdown、native VLAN 1、僅屬 VLAN 1，使 DUT
  看到 link down。park 範圍為拓樸互聯 link 出現的 port（不掃全機），略過被
  session 持有的 port，只清池內殘留 VLAN，絕不動池外 VLAN（例如 4093）。
- **啟用（apply）** 兩階段：先在 port 保持 shutdown 時設定（add untagged →
  native → remove 1）並讀回，讀回通過才 `no shutdown` 再讀回，所以讀回不符不會
  留下半開的 link。全部指令冪等，失敗可重跑。
- **釋放（release）** 兩階段：先持久化 `releasing`（資源仍保留），再 park（先
  add 1 untagged，再 `no switchport native vlan`，最後 remove link VLAN）、刪
  VLAN 定義、讀回確認，最後才自帳本移除。失敗時停在 `releasing`，重跑同指令即
  可。`--ledger-only` 只改帳本。
- 指令順序是硬性要求，真機錯序會 `Failed to set PVID`，或讓 hybrid port 的 PVID
  被改成殘留 VLAN。`vlan database` 必須在全域模式，因此 VLAN 定義清除為獨立批次。
- 設備輸出以 `Failed`、`%`、`Invalid input` 等字樣檢查；清除批次容忍
  `does not exist`。互聯 fanout 在連線設定中查無即報 `ConfigError`（fail closed）。
- 預設 dry-run，只有 `--apply` 才連線；dry-run 與 plan 不連線。

### 12.8 真機驗證

以 ECS5550-54X 的 Eth1/42（access）與 Eth1/43（hybrid，帶 4093 tagged）實測，
只動這兩個 port 與 VLAN 3999（`tests/test_ic_live.py`，`live` marker，缺連線
env 一律 skip）。情境 A 跑完整 allocate → apply → verify → release，情境 B 補驗
`switchport mode access` 序列；結束後以新 SSH 連線複驗 VLAN 3999 不存在、兩個
port 回到測前狀態（4093 未被修改）。以 `pytest -s` 執行可印出實際下發的指令與
各階段的 `show` 原始輸出。
注意 release 的 parking 語意是 shutdown，所以釋放後管理面為 Down；live 測試在
斷言 parking 狀態之後自行 `no shutdown` 還原測前的管理面狀態。

### 12.9 已知限制

- 不保證保留 DUT 自己送出的內層 VLAN tag（未驗證）。
- slow protocol（LACP / LLDP / STP）透通須在 switch 端自行設定，本系統不設定
  也不驗證 `l2protocol-tunnel`。
- 不使用 QinQ：QinQ access port 之間無法互通，且互聯 fanout 沒有 uplink port。
- REST 僅做帳本層（見 §9「互聯 REST」），設備下發（apply / park / verify）僅限
  CLI；沒有 Web UI 頁籤。
- 每個 port 需數次 `show` 讀回，大範圍 park 較慢，可用 `--fanout` / `--ports`
  縮小範圍。
