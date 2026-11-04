"""互聯測試共用輔助（tests/ic_fakes.py，互聯設計 §10）。

提供 ``make_ic_data``（以 dict 組出含 interconnect 區段的小拓樸，可由呼叫端
就地修改以注入錯誤變體）、``write_topology``（寫 tmp YAML）與
``make_ic_topology``（寫檔並以 ``load_topology`` 載入）。

預設拓樸：1 台打流 generator、1 台打流 fanout、``groups`` 組互聯 DUT（每組 4
台，il_ports=Eth8..Eth11，slot 1..4），全在 rack-1；互聯 fanout ``ic-1``
（48 port，最多承載 3 組）。組 k（0 起算）第 i 台 DUT 第 j 個 il_port 接
``ic-1`` 的 port ``k*16 + i*4 + j + 1``。

另提供 ``FakeEcSwitch``：假 netmiko connection，內含小型設備狀態機，行為照
device-findings 實測（未是成員就 native vlan → ``Failed to set PVID``、
``no switchport native vlan`` 時 port 不在 VLAN 1 → ``Failed to set PVID``、
remove 不存在的 VLAN → ``VLAN<n> does not exist``、add untagged 自動建 VLAN、
``vlan database`` 於 config-if 內 → ``% Invalid input detected``、移除原生
VLAN 成員時 PVID 自動改成剩餘成員）。使「指令順序正確性」成為可離線驗證的
行為，而非字串比對。

歷史（HISTORY）：
  - 0.1.0  互聯 fanout 初版。
  - 0.2.0  新增 FakeEcSwitch 設備狀態機（含 hybrid + 4093 tagged 初始狀態）。
  - 0.3.0  設備下發 REST：新增 FakeTrafficSwitch（打流側 show vlan／
           show dot1q-tunnel 假設備）、FakeSwitchFarm（依 host 配發假設備，並
           可注入認證失敗／逾時／ReadTimeout／靜默忽略設定等失敗模式）與
           以 netmiko 例外類別名命名的替身例外。

Copyright (c) 2026 topo-manager。
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import yaml

from topo_manager.loader import load_topology
from topo_manager.models import Topology

IL_PORTS = ["Eth8", "Eth9", "Eth10", "Eth11"]


def make_ic_data(groups: int = 1) -> Dict:
    """回傳含 interconnect 區段的拓樸 dict。"""
    duts = []
    ic_links = []
    for k in range(groups):
        for i in range(4):
            name = "dut-g{0}-{1}".format(k + 1, i)
            duts.append(
                {
                    "name": name,
                    "model": "modelA",
                    "tg_ports": ["Eth0", "Eth1"],
                    "il_ports": list(IL_PORTS),
                    "slot": i + 1,
                    "group": "g{0}".format(k + 1),
                }
            )
            for j, il_port in enumerate(IL_PORTS):
                ic_links.append(
                    {
                        "a": {"dev": name, "port": il_port},
                        "b": {"dev": "ic-1", "port": k * 16 + i * 4 + j + 1},
                    }
                )
    return {
        "version": 1,
        "site": "ic-test",
        "generators": [{"name": "ixia-1", "type": "ixia", "ports": [1, 2]}],
        "fanouts": [{"name": "leaf-1", "role": "leaf", "port_count": 32}],
        "racks": [{"name": "rack-1", "duts": duts}],
        "links": [
            {"a": {"dev": "ixia-1", "port": 1}, "b": {"dev": "leaf-1", "port": 1}},
            {"a": {"dev": "leaf-1", "port": 11}, "b": {"dev": duts[0]["name"], "port": "Eth0"}},
        ],
        "interconnect": {
            "fanouts": [{"name": "ic-1", "port_count": 48, "rack": "rack-1"}],
            "links": ic_links,
        },
    }


def write_topology(tmp_path: Path, data: Dict, name: str = "topo.yaml") -> Path:
    """把拓樸 dict 寫成 YAML 檔並回傳路徑。"""
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def make_ic_topology(
    tmp_path: Path,
    groups: int = 1,
    mutate: Optional[Callable[[Dict], None]] = None,
) -> Topology:
    """組拓樸 dict（可選 ``mutate`` 就地修改）→ 寫 tmp YAML → load_topology。"""
    data = make_ic_data(groups)
    if mutate is not None:
        mutate(data)
    return load_topology(write_topology(tmp_path, data))


# ======================================================================
# FakeEcSwitch：小型 Edgecore 設備狀態機（照 device-findings 實測行為）
# ======================================================================
_INVALID_LINES = ["                      ^", "% Invalid input detected at '^' marker."]
_SHOW_STATUS_RE = re.compile(r"^show interfaces status ethernet 1/(\d+)$")
_SHOW_SWITCHPORT_RE = re.compile(r"^show interfaces switchport ethernet 1/(\d+)$")
_SHOW_VLAN_ID_RE = re.compile(r"^show vlan id (\d+)$")
_INTERFACE_RE = re.compile(r"^interface ethernet 1/(\d+)$")
_ADD_RE = re.compile(r"^switchport allowed vlan add (\d+) (untagged|tagged)$")
_REMOVE_RE = re.compile(r"^switchport allowed vlan remove (\d+)$")
_NATIVE_RE = re.compile(r"^switchport native vlan (\d+)$")
_NO_VLAN_RE = re.compile(r"^no vlan (\d+)$")


class FakeEcSwitch:
    """假 netmiko connection + 設備狀態機。

    初始狀態：``port_count`` 個 access port，皆管理面 Up、link Down、native 1、
    VLAN 1 untagged。``set_hybrid`` 可把某 port 改成 hybrid 並帶 tagged VLAN
    （如 findings 的 Eth1/43 + 4093）。

    測試鉤子：
      - ``silent_drop``：指令字串集合；命中時整行被設備默默忽略（用來模擬讀回不符）。
      - ``config_batches``：每次 ``send_config_set`` 的指令清單。
      - ``commands``：所有設定指令（攤平）。
      - ``read_commands``：所有 ``send_command`` 指令。
      - ``connect_count`` / ``disconnect_count``。
    """

    def __init__(self, port_count: int = 54) -> None:
        self.ports: Dict[int, Dict] = {
            number: {
                "enabled": True,
                "link_up": False,
                "mode": "Access",
                "native": 1,
                "allowed": {1: "u"},
            }
            for number in range(1, port_count + 1)
        }
        self.defined: Set[int] = {1}
        self.silent_drop: Set[str] = set()
        self.config_batches: List[List[str]] = []
        self.commands: List[str] = []
        self.read_commands: List[str] = []
        self.connect_count = 0
        self.disconnect_count = 0

    # ---- 初始狀態設定 -------------------------------------------------
    def set_hybrid(self, port: int, tagged: Optional[List[int]] = None) -> None:
        """把 port 設為 hybrid，並加上 tagged 成員（自動建立 VLAN 定義）。"""
        self.ports[port]["mode"] = "Hybrid"
        for vlan in tagged or []:
            self.ports[port]["allowed"][vlan] = "t"
            self.defined.add(vlan)

    def snapshot(self) -> Dict:
        """回傳目前狀態的深拷貝（用於前後比對）。"""
        return copy.deepcopy({"ports": self.ports, "defined": sorted(self.defined)})

    # ---- netmiko connection 介面 --------------------------------------
    def factory(self, **kwargs: object) -> "FakeEcSwitch":
        """connection_factory：回傳自身（共用同一份設備狀態）。"""
        self.connect_count += 1
        return self

    def enable(self) -> None:
        """no-op。"""

    def disconnect(self) -> None:
        """記錄斷線次數。"""
        self.disconnect_count += 1

    def send_command(self, command: str, **kwargs: object) -> str:
        """唯讀 show 指令。"""
        self.read_commands.append(command)
        if command == "terminal length 0":
            return ""
        match = _SHOW_STATUS_RE.match(command)
        if match is not None and int(match.group(1)) in self.ports:
            return self._show_status(int(match.group(1)))
        match = _SHOW_SWITCHPORT_RE.match(command)
        if match is not None and int(match.group(1)) in self.ports:
            return self._show_switchport(int(match.group(1)))
        match = _SHOW_VLAN_ID_RE.match(command)
        if match is not None:
            return self._show_vlan_id(int(match.group(1)))
        return "\n".join(_INVALID_LINES)

    def send_config_set(
        self,
        commands: List[str],
        config_mode_command: Optional[str] = None,
        read_timeout: Optional[int] = None,
        **kwargs: object,
    ) -> str:
        """設定指令：每批從全域模式開始；回傳含回顯與錯誤行的文字。"""
        self.config_batches.append(list(commands))
        state: Dict = {"mode": "global", "port": None}
        out: List[str] = []
        prompts = {"global": "(config)#", "if": "(config-if)#", "vlandb": "(config-vlan)#"}
        for command in commands:
            out.append("Vty-1" + prompts[state["mode"]] + command)
            self.commands.append(command)
            if command in self.silent_drop:
                continue
            out.extend(self._exec(command, state))
        return "\n".join(out)

    # ---- 指令執行 -----------------------------------------------------
    def _exec(self, command: str, state: Dict) -> List[str]:
        mode = state["mode"]
        if command == "exit":
            state["mode"] = "global"
            state["port"] = None
            return []
        match = _INTERFACE_RE.match(command)
        if match is not None and mode in ("global", "if"):
            if int(match.group(1)) in self.ports:
                state["mode"] = "if"
                state["port"] = int(match.group(1))
                return []
            return list(_INVALID_LINES)
        if mode == "global" and command == "vlan database":
            state["mode"] = "vlandb"
            return []
        if mode == "vlandb":
            match = _NO_VLAN_RE.match(command)
            if match is not None:
                vlan = int(match.group(1))
                if vlan != 1:
                    self.defined.discard(vlan)
                    for port in self.ports.values():
                        port["allowed"].pop(vlan, None)
                return []
            return list(_INVALID_LINES)
        if mode == "if":
            return self._exec_interface(command, state["port"])
        return list(_INVALID_LINES)

    def _exec_interface(self, command: str, number: int) -> List[str]:
        port = self.ports[number]
        failed = ["Failed to set PVID on ethernet 1/{0}".format(number)]
        if command == "shutdown":
            port["enabled"] = False
            return []
        if command == "no shutdown":
            port["enabled"] = True
            return []
        if command == "switchport mode access":
            port["mode"] = "Access"
            return []
        match = _ADD_RE.match(command)
        if match is not None:
            vlan = int(match.group(1))
            self.defined.add(vlan)
            port["allowed"][vlan] = "u" if match.group(2) == "untagged" else "t"
            return []
        match = _NATIVE_RE.match(command)
        if match is not None:
            vlan = int(match.group(1))
            if vlan not in port["allowed"]:
                return failed
            port["native"] = vlan
            return []
        if command == "no switchport native vlan":
            if 1 not in port["allowed"]:
                return failed
            port["native"] = 1
            return []
        match = _REMOVE_RE.match(command)
        if match is not None:
            vlan = int(match.group(1))
            if vlan not in self.defined:
                return ["VLAN{0} does not exist".format(vlan)]
            if vlan in port["allowed"]:
                del port["allowed"][vlan]
                if port["native"] == vlan:
                    port["native"] = min(port["allowed"]) if port["allowed"] else 1
            return []
        return list(_INVALID_LINES)

    # ---- show 輸出（格式逐字取自 findings） ----------------------------
    def _show_status(self, number: int) -> str:
        port = self.ports[number]
        return "\n".join(
            [
                "Information of Eth 1/{0}".format(number),
                " Basic Information:",
                "  Port Type              : 10GBASE SFP+",
                "  Port Admin             : {0}".format("Up" if port["enabled"] else "Down"),
                "  Link Status            : {0}".format("Up" if port["link_up"] else "Down"),
            ]
        )

    def _show_switchport(self, number: int) -> str:
        port = self.ports[number]
        allowed = ", ".join(
            "{0}({1})".format(vlan, port["allowed"][vlan]) for vlan in sorted(port["allowed"])
        )
        return "\n".join(
            [
                "Information of Eth 1/{0}".format(number),
                " VLAN Membership Mode          : {0}".format(port["mode"]),
                " Ingress Rule                  : Disabled",
                " Acceptable Frame Type         : All frames",
                " Native VLAN                   : {0}".format(port["native"]),
                " Priority for Untagged Traffic : 0",
                " GVRP Status                   : Disabled",
                " Allowed VLAN                  :  {0}".format(allowed),
                " Forbidden VLAN                : ",
            ]
        )

    def _show_vlan_id(self, vlan: int) -> str:
        if vlan not in self.defined:
            return "No such VLAN."
        members = " ".join(
            "Eth1/{0:>2}(S)".format(number)
            for number in sorted(self.ports)
            if vlan in self.ports[number]["allowed"]
        )
        return "\n".join(
            [
                "VLAN ID             : {0}".format(vlan),
                "Type                : Static",
                "Name                : ",
                "Status              : Active",
                "Ports/Port Channels : {0}".format(members),
            ]
        )


# ======================================================================
# 設備下發 REST 用假設備（絕不連真實設備）
# ======================================================================
class NetmikoAuthenticationException(Exception):
    """替身：類別名與 netmiko 的認證失敗例外相同，供 driver 以名辨識。"""


class NetmikoTimeoutException(Exception):
    """替身：類別名與 netmiko 的連線逾時例外相同。"""


class ReadTimeout(Exception):
    """替身：類別名與 netmiko 4.x send_command 逾時例外相同。"""


class FakeTrafficSwitch:
    """打流側假 netmiko connection：回應 ``show vlan`` 與 ``show dot1q-tunnel``。

    只模擬 EcSshDriver 下發所需的最小行為：進介面、tagged 成員、native vlan、
    tunnel mode、全域 tunnel control。``drop_config=True`` 時所有設定指令被靜默
    忽略（用來製造 read-back 不符）。
    """

    def __init__(self) -> None:
        self.tagged: Dict[int, Set[int]] = {}
        self.tunnel_modes: Dict[int, str] = {}
        self.tunnel_enabled = False
        self.drop_config = False
        self.config_batches: List[List[str]] = []
        self.connect_count = 0

    def enable(self) -> None:
        """no-op。"""

    def disconnect(self) -> None:
        """no-op。"""

    def send_command(self, command: str, **kwargs: object) -> str:
        """唯讀 show 指令。"""
        if command == "terminal length 0":
            return ""
        if command == "show vlan":
            return self._show_vlan()
        if command == "show dot1q-tunnel":
            return self._show_tunnel()
        return "% Invalid input detected"

    def send_config_set(
        self, commands: List[str], config_mode_command: Optional[str] = None, **kwargs: object
    ) -> str:
        """設定指令：依序套用到狀態（drop_config 時略過）。"""
        self.config_batches.append(list(commands))
        if self.drop_config:
            return ""
        port = None
        for command in commands:
            match = re.match(r"^interface ethernet 1/(\d+)$", command)
            if match is not None:
                port = int(match.group(1))
                continue
            if command == "dot1q-tunnel system-tunnel-control":
                self.tunnel_enabled = True
                continue
            if port is None:
                continue
            match = re.match(r"^switchport allowed vlan add (\d+) tagged$", command)
            if match is not None:
                self.tagged.setdefault(int(match.group(1)), set()).add(port)
                continue
            match = re.match(r"^switchport allowed vlan remove (\d+)$", command)
            if match is not None:
                self.tagged.get(int(match.group(1)), set()).discard(port)
                continue
            if command == "switchport dot1q-tunnel mode access":
                self.tunnel_modes[port] = "Access"
            elif command == "switchport dot1q-tunnel mode uplink":
                self.tunnel_modes[port] = "Uplink"
            elif command == "no switchport dot1q-tunnel mode":
                self.tunnel_modes.pop(port, None)
        return ""

    def _show_vlan(self) -> str:
        blocks = ["VLAN ID             : 1", "Ports/Port Channels : "]
        for vlan in sorted(self.tagged):
            members = " ".join(
                "Eth1/{0:>2}(S)".format(number) for number in sorted(self.tagged[vlan])
            )
            blocks.append("VLAN ID             : {0}".format(vlan))
            blocks.append("Ports/Port Channels : {0}".format(members))
        return "\n".join(blocks)

    def _show_tunnel(self) -> str:
        lines = [
            "802.1Q Tunnel Status : {0}".format("Enabled" if self.tunnel_enabled else "Disabled")
        ]
        for number in sorted(self.tunnel_modes):
            lines.append("Eth 1/{0:>2} {1} Disabled".format(number, self.tunnel_modes[number]))
        return "\n".join(lines)


class _ReadTimeoutConnection:
    """連線後每個 send_command 都丟 ReadTimeout 的假 connection。"""

    def enable(self) -> None:
        """no-op。"""

    def disconnect(self) -> None:
        """no-op。"""

    def send_command(self, command: str, **kwargs: object) -> str:
        raise ReadTimeout("read timed out")


class FakeSwitchFarm:
    """依 host 配發假設備的 ``connection_factory``，可注入失敗模式。

    參數：
      - kind：``"traffic"`` 配 FakeTrafficSwitch；``"ic"`` 配 FakeEcSwitch。
      - mode：``ok`` | ``auth`` | ``timeout`` | ``readtimeout`` | ``drop``。
    屬性：
      - connects：每次 factory 被呼叫時的 host 清單（可斷言「從未連線」）。
      - switches：host → 假設備。
    """

    def __init__(self, kind: str, mode: str = "ok") -> None:
        self.kind = kind
        self.mode = mode
        self.connects: List[str] = []
        self.switches: Dict[str, object] = {}

    def factory(self, **kwargs: object) -> object:
        """netmiko connection_factory：依模式丟替身例外或回假設備。"""
        host = str(kwargs.get("host"))
        self.connects.append(host)
        if self.mode == "auth":
            raise NetmikoAuthenticationException("auth failed")
        if self.mode == "timeout":
            raise NetmikoTimeoutException("timed out")
        if self.mode == "readtimeout":
            return _ReadTimeoutConnection()
        if host not in self.switches:
            switch = FakeTrafficSwitch() if self.kind == "traffic" else FakeEcSwitch()
            if self.mode == "drop":
                if self.kind == "traffic":
                    switch.drop_config = True
                else:
                    switch.silent_drop = {"no shutdown"}
            self.switches[host] = switch
        return self.switches[host]
