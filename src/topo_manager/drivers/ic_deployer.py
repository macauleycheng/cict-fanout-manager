"""互聯 fanout 下發編排層（drivers/ic_deployer.py，互聯設計 §6.4–§6.6）。

MODULE NAME：
  topo_manager.drivers.ic_deployer

PURPOSE：
  把純指令組裝（``ec_ssh_ic``）與 driver 編排成可供 CLI 呼叫的高階動作：

  - ``plan_enable`` / ``plan_release`` / ``plan_park``：純算，絕不連線。
  - ``apply``：啟用一個 session（設備鎖內重讀帳本 → 預檢 → phase 1 → 讀回
    → phase 2 → 最終讀回）。
  - ``release``：兩階段釋放（begin_release → park → 清 VLAN → 讀回 →
    commit_release）。
  - ``park``：把拓樸互聯區段的 port 還原為 parking，跳過被 session 持有的 port。
  - ``verify_session`` / ``verify_parked``：唯讀查核。

NOTES：
  - 只有 ``apply=True`` 才會呼叫 ``driver_factory``；dry-run 與 plan 絕不連線。
  - 會寫設備的動作先取 ``store.device_locked()``，並在鎖內重新讀帳本；鎖序為
    「設備鎖（外）→ 帳本鎖（內）」，allocator 的 begin／commit_release 自行取
    帳本鎖。
  - 互聯 fanout 在連線設定中查無 → ``ConfigError``（fail closed）；dry-run 不
    報錯，只在計畫中標示 ``configured=False``。
  - 啟用失敗不做 rollback：port 要嘛仍 shutdown，要嘛已 Up 且設定已驗過；全部
    指令冪等，可重跑 apply 或 release。
  - ``force_access_mode`` 必須與 driver 工廠建出的 driver 一致；讀回檢查以
    driver 的屬性為準。
  - 本模組不 import netmiko。

HISTORY：
  - 0.1.0  互聯 fanout 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from topo_manager.drivers.ec_ssh import EcSshDriver
from topo_manager.drivers.ec_ssh_ic import (
    EcSshInterconnectDriver,
    PortStatus,
    PortSwitchport,
    build_park_batch,
    build_phase1_commands,
    build_phase2_commands,
    build_vlan_clear_commands,
    is_port_clean,
    pool_vlans,
)
from topo_manager.drivers.planner import map_interface
from topo_manager.errors import (
    ApplyError,
    ConfigError,
    InterconnectRequestError,
    InterconnectSessionNotFoundError,
    ReadBackMismatchError,
)
from topo_manager.interconnect.allocator import PHASE_ALLOCATED
from topo_manager.interconnect.layout import IcGraph
from topo_manager.interconnect.models import IcSession

# 重跑提示（失敗時附在錯誤訊息後）。
_RERUN_HINT = (
    "（不做 rollback：port 要嘛仍 shutdown，要嘛已 Up 且設定已驗過；全部指令冪等，"
    "可重跑同一指令或改用 release {0}）"
)

# (fanout port 編號, 介面名 'Eth1/N', VLAN)
_PortItem = Tuple[int, str, int]


@dataclass(frozen=True)
class IcFanoutPlan:
    """單一互聯 fanout 的指令計畫。

    欄位：
      - fanout：互聯 fanout 名。
      - configured：是否有連線設定（False 時 apply 會 ``ConfigError``）。
      - batches：依序送出的設定批次（每批一次 ``send_config_set``）。
    """

    fanout: str
    configured: bool
    batches: List[List[str]]

    @property
    def commands(self) -> List[str]:
        """所有批次攤平後的指令清單。"""
        return [command for batch in self.batches for command in batch]


@dataclass(frozen=True)
class IcPlanResult:
    """啟用／釋放／park 的計畫（apply 後帶回讀備註）。

    欄位：
      - session_id：session 識別碼；park 為 ``'(park)'``。
      - action：``'apply'`` | ``'release'`` | ``'park'``。
      - vlans：涉及的 VLAN（升冪）。
      - fanouts：各 fanout 的指令計畫（依名稱排序）。
      - notes：備註與警告（如 Link Status 資訊、被保留的 VLAN）。
    """

    session_id: str
    action: str
    vlans: List[int]
    fanouts: List[IcFanoutPlan]
    notes: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class IcParkResult:
    """park 結果。

    欄位：
      - applied：是否真的寫入設備。
      - fanouts：各 fanout 的指令（dry-run 為靜態 baseline，apply 為實際指令）。
      - parked：apply 後實際 park 的 ``{fanout: [port...]}``；dry-run 為空。
      - skipped：被 session 持有而跳過的 ``[(fanout, port, session_id)]``。
      - warnings：警告（如被保留的 VLAN）。
    """

    applied: bool
    fanouts: List[IcFanoutPlan]
    parked: Dict[str, List[int]] = field(default_factory=dict)
    skipped: List[Tuple[str, int, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class IcVerifyResult:
    """verify 結果；``problems`` 為空代表設備與期望相符。"""

    target: str
    problems: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否全部相符。"""
        return not self.problems


def _node_parts(node: str) -> Tuple[str, int]:
    """把 ``'<fanout>:<port>'`` 拆成 (fanout, port 編號)。"""
    fanout, _, port = node.rpartition(":")
    return fanout, int(port)


class InterconnectDeployer:
    """互聯 fanout 下發編排器。

    狀態封裝於實例屬性：driver 工廠、連線設定、store／allocator（寫設備動作
    才需要）、VLAN 池範圍與 ``force_access_mode``。
    """

    def __init__(
        self,
        driver_factory: Callable[[str, object], EcSshInterconnectDriver],
        config: object,
        *,
        store: Optional[object] = None,
        allocator: Optional[object] = None,
        vlan_min: int = 100,
        vlan_max: int = 3000,
        force_access_mode: bool = True,
    ) -> None:
        """記錄依賴。

        INPUT：
          - driver_factory：``(fanout 名, FanoutEntry) -> EcSshInterconnectDriver``。
          - config：``FanoutConfig``（具 ``fanouts`` 映射）。
          - store：``IcSessionStore``；apply／release／park 的設備鎖與重讀帳本用。
          - allocator：``InterconnectAllocator``；release 的 begin／commit 用。
          - vlan_min／vlan_max：VLAN 池範圍（只動池內 VLAN）。
          - force_access_mode：啟用時是否下 ``switchport mode access``。
        """
        self._driver_factory = driver_factory
        self._config = config
        self._store = store
        self._allocator = allocator
        self._vlan_min = vlan_min
        self._vlan_max = vlan_max
        self._force_access_mode = force_access_mode

    # ------------------------------------------------------------------
    # 內部輔助
    # ------------------------------------------------------------------
    def _entry(self, fanout: str) -> Optional[object]:
        """取 fanout 的連線設定；查無回 None。"""
        return self._config.fanouts.get(fanout)

    def _require_configured(self, fanouts: Sequence[str]) -> None:
        """所有 fanout 都須有連線設定；否則 ConfigError（任何寫入之前）。"""
        missing = sorted(name for name in set(fanouts) if self._entry(name) is None)
        if missing:
            raise ConfigError(
                "互聯 fanout {0} 不在連線設定中（請於 credentials 的 fanouts 鍵加入同名項目）"
                .format(", ".join(missing))
            )

    def _require_store(self) -> object:
        """寫設備動作需要 store；缺少屬程式錯誤。"""
        if self._store is None:
            raise ValueError("此動作需要在建構時提供 store")
        return self._store

    def _require_allocator(self) -> object:
        """release 需要 allocator；缺少屬程式錯誤。"""
        if self._allocator is None:
            raise ValueError("release 需要在建構時提供 allocator")
        return self._allocator

    def _iface(self, fanout: str, port: int) -> Tuple[int, str]:
        """port 編號 → (編號, 介面名)；經 planner 的 fail-closed 映射。"""
        entry = self._entry(fanout)
        template = getattr(entry, "interface_template", None)
        iface = map_interface(fanout, str(port), template)
        return int(EcSshDriver._port_from_interface(iface)), iface

    def _session_items(self, session: IcSession) -> Dict[str, List[_PortItem]]:
        """session 的 ``{fanout: [(port, iface, vlan)]}``（依 port 排序）。"""
        grouped: Dict[str, Dict[int, _PortItem]] = {}
        for link in session.links:
            for node in (link.a_node, link.b_node):
                fanout, number = _node_parts(node)
                port, iface = self._iface(fanout, number)
                grouped.setdefault(fanout, {})[port] = (port, iface, link.vlan)
        return {
            fanout: [items[port] for port in sorted(items)]
            for fanout, items in grouped.items()
        }

    def _expected_members(self, session: IcSession) -> Dict[str, Dict[int, Set[str]]]:
        """``{fanout: {vlan: {介面名...}}}``，每條 link 兩端。"""
        expected: Dict[str, Dict[int, Set[str]]] = {}
        for link in session.links:
            for node in (link.a_node, link.b_node):
                fanout, number = _node_parts(node)
                _port, iface = self._iface(fanout, number)
                expected.setdefault(fanout, {}).setdefault(link.vlan, set()).add(iface)
        return expected

    @contextmanager
    def _connected(self, fanout: str) -> Iterator[EcSshInterconnectDriver]:
        """連線到 fanout，離開時一律 disconnect。"""
        entry = self._entry(fanout)
        if entry is None:
            raise ConfigError("互聯 fanout {0} 不在連線設定中".format(fanout))
        driver = self._driver_factory(fanout, entry)
        driver.connect()
        try:
            yield driver
        finally:
            driver.disconnect()

    def _fresh_session(self, session_id: str) -> IcSession:
        """鎖內重讀帳本取得 session；查無丟 InterconnectSessionNotFoundError。"""
        loaded = self._require_store().load()
        for item in loaded.sessions:
            if item.session_id == session_id:
                return item
        raise InterconnectSessionNotFoundError(
            "查無互聯 session '{0}'（可能已被釋放）".format(session_id)
        )

    def _owned_from_store(self) -> Dict[str, str]:
        """鎖內重讀帳本，回傳 ``{fanout 節點: session_id}``。"""
        owned: Dict[str, str] = {}
        for item in self._require_store().load().sessions:
            for link in item.links:
                owned[link.a_node] = item.session_id
                owned[link.b_node] = item.session_id
        return owned

    @staticmethod
    def _with_hint(exc: Exception, session_id: str) -> Exception:
        """為 ApplyError／ReadBackMismatchError 附上重跑提示（同類別、保留 cause）。"""
        wrapped = type(exc)("{0}{1}".format(exc, _RERUN_HINT.format(session_id)))
        wrapped.__cause__ = exc
        return wrapped

    # ------------------------------------------------------------------
    # 狀態檢查（apply 讀回、verify 共用）
    # ------------------------------------------------------------------
    def _check_enabled(
        self,
        fanout: str,
        item: _PortItem,
        switchport: PortSwitchport,
        status: PortStatus,
        expect_up: Optional[bool],
        force_access: bool,
    ) -> List[str]:
        """啟用狀態檢查：native、池內成員恰為 {V:'u'}、無 VLAN 1、mode、管理面狀態。"""
        port, iface, vlan = item
        label = "{0} {1}".format(fanout, iface)
        problems: List[str] = []
        if switchport.native_vlan != vlan:
            problems.append(
                "{0}：Native VLAN 為 {1}，預期 {2}".format(label, switchport.native_vlan, vlan)
            )
        pool = pool_vlans(switchport.allowed, self._vlan_min, self._vlan_max)
        if pool != [vlan] or switchport.allowed.get(vlan) != "u":
            problems.append(
                "{0}：池內 VLAN 成員為 {1}，預期恰為 {{{2}: 'u'}}".format(
                    label, {v: switchport.allowed[v] for v in pool}, vlan
                )
            )
        if 1 in switchport.allowed:
            problems.append("{0}：仍是 VLAN 1 成員".format(label))
        if force_access and switchport.mode.lower() != "access":
            problems.append("{0}：mode 為 {1}，預期 Access".format(label, switchport.mode))
        if expect_up is True and not status.enabled:
            problems.append("{0}：Port Admin 為 Down，預期 Up".format(label))
        if expect_up is False and status.enabled:
            problems.append("{0}：Port Admin 為 Up，預期 Down".format(label))
        return problems

    def _check_parked(
        self,
        fanout: str,
        iface: str,
        switchport: PortSwitchport,
        status: PortStatus,
    ) -> List[str]:
        """parking 狀態檢查：管理面 Down、native 1、VLAN 1 untagged、無池內 VLAN。"""
        label = "{0} {1}".format(fanout, iface)
        problems: List[str] = []
        if status.enabled:
            problems.append("{0}：Port Admin 為 Up，預期 Down".format(label))
        if switchport.native_vlan != 1:
            problems.append("{0}：Native VLAN 為 {1}，預期 1".format(label, switchport.native_vlan))
        if switchport.allowed.get(1) != "u":
            problems.append("{0}：不在 VLAN 1 untagged 成員內".format(label))
        stray = pool_vlans(switchport.allowed, self._vlan_min, self._vlan_max)
        if stray:
            problems.append("{0}：仍有池內 VLAN {1}".format(label, stray))
        return problems

    @staticmethod
    def _check_members(
        fanout: str,
        driver: EcSshInterconnectDriver,
        expected: Dict[int, Set[str]],
    ) -> List[str]:
        """每個 VLAN 的成員必須恰為預期介面集合。"""
        problems: List[str] = []
        for vlan in sorted(expected):
            members = driver.read_vlan_members(vlan)
            actual = set(members) if members is not None else None
            if actual != expected[vlan]:
                problems.append(
                    "{0}：VLAN {1} 成員為 {2}，預期 {3}".format(
                        fanout,
                        vlan,
                        "不存在" if actual is None else sorted(actual),
                        sorted(expected[vlan]),
                    )
                )
        return problems

    # ------------------------------------------------------------------
    # plan（純算）
    # ------------------------------------------------------------------
    def plan_enable(self, session: IcSession) -> IcPlanResult:
        """純算啟用計畫：每個 fanout 兩批（phase 1、phase 2）。"""
        fanouts: List[IcFanoutPlan] = []
        for fanout, items in sorted(self._session_items(session).items()):
            phase1 = build_phase1_commands(
                [(port, vlan) for port, _iface, vlan in items], self._force_access_mode
            )
            phase2 = build_phase2_commands([port for port, _iface, _vlan in items])
            fanouts.append(
                IcFanoutPlan(fanout, self._entry(fanout) is not None, [phase1, phase2])
            )
        return IcPlanResult(
            session_id=session.session_id,
            action="apply",
            vlans=sorted({link.vlan for link in session.links}),
            fanouts=fanouts,
            notes=["phase 1 讀回驗證通過後才會執行 phase 2（no shutdown）"],
        )

    def plan_release(self, session: IcSession) -> IcPlanResult:
        """純算釋放計畫：park 批次（以 session VLAN 當殘留）＋ VLAN 清除批次。"""
        fanouts: List[IcFanoutPlan] = []
        for fanout, items in sorted(self._session_items(session).items()):
            park = build_park_batch(
                [(port, [vlan], False) for port, _iface, vlan in items]
            )
            clear = build_vlan_clear_commands(vlan for _port, _iface, vlan in items)
            fanouts.append(
                IcFanoutPlan(fanout, self._entry(fanout) is not None, [park, clear])
            )
        return IcPlanResult(
            session_id=session.session_id,
            action="release",
            vlans=sorted({link.vlan for link in session.links}),
            fanouts=fanouts,
            notes=["apply 時以讀回為準：已乾淨的 port 只下 shutdown"],
        )

    def _park_scope(
        self,
        graph: IcGraph,
        fanouts: Optional[Sequence[str]],
        ports: Optional[Sequence[int]],
    ) -> Dict[str, List[int]]:
        """park／verify 範圍：互聯 link 出現的 fanout port，可用 fanouts／ports 縮小。"""
        if fanouts:
            unknown = sorted(set(fanouts) - set(graph.fanouts))
            if unknown:
                raise InterconnectRequestError(
                    "找不到互聯 fanout：{0}（可用：{1}）".format(
                        ", ".join(unknown), ", ".join(sorted(graph.fanouts))
                    )
                )
        scope: Dict[str, Set[int]] = {}
        for group in graph.groups.values():
            for dut in group.duts:
                for port in dut.ports:
                    if fanouts and port.fanout not in fanouts:
                        continue
                    scope.setdefault(port.fanout, set()).add(port.fanout_port)
        if ports:
            known = set().union(*scope.values()) if scope else set()
            unknown_ports = sorted(set(ports) - known)
            if unknown_ports:
                raise InterconnectRequestError(
                    "port {0} 不在互聯區段的 link 中".format(unknown_ports)
                )
            scope = {
                name: {p for p in numbers if p in set(ports)}
                for name, numbers in scope.items()
            }
        return {name: sorted(numbers) for name, numbers in sorted(scope.items()) if numbers}

    def _split_owned(
        self, scope: Dict[str, List[int]], owned: Dict[str, str]
    ) -> Tuple[Dict[str, List[int]], List[Tuple[str, int, str]]]:
        """把範圍分成「可動」與「被持有而跳過」。"""
        free: Dict[str, List[int]] = {}
        skipped: List[Tuple[str, int, str]] = []
        for fanout, numbers in scope.items():
            for number in numbers:
                owner = owned.get("{0}:{1}".format(fanout, number))
                if owner is None:
                    free.setdefault(fanout, []).append(number)
                else:
                    skipped.append((fanout, number, owner))
        return free, skipped

    def plan_park(
        self,
        graph: IcGraph,
        fanouts: Optional[Sequence[str]] = None,
        ports: Optional[Sequence[int]] = None,
        owned: Optional[Dict[str, str]] = None,
    ) -> IcPlanResult:
        """純算 park 靜態 baseline：每個 port 一個 ``shutdown``。"""
        free, _skipped = self._split_owned(
            self._park_scope(graph, fanouts, ports), owned or {}
        )
        plans = [
            IcFanoutPlan(
                fanout,
                self._entry(fanout) is not None,
                [build_park_batch([(number, [], True) for number in numbers])],
            )
            for fanout, numbers in sorted(free.items())
        ]
        return IcPlanResult(
            session_id="(park)",
            action="park",
            vlans=[],
            fanouts=plans,
            notes=["apply 時會讀回每個 port，並清理池內殘留 VLAN 與已空的 VLAN 定義"],
        )

    # ------------------------------------------------------------------
    # apply
    # ------------------------------------------------------------------
    def apply(self, session: IcSession, *, apply: bool = False) -> IcPlanResult:
        """啟用一個 session；``apply=False`` 只回計畫，不連線、不碰 store。

        例外：
          - InterconnectSessionNotFoundError：鎖內重讀帳本查無 session。
          - InterconnectRequestError：session 已在 releasing。
          - ConfigError：fanout 無連線設定。
          - ApplyError：預檢發現 VLAN 被他人使用，或設備回錯誤字樣。
          - ReadBackMismatchError：讀回不符（phase 1 失敗時 port 仍 shutdown）。
        """
        plan = self.plan_enable(session)
        if not apply:
            return plan
        with self._require_store().device_locked():
            fresh = self._fresh_session(session.session_id)
            if fresh.phase != PHASE_ALLOCATED:
                raise InterconnectRequestError(
                    "session {0} 正在釋放（phase={1}），不能啟用；請先完成 release"
                    .format(fresh.session_id, fresh.phase)
                )
            plan = self.plan_enable(fresh)
            self._require_configured([item.fanout for item in plan.fanouts])
            notes: List[str] = list(plan.notes)
            try:
                for fanout, items in sorted(self._session_items(fresh).items()):
                    expected = self._expected_members(fresh)[fanout]
                    with self._connected(fanout) as driver:
                        notes.extend(self._apply_fanout(fanout, driver, items, expected))
            except (ApplyError, ReadBackMismatchError) as exc:
                raise self._with_hint(exc, fresh.session_id) from exc
            return replace(plan, notes=notes)

    def _apply_fanout(
        self,
        fanout: str,
        driver: EcSshInterconnectDriver,
        items: List[_PortItem],
        expected: Dict[int, Set[str]],
    ) -> List[str]:
        """單一 fanout：預檢 → phase 1 → 讀回 → phase 2 → 最終讀回。"""
        # 預檢（不寫入）：VLAN 若存在，成員必須 ⊆ 本 link 兩端介面（冪等重跑）。
        for vlan in sorted(expected):
            members = driver.read_vlan_members(vlan)
            if members is not None and not set(members) <= expected[vlan]:
                raise ApplyError(
                    "VLAN {0} 在 fanout {1} 上已被其他 port 使用（{2}）；"
                    "請先 release/park 或換 VLAN 池".format(
                        vlan, fanout, sorted(set(members) - expected[vlan])
                    )
                )
        driver.enable_phase1([(port, vlan) for port, _iface, vlan in items])
        problems: List[str] = []
        for item in items:
            problems.extend(
                self._check_enabled(
                    fanout,
                    item,
                    driver.read_port_switchport(item[0]),
                    driver.read_port_status(item[0]),
                    expect_up=False,
                    force_access=driver.force_access_mode,
                )
            )
        problems.extend(self._check_members(fanout, driver, expected))
        if problems:
            raise ReadBackMismatchError(
                "phase 1 讀回不符（port 仍為 shutdown，未執行 no shutdown）：\n  "
                + "\n  ".join(problems)
            )
        driver.enable_phase2([port for port, _iface, _vlan in items])
        notes: List[str] = []
        final: List[str] = []
        for port, iface, _vlan in items:
            status = driver.read_port_status(port)
            if not status.enabled:
                final.append("{0} {1}：Port Admin 為 Down，預期 Up".format(fanout, iface))
            notes.append(
                "{0} {1} Link Status: {2}（僅供參考）".format(
                    fanout, iface, "Up" if status.link_up else "Down"
                )
            )
        if final:
            raise ReadBackMismatchError("phase 2 讀回不符：\n  " + "\n  ".join(final))
        return notes

    # ------------------------------------------------------------------
    # release
    # ------------------------------------------------------------------
    def release(self, session: IcSession, *, apply: bool = False) -> IcPlanResult:
        """兩階段釋放；``apply=False`` 只回計畫，不連線、不改帳本。

        apply：設備鎖 → begin_release（持久化 releasing）→ park（讀回為準）→
        清 VLAN 定義 → 讀回確認 → commit_release。任一步失敗 session 停在
        releasing，資源仍被保留，重跑同指令即可。
        """
        plan = self.plan_release(session)
        if not apply:
            return plan
        allocator = self._require_allocator()
        with self._require_store().device_locked():
            self._require_configured([item.fanout for item in plan.fanouts])
            fresh = allocator.begin_release(session.session_id)
            notes: List[str] = list(plan.notes)
            try:
                for fanout, items in sorted(self._session_items(fresh).items()):
                    with self._connected(fanout) as driver:
                        notes.extend(self._release_fanout(fanout, driver, items))
            except (ApplyError, ReadBackMismatchError) as exc:
                raise self._with_hint(exc, fresh.session_id) from exc
            allocator.commit_release(fresh.session_id)
            return replace(self.plan_release(fresh), notes=notes)

    def _park_and_clear(
        self,
        fanout: str,
        driver: EcSshInterconnectDriver,
        numbers: Sequence[int],
        extra_vlans: Sequence[int],
    ) -> Tuple[List[int], List[int], List[str]]:
        """讀回 → park → 清 VLAN 定義。回傳 (已清除 VLAN, 被保留 VLAN, 警告)。"""
        park_items = []
        strays: Set[int] = set(extra_vlans)
        for number in numbers:
            switchport = driver.read_port_switchport(number)
            stray = pool_vlans(switchport.allowed, self._vlan_min, self._vlan_max)
            strays.update(stray)
            park_items.append((number, stray, is_port_clean(switchport, stray)))
        driver.park_ports(park_items)
        cleared, warnings = driver.clear_vlan_definitions(
            strays, self._vlan_min, self._vlan_max
        )
        kept = [
            vlan
            for vlan in sorted(strays)
            if vlan not in cleared and self._vlan_min <= vlan <= self._vlan_max
            and driver.read_vlan_members(vlan) is not None
        ]
        return cleared, kept, [
            "{0}：{1}".format(fanout, warning) for warning in warnings
        ]

    def _release_fanout(
        self,
        fanout: str,
        driver: EcSshInterconnectDriver,
        items: List[_PortItem],
    ) -> List[str]:
        """單一 fanout 的釋放：park → 清 VLAN → 讀回確認；回傳警告。"""
        session_vlans = sorted({vlan for _port, _iface, vlan in items})
        _cleared, kept, warnings = self._park_and_clear(
            fanout, driver, [port for port, _iface, _vlan in items], session_vlans
        )
        problems: List[str] = []
        for port, iface, _vlan in items:
            problems.extend(
                self._check_parked(
                    fanout, iface, driver.read_port_switchport(port), driver.read_port_status(port)
                )
            )
        for vlan in session_vlans:
            if vlan not in kept and driver.read_vlan_members(vlan) is not None:
                problems.append("{0}：VLAN {1} 的定義仍存在".format(fanout, vlan))
        if problems:
            raise ReadBackMismatchError("release 讀回不符：\n  " + "\n  ".join(problems))
        return warnings

    # ------------------------------------------------------------------
    # park
    # ------------------------------------------------------------------
    def park(
        self,
        graph: IcGraph,
        owned: Optional[Dict[str, str]] = None,
        *,
        fanouts: Optional[Sequence[str]] = None,
        ports: Optional[Sequence[int]] = None,
        apply: bool = False,
    ) -> IcParkResult:
        """把互聯區段 port 還原為 parking；被 session 持有的 port 跳過並列出。

        ``apply=False``：只回靜態 baseline，不連線。``apply=True``：設備鎖內
        重讀帳本取得持有者（取代傳入的 ``owned``），再逐 port 讀回、park、清
        VLAN 定義、讀回確認。
        """
        scope = self._park_scope(graph, fanouts, ports)
        if not apply:
            free, skipped = self._split_owned(scope, owned or {})
            plan = self.plan_park(graph, fanouts, ports, owned)
            return IcParkResult(applied=False, fanouts=plan.fanouts, skipped=skipped)
        with self._require_store().device_locked():
            free, skipped = self._split_owned(scope, self._owned_from_store())
            self._require_configured(list(free))
            plans: List[IcFanoutPlan] = []
            parked: Dict[str, List[int]] = {}
            warnings: List[str] = []
            for fanout, numbers in sorted(free.items()):
                with self._connected(fanout) as driver:
                    commands, fanout_warnings = self._park_fanout(fanout, driver, numbers)
                plans.append(IcFanoutPlan(fanout, True, commands))
                parked[fanout] = list(numbers)
                warnings.extend(fanout_warnings)
            return IcParkResult(
                applied=True, fanouts=plans, parked=parked, skipped=skipped, warnings=warnings
            )

    def _park_fanout(
        self, fanout: str, driver: EcSshInterconnectDriver, numbers: Sequence[int]
    ) -> Tuple[List[List[str]], List[str]]:
        """單一 fanout 的 park：讀回 → park → 清 VLAN → 讀回確認。"""
        before_items = []
        for number in numbers:
            switchport = driver.read_port_switchport(number)
            stray = pool_vlans(switchport.allowed, self._vlan_min, self._vlan_max)
            before_items.append((number, stray, is_port_clean(switchport, stray)))
        park_commands = build_park_batch(before_items)
        _cleared, _kept, warnings = self._park_and_clear(fanout, driver, numbers, [])
        problems: List[str] = []
        for number in numbers:
            _port, iface = self._iface(fanout, number)
            problems.extend(
                self._check_parked(
                    fanout, iface, driver.read_port_switchport(number), driver.read_port_status(number)
                )
            )
        if problems:
            raise ReadBackMismatchError("park 讀回不符：\n  " + "\n  ".join(problems))
        return [park_commands], warnings

    # ------------------------------------------------------------------
    # verify（唯讀）
    # ------------------------------------------------------------------
    def verify_session(self, session: IcSession) -> IcVerifyResult:
        """唯讀查核 session 是否處於啟用狀態（管理面 Up、成員與 native 正確）。"""
        problems: List[str] = []
        items_by_fanout = self._session_items(session)
        self._require_configured(list(items_by_fanout))
        expected = self._expected_members(session)
        for fanout, items in sorted(items_by_fanout.items()):
            with self._connected(fanout) as driver:
                for item in items:
                    problems.extend(
                        self._check_enabled(
                            fanout,
                            item,
                            driver.read_port_switchport(item[0]),
                            driver.read_port_status(item[0]),
                            expect_up=True,
                            force_access=driver.force_access_mode,
                        )
                    )
                problems.extend(self._check_members(fanout, driver, expected[fanout]))
        return IcVerifyResult(target=session.session_id, problems=problems)

    def verify_parked(
        self,
        graph: IcGraph,
        owned: Optional[Dict[str, str]] = None,
        *,
        fanouts: Optional[Sequence[str]] = None,
        ports: Optional[Sequence[int]] = None,
    ) -> IcVerifyResult:
        """唯讀查核未被持有的互聯 port 是否為 parking；池外 VLAN 忽略。"""
        free, _skipped = self._split_owned(
            self._park_scope(graph, fanouts, ports), owned or {}
        )
        self._require_configured(list(free))
        problems: List[str] = []
        for fanout, numbers in sorted(free.items()):
            with self._connected(fanout) as driver:
                for number in numbers:
                    _port, iface = self._iface(fanout, number)
                    problems.extend(
                        self._check_parked(
                            fanout,
                            iface,
                            driver.read_port_switchport(number),
                            driver.read_port_status(number),
                        )
                    )
        return IcVerifyResult(target="(parked)", problems=problems)
