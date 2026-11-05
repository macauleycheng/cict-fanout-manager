/*
 * CICT Fanout Manager Web UI 互聯（interconnect）頁籤行為（design §3、§4）。
 *
 * 互聯頁籤的全部前端行為：呼叫既有互聯帳本層 REST（summary / available /
 * sessions GET+POST / sessions DELETE / plan），列出互聯 session 並支援文字
 * filter、選中某 session 時在「每組一張小圖」的互聯拓樸區畫出該 session 的每
 * 條 link（標 VLAN 與兩端 il_port），以及建立表單（profile 或 pairs 二擇一）
 * 與釋放按鈕。友善錯誤以 banner 呈現；無互聯區段（404）時整頁籤顯示中性提示。
 *
 * 不呼叫任何會連設備的端點（apply / verify / park / release-device 一律不出
 * 現），不處理 X-API-Token。零 build、純 vanilla ES5-相容 JS + inline SVG
 * （document.createElementNS），服出去的 /static/ic.js。使用者面向文字為英文。
 *
 * 與 app.js 的唯一共享是頁首與頁籤列；互聯邏輯自帶獨立的 icState 與 refresh，
 * 與打流邏輯互不污染（design §2 方案 B）。
 */

"use strict";

var IC_SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------ *
 * Node-id helpers（沿用 app.js 的正確寫法，防 split 陷阱，design §4.1）。
 *
 * node id 格式為 "<device>:<port>"，device 段不含 ':'、port 段可含 '/' 不含
 * ':'。JS 的 String.prototype.split 第二引數是「回傳陣列上限」而非 Python 的
 * maxsplit，limit 1 會把 port 段整個丟掉；devOf/portOf 以 indexOf + slice 避
 * 開此陷阱。互聯 link 的端點主要用已分好的 a_dut/a_port 欄位，a_node/b_node
 * （fanout 節點）若需拆解一律經此 helper。
 * ------------------------------------------------------------------ */

function icDevOf(nodeId) {
    var idx = nodeId.indexOf(":");
    if (idx === -1) {
        return nodeId;
    }
    return nodeId.slice(0, idx);
}

function icPortOf(nodeId) {
    return nodeId.slice(nodeId.indexOf(":") + 1);
}

/* ------------------------------------------------------------------ *
 * 互聯用戶端狀態（design §4.2）。
 * ------------------------------------------------------------------ */

var icState = {
    summary: null,        /* GET /interconnect/summary */
    sessions: [],         /* GET /interconnect/sessions 的 .sessions */
    available: null,      /* GET /interconnect/resources/available */
    selectedId: null,     /* 選中的互聯 session_id，或 null */
    notConfigured: false, /* 404 無互聯區段 → 中性提示 */
    loaded: false         /* 是否已做過首次載入（lazy load） */
};

/* ------------------------------------------------------------------ *
 * fetch 與錯誤映射（design §4.3，式樣對齊 app.js 的 apiFetch）。
 * ------------------------------------------------------------------ */

function IcApiError(type, message, status) {
    this.type = type;
    this.message = message;
    this.status = status;
}

function icApiFetch(method, url, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined && body !== null) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(
        function (response) {
            return response.text().then(function (raw) {
                var parsed = null;
                if (raw) {
                    try {
                        parsed = JSON.parse(raw);
                    } catch (e) {
                        parsed = null;
                    }
                }
                if (response.ok) {
                    return parsed;
                }
                /* 結構化錯誤：{"error": {"type", "message"}}。 */
                if (parsed && parsed.error && parsed.error.type) {
                    throw new IcApiError(
                        parsed.error.type,
                        parsed.error.message || "",
                        response.status
                    );
                }
                /* 非 JSON（如 starlette 的 plaintext 404/500）。 */
                throw new IcApiError(
                    "Unknown",
                    response.status + " " + response.statusText,
                    response.status
                );
            });
        },
        function () {
            /* 網路失敗（fetch reject）。 */
            throw new IcApiError("Network", "Cannot reach server", 0);
        }
    );
}

/* 把後端錯誤 type 映射為英文友善文案（design §4.3）。err.type 為後端
 * exc.__class__.__name__ 的精確類別名；列舉已驗證的 type 並保留 default。 */
function icFriendlyMessage(err) {
    switch (err.type) {
        case "InterconnectNotConfiguredError":
            icState.notConfigured = true;
            return "This topology has no interconnect section.";
        case "InterconnectSessionNotFoundError":
            return "Session already gone; refreshing.";
        case "InterconnectConflictError":
            return "Those interconnect ports are already in use; "
                + "release a session and retry.";
        case "VlanPoolExhaustedError":
            return "No free interconnect VLAN left in the pool.";
        case "InterconnectRequestError":
        case "ValidationError":
            return "Invalid request: check the selected DUTs/profile or "
                + "port pairs.";
        default:
            return "Server error; please retry.";
    }
}

/* ------------------------------------------------------------------ *
 * 純函式（供 Python-mirror 測試，design §4.7、§7）。
 * ------------------------------------------------------------------ */

/* icSessionMatchesFilter(session, needle)：把 session 的可過濾欄位串接小寫後
 * 做 indexOf 比對（session_id / group / profile / 每條 link 的 vlan /
 * a_dut / a_port / b_dut / b_port）。needle 為空字串時一律命中。純函式、無
 * DOM。 */
function icSessionMatchesFilter(session, needle) {
    if (!needle) {
        return true;
    }
    var parts = [
        session.session_id || "",
        session.group || "",
        session.profile || ""
    ];
    var links = session.links || [];
    for (var i = 0; i < links.length; i += 1) {
        var link = links[i];
        parts.push(String(link.vlan));
        parts.push(link.a_dut || "");
        parts.push(link.a_port || "");
        parts.push(link.b_dut || "");
        parts.push(link.b_port || "");
    }
    return parts.join(" ").toLowerCase().indexOf(needle) !== -1;
}

/* 在 summary.groups 中找出含某 DUT 名的組；找不到回 null。 */
function icFindGroupForDut(summary, dutName) {
    if (!summary || !summary.groups) {
        return null;
    }
    for (var i = 0; i < summary.groups.length; i += 1) {
        var group = summary.groups[i];
        var duts = group.duts || [];
        for (var j = 0; j < duts.length; j += 1) {
            if (duts[j].name === dutName) {
                return group;
            }
        }
    }
    return null;
}

/* 在一組的 duts[] 中以 DUT 名查 label（d0..d3）；找不到回 null。 */
function icLabelForDut(group, dutName) {
    var duts = group.duts || [];
    for (var i = 0; i < duts.length; i += 1) {
        if (duts[i].name === dutName) {
            return duts[i].label;
        }
    }
    return null;
}

/* buildSessionDrawLinks(session, summary)：把一筆 session 的 links[] 整理成
 * 要畫的連線集合。回傳
 *   [{groupName, aLabel, bLabel, aDut, aPort, bDut, bPort,
 *     aFanout, bFanout, vlan}, ...]
 *
 * aFanout / bFanout 為兩端 fanout port，取自 link 的 a_node / b_node（形如
 * "ic-1-1:1"）冒號後那段，一律經 icPortOf 拆解（防 split limit 陷阱，§4.1）。
 *
 * not-found 契約（design §4.4/§4.7、review Finding 4）：
 *   - 先以 session 第一條 link 的 a_dut（退而用 session.group）在 summary 定
 *     位所屬組；比不到任何組即回傳 []（不丟例外、該 session 不畫線）。
 *   - 組內找到後，對每條 link 以 a_dut/b_dut 名字查該組 label；任一端查不到
 *     label 時跳過該條 link（其餘可解析的 link 照常輸出）。
 *
 * 純函式、無 DOM、不依賴 fetch。 */
function buildSessionDrawLinks(session, summary) {
    var out = [];
    if (!session || !summary) {
        return out;
    }
    var links = session.links || [];
    /* 以第一條 link 的 a_dut 定位組；無 link 時退用 session.group。 */
    var group = null;
    if (links.length > 0) {
        group = icFindGroupForDut(summary, links[0].a_dut);
    }
    if (group === null && session.group) {
        var groups = summary.groups || [];
        for (var g = 0; g < groups.length; g += 1) {
            if (groups[g].name === session.group) {
                group = groups[g];
                break;
            }
        }
    }
    if (group === null) {
        return out;
    }
    for (var i = 0; i < links.length; i += 1) {
        var link = links[i];
        var aLabel = icLabelForDut(group, link.a_dut);
        var bLabel = icLabelForDut(group, link.b_dut);
        if (aLabel === null || bLabel === null) {
            continue;
        }
        out.push({
            groupName: group.name,
            aLabel: aLabel,
            bLabel: bLabel,
            aDut: link.a_dut,
            aPort: link.a_port,
            bDut: link.b_dut,
            bPort: link.b_port,
            /* fanout port：a_node / b_node 冒號後那段（經 icPortOf）。 */
            aFanout: icPortOf(link.a_node || ""),
            bFanout: icPortOf(link.b_node || ""),
            vlan: link.vlan
        });
    }
    return out;
}

/* icLinkLabelText(drawLink)：把一條 buildSessionDrawLinks 的連線組成拓樸圖上的
 * 標籤文字（兩行）。回傳 { top, bottom }：
 *   - top：VLAN（如 "VLAN 1000"）。
 *   - bottom：兩端各自以 "<il_port> @ <fanout_port>" 表示、中間以雙箭頭相連
 *     （如 "Eth8 @ 1 \u2194 Eth8 @ 2"），讓每端的 DUT il_port 與其對應的
 *     fanout port 清楚成對，避免一行文字過長而擁擠。
 *
 * 純函式、無 DOM；供 renderIcGroup 與 Python-mirror 測試共用。 */
function icLinkLabelText(drawLink) {
    return {
        top: "VLAN " + drawLink.vlan,
        bottom: drawLink.aPort + " @ " + drawLink.aFanout
            + " \u2194 " + drawLink.bPort + " @ " + drawLink.bFanout
    };
}

/* ------------------------------------------------------------------ *
 * SVG 輔助。
 * ------------------------------------------------------------------ */

function icSvgEl(name, attrs) {
    var el = document.createElementNS(IC_SVG_NS, name);
    if (attrs) {
        var k;
        for (k in attrs) {
            if (attrs.hasOwnProperty(k)) {
                el.setAttribute(k, attrs[k]);
            }
        }
    }
    return el;
}

/* 方形四角座標（d0 左上、d1 右上、d2 右下、d3 左下）。 */
var IC_LAYOUT = {
    width: 320,
    height: 220,
    marginX: 60,
    marginY: 50,
    boxWidth: 100,
    boxHeight: 44
};

function icCornerFor(label) {
    var innerW = IC_LAYOUT.width - IC_LAYOUT.marginX * 2 - IC_LAYOUT.boxWidth;
    var innerH = IC_LAYOUT.height - IC_LAYOUT.marginY * 2 - IC_LAYOUT.boxHeight;
    switch (label) {
        case "d0":
            return { x: IC_LAYOUT.marginX, y: IC_LAYOUT.marginY };
        case "d1":
            return { x: IC_LAYOUT.marginX + innerW, y: IC_LAYOUT.marginY };
        case "d2":
            return {
                x: IC_LAYOUT.marginX + innerW,
                y: IC_LAYOUT.marginY + innerH
            };
        case "d3":
            return { x: IC_LAYOUT.marginX, y: IC_LAYOUT.marginY + innerH };
        default:
            return null;
    }
}

function icCenterOf(corner) {
    return {
        x: corner.x + IC_LAYOUT.boxWidth / 2,
        y: corner.y + IC_LAYOUT.boxHeight / 2
    };
}

/* ------------------------------------------------------------------ *
 * 互聯拓樸渲染（每組一張小圖，design §4.4/§4.6）。
 * ------------------------------------------------------------------ */

function currentIcSelection() {
    if (!icState.selectedId) {
        return null;
    }
    for (var i = 0; i < icState.sessions.length; i += 1) {
        if (icState.sessions[i].session_id === icState.selectedId) {
            return icState.sessions[i];
        }
    }
    return null;
}

function renderIcTopology() {
    var container = document.getElementById("ic-topology");
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (icState.notConfigured) {
        var note = document.createElement("p");
        note.className = "empty";
        note.textContent = "This topology has no interconnect section.";
        container.appendChild(note);
        return;
    }
    if (!icState.summary || !icState.summary.groups
            || icState.summary.groups.length === 0) {
        var none = document.createElement("p");
        none.className = "empty";
        none.textContent = "No interconnect groups.";
        container.appendChild(none);
        return;
    }

    var selected = currentIcSelection();
    var drawLinks = selected
        ? buildSessionDrawLinks(selected, icState.summary)
        : [];
    /* 依組名歸類要畫的連線。 */
    var linksByGroup = {};
    for (var d = 0; d < drawLinks.length; d += 1) {
        var dl = drawLinks[d];
        if (!linksByGroup[dl.groupName]) {
            linksByGroup[dl.groupName] = [];
        }
        linksByGroup[dl.groupName].push(dl);
    }

    var groups = icState.summary.groups;
    for (var gi = 0; gi < groups.length; gi += 1) {
        container.appendChild(
            renderIcGroup(groups[gi], linksByGroup[groups[gi].name] || [])
        );
    }
}

function renderIcGroup(group, groupLinks) {
    var wrap = document.createElement("div");
    wrap.className = "ic-group";

    var title = document.createElement("h3");
    title.textContent = group.name + " \u00b7 fanout " + group.fanout
        + " \u00b7 rack " + group.rack;
    wrap.appendChild(title);

    var svg = icSvgEl("svg", {
        "class": "ic-group-svg",
        viewBox: "0 0 " + IC_LAYOUT.width + " " + IC_LAYOUT.height,
        xmlns: IC_SVG_NS
    });

    var edgeGroup = icSvgEl("g", { "class": "ic-edges" });
    var nodeGroup = icSvgEl("g", { "class": "ic-nodes" });
    svg.appendChild(edgeGroup);
    svg.appendChild(nodeGroup);

    /* 連線先畫，節點後畫（節點蓋在線上）。 */
    for (var i = 0; i < groupLinks.length; i += 1) {
        var dl = groupLinks[i];
        var cornerA = icCornerFor(dl.aLabel);
        var cornerB = icCornerFor(dl.bLabel);
        if (!cornerA || !cornerB) {
            continue;
        }
        var ca = icCenterOf(cornerA);
        var cb = icCenterOf(cornerB);
        edgeGroup.appendChild(icSvgEl("line", {
            "class": "ic-edge active",
            x1: ca.x, y1: ca.y, x2: cb.x, y2: cb.y
        }));
        /* 兩行標籤：第一行 VLAN、第二行兩端 "il_port @ fanout"（含 fanout
         * port），以 <tspan> 分行避免單行過長而擁擠（語意審查觀察第 1 項）。 */
        var labelXy = {
            x: (ca.x + cb.x) / 2,
            y: (ca.y + cb.y) / 2 - 4
        };
        var labelText = icLinkLabelText(dl);
        var label = icSvgEl("text", {
            "class": "ic-edge-label",
            x: labelXy.x,
            y: labelXy.y,
            "text-anchor": "middle"
        });
        var topSpan = icSvgEl("tspan", { x: labelXy.x, dy: "0" });
        topSpan.textContent = labelText.top;
        var bottomSpan = icSvgEl("tspan", { x: labelXy.x, dy: "10" });
        bottomSpan.textContent = labelText.bottom;
        label.appendChild(topSpan);
        label.appendChild(bottomSpan);
        edgeGroup.appendChild(label);
    }

    /* 節點（方形四角）。 */
    var duts = group.duts || [];
    for (var j = 0; j < duts.length; j += 1) {
        var dut = duts[j];
        var corner = icCornerFor(dut.label);
        if (!corner) {
            continue;
        }
        var g = icSvgEl("g", { "class": "ic-node" });
        g.appendChild(icSvgEl("rect", {
            x: corner.x, y: corner.y,
            width: IC_LAYOUT.boxWidth, height: IC_LAYOUT.boxHeight,
            rx: 8, ry: 8
        }));
        var nameText = icSvgEl("text", {
            "class": "ic-node-name",
            x: corner.x + IC_LAYOUT.boxWidth / 2,
            y: corner.y + 19,
            "text-anchor": "middle"
        });
        nameText.textContent = dut.name;
        g.appendChild(nameText);
        var labelText = icSvgEl("text", {
            "class": "ic-node-label",
            x: corner.x + IC_LAYOUT.boxWidth / 2,
            y: corner.y + 34,
            "text-anchor": "middle"
        });
        labelText.textContent = dut.label;
        g.appendChild(labelText);
        nodeGroup.appendChild(g);
    }

    wrap.appendChild(svg);
    return wrap;
}

/* ------------------------------------------------------------------ *
 * 互聯 session 清單 + filter（design §4.5）。
 * ------------------------------------------------------------------ */

function renderIcSessionList() {
    var ul = document.getElementById("ic-session-list");
    var needle = document.getElementById("ic-session-filter").value
        .trim().toLowerCase();
    while (ul.firstChild) {
        ul.removeChild(ul.firstChild);
    }

    if (icState.notConfigured) {
        var noneLi = document.createElement("li");
        noneLi.className = "empty";
        noneLi.textContent = "This topology has no interconnect section.";
        noneLi.style.cursor = "default";
        ul.appendChild(noneLi);
        return;
    }

    var shown = 0;
    for (var i = 0; i < icState.sessions.length; i += 1) {
        var s = icState.sessions[i];
        if (!icSessionMatchesFilter(s, needle)) {
            continue;
        }
        shown += 1;
        var li = document.createElement("li");
        if (s.session_id === icState.selectedId) {
            li.className = "selected";
        }
        var sid = document.createElement("span");
        sid.className = "sid";
        sid.textContent = s.session_id;
        var meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = s.group + " / " + s.profile + " / "
            + (s.links || []).length + " links";
        li.appendChild(sid);
        li.appendChild(meta);
        li.addEventListener("click", (function (id) {
            return function () { selectIcSession(id); };
        })(s.session_id));
        ul.appendChild(li);
    }

    if (shown === 0) {
        var empty = document.createElement("li");
        empty.className = "empty";
        empty.textContent = icState.sessions.length === 0
            ? "No interconnect sessions."
            : "No sessions match the filter.";
        empty.style.cursor = "default";
        ul.appendChild(empty);
    }
}

function selectIcSession(sessionId) {
    if (icState.selectedId === sessionId) {
        icState.selectedId = null;
    } else {
        icState.selectedId = sessionId;
    }
    renderIcSessionList();
    renderIcTopology();
    renderIcDetail();
}

/* ------------------------------------------------------------------ *
 * 互聯 detail 面板（design §4.5）。
 * ------------------------------------------------------------------ */

function renderIcDetail() {
    var el = document.getElementById("ic-detail-content");
    while (el.firstChild) {
        el.removeChild(el.firstChild);
    }
    var selected = currentIcSelection();
    if (!selected) {
        var empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent = "No interconnect session selected.";
        el.appendChild(empty);
        return;
    }

    var dl = document.createElement("dl");
    icAddDetailRow(dl, "Session", selected.session_id);
    icAddDetailRow(dl, "Group", selected.group);
    icAddDetailRow(dl, "Fanout", selected.fanout);
    icAddDetailRow(dl, "Profile", selected.profile);
    icAddDetailRow(dl, "DUTs", (selected.duts || []).join(", "));
    el.appendChild(dl);

    var links = selected.links || [];
    var ul = document.createElement("ul");
    ul.className = "ic-link-list";
    for (var i = 0; i < links.length; i += 1) {
        var link = links[i];
        var li = document.createElement("li");
        li.textContent = "VLAN " + link.vlan + ": "
            + link.a_dut + ":" + link.a_port
            + " \u2194 " + link.b_dut + ":" + link.b_port
            + " (" + link.a_node + " / " + link.b_node + ")";
        ul.appendChild(li);
    }
    el.appendChild(ul);

    var releaseBtn = document.createElement("button");
    releaseBtn.className = "danger";
    releaseBtn.type = "button";
    releaseBtn.textContent = "Release session";
    releaseBtn.style.marginTop = "10px";
    releaseBtn.addEventListener("click", (function (id) {
        return function () { releaseIcSession(id); };
    })(selected.session_id));
    el.appendChild(releaseBtn);
}

function icAddDetailRow(dl, label, value) {
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = value;
    dl.appendChild(dt);
    dl.appendChild(dd);
}

/* ------------------------------------------------------------------ *
 * 建立表單（design §4.6）。
 * ------------------------------------------------------------------ */

function icCurrentMode() {
    return document.getElementById("ic-mode-pairs").checked
        ? "pairs"
        : "profile";
}

function icSelectedGroupName() {
    return document.getElementById("ic-sel-group").value;
}

function icGroupByName(name) {
    if (!icState.available || !icState.available.groups) {
        return null;
    }
    for (var i = 0; i < icState.available.groups.length; i += 1) {
        if (icState.available.groups[i].name === name) {
            return icState.available.groups[i];
        }
    }
    return null;
}

/* 重建 group 下拉（來自 summary.groups[].name）。 */
function renderIcCreateControls() {
    var groupSel = document.getElementById("ic-sel-group");
    var prev = groupSel.value;
    icResetSelect(groupSel, "Select a group\u2026");
    if (icState.summary && icState.summary.groups) {
        for (var i = 0; i < icState.summary.groups.length; i += 1) {
            icAddOption(groupSel, icState.summary.groups[i].name,
                icState.summary.groups[i].name);
        }
    }
    if (icHasOption(groupSel, prev)) {
        groupSel.value = prev;
    }
    icRenderModeFields();
    icUpdateCreateEnabled();
}

/* 依當前模式與選中組重建 DUT 多選 / pairs 列（只列當前可建資源）。 */
function icRenderModeFields() {
    var mode = icCurrentMode();
    document.getElementById("ic-profile-fields").hidden = (mode !== "profile");
    document.getElementById("ic-pairs-fields").hidden = (mode !== "pairs");

    var groupName = icSelectedGroupName();
    var availGroup = icGroupByName(groupName);

    if (mode === "profile") {
        var dutsSel = document.getElementById("ic-sel-duts");
        while (dutsSel.firstChild) {
            dutsSel.removeChild(dutsSel.firstChild);
        }
        /* DUT 清單取自 summary 該組（順序即選定順序）。 */
        var summaryGroup = null;
        if (icState.summary && icState.summary.groups) {
            for (var i = 0; i < icState.summary.groups.length; i += 1) {
                if (icState.summary.groups[i].name === groupName) {
                    summaryGroup = icState.summary.groups[i];
                    break;
                }
            }
        }
        if (summaryGroup) {
            for (var j = 0; j < summaryGroup.duts.length; j += 1) {
                icAddOption(dutsSel, summaryGroup.duts[j].name,
                    summaryGroup.duts[j].label + " : " + summaryGroup.duts[j].name);
            }
        }
    } else {
        /* pairs 模式：重置為單一可增刪的 pair 列。 */
        var rows = document.getElementById("ic-pairs-rows");
        while (rows.firstChild) {
            rows.removeChild(rows.firstChild);
        }
        if (availGroup) {
            icAddPairRow(availGroup);
        }
    }
}

/* pairs 模式的選項：展開該組 available.buildable_links 的兩端為 <dut>:<il_port>。
 * 只列當前可建資源（design §4.6/FR-7）。 */
function icBuildablePairOptions(availGroup) {
    var options = [];
    var links = (availGroup && availGroup.buildable_links) || [];
    for (var i = 0; i < links.length; i += 1) {
        var link = links[i];
        var aVal = link.a_dut + ":" + link.a_port;
        var bVal = link.b_dut + ":" + link.b_port;
        options.push({ a: aVal, b: bVal });
    }
    return options;
}

function icAddPairRow(availGroup) {
    var rows = document.getElementById("ic-pairs-rows");
    var options = icBuildablePairOptions(availGroup);

    var row = document.createElement("div");
    row.className = "ic-pair-row";

    var aSel = document.createElement("select");
    aSel.className = "ic-pair-a";
    var bSel = document.createElement("select");
    bSel.className = "ic-pair-b";
    icResetSelect(aSel, "A end\u2026");
    icResetSelect(bSel, "B end\u2026");
    for (var i = 0; i < options.length; i += 1) {
        icAddOption(aSel, options[i].a, options[i].a);
        icAddOption(bSel, options[i].b, options[i].b);
    }

    var removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "ic-pair-remove";
    removeBtn.textContent = "\u00d7";
    removeBtn.addEventListener("click", function () {
        rows.removeChild(row);
        icUpdateCreateEnabled();
    });

    aSel.addEventListener("change", icUpdateCreateEnabled);
    bSel.addEventListener("change", icUpdateCreateEnabled);

    row.appendChild(aSel);
    row.appendChild(bSel);
    row.appendChild(removeBtn);
    rows.appendChild(row);
}

function icCollectPairs() {
    var rows = document.getElementById("ic-pairs-rows")
        .getElementsByClassName("ic-pair-row");
    var pairs = [];
    for (var i = 0; i < rows.length; i += 1) {
        var a = rows[i].getElementsByClassName("ic-pair-a")[0].value;
        var b = rows[i].getElementsByClassName("ic-pair-b")[0].value;
        if (a && b) {
            pairs.push({ a: a, b: b });
        }
    }
    return pairs;
}

function icSelectedDuts() {
    var dutsSel = document.getElementById("ic-sel-duts");
    var duts = [];
    for (var i = 0; i < dutsSel.options.length; i += 1) {
        if (dutsSel.options[i].selected) {
            duts.push(dutsSel.options[i].value);
        }
    }
    return duts;
}

function icBuildRequestBody() {
    var group = icSelectedGroupName();
    if (!group) {
        return null;
    }
    if (icCurrentMode() === "profile") {
        var duts = icSelectedDuts();
        if (duts.length < 2) {
            return null;
        }
        return {
            group: group,
            duts: duts,
            profile: document.getElementById("ic-sel-profile").value
        };
    }
    var pairs = icCollectPairs();
    if (pairs.length === 0) {
        return null;
    }
    return { group: group, pairs: pairs };
}

function icUpdateCreateEnabled() {
    var ok = icBuildRequestBody() !== null && !icState.notConfigured;
    document.getElementById("ic-btn-allocate").disabled = !ok;
    document.getElementById("ic-btn-preview").disabled = !ok;
}

function icResetSelect(sel, placeholder) {
    while (sel.firstChild) {
        sel.removeChild(sel.firstChild);
    }
    icAddOption(sel, "", placeholder);
}

function icAddOption(sel, value, label) {
    var opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    sel.appendChild(opt);
}

function icHasOption(sel, value) {
    if (!value) {
        return false;
    }
    for (var i = 0; i < sel.options.length; i += 1) {
        if (sel.options[i].value === value) {
            return true;
        }
    }
    return false;
}

/* ------------------------------------------------------------------ *
 * 建立 / 預覽 / 釋放（design §4.6）。method 以字面字串傳入（AC-3）。
 * ------------------------------------------------------------------ */

function allocateIcSession() {
    var body = icBuildRequestBody();
    if (!body) {
        return;
    }
    icApiFetch("POST", "/interconnect/sessions", body).then(
        function () {
            clearIcError();
            icRefreshAll();
        },
        function (err) {
            showIcError(err.type, icFriendlyMessage(err));
        }
    );
}

function previewIcSession() {
    var body = icBuildRequestBody();
    if (!body) {
        return;
    }
    icApiFetch("POST", "/interconnect/plan", body).then(
        function (plan) {
            clearIcError();
            renderIcPreview(plan);
        },
        function (err) {
            showIcError(err.type, icFriendlyMessage(err));
        }
    );
}

function renderIcPreview(plan) {
    var el = document.getElementById("ic-detail-content");
    while (el.firstChild) {
        el.removeChild(el.firstChild);
    }
    var head = document.createElement("p");
    head.className = "ic-preview-head";
    head.textContent = "Preview (not allocated)";
    el.appendChild(head);

    var preview = plan && plan.preview;
    var links = (preview && preview.links) || [];
    var ul = document.createElement("ul");
    ul.className = "ic-link-list";
    for (var i = 0; i < links.length; i += 1) {
        var link = links[i];
        var li = document.createElement("li");
        li.textContent = "VLAN " + link.vlan + ": "
            + link.a_dut + ":" + link.a_port
            + " \u2194 " + link.b_dut + ":" + link.b_port;
        ul.appendChild(li);
    }
    el.appendChild(ul);
}

function releaseIcSession(sessionId) {
    icApiFetch("DELETE", "/interconnect/sessions/"
            + encodeURIComponent(sessionId)).then(
        function () {
            clearIcError();
            if (icState.selectedId === sessionId) {
                icState.selectedId = null;
            }
            icRefreshAll();
        },
        function (err) {
            if (err.type === "InterconnectSessionNotFoundError") {
                showIcError(err.type, "Session already gone; refreshing.");
                if (icState.selectedId === sessionId) {
                    icState.selectedId = null;
                }
                icRefreshAll();
            } else {
                showIcError(err.type, icFriendlyMessage(err));
            }
        }
    );
}

/* ------------------------------------------------------------------ *
 * 錯誤 banner（design §4.3）。
 * ------------------------------------------------------------------ */

function showIcError(type, message) {
    var banner = document.getElementById("ic-error-banner");
    banner.querySelector(".type").textContent = type || "";
    banner.querySelector(".msg").textContent = message || "";
    banner.setAttribute("data-type", type || "");
    banner.classList.add("visible");
}

function clearIcError() {
    var banner = document.getElementById("ic-error-banner");
    banner.classList.remove("visible");
    banner.removeAttribute("data-type");
}

/* ------------------------------------------------------------------ *
 * icRefreshAll：並行抓三個讀端點並重繪（design §4.6）。
 * ------------------------------------------------------------------ */

function icRefreshAll() {
    icState.notConfigured = false;
    var readFailed = false;

    var sumP = icApiFetch("GET", "/interconnect/summary").then(
        function (data) { icState.summary = data; },
        function (err) {
            if (err.type === "InterconnectNotConfiguredError") {
                icState.notConfigured = true;
            } else {
                readFailed = true;
                showIcError(err.type,
                    "Failed to load interconnect summary: "
                        + icFriendlyMessage(err));
            }
        }
    );
    var sessP = icApiFetch("GET", "/interconnect/sessions").then(
        function (data) { icState.sessions = (data && data.sessions) || []; },
        function (err) {
            if (err.type === "InterconnectNotConfiguredError") {
                icState.notConfigured = true;
            } else {
                readFailed = true;
                showIcError(err.type,
                    "Failed to load interconnect sessions: "
                        + icFriendlyMessage(err));
            }
        }
    );
    var availP = icApiFetch("GET", "/interconnect/resources/available").then(
        function (data) { icState.available = data; },
        function (err) {
            if (err.type === "InterconnectNotConfiguredError") {
                icState.notConfigured = true;
            } else {
                readFailed = true;
                showIcError(err.type,
                    "Failed to load interconnect resources: "
                        + icFriendlyMessage(err));
            }
        }
    );

    return Promise.all([sumP, sessP, availP]).then(function () {
        icState.loaded = true;
        if (icState.notConfigured) {
            /* 無互聯區段：中性提示、清狀態、不紅 banner。 */
            clearIcError();
            icState.summary = null;
            icState.sessions = [];
            icState.available = null;
            icState.selectedId = null;
        }
        /* 選中若已不存在則清選。 */
        if (icState.selectedId && !currentIcSelection()) {
            icState.selectedId = null;
        }
        renderIcTopology();
        renderIcSessionList();
        renderIcDetail();
        renderIcCreateControls();
        if (readFailed || icState.notConfigured) {
            document.getElementById("ic-btn-allocate").disabled = true;
            document.getElementById("ic-btn-preview").disabled = true;
        }
    });
}

/* ------------------------------------------------------------------ *
 * 綁定（lazy：首次顯示互聯頁籤才載入；design §5）。
 * ------------------------------------------------------------------ */

function wireIc() {
    document.getElementById("ic-mode-profile").addEventListener("change", function () {
        icRenderModeFields();
        icUpdateCreateEnabled();
    });
    document.getElementById("ic-mode-pairs").addEventListener("change", function () {
        icRenderModeFields();
        icUpdateCreateEnabled();
    });
    document.getElementById("ic-sel-group").addEventListener("change", function () {
        icRenderModeFields();
        icUpdateCreateEnabled();
    });
    document.getElementById("ic-sel-duts").addEventListener("change", icUpdateCreateEnabled);
    document.getElementById("ic-sel-profile").addEventListener("change", icUpdateCreateEnabled);
    document.getElementById("ic-add-pair").addEventListener("click", function () {
        var availGroup = icGroupByName(icSelectedGroupName());
        if (availGroup) {
            icAddPairRow(availGroup);
        }
    });
    document.getElementById("ic-btn-allocate").addEventListener("click", allocateIcSession);
    document.getElementById("ic-btn-preview").addEventListener("click", previewIcSession);
    document.getElementById("ic-session-filter").addEventListener("input", renderIcSessionList);

    var close = document.querySelector("#ic-error-banner .close");
    if (close) {
        close.addEventListener("click", clearIcError);
    }

    /* 首次顯示互聯頁籤時才載入（避免非互聯拓樸在背景噴錯）。 */
    document.addEventListener("ic-tab-shown", function () {
        if (!icState.loaded) {
            icRefreshAll();
        }
    });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireIc);
} else {
    wireIc();
}
