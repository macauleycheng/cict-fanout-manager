"""Edgecore 風格 SSH fanout driver（ec_ssh.py，設計 §8）。

本模組提供 ``FanoutDriver`` 的具體實作 ``EcSshDriver``：以 netmiko 對 fanout
設備建立 SSH 連線、唯讀讀取 VLAN 狀態、由純邏輯變更計畫組裝設定指令並下發。

netmiko 相依刻意採「延遲 import」（僅於實際要建立連線的 ``connect()`` 內
``import netmiko``）：純邏輯（build_commands）、設定載入與 CLI 路徑皆不觸發
import，確保未安裝 netmiko 的環境仍可匯入本模組並跑單元測試。單元測試以注入
``connection_factory`` 替身繞過真 netmiko（factory 回傳具 ``send_command`` /
``send_config_set`` / ``enable`` / ``disconnect`` 介面的 fake connection）。

指令樣板一律以模組常數集中管理（``_DISABLE_PAGER_TEMPLATE`` /
``_CREATE_VLAN_TEMPLATE`` / ``_ENTER_INTF_TEMPLATE`` / ``_APPLY_MEMBER_TEMPLATE``
/ ``_CLEAR_MEMBER_TEMPLATE``），目前為暫定值並標 ``TODO(4a)``：待實機
（192.168.42.237）確認真實 CLI 語法後，僅需更新本區常數即可，不動邏輯。

netmiko 的連線/認證/逾時例外一律就地捕捉並包成 ``DriverConnectionError``
（保留 ``__cause__``、訊息不含密碼，設計 §8、§9）。

歷史（HISTORY）：
  - 0.4.0  FEAT-002 初版，建立 EcSshDriver（netmiko 延遲 import、可注入
           connection_factory 的 fake-connection 可測、模組常數指令樣板、
           connect/enable/關 pager、read_show/read_vlans 自寫行解析、
           build_commands 純組裝、apply_vlan/clear_vlan 以 send_config_set
           下發、netmiko 例外包成 DriverConnectionError）。netmiko 僅於
           connect() 內延遲 import。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Set

from topo_manager.drivers.base import (
    FanoutDriver,
    PortVlanChange,
    VlanMember,
    VlanState,
)
from topo_manager.errors import (
    DriverConnectionError,
    DriverDependencyError,
    DriverParseError,
)

# 指令樣板（模組常數，設計 §8）。TODO(4a)：以下皆為暫定值，待實機
# （192.168.42.237 admin/admin）確認 Edgecore 真實 CLI 語法後更新，不動邏輯。
_DISABLE_PAGER_TEMPLATE = "terminal length 0"
_CREATE_VLAN_TEMPLATE = "vlan {vlan}"
_ENTER_INTF_TEMPLATE = "interface {interface}"
_APPLY_MEMBER_TEMPLATE = "switchport trunk allowed vlan add {vlan}"
_CLEAR_MEMBER_TEMPLATE = "switchport trunk allowed vlan remove {vlan}"

# show vlan 輸出的行解析樣板（自寫解析，設計 §8）。TODO(4a)：暫以通用格式
# 解析——每行首為 VLAN id，之後出現的 EthX/Y token 視為該 VLAN 的 tagged 成員；
# 待實機輸出確認後調整。
_VLAN_LINE_RE = re.compile(r"^\s*(\d+)\b(.*)$")
_INTERFACE_TOKEN_RE = re.compile(r"\bEth\d+/\d+\b")


class EcSshDriver(FanoutDriver):
    """Edgecore 風格 SSH fanout driver（netmiko-based，設計 §8）。

    狀態封裝於實例屬性（不使用全域可變狀態）：連線參數與底層 connection
    handle 皆為實例屬性。netmiko 相依於 ``connect()`` 內延遲 import。
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        device_type: str = "cisco_ios",
        secret: Optional[str] = None,
        interface_template: str = "Eth1/{port}",
        connection_factory: Optional[Callable[..., object]] = None,
    ) -> None:
        """記錄連線參數（不在此建立連線，設計 §8）。

        INPUT：
          - host：設備可達位址（str）。
          - username：登入帳號（str）。
          - password：登入密碼（str，不會出現在任何例外訊息中）。
          - device_type：netmiko device_type（str），預設 ``'cisco_ios'``。
          - secret：enable 密碼（Optional[str]）；非空時 connect 後呼叫
            ``enable()`` 進入特權模式。
          - interface_template：port→介面名樣板（str），供呼叫端參照（本
            driver 不直接用，介面名由 planner 產生）。
          - connection_factory：連線工廠（Optional[Callable]）；None 時於
            ``connect()`` 延遲 import netmiko 並用其 ``ConnectHandler``。測試
            注入 fake factory 以繞過真 netmiko。

        OUTPUT：無。

        RETURN：None。

        例外：無（純屬性賦值）。
        """
        self._host = host
        self._username = username
        self._password = password
        self._device_type = device_type
        self._secret = secret
        self._interface_template = interface_template
        self._connection_factory = connection_factory
        # 底層 netmiko connection handle（連線後才有；斷線後設回 None）。
        self._connection: Optional[object] = None

    def _resolve_factory(self) -> Callable[..., object]:
        """取得連線工廠：優先用注入者，否則延遲 import netmiko（設計 §8）。

        INPUT：無。

        OUTPUT：無。

        RETURN：
          - 可呼叫的連線工廠（``Callable``）；注入者原樣回傳，否則回 netmiko
            的 ``ConnectHandler``。

        例外：
          - DriverDependencyError：未注入工廠且 ``import netmiko`` 失敗（保留
            ``__cause__``，指引 ``pip install topo-manager[drivers]``）。
        """
        if self._connection_factory is not None:
            return self._connection_factory
        try:
            import netmiko  # 延遲 import：僅在真的要連線時才需要 netmiko。
        except ImportError as exc:
            raise DriverDependencyError(
                "fanout SSH 下發需要選用相依 netmiko，但目前未安裝；請執行 "
                "'pip install topo-manager[drivers]' 後重試"
            ) from exc
        return netmiko.ConnectHandler

    def connect(self) -> None:
        """建立連線、（如有 secret）進入 enable、並顯式關閉 pager（設計 §8）。

        INPUT：無。

        OUTPUT：無。

        RETURN：None。

        例外：
          - DriverDependencyError：netmiko 未安裝（由 ``_resolve_factory``）。
          - DriverConnectionError：netmiko 連線/認證/逾時例外（就地捕捉包裝，
            保留 ``__cause__``、訊息不含密碼）。
        """
        factory = self._resolve_factory()
        try:
            self._connection = factory(
                host=self._host,
                username=self._username,
                password=self._password,
                device_type=self._device_type,
                secret=self._secret,
            )
            if self._secret:
                self._connection.enable()
            # 顯式下關 pager，避免分頁符號污染回讀輸出（TODO(4a) 樣板暫定）。
            self._connection.send_command(_DISABLE_PAGER_TEMPLATE)
        except Exception as exc:  # noqa: BLE001 — 下方僅對 netmiko 例外重拋。
            self._reraise_as_connection_error(exc, "連線 fanout 設備")

    def disconnect(self) -> None:
        """關閉連線（冪等；未連線為 no-op，關閉失敗吞下，設計 §8）。

        INPUT：無。

        OUTPUT：無。

        RETURN：None。

        例外：不對外拋出（確保可安全重複呼叫、context manager 離開安全）。
        """
        if self._connection is None:
            return
        try:
            self._connection.disconnect()
        except Exception:  # noqa: BLE001 — 關閉失敗不應掩蓋臨界區例外。
            pass
        finally:
            self._connection = None

    def read_show(self, command: str) -> str:
        """送出一則唯讀 show 指令並回傳原始輸出（設計 §8）。

        INPUT：
          - command：要送出的 show 指令字串（str）。

        OUTPUT：無。

        RETURN：
          - 設備回傳的原始文字輸出（str）。

        例外：
          - DriverConnectionError：未連線或 netmiko 送指令時連線失敗（保留
            ``__cause__``）。
        """
        connection = self._require_connection()
        try:
            return connection.send_command(command)
        except Exception as exc:  # noqa: BLE001
            self._reraise_as_connection_error(
                exc, "送出唯讀指令 '{0}'".format(command)
            )

    def read_vlans(self) -> VlanState:
        """下 ``show vlan`` 並自寫行解析成 ``VlanState``（設計 §8）。

        INPUT：無。

        OUTPUT：無。

        RETURN：
          - ``VlanState``（本 fanout 的 tagged 成員集合與既有 VLAN 集合）。

        例外：
          - DriverConnectionError：連線失敗（由 ``read_show``）。
          - DriverParseError：輸出無法解析為合法 VLAN 狀態（含片段摘要）。
        """
        raw_output = self.read_show("show vlan")
        return self._parse_vlan_output(raw_output)

    def _parse_vlan_output(self, raw_output: str) -> VlanState:
        """把 ``show vlan`` 原始輸出解析成 ``VlanState``（設計 §8）。

        TODO(4a)：目前以通用格式解析——每行若以數字開頭視為一筆 VLAN，該行
        之後出現的 ``EthX/Y`` token 視為該 VLAN 的 tagged 成員。待實機輸出
        確認後以真機格式取代本解析（含 tagged/untagged 欄位判定）。

        INPUT：
          - raw_output：``show vlan`` 的原始文字輸出（str）。

        OUTPUT：無。

        RETURN：
          - 解析後的 ``VlanState``。

        例外：
          - DriverParseError：輸出非字串或完全解析不到任何 VLAN 行（含片段
            摘要，供診斷設備型號/韌體差異）。
        """
        if not isinstance(raw_output, str):
            raise DriverParseError(
                "無法解析 'show vlan' 輸出（fanout {0}）：預期字串，實得 {1}"
                .format(self._host, type(raw_output).__name__)
            )

        members: Set[VlanMember] = set()
        existing_vlans: Set[int] = set()
        matched_any = False

        for line in raw_output.splitlines():
            line_match = _VLAN_LINE_RE.match(line)
            if line_match is None:
                continue
            matched_any = True
            vlan_id = int(line_match.group(1))
            existing_vlans.add(vlan_id)
            remainder = line_match.group(2)
            for interface in _INTERFACE_TOKEN_RE.findall(remainder):
                members.add(
                    VlanMember(vlan=vlan_id, interface=interface, tagged=True)
                )

        if not matched_any:
            snippet = raw_output.strip()[:200]
            raise DriverParseError(
                "無法從 'show vlan' 輸出解析出任何 VLAN（fanout {0}）；輸出片段："
                "{1}".format(self._host, snippet)
            )

        return VlanState(
            fanout=self._host,
            members=frozenset(members),
            existing_vlans=frozenset(existing_vlans),
        )

    def build_commands(self, changes: List[PortVlanChange]) -> List[str]:
        """由變更計畫以模組常數樣板組裝設定指令（純邏輯、不送出，設計 §8）。

        每筆 ``action='add'`` 變更先確保 VLAN 存在（``_CREATE_VLAN_TEMPLATE``）、
        進入介面（``_ENTER_INTF_TEMPLATE``）、再套 tagged 成員
        （``_APPLY_MEMBER_TEMPLATE``）；``action='remove'`` 則用
        ``_CLEAR_MEMBER_TEMPLATE``。同一 VLAN 的建立指令只產生一次（去重）。

        INPUT：
          - changes：本 fanout 的 ``PortVlanChange`` 清單。

        OUTPUT：無。

        RETURN：
          - 依序要送出的設定指令字串清單（List[str]）；空輸入回空清單。

        例外：不涉及 I/O，故不拋連線類例外。
        """
        commands: List[str] = []
        created_vlans: Set[int] = set()

        for change in changes:
            if change.action == "add" and change.vlan not in created_vlans:
                commands.append(
                    _CREATE_VLAN_TEMPLATE.format(vlan=change.vlan)
                )
                created_vlans.add(change.vlan)
            commands.append(
                _ENTER_INTF_TEMPLATE.format(interface=change.interface)
            )
            if change.action == "add":
                commands.append(
                    _APPLY_MEMBER_TEMPLATE.format(
                        interface=change.interface, vlan=change.vlan
                    )
                )
            else:
                commands.append(
                    _CLEAR_MEMBER_TEMPLATE.format(
                        interface=change.interface, vlan=change.vlan
                    )
                )

        return commands

    def apply_vlan(self, changes: List[PortVlanChange]) -> None:
        """組裝並以 ``send_config_set`` 下發 add 變更（設計 §8）。

        INPUT：
          - changes：本 fanout 的 ``PortVlanChange`` 清單。

        OUTPUT：無。

        RETURN：None。

        例外：
          - DriverConnectionError：未連線或下發時連線失敗（保留 ``__cause__``）。
        """
        self._send_config(self.build_commands(changes), "下發 VLAN 設定")

    def clear_vlan(self, changes: List[PortVlanChange]) -> None:
        """以清除樣板組裝並下發反向指令（best-effort 清理用，設計 §8）。

        以 ``action='remove'`` 覆寫傳入變更，確保 ``build_commands`` 走清除分支
        （呼叫端傳入的多為原 add 變更）。

        INPUT：
          - changes：要清除的 ``PortVlanChange`` 清單。

        OUTPUT：無。

        RETURN：None。

        例外：
          - DriverConnectionError：未連線或下發時連線失敗（保留 ``__cause__``）。
        """
        remove_changes = [
            PortVlanChange(
                fanout=change.fanout,
                interface=change.interface,
                vlan=change.vlan,
                action="remove",
                topology_node=change.topology_node,
            )
            for change in changes
        ]
        self._send_config(
            self.build_commands(remove_changes), "清除 VLAN 設定"
        )

    def _send_config(self, commands: List[str], action_desc: str) -> None:
        """以 ``send_config_set`` 送出一批設定指令（空批為 no-op，設計 §8）。

        INPUT：
          - commands：要送出的設定指令清單（List[str]）。
          - action_desc：動作描述（str），供錯誤訊息標示。

        OUTPUT：無。

        RETURN：None。

        例外：
          - DriverConnectionError：未連線或下發時連線失敗（保留 ``__cause__``）。
        """
        if not commands:
            return
        connection = self._require_connection()
        try:
            connection.send_config_set(commands)
        except Exception as exc:  # noqa: BLE001
            self._reraise_as_connection_error(exc, action_desc)

    def _require_connection(self) -> object:
        """取得已建立的連線 handle；未連線即丟 ``DriverConnectionError``。

        INPUT：無。

        OUTPUT：無。

        RETURN：
          - 底層連線 handle（object）。

        例外：
          - DriverConnectionError：尚未呼叫 ``connect()`` 或已斷線。
        """
        if self._connection is None:
            raise DriverConnectionError(
                "尚未與 fanout 設備（{0}）建立連線，請先呼叫 connect()".format(
                    self._host
                )
            )
        return self._connection

    def _reraise_as_connection_error(
        self, exc: Exception, action_desc: str
    ) -> None:
        """把 netmiko 例外包成 ``DriverConnectionError``（不含密碼，設計 §8、§9）。

        僅攔截 netmiko 的連線/認證/逾時例外（以類別名判定，避免未安裝 netmiko
        時 import 其例外型別）；其他非 netmiko 例外原樣往外傳（視為程式 bug，
        保留 traceback）。訊息僅含 host 與底層原因類別/訊息，絕不含密碼。

        INPUT：
          - exc：捕捉到的原始例外。
          - action_desc：動作描述（str），供錯誤訊息標示。

        OUTPUT：無。

        RETURN：不回傳（必定重拋）。

        例外：
          - DriverConnectionError：netmiko 連線類例外（保留 ``__cause__``）。
          - 原始例外：非 netmiko 連線類例外原樣重拋。
        """
        exc_class_name = type(exc).__name__
        netmiko_connection_errors = {
            "NetmikoTimeoutException",
            "NetmikoAuthenticationException",
            "NetMikoTimeoutException",
            "NetMikoAuthenticationException",
            "ConnectionException",
        }
        if exc_class_name in netmiko_connection_errors:
            raise DriverConnectionError(
                "{0}失敗（fanout {1}）：{2}".format(
                    action_desc, self._host, exc_class_name
                )
            ) from exc
        # 非 netmiko 連線類例外：原樣往外傳（不吞、不改）。
        raise exc
