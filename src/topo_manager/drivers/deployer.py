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

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List

from topo_manager.drivers.base import FanoutDriver, PortVlanChange, VlanState
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
        """對已套用的 fanout best-effort 清除（吞下清理錯誤，設計 §7.2）。

        INPUT：
          - applied：``[(fanout 名, [PortVlanChange]), ...]`` 已套用清單。

        OUTPUT：無。

        RETURN：None。

        例外：不對外拋出（清理為 best-effort；原始失敗才是回報主因）。
        """
        for fanout_name, changes in applied:
            try:
                driver = self._driver_factory(
                    fanout_name, self._config.fanouts[fanout_name]
                )
                driver.connect()
                try:
                    driver.clear_vlan(changes)
                finally:
                    driver.disconnect()
            except Exception:  # noqa: BLE001 — 清理失敗不掩蓋原始錯誤。
                continue

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
        """把缺漏項排版成 ``ReadBackMismatchError`` 的訊息（逐條列出）。

        INPUT：
          - fanout_name：fanout 裝置名（str）。
          - missing：缺漏的 ``PortVlanChange`` 清單。

        OUTPUT：無。

        RETURN：
          - 多行訊息字串（含 fanout 名與各 (interface, vlan) 缺漏）。
        """
        items = ", ".join(
            "({0}, VLAN {1})".format(change.interface, change.vlan)
            for change in missing
        )
        return "回讀驗證不符（fanout {0}）：下列 tagged 成員仍缺漏：{1}".format(
            fanout_name, items
        )
