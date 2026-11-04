"""Edgecore 互聯 fanout SSH driver（drivers/ec_ssh_ic.py，互聯設計 §6.2、§6.3）。

MODULE NAME：
  topo_manager.drivers.ec_ssh_ic

PURPOSE：
  提供互聯 fanout（DUT 與 DUT 之間）所需的設備端能力：

  - 讀回 parser（純函式）：``parse_port_status``、``parse_port_switchport``、
    ``parse_vlan_id``，輸入為設備原始輸出字串、輸出為不可變 dataclass。
  - 指令組裝（純函式，單元測試逐字比對）：啟用 phase 1（設定，port 保持
    shutdown）、phase 2（開 port）、park（還原為 parking）、VLAN 定義清除。
  - ``EcSshInterconnectDriver``：繼承 ``EcSshDriver``，新增「會掃描輸出錯誤
    字樣」的 ``_send_config_checked`` 與上述指令的下發／讀回方法。

NOTES：
  - 所有指令字面皆來自真機實測（device-findings §2），不新增臆測字面。
  - 指令順序是硬性要求（錯序在真機會 ``Failed to set PVID`` 或讓 hybrid port 的
    PVID 被改成殘留 VLAN）：
      * 啟用：add untagged → native → remove 1 → no shutdown；
      * park：shutdown → add 1 untagged → no native → remove link VLAN。
  - ``vlan database`` 必須在全域模式下，故 VLAN 定義清除一律是獨立的第二批
    ``send_config_set``，不可與介面指令同批。
  - 只動池內 VLAN：park／verify 對池外 VLAN（如他人放的 tagged VLAN）一律不碰。
  - 原 ``EcSshDriver._send_config`` 不掃輸出的缺陷刻意不修改（打流側行為不動），
    僅互聯路徑使用 checked 版本。
  - 本模組不 import netmiko（沿用父類延遲 import）。

HISTORY：
  - 0.1.0  互聯 fanout 初版：parser、指令組裝、EcSshInterconnectDriver。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from topo_manager.drivers.ec_ssh import (
    _ACCESS_MODE_TEMPLATE,
    _CLEAR_MEMBER_TEMPLATE,
    _CONFIG_MODE_COMMAND,
    _ENTER_INTF_TEMPLATE,
    _NATIVE_VLAN_TEMPLATE,
    _NO_NATIVE_VLAN_TEMPLATE,
    EcSshDriver,
)
from topo_manager.errors import ApplyError, DriverParseError

# ====================================================================
# 指令樣板（模組常數；全部來自 device-findings §2）
# ====================================================================
_IC_SHUTDOWN = "shutdown"
_IC_NO_SHUTDOWN = "no shutdown"
_IC_MODE_ACCESS = _ACCESS_MODE_TEMPLATE  # switchport mode access
_IC_ADD_UNTAGGED = "switchport allowed vlan add {vlan} untagged"
_IC_NATIVE = _NATIVE_VLAN_TEMPLATE  # switchport native vlan {vlan}
_IC_REMOVE_VLAN = _CLEAR_MEMBER_TEMPLATE  # switchport allowed vlan remove {vlan}
_IC_NO_NATIVE = _NO_NATIVE_VLAN_TEMPLATE  # no switchport native vlan
_IC_VLAN_DB = "vlan database"
_IC_NO_VLAN = "no vlan {vlan}"
_IC_SHOW_VLAN_ID = "show vlan id {vlan}"
_IC_SHOW_SWITCHPORT = "show interfaces switchport ethernet 1/{port}"
_IC_SHOW_STATUS = "show interfaces status ethernet 1/{port}"

# 預設 VLAN（park 後 port 的歸屬）。
_DEFAULT_VLAN = 1

# send_config_set 的讀取逾時（秒），沿用設計 §6.2。
_CONFIG_READ_TIMEOUT = 60

# 設備輸出中視為錯誤的字樣（findings §3.3）；``does not exist`` 僅清除批次容忍。
_ERROR_LINE_RE = re.compile(
    r"Failed|^\s*%|Invalid input|Unrecognized|Incomplete|Ambiguous|does not exist",
    re.IGNORECASE,
)
_MISSING_TEXT = "does not exist"

# ====================================================================
# 讀回 parser 用正則
# ====================================================================
_PORT_ADMIN_RE = re.compile(r"Port\s+Admin\s*:\s*(Up|Down)", re.IGNORECASE)
_LINK_STATUS_RE = re.compile(r"Link\s+Status\s*:\s*(Up|Down)", re.IGNORECASE)
_SW_MODE_RE = re.compile(r"^\s*VLAN\s+Membership\s+Mode\s*:\s*(\w+)", re.IGNORECASE)
_SW_NATIVE_RE = re.compile(r"^\s*Native\s+VLAN\s*:\s*(\d+)", re.IGNORECASE)
_SW_ALLOWED_RE = re.compile(r"^\s*Allowed\s+VLAN\s*:\s*(.*)$", re.IGNORECASE)
_ALLOWED_TOKEN_RE = re.compile(r"(\d+)\((u|t)\)")
_NO_SUCH_VLAN_RE = re.compile(r"No\s+such\s+VLAN", re.IGNORECASE)

# 診斷訊息中附上的原始輸出片段長度。
_SNIPPET_LEN = 200


@dataclass(frozen=True)
class PortStatus:
    """``show interfaces status`` 的讀回結果。

    欄位：
      - enabled：管理面狀態（``Port Admin`` 為 Up 時 True）。
      - link_up：實體 link 狀態（``Link Status`` 為 Up 時 True）。
    """

    enabled: bool
    link_up: bool


@dataclass(frozen=True)
class PortSwitchport:
    """``show interfaces switchport`` 的讀回結果。

    欄位：
      - mode：VLAN Membership Mode 原字串（Access | Hybrid | Trunk）。
      - native_vlan：Native VLAN（PVID）。
      - allowed：``{vlan: 'u' | 't'}``，u=untagged、t=tagged。
    """

    mode: str
    native_vlan: int
    allowed: Dict[int, str]


def _snippet(raw: object) -> str:
    """取原始輸出的前段作為診斷片段。"""
    return str(raw).strip()[:_SNIPPET_LEN]


def parse_port_status(raw: str) -> PortStatus:
    """解析 ``show interfaces status ethernet 1/N``。

    INPUT：
      - raw：設備原始輸出（str）。

    RETURN：
      - ``PortStatus``。

    例外：
      - DriverParseError：輸入非字串，或缺 ``Port Admin`` / ``Link Status`` 任一行。
    """
    if not isinstance(raw, str):
        raise DriverParseError(
            "無法解析 port 狀態輸出：預期字串，實得 {0}".format(type(raw).__name__)
        )
    enabled_match = _PORT_ADMIN_RE.search(raw)
    link_match = _LINK_STATUS_RE.search(raw)
    if enabled_match is None or link_match is None:
        raise DriverParseError(
            "無法解析 port 狀態輸出（缺 Port Admin 或 Link Status）；輸出片段：{0}".format(
                _snippet(raw)
            )
        )
    return PortStatus(
        enabled=enabled_match.group(1).lower() == "up",
        link_up=link_match.group(1).lower() == "up",
    )


def parse_port_switchport(raw: str) -> PortSwitchport:
    """解析 ``show interfaces switchport ethernet 1/N``。

    ``Allowed VLAN`` 行後若下一行縮排且不含冒號，視為續行併入；token 以
    ``<vlan>(u|t)`` 抽取。

    INPUT：
      - raw：設備原始輸出（str）。

    RETURN：
      - ``PortSwitchport``。

    例外：
      - DriverParseError：輸入非字串，或 mode / native / allowed 任一缺漏。
    """
    if not isinstance(raw, str):
        raise DriverParseError(
            "無法解析 switchport 輸出：預期字串，實得 {0}".format(type(raw).__name__)
        )
    mode: Optional[str] = None
    native: Optional[int] = None
    allowed_text: Optional[str] = None
    lines = raw.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        mode_match = _SW_MODE_RE.match(line)
        native_match = _SW_NATIVE_RE.match(line)
        allowed_match = _SW_ALLOWED_RE.match(line)
        if mode_match is not None and mode is None:
            mode = mode_match.group(1)
        elif native_match is not None and native is None:
            native = int(native_match.group(1))
        elif allowed_match is not None and allowed_text is None:
            allowed_text = allowed_match.group(1)
            # 續行：縮排、非空、不含冒號。
            while index + 1 < len(lines):
                nxt = lines[index + 1]
                if nxt.strip() and nxt[0] in " \t" and ":" not in nxt:
                    allowed_text += " " + nxt.strip()
                    index += 1
                else:
                    break
        index += 1
    if mode is None or native is None or allowed_text is None:
        raise DriverParseError(
            "無法解析 switchport 輸出（缺 VLAN Membership Mode / Native VLAN / "
            "Allowed VLAN）；輸出片段：{0}".format(_snippet(raw))
        )
    allowed = {
        int(vlan): kind for vlan, kind in _ALLOWED_TOKEN_RE.findall(allowed_text)
    }
    return PortSwitchport(mode=mode, native_vlan=native, allowed=allowed)


def parse_vlan_id(raw: str, vlan: int) -> Optional[FrozenSet[str]]:
    """解析 ``show vlan id V``。

    重用 ``EcSshDriver._parse_vlan_output``（成員 ``Eth1/ 2(S)`` 已歸一化為
    ``Eth1/2``）。``(S)`` 對 tagged／untagged 都出現，untagged 判斷須改讀
    switchport，本函式只回「成員集合」。

    INPUT：
      - raw：設備原始輸出（str）。
      - vlan：查詢的 VLAN id（int）。

    RETURN：
      - ``None``：``No such VLAN``（VLAN 不存在）。
      - ``frozenset``：成員 ``'Eth1/N'`` 集合；VLAN 定義存在但無成員時為空集合。

    例外：
      - DriverParseError：輸入非字串，或輸出既非 ``No such VLAN`` 也解析不到該 VLAN。
    """
    if not isinstance(raw, str):
        raise DriverParseError(
            "無法解析 show vlan id 輸出：預期字串，實得 {0}".format(type(raw).__name__)
        )
    if _NO_SUCH_VLAN_RE.search(raw) is not None:
        return None
    # 父類 parser 只用實例的 _host 做訊息；以占位實例重用，不連線。
    state = EcSshDriver(host="-", username="-", password="-")._parse_vlan_output(raw)
    if vlan not in state.existing_vlans:
        raise DriverParseError(
            "show vlan id {0} 輸出中找不到該 VLAN；輸出片段：{1}".format(vlan, _snippet(raw))
        )
    return frozenset(
        member.interface for member in state.members if member.vlan == vlan
    )


# ====================================================================
# 指令組裝（純函式）
# ====================================================================
def pool_vlans(allowed: Dict[int, str], vlan_min: int, vlan_max: int) -> List[int]:
    """回傳 port 目前屬於池範圍內（且非 VLAN 1）的 VLAN 清單（升冪）。"""
    return sorted(
        vlan
        for vlan in allowed
        if vlan != _DEFAULT_VLAN and vlan_min <= vlan <= vlan_max
    )


def is_port_clean(switchport: PortSwitchport, stray: Sequence[int]) -> bool:
    """port 是否已是 parking 形態：native=1、VLAN 1 untagged、無池內殘留 VLAN。"""
    return (
        switchport.native_vlan == _DEFAULT_VLAN
        and switchport.allowed.get(_DEFAULT_VLAN) == "u"
        and not stray
    )


def build_phase1_commands(
    items: Sequence[Tuple[int, int]], force_access_mode: bool = True
) -> List[str]:
    """啟用 phase 1（設定，port 保持 shutdown）。

    每個 port：shutdown →（force 時）mode access → add V untagged → native V →
    remove 1。順序不可更動：native 必須在 add 之後，remove 1 在 native 之後。

    INPUT：
      - items：``[(fanout port 編號, VLAN)]``。
      - force_access_mode：是否下 ``switchport mode access``。
    """
    commands: List[str] = []
    for port, vlan in items:
        commands.append(_ENTER_INTF_TEMPLATE.format(port=port))
        commands.append(_IC_SHUTDOWN)
        if force_access_mode:
            commands.append(_IC_MODE_ACCESS)
        commands.append(_IC_ADD_UNTAGGED.format(vlan=vlan))
        commands.append(_IC_NATIVE.format(vlan=vlan))
        commands.append(_IC_REMOVE_VLAN.format(vlan=_DEFAULT_VLAN))
    return commands


def build_phase2_commands(ports: Iterable[int]) -> List[str]:
    """啟用 phase 2（開 port）：每個 port ``no shutdown``。"""
    commands: List[str] = []
    for port in ports:
        commands.append(_ENTER_INTF_TEMPLATE.format(port=port))
        commands.append(_IC_NO_SHUTDOWN)
    return commands


def build_park_commands(port: int, stray: Sequence[int], clean: bool) -> List[str]:
    """單一 port 的 park 指令。

    clean 時只下 ``shutdown``；否則依 findings §2.2：shutdown → add 1 untagged
    （必須先於 no native）→ no native → 對每個殘留 VLAN remove。

    INPUT：
      - port：fanout port 編號。
      - stray：要移除的池內殘留 VLAN（>1）。
      - clean：port 是否已乾淨。
    """
    commands = [_ENTER_INTF_TEMPLATE.format(port=port), _IC_SHUTDOWN]
    if clean:
        return commands
    commands.append(_IC_ADD_UNTAGGED.format(vlan=_DEFAULT_VLAN))
    commands.append(_IC_NO_NATIVE)
    for vlan in sorted(set(stray)):
        if vlan != _DEFAULT_VLAN:
            commands.append(_IC_REMOVE_VLAN.format(vlan=vlan))
    return commands


def build_park_batch(items: Sequence[Tuple[int, Sequence[int], bool]]) -> List[str]:
    """多個 port 的 park 指令串接；items 為 ``[(port, stray, clean)]``。"""
    commands: List[str] = []
    for port, stray, clean in items:
        commands.extend(build_park_commands(port, stray, clean))
    return commands


def build_vlan_clear_commands(vlans: Iterable[int]) -> List[str]:
    """VLAN 定義清除批次：``vlan database`` + 每個 ``no vlan V``；無 VLAN 回空。"""
    ordered = sorted(set(vlans))
    if not ordered:
        return []
    return [_IC_VLAN_DB] + [_IC_NO_VLAN.format(vlan=vlan) for vlan in ordered]


class EcSshInterconnectDriver(EcSshDriver):
    """互聯 fanout driver：在 ``EcSshDriver`` 上新增 checked 下發與讀回方法。

    繼承 connect／disconnect／read_show／_require_connection／
    _reraise_as_connection_error；``FanoutDriver`` 的抽象方法已由父類實作，故
    可直接實例化。
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
        force_access_mode: bool = True,
    ) -> None:
        """記錄連線參數與 ``force_access_mode``（True 時 phase 1 下 mode access）。"""
        super().__init__(
            host=host,
            username=username,
            password=password,
            device_type=device_type,
            secret=secret,
            interface_template=interface_template,
            connection_factory=connection_factory,
        )
        self._force_access_mode = force_access_mode

    @property
    def force_access_mode(self) -> bool:
        """是否在啟用時強制 ``switchport mode access``。"""
        return self._force_access_mode

    def _send_config_checked(
        self, batch: List[str], desc: str, tolerate_missing: bool = False
    ) -> str:
        """送出一批設定指令並掃描輸出中的錯誤字樣。

        INPUT：
          - batch：設定指令清單；空批為 no-op 回空字串。
          - desc：動作描述（供錯誤訊息）。
          - tolerate_missing：True（僅清除批次）時容忍含 ``does not exist`` 的行。

        RETURN：
          - 設備回傳的原始輸出（str）。

        例外：
          - ApplyError：輸出含錯誤字樣；訊息含 fanout、命中行與前後各一行。
          - DriverConnectionError：連線類失敗（由父類包裝）。
        """
        if not batch:
            return ""
        connection = self._require_connection()
        try:
            output = connection.send_config_set(
                batch,
                config_mode_command=_CONFIG_MODE_COMMAND,
                read_timeout=_CONFIG_READ_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            self._reraise_as_connection_error(exc, desc)
        text = output if isinstance(output, str) else ""
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if _ERROR_LINE_RE.search(line) is None:
                continue
            if tolerate_missing and _MISSING_TEXT in line.lower():
                continue
            previous_line = lines[index - 1].strip() if index > 0 else ""
            next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
            raise ApplyError(
                "{0}失敗（fanout {1}）：設備回報『{2}』（前一行：{3}；後一行：{4}）".format(
                    desc, self._host, line.strip(), previous_line, next_line
                )
            )
        return text

    # ------------------------------------------------------------------
    # 讀回
    # ------------------------------------------------------------------
    def read_port_status(self, port: int) -> PortStatus:
        """讀並解析 port 的 管理面／link 狀態。"""
        return parse_port_status(self.read_show(_IC_SHOW_STATUS.format(port=port)))

    def read_port_switchport(self, port: int) -> PortSwitchport:
        """讀並解析 port 的 switchport 狀態。"""
        return parse_port_switchport(
            self.read_show(_IC_SHOW_SWITCHPORT.format(port=port))
        )

    def read_vlan_members(self, vlan: int) -> Optional[FrozenSet[str]]:
        """讀並解析 ``show vlan id V``；None=VLAN 不存在。"""
        return parse_vlan_id(self.read_show(_IC_SHOW_VLAN_ID.format(vlan=vlan)), vlan)

    # ------------------------------------------------------------------
    # 下發
    # ------------------------------------------------------------------
    def enable_phase1(self, items: Sequence[Tuple[int, int]]) -> List[str]:
        """下發啟用 phase 1（port 保持 shutdown）；回傳實際送出的指令。"""
        commands = build_phase1_commands(items, self._force_access_mode)
        self._send_config_checked(commands, "互聯啟用 phase 1 設定")
        return commands

    def enable_phase2(self, ports: Sequence[int]) -> List[str]:
        """下發啟用 phase 2（no shutdown）；回傳實際送出的指令。"""
        commands = build_phase2_commands(ports)
        self._send_config_checked(commands, "互聯啟用 phase 2（開 port）")
        return commands

    def park_ports(self, items: Sequence[Tuple[int, Sequence[int], bool]]) -> List[str]:
        """下發 park（items 為 ``[(port, stray, clean)]``）；回傳實際送出的指令。"""
        commands = build_park_batch(items)
        self._send_config_checked(commands, "互聯 park")
        return commands

    def clear_vlan_definitions(
        self, vlans: Iterable[int], vlan_min: int, vlan_max: int
    ) -> Tuple[List[int], List[str]]:
        """清除已無成員的池內 VLAN 定義（獨立批次，容忍 does not exist）。

        對每個池內且非 1 的 VLAN：``show vlan id V``；不存在則略過；成員為空才
        清除；仍有成員則保留並列為警告。

        RETURN：
          - ``(已清除的 VLAN 清單, 警告清單)``。
        """
        to_clear: List[int] = []
        warnings: List[str] = []
        for vlan in sorted(set(vlans)):
            if vlan == _DEFAULT_VLAN or not (vlan_min <= vlan <= vlan_max):
                continue
            members = self.read_vlan_members(vlan)
            if members is None:
                continue
            if members:
                warnings.append(
                    "VLAN {0} 仍有成員 {1}，保留其定義".format(vlan, ", ".join(sorted(members)))
                )
                continue
            to_clear.append(vlan)
        self._send_config_checked(
            build_vlan_clear_commands(to_clear), "清除 VLAN 定義", tolerate_missing=True
        )
        return to_clear, warnings
