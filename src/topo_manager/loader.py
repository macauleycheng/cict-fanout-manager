"""topo_manager YAML Loader（設計 §4、FR-2）。

本模組提供唯一公開函式 ``load_topology``，負責把拓樸 YAML 讀入、以 PyYAML
解析、交給 Pydantic 做 schema 驗證，最後做跨模型的「參照完整性檢查」。四個
階段各自把底層失敗轉成對應的 ``TopologyError`` 子類，訊息清楚且可定位
（FR-2.2）：

  1. 讀檔失敗 → ``TopologyFileError``（含路徑）。
  2. YAML 解析失敗／空檔／頂層非 mapping → ``TopologyParseError``。
  3. Pydantic schema 驗證失敗 → ``TopologyValidationError``（逐條欄位路徑）。
  4. 參照完整性失敗 → ``TopologyReferenceError``（含 link 序號與名稱/範圍）。

設計分層原則（§10）：
  - 「同一裝置內 port 不重複、port 正規化」屬模型層（已在 models.py）。
  - 「裝置名全域唯一（含 DUT 跨 rack）、rack 名不撞裝置名、link 參照存在、
    fanout port 數字/範圍」屬本 loader 層——需要跨模型的全域視野。

歷史（HISTORY）：
  - 0.1.0  FEAT-002 初版，建立 load_topology 與 _check_referential_integrity
           （設計 §4.1~§4.3、FR-2、FR-4 載入面、FR-6.1）。

Copyright (c) 2026 topo-manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Set, Union

import yaml
from pydantic import ValidationError

from topo_manager.errors import (
    TopologyFileError,
    TopologyParseError,
    TopologyReferenceError,
    TopologyValidationError,
)
from topo_manager.models import Topology


def load_topology(path: Union[str, Path]) -> Topology:
    """讀取並驗證拓樸 YAML，回傳已驗證的 ``Topology`` 模型（設計 §4.1）。

    參數：
      - path：拓樸 YAML 檔路徑（str 或 Path）。

    回傳：
      - 通過 schema 驗證與參照完整性檢查的 ``Topology`` 實例。

    例外：
      - TopologyFileError：檔案不存在或不可讀（訊息含路徑）。
      - TopologyParseError：YAML 語法錯誤、空檔、或頂層非 mapping。
      - TopologyValidationError：Pydantic schema 驗證失敗（逐條欄位路徑）。
      - TopologyReferenceError：參照完整性檢查失敗。
    """
    file_path = Path(path)

    # 階段 1：讀檔。缺檔/不可讀一律轉 TopologyFileError（含路徑）。
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TopologyFileError(
            "找不到拓樸檔案：{0}".format(file_path)
        ) from exc
    except OSError as exc:
        raise TopologyFileError(
            "無法讀取拓樸檔案：{0}（{1}）".format(file_path, exc)
        ) from exc

    # 階段 2：YAML 解析。語法錯誤附 PyYAML 的 line/column 問題描述；空檔或
    # 頂層非 dict 皆視為無效拓樸。
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise TopologyParseError(
            "YAML 解析失敗（{0}）：{1}".format(file_path, _format_yaml_error(exc))
        ) from exc

    if data is None:
        raise TopologyParseError(
            "拓樸檔為空（{0}）：頂層須為 mapping".format(file_path)
        )
    if not isinstance(data, dict):
        raise TopologyParseError(
            "拓樸檔頂層須為 mapping（{0}）：實得 {1}".format(
                file_path, type(data).__name__
            )
        )

    # 階段 3：Pydantic schema 驗證。把 ValidationError 逐條轉成人類可讀字串。
    try:
        topology = Topology.model_validate(data)
    except ValidationError as exc:
        raise TopologyValidationError(
            _format_validation_error(file_path, exc)
        ) from exc

    # 階段 4：參照完整性檢查（跨模型的全域一致性）。
    _check_referential_integrity(topology)

    return topology


def _format_yaml_error(exc: yaml.YAMLError) -> str:
    """把 PyYAML 例外整理成單行描述，盡量附上 line/column（設計 §4.1）。

    PyYAML 的 ``MarkedYAMLError`` 具有 ``problem`` 與 ``problem_mark``，可取得
    問題描述與位置；若無這些屬性則退回 ``str(exc)``。
    """
    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    if problem is not None and mark is not None:
        # mark.line/column 為 0-based，對使用者以 1-based 呈現較直覺。
        return "{0}（line {1}, column {2}）".format(
            problem, mark.line + 1, mark.column + 1
        )
    if problem is not None:
        return str(problem)
    return str(exc)


def _format_validation_error(file_path: Path, exc: ValidationError) -> str:
    """把 Pydantic ``ValidationError`` 逐條轉為人類可讀多行字串（設計 §4.2）。

    每筆 error 的 ``loc`` 以 '.' 串接、list index 以 '[i]' 呈現，接 ``msg``。
    範例：
        拓樸驗證失敗（topology.yaml）：
          - fanouts[1].role：Field required
          - generators[0].ports：List should have at least 1 item ...
    """
    lines = ["拓樸驗證失敗（{0}）：".format(file_path)]
    for error in exc.errors():
        loc_str = _format_loc(error.get("loc", ()))
        msg = error.get("msg", "")
        lines.append("  - {0}：{1}".format(loc_str, msg))
    return "\n".join(lines)


def _format_loc(loc) -> str:
    """把 Pydantic error 的 ``loc`` tuple 串成可定位字串（設計 §4.2）。

    規則：int（list index）以 '[i]' 附於前一段之後；str（欄位名）以 '.' 串接。
    例如 ``('racks', 0, 'duts', 2, 'tg_ports', 1)`` → ``racks[0].duts[2].tg_ports[1]``。
    """
    parts: List[str] = []
    for item in loc:
        if isinstance(item, int):
            if len(parts) > 0:
                parts[-1] = "{0}[{1}]".format(parts[-1], item)
            else:
                parts.append("[{0}]".format(item))
        else:
            parts.append(str(item))
    return ".".join(parts)


def _check_referential_integrity(topology: Topology) -> None:
    """跨模型參照完整性檢查（設計 §4.3）。

    依序檢查：
      (1) 裝置名全域唯一（generator ∪ fanout ∪ 所有 rack 的 dut），含 DUT
          跨 rack 重名（N4）；rack 名另不得與任一裝置名衝突。
      (2) 建立每台裝置的合法 port 集合（generator=ports、dut=tg_ports∪il_ports、
          fanout=1..port_count）。
      (3) 逐條 link 檢查 a、b 兩端的 dev 是否存在、port 是否合法；fanout 端 port
          以 isascii() and isdigit() 解析後檢查 1..port_count（N5）。

    任一不通過即丟 ``TopologyReferenceError``，訊息具體可定位。
    """
    # ---- (1) 裝置名全域唯一；掃描累積，遇重名即報錯（涵蓋 DUT 跨 rack，N4）----
    dev_names: Set[str] = set()

    for generator in topology.generators:
        if generator.name in dev_names:
            raise TopologyReferenceError(
                "裝置名重複：{0}".format(generator.name)
            )
        dev_names.add(generator.name)

    for fanout in topology.fanouts:
        if fanout.name in dev_names:
            raise TopologyReferenceError(
                "裝置名重複：{0}".format(fanout.name)
            )
        dev_names.add(fanout.name)

    for rack in topology.racks:
        for dut in rack.duts:
            if dut.name in dev_names:
                raise TopologyReferenceError(
                    "裝置名重複：{0}".format(dut.name)
                )
            dev_names.add(dut.name)

    # rack 名不納入 dev_names，但不得與任一裝置名衝突（獨立命名空間規則）。
    for rack in topology.racks:
        if rack.name in dev_names:
            raise TopologyReferenceError(
                "rack 名 '{0}' 與裝置名衝突".format(rack.name)
            )

    # ---- (2) 建立每台裝置的合法 port 集合 ----
    # generator / dut 端以字串原值比對；fanout 端於第 (3) 步以數字解析處理，
    # 故此處僅記錄其 port_count。
    gen_dut_ports: Dict[str, Set[str]] = {}
    fanout_port_count: Dict[str, int] = {}

    for generator in topology.generators:
        gen_dut_ports[generator.name] = set(generator.ports)

    for rack in topology.racks:
        for dut in rack.duts:
            # il_ports 納入合法集合（避免合法互聯線被誤判），但不入路徑圖（§4.3）。
            gen_dut_ports[dut.name] = set(dut.tg_ports) | set(dut.il_ports)

    for fanout in topology.fanouts:
        fanout_port_count[fanout.name] = fanout.port_count

    # ---- (3) 逐條 link 檢查兩端 ----
    for index, link in enumerate(topology.links, start=1):
        _check_endpoint(index, link.a, dev_names, gen_dut_ports, fanout_port_count)
        _check_endpoint(index, link.b, dev_names, gen_dut_ports, fanout_port_count)


def _check_endpoint(
    link_index: int,
    endpoint,
    dev_names: Set[str],
    gen_dut_ports: Dict[str, Set[str]],
    fanout_port_count: Dict[str, int],
) -> None:
    """檢查單一 link 端點的 dev 與 port 合法性（設計 §4.3.3、§4.3.4）。

    參數：
      - link_index：link 的 1-based 序號，供報錯定位。
      - endpoint：待檢查的 Endpoint（具 dev、port 屬性）。
      - dev_names：全部合法裝置名集合。
      - gen_dut_ports：generator/dut 的合法 port 字串集合。
      - fanout_port_count：fanout 名 → port_count。

    失敗一律丟 ``TopologyReferenceError``（訊息含 link 序號與裝置名/值/範圍）。
    """
    dev = endpoint.dev
    port = endpoint.port

    # dev 必須存在於命名空間。
    if dev not in dev_names:
        raise TopologyReferenceError(
            "link 第 {0} 條：裝置 '{1}' 不存在".format(link_index, dev)
        )

    # fanout 端：以純 ASCII 數字解析後檢查範圍 1..port_count（N5）。
    if dev in fanout_port_count:
        port_count = fanout_port_count[dev]
        if not (port.isascii() and port.isdigit()):
            raise TopologyReferenceError(
                "link 第 {0} 條：fanout '{1}' 的 port '{2}' 非合法 port 編號"
                "（需為 1..{3} 的整數）".format(link_index, dev, port, port_count)
            )
        port_number = int(port)
        if not (1 <= port_number <= port_count):
            raise TopologyReferenceError(
                "link 第 {0} 條：{1} 無 port '{2}'（有效範圍 1..{3}）".format(
                    link_index, dev, port, port_count
                )
            )
        return

    # generator / dut 端：以字串原值比對其宣告集合。
    legal_ports = gen_dut_ports.get(dev, set())
    if port not in legal_ports:
        raise TopologyReferenceError(
            "link 第 {0} 條：裝置 '{1}' 無 port '{2}'".format(
                link_index, dev, port
            )
        )
