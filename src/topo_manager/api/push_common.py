"""topo_manager 設備下發 REST 的共用輔助（api/push_common.py）。

MODULE NAME：
  topo_manager.api.push_common

PURPOSE：
  供打流側（push_routes）與互聯側（ic_push_routes）共用：

  - ``translate_device_errors``：解開 ``ApplyError.__cause__`` 鏈，讓連線失敗／
    逾時得到正確的 502／504，配置類缺陷還原為原例外（500）。
  - ``require_credentials_for``：連線前核對 session 路徑上的 fanout 都有 credentials。
  - ``load_config``：每請求載入 fanout 連線設定（不快取）。
  - ``default_driver_factory`` / ``default_ic_driver_factory``：預設 driver 工廠。
  - ``ports_by_fanout`` / ``interface_port``：port 編號解析輔助。
  - ``driver_factory_of``：取 app.state 上（測試注入或預設）的工廠。

NOTES：
  - ``drivers.*`` 一律在函式內延遲 import：未啟用下發時不會觸發 drivers 匯入，
    未安裝 netmiko 的環境也不受影響（netmiko 本身更晚才於 connect() 內 import）。
  - 工廠邏輯複製自 ``cli.py`` 與 ``interconnect/cli.py`` 的薄 glue，不 import cli。

HISTORY：
  - 0.1.0  設備下發 REST 初版。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Callable, Container, Dict, Iterator, List, Optional

from fastapi import Request

from topo_manager.api.deps import Settings
from topo_manager.errors import (
    ApplyError,
    ConfigError,
    DeviceTimeoutError,
    DriverConnectionError,
    DriverDependencyError,
    PortMappingError,
)

# ApplyError.__cause__ 鏈最多往下追的層數：實測最大包裝深度為 2
# （Deployer.apply 一層 + InterconnectDeployer._with_hint 一層），再留 1 層餘裕。
_CAUSE_DEPTH = 3

# 介面名 'Eth1/7' 的 port 編號。
_INTERFACE_PORT_RE = re.compile(r"(\d+)$")


@contextmanager
def translate_device_errors() -> Iterator[None]:
    """把 deployer 包裝過的例外還原成可正確映射 HTTP 狀態碼的型別。

    - cause 鏈上有 ``DeviceTimeoutError`` → 以其訊息重丟 ``DeviceTimeoutError``（504）。
    - 有 ``DriverConnectionError`` → 重丟 ``DriverConnectionError``（502）。
    - 有 ``DriverDependencyError`` / ``ConfigError`` / ``PortMappingError`` →
      還原為原例外（落 500，屬服務配置或環境缺陷）。
    - 其他 ``ApplyError``（如設備拒絕指令）→ 原樣重拋（502）。

    例外：
      - DeviceTimeoutError / DriverConnectionError / DriverDependencyError /
        ConfigError / PortMappingError / ApplyError：依上述規則。
    """
    try:
        yield
    except ApplyError as exc:
        cause = exc.__cause__
        for _ in range(_CAUSE_DEPTH):
            if cause is None:
                break
            if isinstance(cause, DeviceTimeoutError):
                raise DeviceTimeoutError(str(exc)) from exc
            if isinstance(cause, DriverConnectionError):
                raise DriverConnectionError(str(exc)) from exc
            if isinstance(cause, (DriverDependencyError, ConfigError, PortMappingError)):
                raise cause
            cause = getattr(cause, "__cause__", None)
        raise


def load_config(settings: Settings) -> object:
    """每請求載入 fanout 連線設定（不快取；憑證檔輪替不需重啟）。

    參數：
      - settings：API 設定（取 ``credentials_path``；None 走 CLI 預設）。

    回傳：
      - ``FanoutConfig``。

    例外：
      - ConfigError：設定檔缺失或不合法（REST 對應 500）。
    """
    from topo_manager.drivers.config import load_fanout_config

    return load_fanout_config(path=settings.credentials_path)


def require_credentials_for(
    session: object, config: object, known_fanouts: Container[str]
) -> None:
    """連線前核對：session 路徑上的每台 fanout 都有 credentials。

    ``planner.plan_changes`` 對缺 credentials 的 fanout 會靜默略過，自動化呼叫端
    會誤以為已下發，故 REST 在任何連線之前拒絕。

    參數：
      - session：打流 session。
      - config：``FanoutConfig``（具 ``fanouts``）。
      - known_fanouts：拓樸中的 fanout 名集合。

    例外：
      - ConfigError：有 fanout 缺 credentials（訊息列出缺漏名稱）。
    """
    from topo_manager.drivers.planner import required_fanouts

    missing = [
        name
        for name in required_fanouts(session, known_fanouts)
        if name not in config.fanouts
    ]
    if missing:
        raise ConfigError(
            "session {0} 需要 fanout [{1}]，但 credentials 中沒有".format(
                getattr(session, "session_id", "?"), ", ".join(missing)
            )
        )


def default_driver_factory(name: str, entry: object) -> object:
    """以 ``FanoutEntry`` 建立打流側 ``EcSshDriver``（延遲 import，不連線）。"""
    from topo_manager.drivers.ec_ssh import EcSshDriver

    return EcSshDriver(
        host=entry.host,
        username=entry.username,
        password=entry.password,
        device_type=entry.device_type,
        interface_template=entry.interface_template,
    )


def default_ic_driver_factory(name: str, entry: object) -> object:
    """以 ``FanoutEntry`` 建立互聯側 driver（``force_access_mode=True``，延遲 import）。"""
    from topo_manager.drivers.ec_ssh_ic import EcSshInterconnectDriver

    return EcSshInterconnectDriver(
        host=entry.host,
        username=entry.username,
        password=entry.password,
        device_type=entry.device_type,
        interface_template=entry.interface_template,
        force_access_mode=True,
    )


def driver_factory_of(request: Request, interconnect: bool) -> Callable[[str, object], object]:
    """取 driver 工廠：``create_app`` 注入者優先，否則用預設工廠。

    參數：
      - request：進來的請求（取 ``app.state``）。
      - interconnect：True 取互聯側，False 取打流側。
    """
    attr = "ic_push_driver_factory" if interconnect else "push_driver_factory"
    injected = getattr(request.app.state, attr, None)
    if injected is not None:
        return injected
    return default_ic_driver_factory if interconnect else default_driver_factory


def interface_port(interface: str) -> Optional[int]:
    """由介面名（如 ``Eth1/7``）取尾端 port 編號；取不到回 None。"""
    match = _INTERFACE_PORT_RE.search(interface)
    return int(match.group(1)) if match is not None else None


def ports_by_fanout(session: object) -> Dict[str, List[int]]:
    """由互聯 session 的 links 彙整 ``{fanout: [port...]}``（節點為 ``fanout:port``）。"""
    grouped: Dict[str, set] = {}
    for link in session.links:
        for node in (link.a_node, link.b_node):
            fanout, _, port = node.rpartition(":")
            grouped.setdefault(fanout, set()).add(int(port))
    return {fanout: sorted(numbers) for fanout, numbers in sorted(grouped.items())}
