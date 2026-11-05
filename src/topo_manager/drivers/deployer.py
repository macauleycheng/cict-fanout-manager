"""fanout 下發編排層（deployer.py，設計 §7）。

本模組把「純邏輯 planner」與「具體 driver」編排成可供 CLI 呼叫的四個高階動作，
並集中管理 dry-run/apply 閘門與回讀（read-back）驗證：

  - plan(session)：純算——呼叫 ``planner.plan_changes`` 得各 fanout 的變更，再
    以各 driver 的 ``build_commands`` 組裝指令；絕不連線。無連線設定的 fanout
    仍列出但明確標示 apply 將跳過。
  - apply(session, apply=False)：唯一的 dry-run/apply 閘門。``apply=False`` 僅
    印 DRY-RUN、不連線、不寫入；``apply=True`` 才對「有連線設定」的 fanout
    connect → read_vlans 快照（以 tagged 成員跳過已滿足項）→ apply_vlan 未跳過
    項 → read-back 以 ``diff_expected_vs_actual`` 驗證缺漏（有缺漏丟
    ``ReadBackMismatchError``）→ disconnect。部分失敗時 best-effort clear_vlan
    已套用項並包成 ``ApplyError``。
  - verify(session)：唯讀——對各 fanout read_vlans 後 diff，回報仍缺漏項。
  - detect_and_reapply(session, apply)：重開機偵測——read_vlans → diff 缺漏 →
    （apply 時）重套缺漏項 → 再比對（仍缺漏丟 ``ReadBackMismatchError``）。

狀態不使用全域可變狀態：``Deployer`` 以 ``driver_factory`` 與 ``config`` 為實例
屬性，driver 生命週期（connect/disconnect）於各動作內以 try/finally 管理。本模組
不 import netmiko（driver 的 netmiko 相依於其 connect 內延遲 import）。

歷史（HISTORY）：
  - 0.4.0  FEAT-002 初版，建立 Deployer（plan 純算、apply 的 dry-run/apply
           單一閘門與 read-back 驗證、tagged 快照跳過、部分失敗 best-effort
           clear + ApplyError、verify 唯讀、detect_and_reapply 重開機偵測
           重套）。不 import netmiko。
  - 0.5.0  Phase 4 QinQ/dot1q-tunnel 擴充（決策 7、8、9）：``_best_effort_clear``
           過濾只 clear UPLINK（產品路徑不動邊緣 port）；新增 ``SetupResult``
           dataclass 與 ``setup_tunnel`` 方法（全域 dot1q-tunnel setup 的
           dry-run/apply 閘門）；``_format_missing`` 依 role 分述診斷字串
           （UPLINK「tagged 成員」、EDGE「tunnel access 設定」）。
           plan/apply/verify/detect_and_reapply 核心邏輯不變。不 import netmiko。
  - 0.6.0  FEAT-001 撤下發：新增 ``remove(session, *, apply=False)``（與 apply
           對稱的反向動作）。dry-run 建**自己的** List[FanoutPlan]（不重用
           plan()——FanoutPlan frozen 且 plan() 組 add 指令），把各 fanout 變更
           改寫為 action='remove' 後以 build_commands 組 remove 分支指令、絕不
           連線；apply 對各 fanout connect → clear_vlan（同時拆 EDGE 與
           UPLINK，顯式 Release 連邊緣一併拆，有別於 _best_effort_clear 僅動
           UPLINK）→ read_vlans → role-split remove 回讀 → disconnect。新增
           ``_remove_one_fanout`` / ``_remove_remaining``（role-split 撤除判
           定：EDGE 以 tunnel_modes 判、UPLINK 以 tagged 成員判，鏡像
           diff_expected_vs_actual 的 role 分流）/ ``_format_remaining``。跨
           fanout best-effort 依序處理，遇硬性 driver 失敗原樣重拋結構化 driver
           例外（不包 ApplyError、不重加）；回讀殘留丟 ReadBackMismatchError
           （409）。不 import netmiko。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from topo_manager.drivers.base import (
    FanoutDriver,
    PortRole_E,
    PortVlanChange,
    VlanState,
)
from topo_manager.drivers.planner import diff_expected_vs_actual, plan_changes
from topo_manager.errors import ApplyError, ReadBackMismatchError


@dataclass(frozen=True)
class FanoutPlan:
    """單一 fanout 的下發計畫（不可變中繼值，設計 §7）。

    欄位：
      - fanout：fanout 裝置名（str）。
      - configured：此 fanout 是否有連線設定（bool）；False 表 apply 將跳過。
      - changes：本 fanout 的 ``PortVlanChange`` 清單（已依 interface 排序）。
      - commands：以 driver.build_commands 組裝的設定指令清單（List[str]）；
        無連線設定時為空清單。
    """

    fanout: str
    configured: bool
    changes: List[PortVlanChange]
    commands: List[str]


@dataclass(frozen=True)
class PlanResult:
    """一筆 session 的完整下發計畫（不可變中繼值，設計 §7）。

    欄位：
      - session_id：來源 session 識別碼（str）。
      - vlan：session 的 VLAN（int）。
      - fanouts：各 fanout 的 ``FanoutPlan`` 清單（依 fanout 名排序）。
    """

    session_id: str
    vlan: int
    fanouts: List[FanoutPlan]


@dataclass(frozen=True)
class VerifyResult:
    """verify/detect 的回讀驗證結果（不可變中繼值，設計 §7）。

    欄位：
      - session_id：來源 session 識別碼（str）。
      - missing_by_fanout：各 fanout 仍缺漏的 ``PortVlanChange`` 清單
        （Dict[fanout 名, List[PortVlanChange]]；全部達成時為空 dict）。
    """

    session_id: str
    missing_by_fanout: Dict[str, List[PortVlanChange]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否全部達成（無任何 fanout 有缺漏）。"""
        return not self.missing_by_fanout


@dataclass(frozen=True)
class SetupResult:
    """單一 fanout 的全域 dot1q-tunnel setup 結果（不可變中繼值，決策 8）。

    欄位：
      - fanout：fanout 裝置名（str）。
      - commands：將下發（dry-run）或已下發（apply）的指令清單（List[str]）。
      - status：apply 後回讀的全域 802.1Q Tunnel Status（Optional[bool]）；
        dry-run 為 None（未連線、未回讀）。
    """

    fanout: str
    commands: List[str]
    status: Optional[bool] = None


class Deployer:
    """fanout 下發編排器（設計 §7）。

    狀態封裝於實例屬性：
      - ``self._driver_factory``：以 (fanout 名, FanoutEntry) 建立 ``FanoutDriver``
        的工廠（Callable）；供測試注入 mock driver。
      - ``self._config``：已載入的 ``FanoutConfig``（含 ``fanouts`` 映射）。
    """

    def __init__(
        self,
        driver_factory: Callable[[str, object], FanoutDriver],
        config: object,
    ) -> None:
        """記錄 driver 工廠與連線設定（設計 §7）。

        INPUT：
          - driver_factory：``(fanout_name, FanoutEntry) -> FanoutDriver`` 工廠。
          - config：``FanoutConfig``（須具 ``fanouts`` 屬性，Mapping[str,
            FanoutEntry]）。

        OUTPUT：無。

        RETURN：None。

        例外：無（純屬性賦值）。
        """
        self._driver_factory = driver_factory
        self._config = config

    def _configured_fanouts(self) -> Dict[str, object]:
        """回傳有連線設定的 fanout 映射（``{名: FanoutEntry}``）。"""
        return dict(self._config.fanouts)

    def _templates(self) -> Dict[str, object]:
        """回傳各 fanout 的 interface_template（``{名: template}``）。"""
        return {
            name: getattr(entry, "interface_template", None)
            for name, entry in self._config.fanouts.items()
        }

    def _changes_by_fanout(
        self, session: object
    ) -> Dict[str, List[PortVlanChange]]:
        """以 planner 純算各 fanout 的變更，依 fanout 分組（設計 §7.1）。

        INPUT：
          - session：一筆已分配的 Session（具 vlan / trunk_segments）。

        OUTPUT：無。

        RETURN：
          - ``{fanout 名: List[PortVlanChange]}``（僅含有連線設定且有變更者）。

        例外：
          - PortMappingError：trunk 端 port 無法對映（由 planner 丟出）。
        """
        configured = self._configured_fanouts()
        all_changes = plan_changes(session, configured, self._templates())
        grouped: Dict[str, List[PortVlanChange]] = {}
        for change in all_changes:
            grouped.setdefault(change.fanout, []).append(change)
        return grouped

    def plan(self, session: object) -> PlanResult:
        """純算下發計畫（絕不連線，設計 §7.1）。

        對每個「有連線設定」的 fanout，以 planner 算出變更並以其 driver 的
        ``build_commands`` 組裝指令；無連線設定的 fanout 不會出現在 planner
        結果中，但若其曾參與 trunk 仍無法得知——plan 僅就「有設定」的 fanout
        產生計畫，並對「有設定但無任何變更」者仍輸出空計畫以利檢視。

        INPUT：
          - session：一筆已分配的 Session。

        OUTPUT：無。

        RETURN：
          - ``PlanResult``（fanouts 依 fanout 名排序）。

        例外：
          - PortMappingError：trunk 端 port 無法對映（由 planner 丟出）。
        """
        grouped = self._changes_by_fanout(session)
        fanout_plans: List[FanoutPlan] = []

        for name in sorted(self._config.fanouts.keys()):
            changes = grouped.get(name, [])
            commands: List[str] = []
            if changes:
                driver = self._driver_factory(name, self._config.fanouts[name])
                commands = driver.build_commands(changes)
            fanout_plans.append(
                FanoutPlan(
                    fanout=name,
                    configured=True,
                    changes=changes,
                    commands=commands,
                )
            )

        return PlanResult(
            session_id=getattr(session, "session_id", "(unknown)"),
            vlan=getattr(session, "vlan", 0),
            fanouts=fanout_plans,
        )

    def apply(self, session: object, *, apply: bool = False) -> PlanResult:
        """唯一的 dry-run/apply 閘門（設計 §7.2）。

        ``apply=False``（預設）：僅回傳計畫（呼叫端印 DRY-RUN），絕不連線、不
        寫入。``apply=True``：對每個「有變更」的 fanout 執行
        connect → read_vlans 快照（以 (interface, vlan, tagged=True) 跳過已滿足
        項）→ apply_vlan 未跳過項 → read-back 以 ``diff_expected_vs_actual``
        驗證（缺漏丟 ``ReadBackMismatchError``）→ disconnect。任一 fanout 失敗
        時，對「本次已成功套用」的 fanout best-effort ``clear_vlan`` 回滾，並
        包成 ``ApplyError``（保留 ``__cause__``）。

        INPUT：
          - session：一筆已分配的 Session。
          - apply：True 才真正連線寫入；False 為 dry-run（keyword-only）。

        OUTPUT：無。

        RETURN：
          - ``PlanResult``（dry-run 與 apply 皆回傳計畫，供呼叫端輸出）。

        例外：
          - ReadBackMismatchError：apply 後回讀仍有缺漏（已盡力清理）。
          - ApplyError：連線/下發失敗（已對已套用 fanout best-effort 清理，
            保留 ``__cause__``）。
          - PortMappingError：trunk 端 port 無法對映（純算階段丟出）。
        """
        plan_result = self.plan(session)
        if not apply:
            return plan_result

        # 記錄本次已成功套用的 fanout 與其變更，供失敗時 best-effort 回滾。
        applied: List[tuple] = []

        for fanout_plan in plan_result.fanouts:
            if not fanout_plan.changes:
                continue
            try:
                self._apply_one_fanout(fanout_plan)
                applied.append((fanout_plan.fanout, fanout_plan.changes))
            except ReadBackMismatchError:
                # 回讀缺漏：清理含本 fanout 在內的已套用項後原樣重拋。
                applied.append((fanout_plan.fanout, fanout_plan.changes))
                self._best_effort_clear(applied)
                raise
            except Exception as exc:  # noqa: BLE001
                # 下發/連線失敗：清理先前已成功的 fanout 並包成 ApplyError。
                self._best_effort_clear(applied)
                if isinstance(exc, ApplyError):
                    raise
                raise ApplyError(
                    "下發 session {0} 至 fanout {1} 失敗：{2}".format(
                        plan_result.session_id,
                        fanout_plan.fanout,
                        type(exc).__name__,
                    )
                ) from exc

        return plan_result

    def _apply_one_fanout(self, fanout_plan: FanoutPlan) -> None:
        """對單一 fanout 執行連線→快照跳過→下發→回讀驗證（設計 §7.2）。

        INPUT：
          - fanout_plan：本 fanout 的 ``FanoutPlan``（changes 非空）。

        OUTPUT：無。

        RETURN：None。

        例外：
          - ReadBackMismatchError：回讀後仍有缺漏（逐條列出）。
          - 其他：連線/下發例外原樣往外傳（由 apply 包成 ApplyError）。
        """
        driver = self._driver_factory(
            fanout_plan.fanout, self._config.fanouts[fanout_plan.fanout]
        )
        driver.connect()
        try:
            # 套用前快照：以 tagged 成員跳過已滿足項（設計 §7.2）。
            snapshot = driver.read_vlans()
            pending = diff_expected_vs_actual(fanout_plan.changes, snapshot)
            if pending:
                driver.apply_vlan(pending)

            # read-back：重讀並驗證期望全部達成。
            after = driver.read_vlans()
            missing = diff_expected_vs_actual(fanout_plan.changes, after)
            if missing:
                raise ReadBackMismatchError(
                    self._format_missing(fanout_plan.fanout, missing)
                )
        finally:
            driver.disconnect()

    def _best_effort_clear(self, applied: List[tuple]) -> None:
        """對已套用的 fanout best-effort 清除（只 clear UPLINK，決策 7）。

        產品路徑（apply 失敗回滾）絕不還原邊緣 port：傳給 ``driver.clear_vlan``
        前先過濾只留 UPLINK 變更；過濾後為空則不連線（不動邊緣 port）。EDGE 的
        還原僅由 live 測試臨界清理路徑顯式呼叫 ``clear_vlan``（決策 7）。

        INPUT：
          - applied：``[(fanout 名, [PortVlanChange]), ...]`` 已套用清單。

        OUTPUT：無。

        RETURN：None。

        例外：不對外拋出（清理為 best-effort；原始失敗才是回報主因）。
        """
        for fanout_name, changes in applied:
            uplink_changes = [
                change
                for change in changes
                if change.role == PortRole_E.UPLINK
            ]
            if not uplink_changes:
                continue
            try:
                driver = self._driver_factory(
                    fanout_name, self._config.fanouts[fanout_name]
                )
                driver.connect()
                try:
                    driver.clear_vlan(uplink_changes)
                finally:
                    driver.disconnect()
            except Exception:  # noqa: BLE001 — 清理失敗不掩蓋原始錯誤。
                continue

    def remove(self, session: object, *, apply: bool = False) -> PlanResult:
        """撤下發：移除本 session 先前下發的設定並以 role-split 回讀驗證（§7）。

        與 ``apply`` 對稱的反向動作。``apply=False``（預設）：建**自己的**
        ``List[FanoutPlan]``（不重用 ``plan(session)``——``FanoutPlan`` 為 frozen
        且 ``plan`` 組的是 add 指令），對每個「有變更」的 fanout 把其變更改寫為
        ``action='remove'``（複製出新的 ``PortVlanChange``，與
        ``EcSshDriver.clear_vlan`` 內部所做改寫相同）後以
        ``driver.build_commands`` 組 remove 分支指令；絕不連線。``apply=True``：
        對每個有變更的 fanout 依序 connect → ``clear_vlan``（同時驅動 EDGE 與
        UPLINK 的 remove 分支——顯式 Release 會拆除邊緣 port，有別於
        apply-rollback 的 ``_best_effort_clear`` 僅動 UPLINK）→ ``read_vlans``
        → 以 role-split remove 判定蒐集殘留 → ``disconnect``（try/finally）。

        跨 fanout 採 best-effort 依序處理；遇第一個硬性 driver 失敗（driver 已
        包好的 ``DriverConnectionError`` / ``DeviceTimeoutError``）即原樣重拋
        結構化 driver 例外（**不**逐 fanout 包成 ``ApplyError``、**不**重加——
        release 期間無法「反撤除」）。回讀殘留則丟 ``ReadBackMismatchError``
        （REST 對應 409）。

        INPUT：
          - session：一筆已分配的 Session。
          - apply：True 才真正連線撤除；False 為 dry-run（keyword-only）。

        OUTPUT：無。

        RETURN：
          - ``PlanResult``（dry-run 與 apply 皆回傳計畫；各 fanout 的 commands
            為 remove 分支指令，VLAN 與 fanout 成員與 ``plan(session)`` 一致）。

        例外：
          - ReadBackMismatchError：撤除後回讀仍有殘留（REST 對應 409）。
          - DriverConnectionError / DeviceTimeoutError：連線/下發硬性失敗原樣
            重拋（不包成 ApplyError）。
          - PortMappingError：trunk 端 port 無法對映（純算階段丟出）。
        """
        grouped = self._changes_by_fanout(session)
        fanout_plans: List[FanoutPlan] = []

        for name in sorted(self._config.fanouts.keys()):
            changes = grouped.get(name, [])
            remove_changes: List[PortVlanChange] = []
            commands: List[str] = []
            if changes:
                driver = self._driver_factory(name, self._config.fanouts[name])
                # 把 add 變更改寫為 remove（複製新物件；FanoutPlan/PortVlanChange
                # 皆 frozen，不能就地改），與 clear_vlan 內部改寫一致。
                remove_changes = [
                    PortVlanChange(
                        fanout=change.fanout,
                        interface=change.interface,
                        vlan=change.vlan,
                        action="remove",
                        topology_node=change.topology_node,
                        role=change.role,
                    )
                    for change in changes
                ]
                commands = driver.build_commands(remove_changes)
            fanout_plans.append(
                FanoutPlan(
                    fanout=name,
                    configured=True,
                    changes=remove_changes,
                    commands=commands,
                )
            )

        plan_result = PlanResult(
            session_id=getattr(session, "session_id", "(unknown)"),
            vlan=getattr(session, "vlan", 0),
            fanouts=fanout_plans,
        )
        if not apply:
            return plan_result

        for fanout_plan in plan_result.fanouts:
            if not fanout_plan.changes:
                continue
            self._remove_one_fanout(fanout_plan)

        return plan_result

    def _remove_one_fanout(self, fanout_plan: FanoutPlan) -> None:
        """對單一 fanout 執行連線→撤除→role-split 回讀驗證（§7）。

        ``clear_vlan`` 同時驅動 EDGE 與 UPLINK 的 remove 分支（顯式 Release 連
        邊緣 port 一併拆除）。回讀後以 role-split remove 判定逐條檢查是否已拆
        除：EDGE 以 ``tunnel_modes`` 是否仍為 Access 判定（絕不以 members 判定
        ——EDGE 在成功與失敗時 members 皆為空，會誤判為通過，finding #1）；
        UPLINK 以 (interface, vlan) 是否仍在 tagged 成員中判定。

        INPUT：
          - fanout_plan：本 fanout 的 ``FanoutPlan``（changes 已改寫為 remove、
            非空）。

        OUTPUT：無。

        RETURN：None。

        例外：
          - ReadBackMismatchError：回讀後仍有殘留（逐條列出）。
          - DriverConnectionError / DeviceTimeoutError：連線/下發失敗原樣往外傳
            （release 不包成 ApplyError、不重加）。
        """
        driver = self._driver_factory(
            fanout_plan.fanout, self._config.fanouts[fanout_plan.fanout]
        )
        driver.connect()
        try:
            # 撤除：clear_vlan 以 action='remove' 改寫後走 remove 分支（已是
            # remove 的變更再改寫仍為 remove，冪等）；EDGE 與 UPLINK 都拆。
            driver.clear_vlan(fanout_plan.changes)

            # read-back：重讀並以 role-split remove 判定蒐集殘留。
            after = driver.read_vlans()
            remaining = self._remove_remaining(fanout_plan.changes, after)
            if remaining:
                raise ReadBackMismatchError(
                    self._format_remaining(fanout_plan.fanout, remaining)
                )
        finally:
            driver.disconnect()

    @staticmethod
    def _remove_remaining(
        expected: List[PortVlanChange], after: VlanState
    ) -> List[PortVlanChange]:
        """role-split 撤除回讀：回傳仍未拆除（殘留）的變更（§7、finding #1）。

        鏡像 ``planner.diff_expected_vs_actual`` 的 role 分流（EDGE 與 UPLINK 落
        在 ``VlanState`` 的不同部位）：

          - 預先算 ``tagged_present``＝``after.members`` 中 tagged 為 True 的
            (interface, vlan)；``access_present``＝``after.tunnel_modes`` 中 mode
            為 Access 的 interface。
          - EDGE：拆除完成的條件為 ``change.interface not in access_present``
            （以 tunnel_modes 判定，**絕不**用 members）。
          - UPLINK：拆除完成的條件為
            ``(change.interface, change.vlan) not in tagged_present``。

        INPUT：
          - expected：本 fanout 預期已拆除的 ``PortVlanChange`` 清單
            （action='remove'）。
          - after：撤除後回讀的 ``VlanState``。

        OUTPUT：無。

        RETURN：
          - 仍殘留（未拆除）的 ``PortVlanChange`` 清單（保留輸入順序；全部拆除
            時回空清單）。
        """
        tagged_present = {
            (member.interface, member.vlan)
            for member in after.members
            if member.tagged is True
        }
        access_present = {
            interface
            for (interface, mode) in after.tunnel_modes
            if mode == "Access"
        }

        remaining: List[PortVlanChange] = []
        for change in expected:
            if change.role == PortRole_E.EDGE:
                torn_down = change.interface not in access_present
            else:
                torn_down = (
                    change.interface,
                    change.vlan,
                ) not in tagged_present
            if not torn_down:
                remaining.append(change)
        return remaining

    @staticmethod
    def _format_remaining(
        fanout_name: str, remaining: List[PortVlanChange]
    ) -> str:
        """把撤除後殘留項依 role 分述排版成訊息（鏡像 _format_missing 風格）。

        依 ``change.role`` 分述殘留真因：UPLINK 類為「tagged 成員」仍殘留、EDGE
        類為「tunnel access 設定」仍殘留。逐條列出各 (interface, vlan)。

        INPUT：
          - fanout_name：fanout 裝置名（str）。
          - remaining：仍殘留的 ``PortVlanChange`` 清單。

        OUTPUT：無。

        RETURN：
          - 多行訊息字串（含 fanout 名與依 role 分述的各 (interface, vlan)
            殘留）。
        """
        uplink_remaining = [
            change for change in remaining if change.role == PortRole_E.UPLINK
        ]
        edge_remaining = [
            change for change in remaining if change.role == PortRole_E.EDGE
        ]

        parts: List[str] = []
        if uplink_remaining:
            items = ", ".join(
                "({0}, VLAN {1})".format(change.interface, change.vlan)
                for change in uplink_remaining
            )
            parts.append("tagged 成員仍殘留：{0}".format(items))
        if edge_remaining:
            items = ", ".join(
                "({0}, VLAN {1})".format(change.interface, change.vlan)
                for change in edge_remaining
            )
            parts.append("tunnel access 設定仍殘留：{0}".format(items))

        return "撤除回讀驗證不符（fanout {0}）：{1}".format(
            fanout_name, "；".join(parts)
        )

    def setup_tunnel(
        self, fanout_names: List[str], *, apply: bool = False
    ) -> List[SetupResult]:
        """全域 ``dot1q-tunnel system-tunnel-control`` setup 編排（決策 8）。

        對傳入的每台 fanout 冪等下發全域 tunnel 總開關：``apply=False``（預設）
        以 ``driver.setup_global_tunnel(apply=False)`` 取指令、不連線、status=
        None；``apply=True`` 則 connect → setup_global_tunnel(apply=True) →
        read_tunnel_status() → disconnect（try/finally）。setup 冪等且無跨台
        交易語意，單台失敗即往外拋（不回滾、不吞）。

        INPUT：
          - fanout_names：要操作的 fanout 名清單（List[str]）。
          - apply：True 才真正連線下發；False 為 dry-run（keyword-only）。

        OUTPUT：無。

        RETURN：
          - 每台的 ``SetupResult`` 清單（依傳入順序）。

        例外：
          - DriverConnectionError：apply 時某台連線/下發失敗（往外拋）。
        """
        results: List[SetupResult] = []
        for fanout_name in fanout_names:
            driver = self._driver_factory(
                fanout_name, self._config.fanouts[fanout_name]
            )
            if not apply:
                commands = driver.setup_global_tunnel(apply=False)
                results.append(
                    SetupResult(
                        fanout=fanout_name, commands=commands, status=None
                    )
                )
                continue
            driver.connect()
            try:
                commands = driver.setup_global_tunnel(apply=True)
                status = driver.read_tunnel_status()
            finally:
                driver.disconnect()
            results.append(
                SetupResult(
                    fanout=fanout_name, commands=commands, status=status
                )
            )
        return results

    def verify(self, session: object) -> VerifyResult:
        """唯讀驗證各 fanout 是否已達成期望（不寫入，設計 §7.3）。

        INPUT：
          - session：一筆已分配的 Session。

        OUTPUT：無。

        RETURN：
          - ``VerifyResult``（各 fanout 仍缺漏項；全部達成時 missing 為空）。

        例外：
          - DriverConnectionError：連線失敗（由 driver 丟出）。
          - DriverParseError：回讀解析失敗（由 driver 丟出）。
        """
        grouped = self._changes_by_fanout(session)
        missing_by_fanout: Dict[str, List[PortVlanChange]] = {}

        for fanout_name, changes in grouped.items():
            if not changes:
                continue
            driver = self._driver_factory(
                fanout_name, self._config.fanouts[fanout_name]
            )
            driver.connect()
            try:
                actual = driver.read_vlans()
            finally:
                driver.disconnect()
            missing = diff_expected_vs_actual(changes, actual)
            if missing:
                missing_by_fanout[fanout_name] = missing

        return VerifyResult(
            session_id=getattr(session, "session_id", "(unknown)"),
            missing_by_fanout=missing_by_fanout,
        )

    def detect_and_reapply(
        self, session: object, *, apply: bool = False
    ) -> VerifyResult:
        """重開機偵測：讀現況→diff 缺漏→（apply 時）重套→再比對（設計 §7.4）。

        對每個 fanout read_vlans 後 diff 出缺漏（疑似設備重開機遺失設定）；若
        ``apply=True`` 則對缺漏項重套 apply_vlan 並再次回讀比對（仍缺漏丟
        ``ReadBackMismatchError``）；``apply=False`` 僅回報缺漏（等同 verify）。

        INPUT：
          - session：一筆已分配的 Session。
          - apply：True 才重套缺漏項；False 僅偵測回報（keyword-only）。

        OUTPUT：無。

        RETURN：
          - ``VerifyResult``：apply=False 回缺漏；apply=True 成功後回空（全部
            達成）。

        例外：
          - ReadBackMismatchError：apply 重套後回讀仍缺漏。
          - DriverConnectionError / DriverParseError / ApplyError：driver 丟出。
        """
        grouped = self._changes_by_fanout(session)
        missing_by_fanout: Dict[str, List[PortVlanChange]] = {}

        for fanout_name, changes in grouped.items():
            if not changes:
                continue
            driver = self._driver_factory(
                fanout_name, self._config.fanouts[fanout_name]
            )
            driver.connect()
            try:
                before = driver.read_vlans()
                missing = diff_expected_vs_actual(changes, before)
                if not missing:
                    continue
                if not apply:
                    missing_by_fanout[fanout_name] = missing
                    continue
                # 重套缺漏項並再比對。
                driver.apply_vlan(missing)
                after = driver.read_vlans()
                still_missing = diff_expected_vs_actual(changes, after)
                if still_missing:
                    raise ReadBackMismatchError(
                        self._format_missing(fanout_name, still_missing)
                    )
            finally:
                driver.disconnect()

        return VerifyResult(
            session_id=getattr(session, "session_id", "(unknown)"),
            missing_by_fanout=missing_by_fanout,
        )

    @staticmethod
    def _format_missing(
        fanout_name: str, missing: List[PortVlanChange]
    ) -> str:
        """把缺漏項依 role 分述排版成訊息（決策 9、finding #8）。

        依 ``change.role`` 分述缺漏真因：UPLINK 類為「tagged 成員」未達成、EDGE
        類為「tunnel access 設定」未達成（邊緣缺漏真因是 tunnel mode 未達
        Access，而非 tagged 成員）。逐條列出各 (interface, vlan)。

        INPUT：
          - fanout_name：fanout 裝置名（str）。
          - missing：缺漏的 ``PortVlanChange`` 清單。

        OUTPUT：無。

        RETURN：
          - 多行訊息字串（含 fanout 名與依 role 分述的各 (interface, vlan)
            缺漏）。
        """
        uplink_missing = [
            change for change in missing if change.role == PortRole_E.UPLINK
        ]
        edge_missing = [
            change for change in missing if change.role == PortRole_E.EDGE
        ]

        parts: List[str] = []
        if uplink_missing:
            items = ", ".join(
                "({0}, VLAN {1})".format(change.interface, change.vlan)
                for change in uplink_missing
            )
            parts.append("tagged 成員仍缺漏：{0}".format(items))
        if edge_missing:
            items = ", ".join(
                "({0}, VLAN {1})".format(change.interface, change.vlan)
                for change in edge_missing
            )
            parts.append("tunnel access 設定仍缺漏：{0}".format(items))

        return "回讀驗證不符（fanout {0}）：{1}".format(
            fanout_name, "；".join(parts)
        )
