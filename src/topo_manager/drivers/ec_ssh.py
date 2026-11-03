"""Edgecore 風格 SSH fanout driver（ec_ssh.py，設計 §8）。

本模組提供 ``FanoutDriver`` 的具體實作 ``EcSshDriver``：以 netmiko 對 fanout
設備建立 SSH 連線、唯讀讀取 VLAN 狀態、由純邏輯變更計畫組裝設定指令並下發。

netmiko 相依刻意採「延遲 import」（僅於實際要建立連線的 ``connect()`` 內
``import netmiko``）：純邏輯（build_commands）、設定載入與 CLI 路徑皆不觸發
import，確保未安裝 netmiko 的環境仍可匯入本模組並跑單元測試。單元測試以注入
``connection_factory`` 替身繞過真 netmiko（factory 回傳具 ``send_command`` /
``send_config_set`` / ``enable`` / ``disconnect`` 介面的 fake connection）。

指令樣板一律以模組常數集中管理（``_DISABLE_PAGER_TEMPLATE`` /
``_ENTER_INTF_TEMPLATE`` / ``_TRUNK_MODE_TEMPLATE`` / ``_APPLY_MEMBER_TEMPLATE``
/ ``_CLEAR_MEMBER_TEMPLATE``），已於 FEAT-003 4a 以實機 ECS5550-54X
（Edgecore、Cisco-like CLI）確認真實語法：進介面 ``interface ethernet 1/<port>``、
trunk 模式 ``switchport mode trunk``（冪等）、加 tagged 成員
``switchport allowed vlan add <vlan> tagged``（必須帶 ``tagged`` 關鍵字）、
移除 ``switchport allowed vlan remove <vlan>``。關 pager 為 ``terminal length 0``
（實機有效）。待跨型號差異時僅需更新本區常數即可（實機位址/帳密僅由 env 提供，
絕不寫入本檔）。

netmiko 的連線/認證/逾時例外一律就地捕捉並包成 ``DriverConnectionError``
（保留 ``__cause__``、訊息不含密碼，設計 §8、§9）。

歷史（HISTORY）：
  - 0.4.0  FEAT-002 初版，建立 EcSshDriver（netmiko 延遲 import、可注入
           connection_factory 的 fake-connection 可測、模組常數指令樣板、
           connect/enable/關 pager、read_show/read_vlans 自寫行解析、
           build_commands 純組裝、apply_vlan/clear_vlan 以 send_config_set
           下發、netmiko 例外包成 DriverConnectionError）。netmiko 僅於
           connect() 內延遲 import。
  - 0.4.1  FEAT-003 4a 實機修正：依 ECS5550-54X 真機實測重寫 _parse_vlan_output
           為 key-value 區塊格式解析（``VLAN ID :`` 開區塊、``Ports/Port
           Channels :`` 含續行收集成員、``Eth1/ N(S)`` token，(S) 視為
           tagged），並歸一化 interface 名（去除斜線後對齊空白）使 read-back
           與 apply 名一致；apply/clear 指令樣板改為實機語法（switchport mode
           trunk 冪等 + ``allowed vlan add <vlan> tagged`` / ``remove``，進介面
           ``interface ethernet 1/<port>``）。不碰 native vlan。

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

# 指令樣板（模組常數，設計 §8）。FEAT-003 4a 已以實機 ECS5550-54X 確認：
#   - 關 pager：'terminal length 0'（實機有效）。
#   - 進介面：'interface ethernet 1/<port>'（提示符 ...(config-if)#）。
#   - trunk 模式：'switchport mode trunk'（冪等——已是 trunk 則不重下）。
#   - 加 tagged 成員：'switchport allowed vlan add <vlan> tagged'（必帶 tagged）。
#   - 移除成員：'switchport allowed vlan remove <vlan>'。
# 不碰 native vlan（使用者定義：fanout 互聯 port 一律 trunk、全封包帶 tag）。
# 實機位址/帳密僅由 env 提供，絕不寫入本檔。
_DISABLE_PAGER_TEMPLATE = "terminal length 0"
# 進全域設定模式的指令：實機 ECS5550-54X 為 'configure'（非 cisco_ios 預設的
# 'configure terminal'）；傳給 netmiko send_config_set 的 config_mode_command。
_CONFIG_MODE_COMMAND = "configure"
_ENTER_INTF_TEMPLATE = "interface ethernet 1/{port}"
_TRUNK_MODE_TEMPLATE = "switchport mode trunk"
_APPLY_MEMBER_TEMPLATE = "switchport allowed vlan add {vlan} tagged"
_CLEAR_MEMBER_TEMPLATE = "switchport allowed vlan remove {vlan}"

# show vlan 真機格式解析樣板（ECS5550-54X，key-value 多行區塊，設計 §8、FEAT-003
# 4a）。每個 VLAN 為一區塊，以 'VLAN ID : <n>' 起始；成員列於 'Ports/Port
# Channels : ...' 行並可續行縮排。成員 token 形如 'Eth1/ 2(S)'（斜線後可能有對齊
# 空白，如 'Eth1/ 2' 與 'Eth1/10'），'(S)' 表 static/tagged 成員。
_VLAN_ID_RE = re.compile(r"^\s*VLAN\s+ID\s*:\s*(\d+)\s*$")
_PORTS_LABEL_RE = re.compile(r"^\s*Ports/Port\s+Channels\s*:\s*(.*)$")
# 任何「以冒號分隔的鍵值行」視為新欄位（用來判斷 Ports 續行是否結束）。
_KEY_VALUE_RE = re.compile(r"^\s*[A-Za-z][\w /]*\s*:\s*")
# 成員 token：EthX/ N 或 EthX/N（斜線後容忍對齊空白），後可帶 '(S)' 等旗標。
_MEMBER_TOKEN_RE = re.compile(
    r"\bEth(\d+)/\s*(\d+)\s*(\([A-Za-z]\))?"
)
# 自 apply 用的 interface 名（如 'Eth1/41'）取出 port 編號，供組裝進介面指令。
_INTERFACE_PORT_RE = re.compile(r"Eth\d+/\s*(\d+)")


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

    @staticmethod
    def _normalize_interface(chassis: str, port: str) -> str:
        """把成員 token 的 (chassis, port) 歸一化成 driver 內部一致的 interface 名。

        實機 show vlan 的成員 token 可能寫成 ``Eth1/ 2``（斜線後有對齊空白）或
        ``Eth1/10``；為了讓 read-back 解析出的 interface 名與 apply 時用的
        interface 名（``interface_template`` 產生的 ``Eth1/41`` 形式）一致比對，
        統一歸一化為 ``Eth<chassis>/<port>``（去除斜線後的對齊空白、port 不補零）。

        INPUT：
          - chassis：機箱/模組編號（str，如 ``'1'``）。
          - port：port 編號（str，可能含前導對齊空白，如 ``' 2'``）。

        OUTPUT：無。

        RETURN：
          - 歸一化後的 interface 名（str，如 ``'Eth1/2'`` / ``'Eth1/10'``）。

        例外：不拋出。
        """
        return "Eth{0}/{1}".format(chassis, int(port))

    def _parse_vlan_output(self, raw_output: str) -> VlanState:
        """把 ``show vlan`` 原始輸出解析成 ``VlanState``（設計 §8、FEAT-003 4a）。

        依 ECS5550-54X 實機格式解析：``show vlan`` 為 key-value 多行區塊，每個
        VLAN 一個區塊，以 ``VLAN ID : <n>`` 起始；該區塊的成員列於
        ``Ports/Port Channels : ...`` 一行並可續行（後續縮排行、直到遇到下一個
        鍵值行或下一個 ``VLAN ID`` 區塊為止）。成員 token 形如 ``Eth1/ 2(S)``
        （斜線後可能有對齊空白），``(S)`` 視為 static/tagged 成員（tagged=True）。
        interface 名經 ``_normalize_interface`` 歸一化，與 apply 用名一致以利
        read-back 比對。

        INPUT：
          - raw_output：``show vlan`` 的原始文字輸出（str）。

        OUTPUT：無。

        RETURN：
          - 解析後的 ``VlanState``。

        例外：
          - DriverParseError：輸出非字串或完全解析不到任何 VLAN 區塊（含片段
            摘要，供診斷設備型號/韌體差異）。
        """
        if not isinstance(raw_output, str):
            raise DriverParseError(
                "無法解析 'show vlan' 輸出（fanout {0}）：預期字串，實得 {1}"
                .format(self._host, type(raw_output).__name__)
            )

        members: Set[VlanMember] = set()
        existing_vlans: Set[int] = set()
        current_vlan: Optional[int] = None
        # 是否正處於某 VLAN 的 'Ports/Port Channels :' 成員收集（含續行）中。
        collecting_ports = False

        for line in raw_output.splitlines():
            vlan_match = _VLAN_ID_RE.match(line)
            if vlan_match is not None:
                current_vlan = int(vlan_match.group(1))
                existing_vlans.add(current_vlan)
                collecting_ports = False
                continue

            ports_match = _PORTS_LABEL_RE.match(line)
            if ports_match is not None:
                # 進入成員收集：本行冒號後文字即首批成員，後續縮排行為續行。
                collecting_ports = True
                self._collect_members(
                    ports_match.group(1), current_vlan, members
                )
                continue

            if collecting_ports:
                # 續行判定：若本行又是一個鍵值行（如 'Status : ...'）或空行，
                # 表示成員區塊結束；否則視為成員續行續收。
                if _KEY_VALUE_RE.match(line) is not None or not line.strip():
                    collecting_ports = False
                    continue
                self._collect_members(line, current_vlan, members)

        if not existing_vlans:
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

    def _collect_members(
        self,
        text: str,
        current_vlan: Optional[int],
        members: Set[VlanMember],
    ) -> None:
        """自一段文字抽出成員 token 並併入 ``members`` 集合（解析輔助）。

        每個 token（``Eth<chassis>/<port>`` 後可帶 ``(S)`` 等旗標）歸一化為一致
        interface 名後，以 ``tagged``（``(S)`` = static/tagged 成員）加入目前
        VLAN 的成員集合。當 ``current_vlan`` 為 None（成員出現在任何 VLAN 區塊
        之前，理論上不會發生）時略過。

        INPUT：
          - text：可能含成員 token 的一段文字（str）。
          - current_vlan：目前所屬 VLAN id（Optional[int]）。
          - members：待併入的成員集合（Set[VlanMember]，就地修改）。

        OUTPUT：
          - 就地將解析到的成員加入 ``members``。

        RETURN：None。

        例外：不拋出。
        """
        if current_vlan is None:
            return
        for match in _MEMBER_TOKEN_RE.finditer(text):
            chassis = match.group(1)
            port = match.group(2)
            flag = match.group(3)
            # (S) = static/tagged 成員（實機 trunk tagged）；本階段下發一律
            # 要求 tagged，故將帶 (S) 旗標成員視為 tagged=True。
            tagged = flag is not None and flag.upper() == "(S)"
            interface = self._normalize_interface(chassis, port)
            members.add(
                VlanMember(
                    vlan=current_vlan, interface=interface, tagged=tagged
                )
            )

    @staticmethod
    def _port_from_interface(interface: str) -> str:
        """自 interface 名（如 ``Eth1/41``）取出 port 編號，供組裝進介面指令。

        實機進介面指令為 ``interface ethernet 1/<port>``，故需由 driver 內部一致
        的 interface 名（``Eth<chassis>/<port>``）取出 ``<port>``。容忍斜線後對齊
        空白；若無法解析則回退為原字串（不致中斷組裝，便於診斷）。

        INPUT：
          - interface：driver 內部 interface 名（str，如 ``'Eth1/41'``）。

        OUTPUT：無。

        RETURN：
          - port 編號字串（str，如 ``'41'``）；無法解析時回原 interface。

        例外：不拋出。
        """
        match = _INTERFACE_PORT_RE.search(interface)
        if match is not None:
            return match.group(1)
        return interface

    def build_commands(self, changes: List[PortVlanChange]) -> List[str]:
        """由變更計畫以模組常數樣板組裝設定指令（純邏輯、不送出，設計 §8）。

        依 ECS5550-54X 實機語法：每筆 ``action='add'`` 變更先進入介面
        （``_ENTER_INTF_TEMPLATE``，``interface ethernet 1/<port>``）、確保為
        trunk 模式（``_TRUNK_MODE_TEMPLATE``，``switchport mode trunk``）、再加
        tagged 成員（``_APPLY_MEMBER_TEMPLATE``，``... add <vlan> tagged``）。
        trunk 模式於同一批中對同一介面只下一次（冪等去重；設備端重下亦為 no-op）。
        ``action='remove'`` 進介面後以 ``_CLEAR_MEMBER_TEMPLATE``
        （``... remove <vlan>``）移除。不碰 native vlan。

        INPUT：
          - changes：本 fanout 的 ``PortVlanChange`` 清單。

        OUTPUT：無。

        RETURN：
          - 依序要送出的設定指令字串清單（List[str]）；空輸入回空清單。

        例外：不涉及 I/O，故不拋連線類例外。
        """
        commands: List[str] = []
        # 本批中已下過 trunk 模式的介面（冪等去重，避免對同一介面重複下發）。
        trunked_interfaces: Set[str] = set()

        for change in changes:
            port = self._port_from_interface(change.interface)
            commands.append(_ENTER_INTF_TEMPLATE.format(port=port))
            if change.action == "add":
                # 確保為 trunk 模式（同一批同一介面只下一次）。
                if change.interface not in trunked_interfaces:
                    commands.append(_TRUNK_MODE_TEMPLATE)
                    trunked_interfaces.add(change.interface)
                commands.append(
                    _APPLY_MEMBER_TEMPLATE.format(vlan=change.vlan)
                )
            else:
                commands.append(
                    _CLEAR_MEMBER_TEMPLATE.format(vlan=change.vlan)
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
            # 實機進設定模式用 'configure'（非 cisco_ios 預設 'configure
            # terminal'），以 config_mode_command 覆寫；netmiko 會自動 end 離開。
            connection.send_config_set(
                commands, config_mode_command=_CONFIG_MODE_COMMAND
            )
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
