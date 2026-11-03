"""drivers 層 live 設備測試骨架（設計 §8.2、§11.2、FEAT-003）。

本檔所有測試皆標 ``@pytest.mark.live``：預設因 pyproject 的 ``addopts =
"-m 'not live'"`` 不被收集；即使以 ``-m live`` 明確收集，缺少連線 env 時亦一律
``pytest.skip``（絕不 fail、絕不 error）。live 設備的位址/帳密僅由環境變數提供，
永不自任何版控檔讀取（設計 §11.1、finding #10）。

連線 env（缺任一即整檔 skip）：
  - ``TOPO_FANOUT_HOST``：設備可達位址。
  - ``TOPO_FANOUT_USERNAME`` / ``TOPO_FANOUT_PASSWORD``：登入帳密。
可選 env：
  - ``TOPO_FANOUT_DEVICE_TYPE``：netmiko device_type（預設 ``cisco_ios``）。
  - ``TOPO_FANOUT_SECRET``：enable 密碼（有則 connect 後進特權模式）。
  - ``TOPO_FANOUT_INTERFACE_TEMPLATE``：port→介面名樣板（預設 ``Eth1/{port}``）。
  - ``TOPO_FANOUT_TRUNK_PORT``：4b 寫入/復原測試要操作的 trunk port 編號
    （缺此一項時 4b 單獨 skip；4a 唯讀仍可跑）。建議 Eth1/41。
  - ``TOPO_FANOUT_EDGE_PORT``：Phase 4 QinQ 邊緣 port 寫入/復原要操作的 port
    編號（缺此一項時邊緣 live 測試單獨 skip）。建議 Eth1/42。
  - ``TOPO_FANOUT_CAPTURE_DIR``：擷取真機 ``show vlan`` / ``show dot1q-tunnel``
    輸出的存放目錄（缺省時寫入 pytest 的 tmp_path，不落入版控）。

4a（唯讀）：connect → ``read_show('show version')`` → ``read_vlans()``，斷言能連、
能解析、且全程未寫入；並擷取真機 ``show vlan`` 原始輸出存檔，供日後把 ec_ssh 的
暫定 parser fixture 換成真機輸出。存檔頂端附「§8.2 待證清單」註解，提醒覆核：
device_type、關 pager 指令、apply/clear 指令樣板、是否登入即 enable、interface
命名正則。

4b（高風險）：以保留的 VLAN 3999 對某 trunk port ``apply_vlan`` →
read-back（比對該 interface 以 tagged=True 現身）→ ``clear_vlan`` →
read-back（確認已還原、VLAN 3999 成員消失）。整段寫入/驗證/還原以 try/finally
包覆，確保即使中途斷言失敗仍強制清除復原（設計 §11.2），不在設備上留痕。

歷史（HISTORY）：
  - 0.4.0  FEAT-003 初版，建立 live 測試骨架（預設 skip；4a 唯讀探測並擷取
           show vlan 供 parser fixture；4b VLAN 3999 寫入/復原並 try/finally
           保證還原）。連線憑證一律讀 env，不從版控檔讀。
  - 0.5.0  Phase 4 QinQ/dot1q-tunnel 擴充（決策 4、finding #1，AC-6/13/14）：
           擷取真機 ``show dot1q-tunnel`` 原始輸出供 per-port 正則/fixture 覆核；
           全域 ``setup_global_tunnel(apply=True)`` + ``read_tunnel_status`` 確認
           Enabled（冪等）；邊緣 port（TOPO_FANOUT_EDGE_PORT）native vlan 3999 +
           dot1q-tunnel mode access 寫入→read-back Mode=Access→try/finally 以
           EDGE remove（no native vlan + no dot1q-tunnel mode 還原 Normal）還原
           並 read-back；互聯 port 加驗 dot1q-tunnel mode uplink 並以 UPLINK
           remove + no dot1q-tunnel mode 還原、read-back 確認回 Normal。finally
           另刪自動建立的 VLAN 3999 定義（vlan database / no vlan 3999），確保
           測後完全無殘留。只動 VLAN 3999 與測試 port，絕不動既有 VLAN
           1/2/3/4/10/20/4093 與 Eth1/1/Eth1/11。缺 env 一律 skip。
  - 0.5.1  live 還原指令修正（finding #1）：真機 ECS5550-54X 不接受
           'switchport dot1q-tunnel mode normal'（回 '% Invalid input
           detected'），tunnel 模式還原正確字面為 'no switchport dot1q-tunnel
           mode'；VLAN 刪除須於 'vlan database' 內 'no vlan <id>'。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

import pytest

from topo_manager.drivers.base import PortRole_E, PortVlanChange, VlanState
from topo_manager.drivers.ec_ssh import EcSshDriver

# 本檔整檔標記 live：預設不收集；-m live 時缺 env 亦 skip。
pytestmark = pytest.mark.live

# 4b 寫入/復原測試固定使用的保留 VLAN（設計 §11.2）；選此高位保留值以降低與
# 現場既有 VLAN 衝突的風險。
_RESERVED_TEST_VLAN = 3999

# 4a 擷取檔頂端附帶的 §8.2 覆核結論（FEAT-003 4a 已以實機 ECS5550-54X 覆核）。
_CAPTURE_HEADER = (
    "# 真機 'show vlan' 原始輸出擷取（FEAT-003 4a）。\n"
    "# §8.2 覆核結論——已以實機 ECS5550-54X（Edgecore、Cisco-like CLI）確認：\n"
    "#   1. device_type：netmiko device_type=cisco_ios 可連（show version 成功）。\n"
    "#   2. 關 pager 指令：_DISABLE_PAGER_TEMPLATE='terminal length 0' 有效。\n"
    "#   3. apply/clear 樣板：進介面 'interface ethernet 1/<port>'、trunk 模式\n"
    "#      'switchport mode trunk'（冪等）、加成員 'switchport allowed vlan\n"
    "#      add <vlan> tagged'（必帶 tagged）、移除 'switchport allowed vlan\n"
    "#      remove <vlan>'；進設定模式用 'configure'（非 'configure terminal'）。\n"
    "#   4. 是否登入即 enable：免 secret 即可下設定（登入帳號登入即具權限）。\n"
    "#   5. show vlan 格式：key-value 多行區塊（'VLAN ID :' 開區塊、'Ports/Port\n"
    "#      Channels :' 含續行收集成員），成員 token 'Eth1/ N(S)'，(S)=tagged；\n"
    "#      parser 已改為 _VLAN_ID_RE/_PORTS_LABEL_RE/_MEMBER_TOKEN_RE 並歸一化\n"
    "#      interface 名（'Eth1/ 2' → 'Eth1/2'）以利 read-back 比對。\n"
)


def _live_env() -> Dict[str, str]:
    """讀取並回傳 live 連線所需的環境變數（缺必填即 skip，設計 §11.1）。

    INPUT：無（讀取程序環境變數 ``os.environ``）。

    OUTPUT：無。

    RETURN：
      - ``{'host','username','password','device_type','secret',
        'interface_template'}`` 字串映射；``secret`` 缺省為空字串。

    例外：
      - 不拋出；缺任一必填連線 env（host/username/password）時以
        ``pytest.skip`` 中止（絕不 fail）。
    """
    host = os.environ.get("TOPO_FANOUT_HOST")
    username = os.environ.get("TOPO_FANOUT_USERNAME")
    password = os.environ.get("TOPO_FANOUT_PASSWORD")
    if not host or not username or not password:
        pytest.skip(
            "缺少 live 連線 env（TOPO_FANOUT_HOST/USERNAME/PASSWORD），"
            "跳過 live 測試"
        )
    return {
        "host": host,
        "username": username,
        "password": password,
        "device_type": os.environ.get(
            "TOPO_FANOUT_DEVICE_TYPE", "cisco_ios"
        ),
        "secret": os.environ.get("TOPO_FANOUT_SECRET", ""),
        "interface_template": os.environ.get(
            "TOPO_FANOUT_INTERFACE_TEMPLATE", "Eth1/{port}"
        ),
    }


@pytest.fixture
def live_env() -> Dict[str, str]:
    """pytest fixture：提供 live 連線 env（缺必填即 skip）。

    INPUT：無。

    OUTPUT：無。

    RETURN：
      - ``_live_env()`` 的連線 env 映射。

    例外：
      - 不拋出；缺必填 env 時由 ``_live_env`` 以 ``pytest.skip`` 中止。
    """
    return _live_env()


@pytest.fixture
def live_driver(live_env: Dict[str, str]) -> EcSshDriver:
    """pytest fixture：以 live env 建構未連線的 ``EcSshDriver``。

    INPUT：
      - live_env：``live_env`` fixture 提供的連線 env 映射。

    OUTPUT：無。

    RETURN：
      - 以 live env 設定、尚未 connect 的 ``EcSshDriver`` 實例（由各測試自行
        以 context manager 管理連線生命週期）。

    例外：
      - 不拋出（純建構；連線於測試內以 ``with`` 建立）。
    """
    secret: Optional[str] = live_env["secret"] or None
    return EcSshDriver(
        host=live_env["host"],
        username=live_env["username"],
        password=live_env["password"],
        device_type=live_env["device_type"],
        secret=secret,
        interface_template=live_env["interface_template"],
    )


def _members_for_interface(state: VlanState, interface: str) -> set:
    """由 ``VlanState`` 取出指定 interface 的 (vlan, tagged) 集合（測試輔助）。

    INPUT：
      - state：回讀的 ``VlanState``。
      - interface：要篩選的設備 interface 名（str）。

    OUTPUT：無。

    RETURN：
      - ``{(vlan, tagged)}`` 集合，僅含該 interface 的成員。

    例外：不拋出。
    """
    return {
        (member.vlan, member.tagged)
        for member in state.members
        if member.interface == interface
    }


# 刪除保留測試 VLAN 定義的指令序列（實機 ECS5550-54X：於 'vlan database' 內
# 'no vlan <id>'；live 實測確認，'no vlan <id>' 於全域設定模式回
# '% Invalid input detected'）。apply_vlan 會讓設備自動建立該 VLAN，clear_vlan
# 僅移除 port 成員/模式而不刪 VLAN 定義，故 live 測試於 finally 另行刪除，確保
# 測後設備完全無 VLAN 3999 殘留（§11.2、完成驗證標準）。
_DELETE_RESERVED_VLAN_COMMANDS = [
    "vlan database",
    "no vlan {0}".format(_RESERVED_TEST_VLAN),
]


def _delete_reserved_vlan(driver: EcSshDriver) -> None:
    """刪除保留測試 VLAN 3999 的定義（best-effort 清理；絕不拋出，測試輔助）。

    供 live 測試 finally 區塊呼叫：即使刪除失敗（韌體差異）也不掩蓋臨界區的
    原始斷言失敗。經 driver 既有的 ``_send_config`` 下發（與單元測試一致地使用
    driver 內部方法），不擴充對外 API。

    INPUT：
      - driver：已連線的 ``EcSshDriver``。

    OUTPUT：無（於設備上刪除 VLAN 定義）。

    RETURN：None。

    例外：不拋出（best-effort）。
    """
    try:
        driver._send_config(
            list(_DELETE_RESERVED_VLAN_COMMANDS), "刪除保留測試 VLAN"
        )
    except Exception:  # noqa: BLE001 — 清理 best-effort，不掩蓋臨界區例外。
        pass


def test_live_readonly_probe_and_capture(
    live_driver: EcSshDriver, tmp_path: Path
) -> None:
    """4a 唯讀探測：連線→show version→read_vlans，並擷取 show vlan 存檔。

    INPUT：
      - live_driver：以 live env 建構的 ``EcSshDriver`` fixture。
      - tmp_path：pytest 內建暫存目錄 fixture（擷取檔的預設落點）。

    OUTPUT：
      - 把真機 ``show vlan`` 原始輸出（含 §8.2 待證清單標頭）寫入
        ``TOPO_FANOUT_CAPTURE_DIR``（若設）或 ``tmp_path``，供 ec_ssh parser
        fixture 覆核。

    RETURN：None。

    例外：
      - 連線/解析類例外（DriverConnectionError/DriverParseError）會使測試 fail
        （唯讀，不涉及設備寫入或還原）。
    """
    with live_driver as driver:
        # 唯讀：show version 僅驗證能送指令、能取得非空回應。
        version_output = driver.read_show("show version")
        assert isinstance(version_output, str)

        # 唯讀：read_vlans 必須能解析成合法 VlanState。
        state = driver.read_vlans()
        assert isinstance(state, VlanState)
        assert state.fanout == live_driver._host

        # 擷取真機 show vlan 原始輸出供日後 parser fixture 覆核。
        raw_show_vlan = driver.read_show("show vlan")

    capture_dir_env = os.environ.get("TOPO_FANOUT_CAPTURE_DIR")
    capture_dir = Path(capture_dir_env) if capture_dir_env else tmp_path
    capture_dir.mkdir(parents=True, exist_ok=True)
    capture_path = capture_dir / "live_show_vlan.txt"
    capture_path.write_text(
        _CAPTURE_HEADER + "\n" + raw_show_vlan, encoding="utf-8"
    )

    # 擷取檔必須已落地且含真機輸出（非僅標頭）。
    assert capture_path.exists()


def test_live_apply_and_rollback_vlan_3999(
    live_driver: EcSshDriver,
) -> None:
    """4b 高風險：VLAN 3999 對 trunk port apply→read-back→clear→read-back 還原。

    需 ``TOPO_FANOUT_TRUNK_PORT`` 指定要操作的 trunk port 編號；缺此 env 時單獨
    skip（4a 唯讀仍可跑）。整段寫入/驗證/還原以 try/finally 包覆：即使 read-back
    斷言失敗，finally 仍強制 ``clear_vlan`` 還原，確保不在設備留痕（設計 §11.2）。

    INPUT：
      - live_driver：以 live env 建構的 ``EcSshDriver`` fixture。

    OUTPUT：無（測試結束時設備狀態還原為測試前）。

    RETURN：None。

    例外：
      - 連線/下發/回讀類例外或 read-back 不符會使測試 fail；finally 仍會嘗試
        清除還原。
    """
    trunk_port = os.environ.get("TOPO_FANOUT_TRUNK_PORT")
    if not trunk_port:
        pytest.skip(
            "缺少 TOPO_FANOUT_TRUNK_PORT（4b 寫入/復原需指定 trunk port），"
            "跳過 4b"
        )

    interface = live_driver._interface_template.format(port=trunk_port)
    change = PortVlanChange(
        fanout=live_driver._host,
        interface=interface,
        vlan=_RESERVED_TEST_VLAN,
        action="add",
        topology_node="live-test:{0}".format(trunk_port),
    )

    with live_driver as driver:
        applied = False
        try:
            # 寫入：對 trunk port 套用保留 VLAN 3999（tagged 成員）。
            driver.apply_vlan([change])
            applied = True

            # read-back：該 interface 必須以 (3999, tagged=True) 現身。
            after_apply = driver.read_vlans()
            assert (
                _RESERVED_TEST_VLAN,
                True,
            ) in _members_for_interface(after_apply, interface)
        finally:
            # 無論斷言成敗，強制清除還原（設計 §11.2）：先移除 port 成員，再刪
            # 自動建立的 VLAN 3999 定義，確保設備完全無殘留。
            if applied:
                driver.clear_vlan([change])
                _delete_reserved_vlan(driver)

        # read-back：確認 VLAN 3999 成員已自該 interface 還原消失。
        after_clear = driver.read_vlans()
        assert (
            _RESERVED_TEST_VLAN,
            True,
        ) not in _members_for_interface(after_clear, interface)


# --------------------------------------------------------------------
# Phase 4 QinQ / dot1q-tunnel live 測試（決策 4、finding #1，AC-6/13/14）。
# --------------------------------------------------------------------


def test_live_global_tunnel_setup_idempotent(
    live_driver: EcSshDriver,
) -> None:
    """全域 setup：setup_global_tunnel(apply=True) + read_tunnel_status 確認 Enabled。

    setup 冪等（真機保證）；連續下發不改結果。此測試只動全域總開關，不動任何
    port。

    INPUT：
      - live_driver：以 live env 建構的 ``EcSshDriver`` fixture。

    OUTPUT：無。

    RETURN：None。

    例外：連線/下發類例外會使測試 fail（全域 setup 無還原需求，冪等）。
    """
    with live_driver as driver:
        driver.setup_global_tunnel(apply=True)
        status = driver.read_tunnel_status()
        assert status is True


def test_live_capture_show_dot1q_tunnel(
    live_driver: EcSshDriver, tmp_path: Path
) -> None:
    """擷取真機 'show dot1q-tunnel' 原始輸出供 per-port 正則/fixture 覆核（finding #1）。

    於已 setup 的設備上擷取完整原始輸出（含全域 status 行與 per-port Mode 表），
    寫入 ``TOPO_FANOUT_CAPTURE_DIR``/``tmp_path``，供把 ec_ssh 的
    ``_SAMPLE_SHOW_DOT1Q_TUNNEL`` fixture 與 ``_TUNNEL_PORT_MODE_RE`` 覆核對齊
    真機。此測試唯讀，不寫入設備。

    INPUT：
      - live_driver：以 live env 建構的 ``EcSshDriver`` fixture。
      - tmp_path：pytest 內建暫存目錄（擷取檔預設落點）。

    OUTPUT：
      - 把真機 ``show dot1q-tunnel`` 原始輸出寫入擷取目錄。

    RETURN：None。

    例外：連線類例外會使測試 fail（唯讀）。
    """
    with live_driver as driver:
        raw_tunnel = driver.read_show("show dot1q-tunnel")

    capture_dir_env = os.environ.get("TOPO_FANOUT_CAPTURE_DIR")
    capture_dir = Path(capture_dir_env) if capture_dir_env else tmp_path
    capture_dir.mkdir(parents=True, exist_ok=True)
    capture_path = capture_dir / "live_show_dot1q_tunnel.txt"
    header = (
        "# 真機 'show dot1q-tunnel' 原始輸出擷取（Phase 4 QinQ，finding #1）。\n"
        "# 用途：覆核 ec_ssh 的 _SAMPLE_SHOW_DOT1Q_TUNNEL fixture 與\n"
        "#   _TUNNEL_STATUS_RE / _TUNNEL_PORT_MODE_RE；若真機 per-port Mode 行或\n"
        "#   status 行格式與現有正則不符，以本擷取字面覆寫 fixture/正則並更新\n"
        "#   單元測試（決策 4、finding #1）。\n"
    )
    capture_path.write_text(header + "\n" + raw_tunnel, encoding="utf-8")
    assert capture_path.exists()


def test_live_edge_port_native_vlan_and_tunnel_access(
    live_driver: EcSshDriver,
) -> None:
    """邊緣 port：native vlan 3999 + dot1q-tunnel access 寫入→read-back→還原（AC-13）。

    需 ``TOPO_FANOUT_EDGE_PORT``；缺此 env 時單獨 skip。整段寫入/驗證/還原以
    try/finally 包覆：finally 以 EDGE remove（no native vlan + mode normal）
    強制還原，確保不在設備留痕（決策 7、§11.2）。只動 VLAN 3999 與該邊緣 port。

    INPUT：
      - live_driver：以 live env 建構的 ``EcSshDriver`` fixture。

    OUTPUT：無（測試結束時設備狀態還原為測試前）。

    RETURN：None。

    例外：連線/下發/回讀類例外或 read-back 不符會使測試 fail；finally 仍會
      嘗試還原。
    """
    edge_port = os.environ.get("TOPO_FANOUT_EDGE_PORT")
    if not edge_port:
        pytest.skip(
            "缺少 TOPO_FANOUT_EDGE_PORT（邊緣 port 寫入/復原需指定），跳過"
        )

    interface = live_driver._interface_template.format(port=edge_port)
    edge_change = PortVlanChange(
        fanout=live_driver._host,
        interface=interface,
        vlan=_RESERVED_TEST_VLAN,
        action="add",
        topology_node="live-edge:{0}".format(edge_port),
        role=PortRole_E.EDGE,
    )

    with live_driver as driver:
        applied = False
        try:
            driver.apply_vlan([edge_change])
            applied = True

            # read-back：該 interface 的 dot1q-tunnel Mode 須為 Access。
            after_apply = driver.read_vlans()
            assert (interface, "Access") in after_apply.tunnel_modes
        finally:
            # EDGE remove（no native vlan + no dot1q-tunnel mode）還原 port，
            # 再刪自動建立的 VLAN 3999 定義，確保無殘留（決策 7、§11.2）。
            if applied:
                driver.clear_vlan([edge_change])
                _delete_reserved_vlan(driver)

        # read-back：確認 tunnel mode 已還原（不再是 Access）。
        after_clear = driver.read_vlans()
        assert (interface, "Access") not in after_clear.tunnel_modes


def test_live_uplink_port_tunnel_uplink(
    live_driver: EcSshDriver,
) -> None:
    """互聯 port：trunk tagged + dot1q-tunnel uplink 寫入→read-back Mode=Uplink→還原。

    需 ``TOPO_FANOUT_TRUNK_PORT``；缺此 env 時單獨 skip。try/finally 以 UPLINK
    remove（allowed vlan remove）+ mode normal 還原。只動 VLAN 3999 與該互聯
    port。

    INPUT：
      - live_driver：以 live env 建構的 ``EcSshDriver`` fixture。

    OUTPUT：無（測試結束時設備狀態還原）。

    RETURN：None。

    例外：連線/下發/回讀類例外或 read-back 不符會使測試 fail；finally 仍會
      嘗試還原。
    """
    trunk_port = os.environ.get("TOPO_FANOUT_TRUNK_PORT")
    if not trunk_port:
        pytest.skip(
            "缺少 TOPO_FANOUT_TRUNK_PORT（互聯 port tunnel uplink 需指定），跳過"
        )

    interface = live_driver._interface_template.format(port=trunk_port)
    uplink_change = PortVlanChange(
        fanout=live_driver._host,
        interface=interface,
        vlan=_RESERVED_TEST_VLAN,
        action="add",
        topology_node="live-uplink:{0}".format(trunk_port),
        role=PortRole_E.UPLINK,
    )

    with live_driver as driver:
        applied = False
        try:
            driver.apply_vlan([uplink_change])
            applied = True

            # read-back：該 interface 的 dot1q-tunnel Mode 須為 Uplink。
            after_apply = driver.read_vlans()
            assert (interface, "Uplink") in after_apply.tunnel_modes
        finally:
            # 無論斷言成敗，強制還原：UPLINK remove（allowed vlan remove 3999
            # + no dot1q-tunnel mode 還原 Normal），再刪自動建立的 VLAN 3999
            # 定義，確保不在設備留痕（決策 7、§11.2）。
            if applied:
                driver.clear_vlan([uplink_change])
                _delete_reserved_vlan(driver)

        # read-back：確認 tunnel mode 已還原（不再是 Uplink）、VLAN 3999 tagged
        # 成員已自該 interface 消失。
        after_clear = driver.read_vlans()
        assert (interface, "Uplink") not in after_clear.tunnel_modes
        assert (
            _RESERVED_TEST_VLAN,
            True,
        ) not in _members_for_interface(after_clear, interface)
