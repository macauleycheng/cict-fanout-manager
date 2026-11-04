/*
 * CICT Fanout Manager Web UI behavior (design §4, §5, §6, §7, §8).
 *
 * All client-side behavior: fetch the five JSON endpoints, build a
 * device-level graph from GET /topology, derive an N-tier layered layout at
 * runtime via multi-source BFS (never reading `role`), render inline SVG via
 * document.createElementNS, show a hover tooltip, highlight a selected
 * session's path, drive create/release/filter, and surface friendly errors.
 *
 * No build step, no bundler, no external graph library. Served verbatim via
 * GET /static/app.js. User-facing text is English.
 */

"use strict";

var SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------ *
 * Node-id helpers (design §5.1).
 *
 * Every node id is "<device>:<port>" where the device part contains no ":"
 * and the port part may contain "/" but not ":". These two helpers are the
 * ONLY idiom used to split a node id anywhere in this file. JavaScript's
 * String.prototype.split with a limit caps the number of returned array
 * elements (it is NOT Python's maxsplit), so a limit of 1 would drop the
 * port entirely; devOf/portOf avoid that bug and preserve ports with "/".
 * ------------------------------------------------------------------ */

function devOf(nodeId) {
    var idx = nodeId.indexOf(":");
    /* 無冒號（裸名，如 CLI 建的 session port）時回整串，對齊 Python 以首個
     * 冒號切出 device 段的語意；避免 slice(0, -1) 砍掉最後一字的靜默錯值。 */
    if (idx === -1) {
        return nodeId;
    }
    return nodeId.slice(0, idx);
}

function portOf(nodeId) {
    return nodeId.slice(nodeId.indexOf(":") + 1);
}

/* ------------------------------------------------------------------ *
 * Shared client state.
 * ------------------------------------------------------------------ */

var state = {
    topology: null,        /* raw GET /topology payload */
    sessions: [],          /* raw GET /sessions .sessions list */
    available: null,       /* raw GET /resources/available payload */
    selectedId: null,      /* currently highlighted session_id, or null */
    autoRefresh: false,    /* whether auto-refresh is currently enabled */
    autoTimer: null        /* setTimeout handle for the next auto-refresh tick */
};

var AUTO_REFRESH_INTERVAL_MS = 10000;

/* ------------------------------------------------------------------ *
 * API fetch wrapper (design §6).
 * ------------------------------------------------------------------ */

function ApiError(type, message, status) {
    this.type = type;
    this.message = message;
    this.status = status;
}

function apiFetch(method, url, body) {
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
                /* Structured error body: {"error": {"type", "message"}}. */
                if (parsed && parsed.error && parsed.error.type) {
                    throw new ApiError(
                        parsed.error.type,
                        parsed.error.message || "",
                        response.status
                    );
                }
                /* Non-JSON (e.g. a Starlette plaintext 404/500). */
                throw new ApiError(
                    "Unknown",
                    response.status + " " + response.statusText,
                    response.status
                );
            });
        },
        function () {
            /* Network failure (fetch rejects). */
            throw new ApiError("Network", "Cannot reach server", 0);
        }
    );
}

/* ------------------------------------------------------------------ *
 * Device-level graph build + multi-source BFS tiering (design §4).
 * ------------------------------------------------------------------ */

/* Build a device-level model from the raw topology payload:
 *   devices: { name -> {name, type, role, ports[], rack} }
 *   adjacency: { dev -> Set(dev) }  (undirected)
 *   pairLinks: { "a|b" (sorted) -> [ {aNode, bNode, aDev, bDev} ] }
 */
function buildGraph(topology) {
    var devices = {};
    var adjacency = {};
    var pairLinks = {};

    function ensureAdj(dev) {
        if (!adjacency[dev]) {
            adjacency[dev] = {};
        }
    }

    var i, j, k;
    var gens = topology.generators || [];
    for (i = 0; i < gens.length; i += 1) {
        devices[gens[i].name] = {
            name: gens[i].name,
            type: "gen",
            role: gens[i].type || "gen",
            ports: (gens[i].ports || []).slice(),
            rack: null
        };
    }
    var fanouts = topology.fanouts || [];
    for (i = 0; i < fanouts.length; i += 1) {
        /* role is kept as a display-only label; layout never reads it. */
        devices[fanouts[i].name] = {
            name: fanouts[i].name,
            type: "fanout",
            role: fanouts[i].role || "fanout",
            ports: [],
            rack: fanouts[i].rack || null
        };
    }
    var racks = topology.racks || [];
    for (i = 0; i < racks.length; i += 1) {
        var duts = racks[i].duts || [];
        for (j = 0; j < duts.length; j += 1) {
            devices[duts[j].name] = {
                name: duts[j].name,
                type: "dut",
                role: "dut",
                ports: (duts[j].tg_ports || []).slice(),
                rack: racks[i].name
            };
        }
    }

    var links = topology.links || [];
    for (k = 0; k < links.length; k += 1) {
        var link = links[k];
        var aDev = link.a.dev;
        var bDev = link.b.dev;
        var aNode = aDev + ":" + link.a.port;
        var bNode = bDev + ":" + link.b.port;
        if (aDev === bDev) {
            /* intra-device wiring: not a device-pair edge. */
            continue;
        }
        ensureAdj(aDev);
        ensureAdj(bDev);
        adjacency[aDev][bDev] = true;
        adjacency[bDev][aDev] = true;

        var key = [aDev, bDev].sort().join("|");
        if (!pairLinks[key]) {
            pairLinks[key] = [];
        }
        pairLinks[key].push({
            aNode: aNode,
            bNode: bNode,
            aDev: aDev,
            bDev: bDev
        });
    }

    return { devices: devices, adjacency: adjacency, pairLinks: pairLinks };
}

/* Multi-source BFS tier assignment (design §4.2).
 *
 * Seed distance 0 at all generator devices, BFS over undirected device
 * adjacency; each device's tier = BFS distance from the nearest generator.
 * Never branches on `role`. Unreachable devices get tier Infinity (placed in
 * a dedicated right-most "unlinked" column by the renderer).
 *
 * Returns { dev -> tier } where tier is a non-negative integer or Infinity.
 */
function computeTiers(graph) {
    var tiers = {};
    var queue = [];
    var name;

    for (name in graph.devices) {
        if (graph.devices.hasOwnProperty(name)) {
            if (graph.devices[name].type === "gen") {
                tiers[name] = 0;
                queue.push(name);
            } else {
                tiers[name] = Infinity;
            }
        }
    }

    var head = 0;
    while (head < queue.length) {
        var current = queue[head];
        head += 1;
        var neighbors = graph.adjacency[current] || {};
        var neighbor;
        for (neighbor in neighbors) {
            if (neighbors.hasOwnProperty(neighbor)) {
                if (tiers[neighbor] === Infinity) {
                    tiers[neighbor] = tiers[current] + 1;
                    queue.push(neighbor);
                }
            }
        }
    }

    return tiers;
}

/* ------------------------------------------------------------------ *
 * Layout: assign (x, y) coordinates per device and size the viewBox
 * (design §4.3, §4.4).
 * ------------------------------------------------------------------ */

var LAYOUT = {
    marginX: 40,
    marginY: 40,
    columnGap: 200,
    rowGap: 70,
    boxWidth: 130,
    boxHeight: 46
};

function computeLayout(graph, tiers) {
    /* Group devices by tier; Infinity goes to a synthetic right-most column. */
    var finiteTiers = [];
    var name;
    for (name in tiers) {
        if (tiers.hasOwnProperty(name) && tiers[name] !== Infinity) {
            if (finiteTiers.indexOf(tiers[name]) === -1) {
                finiteTiers.push(tiers[name]);
            }
        }
    }
    finiteTiers.sort(function (a, b) { return a - b; });

    var maxTier = finiteTiers.length ? finiteTiers[finiteTiers.length - 1] : 0;
    var unlinkedColumn = maxTier + 1;

    var columns = {}; /* columnIndex -> [device names] */
    for (name in tiers) {
        if (tiers.hasOwnProperty(name)) {
            var col = tiers[name] === Infinity ? unlinkedColumn : tiers[name];
            if (!columns[col]) {
                columns[col] = [];
            }
            columns[col].push(name);
        }
    }

    /* Deterministic within-column ordering: DUTs grouped by rack then name;
     * others by name. */
    var colIndex;
    var maxColumnCount = 0;
    for (colIndex in columns) {
        if (columns.hasOwnProperty(colIndex)) {
            columns[colIndex].sort(function (a, b) {
                var da = graph.devices[a];
                var db = graph.devices[b];
                var ra = da.rack || "";
                var rb = db.rack || "";
                if (ra !== rb) {
                    return ra < rb ? -1 : 1;
                }
                return da.name < db.name ? -1 : (da.name > db.name ? 1 : 0);
            });
            if (columns[colIndex].length > maxColumnCount) {
                maxColumnCount = columns[colIndex].length;
            }
        }
    }

    var positions = {}; /* dev -> {x, y} */
    for (colIndex in columns) {
        if (columns.hasOwnProperty(colIndex)) {
            var list = columns[colIndex];
            var ci = parseInt(colIndex, 10);
            for (var r = 0; r < list.length; r += 1) {
                positions[list[r]] = {
                    x: LAYOUT.marginX + ci * LAYOUT.columnGap,
                    y: LAYOUT.marginY + r * LAYOUT.rowGap
                };
            }
        }
    }

    var columnCount = unlinkedColumn + 1;
    var width = LAYOUT.marginX * 2 + (columnCount - 1) * LAYOUT.columnGap
        + LAYOUT.boxWidth;
    var height = LAYOUT.marginY * 2 + Math.max(0, maxColumnCount - 1)
        * LAYOUT.rowGap + LAYOUT.boxHeight;

    return {
        positions: positions,
        width: Math.max(width, 400),
        height: Math.max(height, 200)
    };
}

/* ------------------------------------------------------------------ *
 * SVG rendering (design §5.1).
 * ------------------------------------------------------------------ */

function svgEl(name, attrs) {
    var el = document.createElementNS(SVG_NS, name);
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

/* Canonical order-independent key for an edge between two node ids. */
function edgeKey(nodeA, nodeB) {
    return [nodeA, nodeB].sort().join("|");
}

function isTrunk(graph, devA, devB) {
    return graph.devices[devA].type === "fanout"
        && graph.devices[devB].type === "fanout";
}

function renderTopology() {
    var svg = document.getElementById("topology-svg");
    while (svg.firstChild) {
        svg.removeChild(svg.firstChild);
    }
    if (!state.topology) {
        return;
    }

    var graph = buildGraph(state.topology);
    var tiers = computeTiers(graph);
    var layout = computeLayout(graph, tiers);

    svg.setAttribute("viewBox", "0 0 " + layout.width + " " + layout.height);

    var edgeGroup = svgEl("g", { "class": "edges" });
    var nodeGroup = svgEl("g", { "class": "nodes" });
    svg.appendChild(edgeGroup);
    svg.appendChild(nodeGroup);

    /* Edges first so nodes paint on top. One line per device-pair link. */
    var key;
    for (key in graph.pairLinks) {
        if (graph.pairLinks.hasOwnProperty(key)) {
            var linkList = graph.pairLinks[key];
            for (var li = 0; li < linkList.length; li += 1) {
                var pl = linkList[li];
                var posA = layout.positions[pl.aDev];
                var posB = layout.positions[pl.bDev];
                if (!posA || !posB) {
                    continue;
                }
                var cls = "edge";
                if (isTrunk(graph, pl.aDev, pl.bDev)) {
                    cls += " trunk";
                }
                var line = svgEl("line", {
                    "class": cls,
                    x1: posA.x + LAYOUT.boxWidth,
                    y1: posA.y + LAYOUT.boxHeight / 2,
                    x2: posB.x,
                    y2: posB.y + LAYOUT.boxHeight / 2,
                    "data-a-dev": pl.aDev,
                    "data-b-dev": pl.bDev,
                    "data-a-node": pl.aNode,
                    "data-b-node": pl.bNode,
                    "data-key": edgeKey(pl.aNode, pl.bNode)
                });
                edgeGroup.appendChild(line);
            }
        }
    }

    /* Device boxes. Each carries its connected port list for the tooltip. */
    var linkPortsByDev = computeLinkPortsByDev(graph);
    var name;
    for (name in graph.devices) {
        if (graph.devices.hasOwnProperty(name)) {
            var dev = graph.devices[name];
            var pos = layout.positions[name];
            if (!pos) {
                continue;
            }
            var ports = (linkPortsByDev[name] || []).slice().sort();
            var gcls = "node " + dev.type;
            if (dev.type === "fanout") {
                gcls += " role-" + String(dev.role).toLowerCase();
            }
            var g = svgEl("g", {
                "class": gcls,
                "data-device": name,
                "data-ports": ports.join(",")
            });
            g.appendChild(svgEl("rect", {
                x: pos.x,
                y: pos.y,
                width: LAYOUT.boxWidth,
                height: LAYOUT.boxHeight,
                rx: 8,
                ry: 8
            }));
            var label = svgEl("text", {
                "class": "label",
                x: pos.x + LAYOUT.boxWidth / 2,
                y: pos.y + 20,
                "text-anchor": "middle"
            });
            label.textContent = name;
            g.appendChild(label);
            var roleText = svgEl("text", {
                "class": "role",
                x: pos.x + LAYOUT.boxWidth / 2,
                y: pos.y + 35,
                "text-anchor": "middle"
            });
            roleText.textContent = dev.type === "dut"
                ? (dev.rack || "dut")
                : dev.role;
            g.appendChild(roleText);

            attachNodeEvents(g, name);
            nodeGroup.appendChild(g);
        }
    }

    /* Re-apply highlight for the preserved selection, if any. */
    applyHighlight();
}

/* Map each device to the port names it exposes on a drawn link. */
function computeLinkPortsByDev(graph) {
    var byDev = {};
    var key;
    for (key in graph.pairLinks) {
        if (graph.pairLinks.hasOwnProperty(key)) {
            var list = graph.pairLinks[key];
            for (var i = 0; i < list.length; i += 1) {
                var pl = list[i];
                addPort(byDev, pl.aDev, portOf(pl.aNode));
                addPort(byDev, pl.bDev, portOf(pl.bNode));
            }
        }
    }
    return byDev;
}

function addPort(byDev, dev, port) {
    if (!byDev[dev]) {
        byDev[dev] = [];
    }
    if (byDev[dev].indexOf(port) === -1) {
        byDev[dev].push(port);
    }
}

/* ------------------------------------------------------------------ *
 * Tooltip on hover (design §5.2). Uses devOf/portOf, never a limited split.
 * ------------------------------------------------------------------ */

function attachNodeEvents(g, deviceName) {
    g.addEventListener("mouseenter", function (evt) {
        showTooltip(evt, deviceName, g.getAttribute("data-ports"));
    });
    g.addEventListener("mousemove", function (evt) {
        positionTooltip(evt);
    });
    g.addEventListener("mouseleave", function () {
        hideTooltip();
    });
}

function sessionPortsOf(dev) {
    var selected = currentSelection();
    if (!selected) {
        return [];
    }
    var result = [];
    var pp = selected.physical_ports || [];
    for (var i = 0; i < pp.length; i += 1) {
        if (devOf(pp[i]) === dev) {
            result.push(portOf(pp[i]));
        }
    }
    return result;
}

function showTooltip(evt, deviceName, portsCsv) {
    var tooltip = document.getElementById("tooltip");
    var linkPorts = portsCsv ? portsCsv.split(",") : [];
    var inSession = sessionPortsOf(deviceName);

    while (tooltip.firstChild) {
        tooltip.removeChild(tooltip.firstChild);
    }
    var head = document.createElement("div");
    head.innerHTML = "<strong></strong>";
    head.firstChild.textContent = deviceName;
    tooltip.appendChild(head);

    var portsLine = document.createElement("div");
    if (linkPorts.length === 0) {
        portsLine.textContent = "(no connected ports)";
    } else {
        for (var i = 0; i < linkPorts.length; i += 1) {
            var span = document.createElement("span");
            span.className = "port";
            if (inSession.indexOf(linkPorts[i]) !== -1) {
                span.className += " in-session";
            }
            span.textContent = linkPorts[i];
            portsLine.appendChild(span);
            if (i < linkPorts.length - 1) {
                portsLine.appendChild(document.createTextNode(", "));
            }
        }
    }
    tooltip.appendChild(portsLine);

    tooltip.classList.add("visible");
    positionTooltip(evt);
}

function positionTooltip(evt) {
    var tooltip = document.getElementById("tooltip");
    tooltip.style.left = (evt.pageX + 12) + "px";
    tooltip.style.top = (evt.pageY + 12) + "px";
}

function hideTooltip() {
    document.getElementById("tooltip").classList.remove("visible");
}

/* ------------------------------------------------------------------ *
 * Session-path highlighting (design §5.3).
 * ------------------------------------------------------------------ */

/* Build the set of canonical edge keys a session's path traverses. Skips
 * intra-device consecutive hops (same device on both sides). */
function computeHighlightedEdgeKeys(path) {
    var keys = {};
    for (var i = 0; i + 1 < path.length; i += 1) {
        var a = path[i];
        var b = path[i + 1];
        if (devOf(a) === devOf(b)) {
            continue; /* intra-device hop: no drawn inter-device edge. */
        }
        keys[edgeKey(a, b)] = true;
    }
    return keys;
}

function currentSelection() {
    if (!state.selectedId) {
        return null;
    }
    for (var i = 0; i < state.sessions.length; i += 1) {
        if (state.sessions[i].session_id === state.selectedId) {
            return state.sessions[i];
        }
    }
    return null;
}

function applyHighlight() {
    var svg = document.getElementById("topology-svg");
    var edges = svg.getElementsByClassName("edge");
    var nodes = svg.getElementsByClassName("node");
    var i;

    /* Clear all active classes first (single-select). */
    for (i = 0; i < edges.length; i += 1) {
        edges[i].classList.remove("active");
    }
    for (i = 0; i < nodes.length; i += 1) {
        nodes[i].classList.remove("active");
    }

    var selected = currentSelection();
    if (!selected) {
        return;
    }

    var path = selected.path || [];
    var keys = computeHighlightedEdgeKeys(path);
    for (i = 0; i < edges.length; i += 1) {
        if (keys[edges[i].getAttribute("data-key")]) {
            edges[i].classList.add("active");
        }
    }

    var activeDevices = {};
    for (i = 0; i < path.length; i += 1) {
        activeDevices[devOf(path[i])] = true;
    }
    for (i = 0; i < nodes.length; i += 1) {
        if (activeDevices[nodes[i].getAttribute("data-device")]) {
            nodes[i].classList.add("active");
        }
    }
}

/* ------------------------------------------------------------------ *
 * Session selection + detail panel (design §5.4, §6).
 * ------------------------------------------------------------------ */

function selectSession(sessionId) {
    if (state.selectedId === sessionId) {
        /* clicking the selected row deselects. */
        state.selectedId = null;
    } else {
        state.selectedId = sessionId;
    }
    applyHighlight();
    renderSessionList();
    renderDetail();
}

/* Derive edge ports: physical_ports minus every trunk segment endpoint
 * (design §5.4), matching routes._occupied_access_ports. */
function edgePortsOf(session) {
    var trunkEnds = {};
    var ts = session.trunk_segments || [];
    for (var i = 0; i < ts.length; i += 1) {
        trunkEnds[ts[i].a_node] = true;
        trunkEnds[ts[i].b_node] = true;
    }
    var result = [];
    var pp = session.physical_ports || [];
    for (var j = 0; j < pp.length; j += 1) {
        if (!trunkEnds[pp[j]]) {
            result.push(pp[j]);
        }
    }
    return result;
}

function renderDetail() {
    var el = document.getElementById("detail-content");
    while (el.firstChild) {
        el.removeChild(el.firstChild);
    }
    var selected = currentSelection();
    if (!selected) {
        var empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent = "No session selected.";
        el.appendChild(empty);
        return;
    }

    var dl = document.createElement("dl");
    addDetailRow(dl, "Session", selected.session_id);
    addDetailRow(dl, "VLAN", String(selected.vlan));
    addDetailRow(dl, "Generator", selected.generator_port);
    addDetailRow(dl, "DUT", selected.dut_port);
    addDetailRow(dl, "Path", (selected.path || []).join(" -> "));
    addDetailRow(dl, "Edge ports", edgePortsOf(selected).join(", ") || "(none)");

    var segs = selected.trunk_segments || [];
    var segText;
    if (segs.length === 0) {
        segText = "(none)";
    } else {
        var parts = [];
        for (var i = 0; i < segs.length; i += 1) {
            parts.push(segs[i].a_node + " -> " + segs[i].b_node
                + " (VLAN " + segs[i].vlan + ")");
        }
        segText = parts.join("; ");
    }
    addDetailRow(dl, "Trunk segments", segText);
    el.appendChild(dl);

    var releaseBtn = document.createElement("button");
    releaseBtn.className = "danger";
    releaseBtn.type = "button";
    releaseBtn.textContent = "Release session";
    releaseBtn.style.marginTop = "10px";
    releaseBtn.addEventListener("click", function () {
        releaseSession(selected.session_id);
    });
    el.appendChild(releaseBtn);
}

function addDetailRow(dl, label, value) {
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = value;
    dl.appendChild(dt);
    dl.appendChild(dd);
}

/* ------------------------------------------------------------------ *
 * Session list + client-side filter (design §5.2, §7).
 * ------------------------------------------------------------------ */

function sessionMatchesFilter(session, needle) {
    if (!needle) {
        return true;
    }
    var hay = [
        session.session_id,
        String(session.vlan),
        session.generator_port,
        session.dut_port
    ].join(" ").toLowerCase();
    return hay.indexOf(needle) !== -1;
}

function renderSessionList() {
    var ul = document.getElementById("session-list");
    var needle = document.getElementById("session-filter").value
        .trim().toLowerCase();
    while (ul.firstChild) {
        ul.removeChild(ul.firstChild);
    }

    var shown = 0;
    for (var i = 0; i < state.sessions.length; i += 1) {
        var s = state.sessions[i];
        if (!sessionMatchesFilter(s, needle)) {
            continue;
        }
        shown += 1;
        var li = document.createElement("li");
        if (s.session_id === state.selectedId) {
            li.className = "selected";
        }
        var sid = document.createElement("span");
        sid.className = "sid";
        sid.textContent = s.session_id;
        var meta = document.createElement("span");
        meta.className = "meta";
        meta.textContent = "VLAN " + s.vlan;
        li.appendChild(sid);
        li.appendChild(meta);
        li.addEventListener("click", (function (id) {
            return function () { selectSession(id); };
        })(s.session_id));
        ul.appendChild(li);
    }

    if (shown === 0) {
        var empty = document.createElement("li");
        empty.className = "empty";
        empty.textContent = state.sessions.length === 0
            ? "No active sessions."
            : "No sessions match the filter.";
        empty.style.cursor = "default";
        ul.appendChild(empty);
    }
}

/* ------------------------------------------------------------------ *
 * Top bar (design §8).
 * ------------------------------------------------------------------ */

function renderTopBar() {
    var identity = document.getElementById("topo-identity");
    if (state.topology) {
        var t = state.topology;
        var genPorts = 0;
        var gens = t.generators || [];
        for (var i = 0; i < gens.length; i += 1) {
            genPorts += (gens[i].ports || []).length;
        }
        var fanoutCount = (t.fanouts || []).length;
        var dutCount = 0;
        var tgPorts = 0;
        var racks = t.racks || [];
        for (var r = 0; r < racks.length; r += 1) {
            var duts = racks[r].duts || [];
            dutCount += duts.length;
            for (var d = 0; d < duts.length; d += 1) {
                tgPorts += (duts[d].tg_ports || []).length;
            }
        }
        identity.textContent = t.site + " / v" + t.version
            + " \u2014 " + genPorts + " gen ports, "
            + fanoutCount + " fanouts, "
            + dutCount + " DUTs (" + tgPorts + " tg ports)";
    } else {
        identity.textContent = "(topology unavailable)";
    }

    /* VLAN usage: distinct VLANs in use, no denominator (A-3). */
    var vlanEl = document.getElementById("vlan-usage");
    var vlans = {};
    for (var j = 0; j < state.sessions.length; j += 1) {
        vlans[state.sessions[j].vlan] = true;
    }
    vlanEl.textContent = "VLANs in use: " + Object.keys(vlans).length;
}

/* ------------------------------------------------------------------ *
 * Create dropdowns + allocate (design §7).
 * ------------------------------------------------------------------ */

function renderCreateControls() {
    var genSel = document.getElementById("sel-generator");
    var dutSel = document.getElementById("sel-dut");
    var prevGen = genSel.value;
    var prevDut = dutSel.value;

    resetSelect(genSel, "Select a generator port\u2026");
    resetSelect(dutSel, "Select a DUT port\u2026");

    if (state.available) {
        var gens = state.available.available_generator_ports || [];
        for (var i = 0; i < gens.length; i += 1) {
            addOption(genSel, gens[i].node,
                gens[i].generator + " : " + gens[i].port);
        }
        var duts = state.available.available_dut_ports || [];
        for (var j = 0; j < duts.length; j += 1) {
            addOption(dutSel, duts[j].node,
                duts[j].dut + " : " + duts[j].port);
        }
    }

    /* Preserve prior selection if still available. */
    if (hasOption(genSel, prevGen)) {
        genSel.value = prevGen;
    }
    if (hasOption(dutSel, prevDut)) {
        dutSel.value = prevDut;
    }

    updateAllocateEnabled();
}

function resetSelect(sel, placeholder) {
    while (sel.firstChild) {
        sel.removeChild(sel.firstChild);
    }
    addOption(sel, "", placeholder);
}

function addOption(sel, value, label) {
    var opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    sel.appendChild(opt);
}

function hasOption(sel, value) {
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

function updateAllocateEnabled() {
    var genSel = document.getElementById("sel-generator");
    var dutSel = document.getElementById("sel-dut");
    var btn = document.getElementById("btn-allocate");
    btn.disabled = !(genSel.value && dutSel.value);
}

function allocateSession() {
    var genSel = document.getElementById("sel-generator");
    var dutSel = document.getElementById("sel-dut");
    if (!genSel.value || !dutSel.value) {
        return;
    }
    var body = {
        generator_port: genSel.value,
        dut_port: dutSel.value
    };
    apiFetch("POST", "/sessions", body).then(
        function () {
            clearError();
            refreshAll();
        },
        function (err) {
            showAllocateError(err);
        }
    );
}

function releaseSession(sessionId) {
    apiFetch("DELETE", "/sessions/" + encodeURIComponent(sessionId)).then(
        function () {
            clearError();
            if (state.selectedId === sessionId) {
                state.selectedId = null;
            }
            refreshAll();
        },
        function (err) {
            if (err.type === "SessionNotFoundError") {
                showError(err.type, "Session already gone; refreshing.");
                if (state.selectedId === sessionId) {
                    state.selectedId = null;
                }
                refreshAll();
            } else {
                showError(err.type, friendlyMessage(err));
            }
        }
    );
}

/* ------------------------------------------------------------------ *
 * Error banner (design §6).
 * ------------------------------------------------------------------ */

function friendlyMessage(err) {
    switch (err.type) {
        case "HardConflictError":
            return "That port pair is already in use by another session. "
                + (err.message || "");
        case "VlanPoolExhaustedError":
            return "No free VLAN left in the pool; release a session and retry.";
        case "PathLookupError":
            /* 後端此型別涵蓋「無實體路徑／找不到 port／裸名不唯一／dev:port
             * 格式錯／找不到 DUT」等多種成因，不限於無路徑；用中性文案避免誤導。 */
            return "Cannot allocate: check the selected ports are still "
                + "available and connected.";
        case "ValidationError":
            return "Invalid selection.";
        case "SessionStoreError":
            return "Server busy or state temporarily unavailable; retry.";
        case "TopologyFileError":
        case "TopologyParseError":
        case "TopologyValidationError":
        case "TopologyReferenceError":
        case "TopologyGraphError":
            return "Topology temporarily unavailable; fix the file and retry.";
        case "ConfigError":
        case "InternalError":
            /* 後端這兩型 message 為中文原文（如「內部錯誤，請聯絡維運」）；英文
             * UI 不外露後端原文，回通案文案以保持語言一致。 */
            return "Server error; please retry or contact operations.";
        case "Network":
            return "Cannot reach server.";
        default:
            return err.message || "Unexpected server error.";
    }
}

function showAllocateError(err) {
    showError(err.type, friendlyMessage(err));
    /* On availability uncertainty we leave dropdowns; refresh reconciles. */
}

function showError(type, message) {
    var banner = document.getElementById("error-banner");
    banner.querySelector(".type").textContent = type || "";
    banner.querySelector(".msg").textContent = message || "";
    banner.setAttribute("data-type", type || "");
    banner.classList.add("visible");
}

function clearError() {
    var banner = document.getElementById("error-banner");
    banner.classList.remove("visible");
    banner.removeAttribute("data-type");
}

/* ------------------------------------------------------------------ *
 * refreshAll: fetch the three GETs concurrently and re-render (design §8).
 * ------------------------------------------------------------------ */

function refreshAll() {
    /* 任一讀取（topology / sessions / available）失敗即視為「讀取降級」，統一
     * disable allocate——以陳舊資料送出的 Allocate 極可能撞 404/409（設計 Issue 5）。 */
    var readFailed = false;

    var topoP = apiFetch("GET", "/topology").then(
        function (data) { state.topology = data; },
        function (err) {
            readFailed = true;
            showError(err.type, "Failed to load topology: " + friendlyMessage(err));
        }
    );
    var sessP = apiFetch("GET", "/sessions").then(
        function (data) { state.sessions = (data && data.sessions) || []; },
        function (err) {
            readFailed = true;
            showError(err.type, "Failed to load sessions: " + friendlyMessage(err));
        }
    );
    var availP = apiFetch("GET", "/resources/available").then(
        function (data) { state.available = data; },
        function (err) {
            readFailed = true;
            showError(err.type, "Failed to load resources: " + friendlyMessage(err));
        }
    );

    return Promise.all([topoP, sessP, availP]).then(function () {
        /* Preserve selection only if it still exists. */
        if (state.selectedId && !currentSelection()) {
            state.selectedId = null;
        }
        renderTopology();
        renderTopBar();
        renderCreateControls();
        renderSessionList();
        renderDetail();
        /* 任一讀取失敗時鎖住 allocate；renderCreateControls 內已依 available
         * 重算 enable 狀態，故此處於其後覆寫以確保降級時一律 disable。 */
        if (readFailed) {
            document.getElementById("btn-allocate").disabled = true;
        }
    });
}

/* ------------------------------------------------------------------ *
 * Auto-refresh: self-scheduling timer (design Issue 1).
 *
 * setInterval(refreshAll, 10s) would fire the next tick even if the current
 * refreshAll is still in flight, causing re-entrancy and a render race (a
 * late-returning older round overwriting newer data). Instead we schedule the
 * NEXT tick only after the current refreshAll settles, so at most one
 * refreshAll runs at a time regardless of how slow a round is.
 * ------------------------------------------------------------------ */

function startAutoRefresh() {
    if (state.autoRefresh) {
        return;
    }
    state.autoRefresh = true;
    scheduleAutoRefresh();
}

function stopAutoRefresh() {
    state.autoRefresh = false;
    if (state.autoTimer !== null) {
        clearTimeout(state.autoTimer);
        state.autoTimer = null;
    }
}

function scheduleAutoRefresh() {
    if (!state.autoRefresh) {
        return;
    }
    state.autoTimer = setTimeout(function () {
        state.autoTimer = null;
        /* Chain the next tick onto completion (success or failure) of this
         * round so rounds never overlap. refreshAll always resolves (each GET
         * has its own catch), so the finally-equivalent then runs either way. */
        refreshAll().then(scheduleAutoRefresh, scheduleAutoRefresh);
    }, AUTO_REFRESH_INTERVAL_MS);
}

/* ------------------------------------------------------------------ *
 * Wiring on DOMContentLoaded.
 * ------------------------------------------------------------------ */

function wire() {
    document.getElementById("btn-refresh").addEventListener("click", function () {
        refreshAll();
    });

    document.getElementById("chk-autorefresh").addEventListener("change", function (evt) {
        if (evt.target.checked) {
            startAutoRefresh();
        } else {
            stopAutoRefresh();
        }
    });

    document.getElementById("sel-generator").addEventListener("change", updateAllocateEnabled);
    document.getElementById("sel-dut").addEventListener("change", updateAllocateEnabled);
    document.getElementById("btn-allocate").addEventListener("click", allocateSession);
    document.getElementById("session-filter").addEventListener("input", renderSessionList);

    var close = document.querySelector("#error-banner .close");
    if (close) {
        close.addEventListener("click", clearError);
    }

    refreshAll();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
} else {
    wire();
}
