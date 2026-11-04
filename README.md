# topo-manager (Topology Manager)

`topo-manager` models a packet-test (traffic-generation) lab end to end and
drives the fanout switches that stitch it together. From a single topology YAML
describing your traffic generators, fanout switches, racks/DUTs, and the
physical cabling between them, the tool will:

- **Validate** the topology and build a directed, multi-tier graph.
- **Inspect** it: print a human-readable summary and resolve the physical path
  from any generator port to any DUT traffic port.
- **Allocate sessions**: reserve a `generator -> DUT` path with a globally
  unique VLAN and persist it to a crash-safe JSON ledger.
- **Push VLAN config** to real fanout switches over SSH, isolating each session
  with a QinQ / 802.1Q `dot1q-tunnel` outer S-VLAN.
- **Expose everything over a REST API** so an external system (such as the
  ATLAS integration) can query resources and request/release sessions via HTTP.

The same model and algorithms handle two-tier, three-tier, and larger fabrics
with no code changes — adding a tier only means describing one more layer of
links in the YAML.

---

## Installation

Requires **Python 3.8+**. Install in editable mode from the project root
(including the test extras):

```bash
pip install -e ".[dev]"
```

Core dependencies:

- `pydantic` (2.5–2.10) — data models and validation
- `networkx` (3.1, the highest version still supporting Python 3.8) — graph
- `PyYAML` — topology YAML parsing
- `fastapi` (0.110.x) — REST API layer
- `uvicorn` (0.23–0.33) — ASGI server for the REST API

The `[dev]` extra adds `pytest` and `httpx` (the latter is used by the
Starlette test client in the REST API tests).

Pushing VLAN config to real switches needs the optional **drivers** extra,
which adds `netmiko` for SSH. `netmiko` is imported lazily — only when a driver
actually opens a connection — so the pure-logic, config, and CLI paths work
without it installed:

```bash
pip install -e ".[dev,drivers]"
```

Two console scripts are installed:

- `topo-manager` — the CLI (equivalent to `python -m topo_manager`)
- `topo-manager-api` — launches the REST API server

---

## Two independent entry points

The tool ships **two separate executables** (both installed by
`pip install -e .`), and they are independent:

| Entry point | What it is | Interface |
| --- | --- | --- |
| `topo-manager` (or `python -m topo_manager`) | A one-shot CLI command — runs a single subcommand, prints the result, and exits | CLI subcommands only (no HTTP server) |
| `topo-manager-api` (or `uvicorn ... --factory`) | A long-running REST API server (also serves a browser [Web UI](#web-ui) on the side) | HTTP endpoints only |

- The **CLI does not start a server** and the **API server does not expose the
  CLI subcommands** — they are launched differently and do different things.
  The API server additionally serves a lightweight browser [Web UI](#web-ui)
  at its root URL, but that UI is still a client of the same HTTP endpoints —
  it is not a third entry point.
- **Pushing VLAN config to fanout switches** (`plan` / `probe` / `apply` /
  `verify` / `setup`) is **CLI-only**; the REST API has no device-push endpoints.
  This also applies to the interconnect feature: the REST API covers
  [interconnect ledger operations](#interconnect-endpoints) (create/release/
  list/plan), but `apply`, `park`, `release --apply`, and `verify` for
  interconnect stay **CLI-only** too.
- Both act on the **same underlying allocator and the same JSON ledger**: point
  the CLI's `--state` and the server's `TOPO_MANAGER_STATE` at the same file and
  they share state. Concurrent use (CLI + API, or multiple processes) is kept
  consistent by a cross-process file lock (see
  [Concurrency and persistence guarantees](#concurrency-and-persistence-guarantees)).
  The interconnect ledger is a **separate** file (CLI `--state` / server
  `TOPO_MANAGER_IC_STATE`); the CLI and the API share consistency on it the
  same way, through the same file-lock mechanism.

Typical usage: automation (e.g. ATLAS) drives the **REST API**; humans use the
**CLI** for inspection, troubleshooting, and fanout device pushes — both against
the same state file.

## CLI overview

The CLI uses a subcommand interface. Most subcommands take a single positional
argument, `<topology.yaml>`:

```bash
python -m topo_manager <command> <topology.yaml> [options]
```

| Command | Purpose | Key options |
| --- | --- | --- |
| `summary` | Load a topology and print a human-readable summary | (none) |
| `path` | Print one generator->DUT physical path | `--from`, `--to` |
| `allocate` | Allocate a session (VLAN + path) and persist it | `--from`, `--to`, `--state`, `--vlan-min`, `--vlan-max` |
| `release` | Release an existing session | `--session`, `--state`, `--vlan-min`, `--vlan-max` |
| `list` | List active sessions (or `--raw` read-only dump) | `--state`, `--raw`, `--vlan-min`, `--vlan-max` |
| `plan` | Compute the fanout push plan (never connects) | `--session` or `--from`/`--to`, `--credentials`, `--fanout` |
| `probe` | Read-only connect to a fanout, print `show version`/`show vlan` | `--credentials`, `--fanout` |
| `apply` | Push a session to the fanout(s) (dry-run unless `--apply`) | `--session`, `--credentials`, `--fanout`, `--apply` |
| `verify` | Read back and verify a session is deployed; `--apply` re-pushes gaps | `--session`, `--credentials`, `--fanout`, `--apply` |
| `setup` | Push the global `dot1q-tunnel` master switch (dry-run unless `--apply`) | `--credentials`, `--fanout`, `--apply` |
| `interconnect` | DUT-to-DUT links on a separate fanout set (nested subcommands, see [Interconnect fanout](#interconnect-fanout)) | `summary`, `allocate`, `release`, `list`, `plan`, `park`, `apply`, `verify` |

Common option reference:

- `--from GEN_PORT` — the generator-side port, either a bare name (e.g.
  `ixia-1/1`) or `dev:port`.
- `--to DUT_PORT` — the DUT endpoint, **always `dev:port`** (e.g.
  `dut-A1:Eth0`). Because `tg_port` names repeat across DUTs, a bare name cannot
  identify it uniquely. Required for `path` / `allocate`.
- `--session SESSION_ID` — the session identifier (e.g. `s-0001`).
- `--state STATE_JSON` — path to the session ledger, default
  `.topo-state/allocations.json` (relative to the **current working
  directory**). Cross-process consistency requires the same cwd, or passing
  `--state` consistently to the same file.
- `--vlan-min` / `--vlan-max` — VLAN pool bounds (default `100`/`3000`), shared
  by `allocate`/`release`/`list` and the deployment commands. The range must
  lie within the usable 802.1Q band `2..4094` (reserved 0/1/4095 excluded); an
  illegal range (e.g. `--vlan-max 9999`) is rejected cleanly with a non-zero
  exit code and no traceback.
- `--raw` (`list` only) — read-only dump of the raw ledger JSON, **bypassing**
  topology and restore checks. See [Inspecting a broken
  ledger](#inspecting-a-broken-ledger-list---raw).
- `--credentials` / `--fanout` / `--apply` — fanout connection options, covered
  in [Pushing VLAN config to fanout
  switches](#pushing-vlan-config-to-fanout-switches).

When loading, path lookup, or allocation fails, the CLI prints a single clear
error line to stderr and exits non-zero (no Python traceback). This includes
I/O failures where `--state` points to a path that cannot be created/read, is
read-only, or the disk is full — all reported as a single error block.

---

## Topology YAML

The topology YAML describes the lab. Top-level keys are `version`, `site`, and
the four entity lists `generators`, `fanouts`, `racks`, and `links`.

- **generators** — each has a `name`, a `type` (e.g. `ixia`, `trex`), and a
  list of `ports`.
- **fanouts** — each has a `name`, a `role` label (e.g. `core`, `spine`, `agg`,
  `leaf`), and a `port_count`.
- **racks** — each has a `name` and a list of `duts`; each DUT has a `name`, a
  `model`, and its `tg_ports` (traffic-generation ports).
- **links** — each link connects endpoint `a` to endpoint `b`, where each
  endpoint is `{dev, port}`. **Only connected ports are listed.** Any port not
  appearing in `links` is treated as an *edge port* (see the QinQ section
  below).

```yaml
version: 1
site: lab-a

generators:
  - name: ixia-1
    type: ixia
    ports: [ixia-1/1, ixia-1/2]

fanouts:
  - name: core-1
    role: core
    port_count: 24
  - name: leaf-1-1
    role: leaf
    port_count: 24

racks:
  - name: rack-1
    duts:
      - name: dut-A1
        model: modelA
        tg_ports: [Eth0, Eth1]

links:
  - a: {dev: ixia-1, port: ixia-1/1}
    b: {dev: core-1, port: 1}
  - a: {dev: leaf-1-1, port: 1}
    b: {dev: core-1, port: 7}
  - a: {dev: dut-A1, port: Eth0}
    b: {dev: leaf-1-1, port: 11}
```

Three ready-to-use examples ship in `examples/`:

- `topology-2tier.yaml` — a small, human-readable Core -> Leaf -> DUT fabric
  (2 racks x 4 DUTs x 2 tg_ports = 8 DUTs / 16 tg_ports).
- `topology-3tier.yaml` — the same rack/DUT block, but the two-tier `core` is
  split into `spine + agg` (Spine -> Agg -> Leaf -> DUT). The model and
  algorithms are unchanged.
- `topology-fullscale.yaml` — a full-scale three-tier fabric
  (6 racks x 24 DUTs x 8 tg_ports), generated by `examples/gen_fullscale.py`
  and checked in. Do not hand-edit it; re-running the script produces
  byte-equivalent output:

  ```bash
  python examples/gen_fullscale.py
  ```

---

## Inspecting a topology

### Summary and path (two-tier)

```bash
python -m topo_manager summary examples/topology-2tier.yaml
python -m topo_manager path examples/topology-2tier.yaml --from ixia-1/1 --to dut-A1:Eth0
```

Actual output:

```
拓樸摘要：examples/topology-2tier.yaml（site=lab-a）
  generator port 數：6
  DUT 台數：8    DUT 打流 port 數：16
  fanout 台數：3
  各層（role）節點數（台數）：core=1, leaf=2
  各 tier 節點數（排除 #core）：{0: 6, 1: 8, 2: 18, 3: 16}
路徑 ixia-1/1 → dut-A1:Eth0：
  ixia-1:ixia-1/1 → core-1:1 → core-1:7 → leaf-1-1:1 → leaf-1-1:11 → dut-A1:Eth0
```

### Summary and path (three-tier)

```bash
python -m topo_manager summary examples/topology-3tier.yaml
python -m topo_manager path examples/topology-3tier.yaml --from ixia-1/1 --to dut-A1:Eth0
```

Actual output:

```
拓樸摘要：examples/topology-3tier.yaml（site=lab-a）
  generator port 數：6
  DUT 台數：8    DUT 打流 port 數：16
  fanout 台數：5
  各層（role）節點數（台數）：agg=2, leaf=2, spine=1
  各 tier 節點數（排除 #core）：{0: 6, 1: 8, 2: 4, 3: 18, 4: 16}
路徑 ixia-1/1 → dut-A1:Eth0：
  ixia-1:ixia-1/1 → spine-1:1 → spine-1:7 → agg-1:1 → agg-1:2 → leaf-1-1:1 → leaf-1-1:11 → dut-A1:Eth0
```

The summary header and labels are emitted in the tool's native locale; the
numbers and the path are the parts you read.

---

## Session allocation

A **session** reserves one physical generator->DUT path and assigns it a
globally unique VLAN. `allocate` builds the session along the shortest path,
picks the VLAN, and writes the result to the JSON ledger named by `--state`
(default `.topo-state/allocations.json`). `list` reads the same ledger;
`release` frees a named session.

A **hard conflict** (the same access port claimed by two sessions) is rejected
and the owners are listed. Multiple sessions sharing a trunk are legal — each
uses a different VLAN.

```bash
# Allocate a session (prints session_id, VLAN, path, and trunk segments)
python -m topo_manager allocate examples/topology-3tier.yaml \
    --from ixia-1/1 --to dut-A1:Eth0 --state .topo-state/allocations.json

# List all active sessions (reads the same state file)
python -m topo_manager list examples/topology-3tier.yaml \
    --state .topo-state/allocations.json

# Release a named session
python -m topo_manager release examples/topology-3tier.yaml \
    --session s-0001 --state .topo-state/allocations.json
```

Actual `allocate` output:

```
已分配：
session s-0001
  VLAN：100
  端點：ixia-1/1 → dut-A1:Eth0
  路徑：ixia-1:ixia-1/1 → spine-1:1 → spine-1:7 → agg-1:1 → agg-1:2 → leaf-1-1:1 → leaf-1-1:11 → dut-A1:Eth0
  trunk 段：
    spine-1:7 → agg-1:1（VLAN 100）
    agg-1:2 → leaf-1-1:1（VLAN 100）
```

### Ledger format

The state file is a JSON ledger with three top-level keys:

- `version` — the ledger **schema version** (integer, currently `2`). On load
  it is compared against the built-in version; a mismatch is rejected cleanly.
- `next_seq` — the **high-water mark** for session sequence numbers (integer).
  Sequence numbers only increase — after releasing the highest-numbered session
  and restarting, the next allocation will not reuse a spent sequence number.
- `sessions` — the array of currently active sessions (each with `session_id`,
  `vlan`, endpoints, `path`, `physical_ports`, and `trunk_segments`).

> `access_ports` is **not** stored in the ledger. It is a derived value,
> recomputed on the fly as `physical_ports` minus the trunk-segment endpoints
> (single source of truth), so it cannot be corrupted by ledger tampering.

### Concurrency and persistence guarantees

`allocate`/`release` take a cross-process exclusive lock via a sibling lock file
(`<state>.lock`). Inside the lock they re-read the latest ledger, make the
decision, and save — so concurrent processes never compute a duplicate sequence
number or VLAN. Writes use *tempfile + fsync + atomic rename + parent-directory
fsync*, so on power loss the ledger is either the old content or the new,
complete content — never a half-written file.

### Inspecting a broken ledger (`list --raw`)

When the ledger version mismatches, the content is inconsistent, or the
topology YAML is missing/corrupt so a normal `list` cannot proceed, use `--raw`
to dump the raw JSON directly (it never touches the topology and runs no
restore checks). The topology argument can be omitted in this mode:

```bash
python -m topo_manager list --raw --state .topo-state/allocations.json
```

---

## Pushing VLAN config to fanout switches

Once a session is allocated, `topo-manager` can push the matching VLAN
configuration to the real fanout switches over SSH. Each session is isolated by
a single outer **S-VLAN** using the 802.1Q `dot1q-tunnel` (QinQ) model.

### The QinQ / dot1q-tunnel model

- Every fanout needs the global `dot1q-tunnel` master switch enabled once (see
  `setup` below).
- **Edge ports** — ports that face DUT traffic ports or the generator. They are
  configured as **access ports** that tag ingress untagged frames with the
  session's S-VLAN as their native VLAN, plus `dot1q-tunnel mode access`. In
  the topology, an edge port is any port **not listed** in `links`.
- **Interconnect (uplink) ports** — the fanout-to-fanout links. They are
  **trunk uplinks** that carry the tagged S-VLANs between fanouts, with
  `dot1q-tunnel mode uplink`.

Which port is edge vs. interconnect is derived from the session's path and
trunk segments, not hard-coded: the trunk-segment endpoints are interconnect
ports; every other node on the path is an edge port.

### Device commands the driver issues

The driver was validated against an **Edgecore ECS5550-54X** (netmiko
`device_type = cisco_ios`). The exact commands it emits are:

- **Global (once per fanout):**
  - `dot1q-tunnel system-tunnel-control`
- **Edge port (facing DUT / generator), with the session VLAN:**
  - `switchport mode access`
  - `switchport native vlan <vlan>`
  - `switchport dot1q-tunnel mode access`
- **Interconnect port (fanout-to-fanout uplink), with the session VLAN:**
  - `switchport mode trunk`
  - `switchport allowed vlan add <vlan> tagged`
  - `switchport dot1q-tunnel mode uplink`

The edge access VLAN is set dynamically per session: it is the session's own
S-VLAN. On this hardware the correct command is `switchport native vlan` (the
`switchport access vlan` form does not exist on the device).

### Subcommands: plan / probe / apply / verify / setup

Dry-run is the **default** for every writing command. Nothing is written to a
device unless you pass `--apply`.

**`plan`** — pure computation, never connects. Shows per-fanout
interface -> VLAN -> commands for a session. Use `--session` for an existing
session, or `--from`/`--to` to preview a not-yet-allocated one (the two are
mutually exclusive):

```bash
python -m topo_manager plan examples/topology-3tier.yaml --session s-0001
python -m topo_manager plan examples/topology-3tier.yaml --from ixia-1/1 --to dut-A1:Eth0
```

**`probe`** — read-only connect to a fanout and print `show version` and
`show vlan`, for diagnostics. Does not take a topology:

```bash
python -m topo_manager probe --fanout core-1
```

**`apply`** — push a session. Dry-run by default; `--apply` actually connects
and writes. On `--apply` the deployer takes a pre-write snapshot (skipping items
already satisfied), pushes the rest, then reads back to verify; a read-back
mismatch is reported and already-applied interconnect ports are best-effort
rolled back:

```bash
# Preview (dry-run)
python -m topo_manager apply examples/topology-3tier.yaml --session s-0001
# Actually write to the device
python -m topo_manager apply examples/topology-3tier.yaml --session s-0001 --apply
```

**`verify`** — read back from the fanout(s) and report whether the session is
fully deployed. With `--apply`, it re-pushes any missing items (useful for
detecting a switch that lost its config after a reboot):

```bash
python -m topo_manager verify examples/topology-3tier.yaml --session s-0001
python -m topo_manager verify examples/topology-3tier.yaml --session s-0001 --apply
```

**`setup`** — push the global `dot1q-tunnel system-tunnel-control` master
switch (idempotent). It does not take a topology. With no `--fanout` it iterates
every fanout in the credentials; `--fanout` targets one. Dry-run by default:

```bash
# Preview on all fanouts
python -m topo_manager setup
# Enable on one fanout for real
python -m topo_manager setup --fanout core-1 --apply
```

### Fanout credentials

Connection details (`host`, `username`, `password`, `device_type`,
`interface_template`) are resolved with the priority **env > JSON file >
built-in defaults**, per field. Credentials are **never** committed.

Environment variables (used for a single device, or to override fields in the
JSON file):

| Variable | Default | Meaning |
| --- | --- | --- |
| `TOPO_FANOUT_HOST` | (none, required) | Reachable device address |
| `TOPO_FANOUT_USERNAME` | (none, required) | Login username |
| `TOPO_FANOUT_PASSWORD` | (none, required) | Login password |
| `TOPO_FANOUT_DEVICE_TYPE` | `cisco_ios` | netmiko device_type |
| `TOPO_FANOUT_INTERFACE_TEMPLATE` | `Eth1/{port}` | port -> interface name template (must contain `{port}`) |

Or a gitignored JSON file (default name `fanout-credentials.json`, or any path
passed with `--credentials`). A template, `fanout-credentials.example.json`, is
checked in:

```json
{
  "fanouts": {
    "example-fanout": {
      "host": "192.0.2.1",
      "username": "CHANGE_ME",
      "password": "CHANGE_ME",
      "device_type": "cisco_ios",
      "interface_template": "Eth1/{port}"
    }
  }
}
```

When the credentials describe more than one fanout, `--fanout <name>` is
required to select one (for `probe` and single-device `setup`).

> **Safety note:** `release` frees a session in the ledger but does **not**
> revert edge ports on the device — that is intentional product behavior. The
> edge-port revert commands exist only in the live test teardown path, not in
> the normal release flow.

---

## Interconnect fanout

Besides the traffic-generation fabric, a lab often needs the DUTs of one group
wired to **each other** (for example four DUTs forming a mesh, a line or a
ring). The interconnect feature builds those DUT-to-DUT links on a **separate
set of fanout switches** that is never shared with the traffic-generation
fanouts. One interconnect fanout can serve several groups, so the switch is
shared instead of dedicated to one group.

Each link is an **802.1Q access VLAN**: the two fanout ports that face the two
DUTs are put into one dedicated VLAN as untagged members, and every link gets
its own VLAN from the pool. The feature is fully separate from the
traffic-generation side: it has its own graph, its own ledger and its own
subcommands, and the existing commands, outputs and ledger are unchanged.

### YAML `interconnect:` section

The section is optional. DUTs that take part carry three extra fields: `group`
(a label shared by the four DUTs of one set), `slot` (1..4, orders the DUTs
inside the group) and `il_ports` (their four interconnect ports, in order
`p0..p3`).

```yaml
racks:
  - name: rack-1
    duts:
      - name: dut-A1
        model: modelA
        tg_ports: [Eth0, Eth1]
        il_ports: [Eth8, Eth9, Eth10, Eth11]
        slot: 1
        group: A-g1
      # ... slots 2..4 of group A-g1 ...
interconnect:
  fanouts:
    - name: ic-1
      port_count: 48
      rack: rack-1
  links:                       # DUT il_port <-> interconnect fanout port
    - a: {dev: dut-A1, port: Eth8}
      b: {dev: ic-1, port: 1}
    # ... 16 links per group ...
```

Every `il_port` of a group member must be linked to exactly one port of the
group's interconnect fanout, and a fanout port may be used only once. A
48-port fanout therefore carries three groups. Two generated examples ship in
`examples/` (`topology-interconnect-small.yaml` and
`topology-interconnect-fullscale.yaml`); they are produced by
`python examples/gen_interconnect.py` and checked in, so re-running the script
gives byte-identical output.

### Pairing table and profiles

Inside a group the ports used between two DUTs are fixed. DUT `i` reaches
neighbour `j` through the port whose index is the rank of `j` among the other
members of the group:

| DUTs | port on the first DUT | port on the second DUT |
| --- | --- | --- |
| d0-d1 | p0 | p0 |
| d0-d2 | p1 | p0 |
| d0-d3 | p2 | p0 |
| d1-d2 | p1 | p1 |
| d1-d3 | p2 | p1 |
| d2-d3 | p2 | p2 |

`p3` is not used by the table. It is reserved for explicit port pairs. The
pairing depends only on the two DUTs, not on which other DUTs were selected.

A **profile** expands the chosen DUTs (2 to 4, in the order given) into links:

- `mesh`: every pair (1 link for 2 DUTs, 3 for 3 DUTs, 6 for 4 DUTs).
- `line`: consecutive pairs in the order given.
- `ring`: `line` plus a closing link between the last and the first DUT. A ring
  of 2 DUTs is rejected, and a ring of 3 DUTs equals a mesh.

For anything the table does not cover, name the ports explicitly with
`--pair dut-A1:p3=dut-A2:p3` (repeatable; a port is either the `il_port` name
or `p0..p3`).

### Conflicts and several sessions per group

Conflicts are decided **by port only**. Two sessions of the same group may run
at the same time as long as they use different fanout ports, for example
`{d0,d1}` and `{d2,d3}` as two independent two-DUT sessions. A four-DUT ring
chosen as `d0,d1,d2,d3` uses the d0-d1 ports and therefore conflicts with a
`{d0,d1}` session, whereas the same four DUTs chosen as `d0,d2,d1,d3` use
other ports and can coexist with it. Reserving whole DUTs is left to the
scheduler (such as ATLAS); this tool does not enforce it.

### CLI

All subcommands live under `interconnect`:

```bash
# Summary of the interconnect graph (fanouts, groups, DUTs, links)
python -m topo_manager interconnect summary topo.yaml

# Allocate a session: 4 DUTs as a ring, or 2 DUTs as a mesh, or explicit pairs
python -m topo_manager interconnect allocate topo.yaml --group A-g1 \
    --duts dut-A1,dut-A2,dut-A3,dut-A4 --profile ring
python -m topo_manager interconnect allocate topo.yaml --group A-g1 \
    --pair dut-A1:p3=dut-A2:p3

# List active sessions (--raw dumps the ledger without loading the topology)
python -m topo_manager interconnect list topo.yaml
python -m topo_manager interconnect list --raw

# Show the commands without connecting or reserving anything
python -m topo_manager interconnect plan topo.yaml --session ic-0001
python -m topo_manager interconnect plan topo.yaml --session ic-0001 --release
python -m topo_manager interconnect plan topo.yaml --park --fanout ic-1

# Put the interconnect ports into the parking state (dry-run unless --apply)
python -m topo_manager interconnect park topo.yaml --fanout ic-1 --apply

# Configure a session on the device, then read it back
python -m topo_manager interconnect apply topo.yaml --session ic-0001 --apply
python -m topo_manager interconnect verify topo.yaml --session ic-0001
python -m topo_manager interconnect verify topo.yaml --parked

# Release a session
python -m topo_manager interconnect release topo.yaml --session ic-0001 --apply
```

`interconnect setup` is an alias of `interconnect park`.

The same ledger operations (summary, create, list, release from the ledger,
plan) are also available over HTTP; see [Interconnect endpoints](#interconnect-endpoints).
Device push stays in the CLI.

### Parking, release and dry-run

- **Parking** is the idle state of an interconnect port: administratively
  down, native VLAN 1, member of VLAN 1 only, so a DUT sees the link as down.
  `park` restores that state for the ports named by the topology's interconnect
  links (never the whole switch), skips ports owned by an active session and
  clears leftover pool VLANs. VLANs outside the pool are never touched.
- `apply` configures a session in two stages. The ports are first configured
  while still shut down and read back; only if the read-back matches are they
  brought up and read back again. A mismatch therefore never leaves a half-open
  link. Every command is idempotent, so a failed run can simply be repeated.
- `release` also has two stages: the session is first marked `releasing` in the
  ledger (its ports and VLANs stay reserved), then the ports are parked and the
  VLAN definitions removed, and only then is the session removed from the
  ledger. If a step fails the session stays `releasing`; run the same command
  again. `release --ledger-only` skips the device and only edits the ledger
  (the device then needs `park --apply` to clean up).
- Every subcommand that writes the device is a **dry-run unless `--apply`** is
  given, and a dry-run never connects.

### State file and locks

Interconnect sessions are stored in their own file, `interconnect.json`
(default `.topo-state/interconnect.json`, override with `--state`), so the
traffic-generation ledger is not affected. A ledger lock serializes ledger
updates and a separate device lock serializes commands that write the device.
The pool is `--vlan-min`/`--vlan-max` (default `100`/`3000`).

### Credentials

Credentials use the same mechanism as [Fanout
credentials](#fanout-credentials). The interconnect fanout names from the
topology must appear under `fanouts` in the credentials JSON file; an
interconnect fanout without an entry is an error (the command fails instead of
skipping it).

### Known limitations

- Preserving an inner VLAN tag sent by the DUT is not verified: the links are
  plain access VLANs, which are expected to drop or retag nothing the DUT
  already tags, but this has not been tested on a device.
- Slow protocols (LACP, LLDP, STP) are not passed through by default. If they
  must reach the other DUT, enable the transparent forwarding on the switch
  yourself; the tool neither configures nor verifies `l2protocol-tunnel`.
- QinQ is not used: QinQ access ports cannot exchange packets with each other,
  and the interconnect fanout has no uplink port, so each link is a separate
  access VLAN instead.
- Reading back a port takes a few `show` commands, so a large `park` takes
  time; use `--fanout` or `--ports` to narrow it.
- Ledger operations (create/release/list/plan) are available over the
  [REST API](#interconnect-endpoints); device push (`apply`, `park`,
  `release --apply`, `verify`) stays **CLI-only**, and there is no Web UI tab
  for interconnect.

---

## REST API

The REST API is a thin FastAPI layer over the same allocation engine, letting
an external system (such as ATLAS) query available resources, request/release
sessions, and inspect the full topology over HTTP. It reuses the exact same
`Allocator` and `JsonSessionStore` — the API layer only maps HTTP <-> Pydantic
models <-> allocator calls <-> exceptions-to-status-codes. It does not rewrite
any algorithm or bypass the file lock.

### Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `TOPO_MANAGER_TOPOLOGY` | (none, **required**) | Topology YAML path |
| `TOPO_MANAGER_STATE` | `.topo-state/allocations.json` | Session ledger path |
| `TOPO_MANAGER_VLAN_MIN` | `100` | VLAN pool lower bound |
| `TOPO_MANAGER_VLAN_MAX` | `3000` | VLAN pool upper bound |
| `TOPO_MANAGER_IC_STATE` | `.topo-state/interconnect.json` | Interconnect ledger path (separate from `TOPO_MANAGER_STATE`) |
| `TOPO_MANAGER_IC_VLAN_MIN` | `100` | Interconnect VLAN pool lower bound |
| `TOPO_MANAGER_IC_VLAN_MAX` | `3000` | Interconnect VLAN pool upper bound |
| `TOPO_MANAGER_HOST` | `0.0.0.0` | `topo-manager-api` bind host |
| `TOPO_MANAGER_PORT` | `8000` | `topo-manager-api` bind port |

If `TOPO_MANAGER_TOPOLOGY` is missing, the topology is invalid, or a VLAN
range is illegal, the app **fails fast at startup** (one stderr line, non-zero
exit code, no traceback) rather than returning 500 on every request. The
interconnect VLAN range is validated only when the topology has an
`interconnect:` section.

The interconnect variables mirror the CLI's `interconnect ... --state`,
`--vlan-min` and `--vlan-max` options and have the same defaults. They do not
change the meaning of the existing variables: `TOPO_MANAGER_STATE` and
`TOPO_MANAGER_VLAN_*` still only apply to traffic sessions. The two ledgers have
different formats, so never point both variables at the same file (the
interconnect endpoints answer 503 if they find a traffic ledger there). The
interconnect VLAN pool must match the `--vlan-min`/`--vlan-max` the CLI uses on
the same ledger; a session whose VLAN falls outside the pool is rejected with
503 when the ledger is loaded.

### Starting the server (two equivalent ways)

```bash
export TOPO_MANAGER_TOPOLOGY=examples/topology-3tier.yaml
export TOPO_MANAGER_STATE=.topo-state/allocations.json

# Method A: point uvicorn at the app factory (builds the app from env vars)
uvicorn "topo_manager.api:create_app" --factory --host 0.0.0.0 --port 8000

# Method B: the convenience entry point (wraps uvicorn.run, reads env vars)
topo-manager-api
```

Yes — once started, `topo-manager-api` is a long-running resident service.

> **Deployment constraint (main thread):** the file lock implements its
> acquisition timeout with `signal.SIGALRM`, and `signal` can only be set on the
> **main thread**. The ASGI event loop must therefore run on the main thread
> (single-process uvicorn does this by default). Do **not** deploy with a
> multi-threaded worker model that moves the event loop onto a background
> thread, or the writing endpoints (POST/DELETE on `/sessions` and on
> `/interconnect/sessions`) will fail to acquire the lock. All interconnect
> handlers are declared `async def` for the same reason.
> To use multiple cores, scale out horizontally with multiple processes (e.g.
> several uvicorn/gunicorn worker processes, each with its own main thread);
> cross-process consistency is guaranteed by the file lock.

> **Liveness characteristic (event-loop blocking):** the write endpoints'
> `async` handlers (traffic `POST/DELETE /sessions` and interconnect
> `POST/DELETE /interconnect/sessions`) call the allocator's
> `allocate/release` directly on the main-thread event loop, where `fcntl.flock(LOCK_EX)` is a **blocking** system call. The
> whole "acquire lock -> reload under lock -> decide -> save -> release"
> sequence holds the event loop synchronously; during that window (at most one
> lock-acquisition timeout, default 10 s) the process serves **no other
> request, including read-only ones**. In a single process this serializes API
> writes, consistent with the lock semantics; but if a concurrent CLI (or
> another API process) holds the lock, this process's responsiveness is frozen
> until the timeout. This is the inherent trade-off of a signal-based file lock,
> not a bug. To scale, use **multiple processes**, never multi-threaded workers.

### Documentation (OpenAPI)

Once running:

- Swagger UI: `http://<host>:8000/docs`
- OpenAPI schema: `http://<host>:8000/openapi.json`

### Endpoints and curl examples

```bash
# 1) Get the full topology (for visualization/reconstruction)
curl -s localhost:8000/topology

# 2) Get available generator/DUT ports (excluding those held by active sessions)
curl -s localhost:8000/resources/available

# 3) Allocate a session (201 + session summary, status=allocated)
curl -s -X POST localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"}'

# 4) List all active sessions
curl -s localhost:8000/sessions

# 5) Release a session (200 + released session summary, status=released)
curl -s -X DELETE localhost:8000/sessions/s-0001
```

Allocating the same `(generator_port, dut_port)` twice (the access port is
already held) returns **409** with a structured error:

```bash
curl -s -X POST localhost:8000/sessions \
    -H 'Content-Type: application/json' \
    -d '{"generator_port": "ixia-1/1", "dut_port": "dut-A1:Eth0"}'
# => HTTP 409
# {"error": {"type": "HardConflictError", "message": "..."}}
```

Error responses always use the uniform shape
`{"error": {"type": ..., "message": ...}}` with **no traceback**. Status-code
mapping:

- hard conflict / VLAN pool exhausted -> **409**
- session not found / no path -> **404**
- ledger I/O or lock-acquisition failure -> **503**
- request-time topology drift -> **503** (see below)
- bad request input (missing field / empty string / extra field) -> **400**
- unexpected error -> **500** (fixed message)

> **Request-time topology drift (read endpoints return 503; read/write
> asymmetry):** the read endpoints (`GET /sessions`, `GET /resources/available`)
> **re-read the topology YAML and rebuild the allocator on every request** to
> get the latest snapshot; the write endpoints (POST/DELETE) share the singleton
> allocator built **at startup** and do not re-read the YAML per request. So if
> the topology file is broken, deleted, or has its permissions changed *after*
> startup:
>
> - **Read endpoints** hit an environmental `TopologyError` on re-read (file /
>   parse / validation / reference / graph build), all mapped to **503**
>   (temporarily unavailable, **retriable after fixing the file**) rather than a
>   misleading 500 `InternalError`.
> - **Write endpoints** use the startup snapshot and are **unaffected**.
>
> This read/write asymmetry is deliberate: startup fail-fast guarantees the
> topology is valid *at startup*, and request-time drift is reported by the read
> endpoints as 503. Only environmental "request-time topology load/graph-build
> failure" maps to 503; business errors are unaffected (no path / session not
> found still 404, hard conflict / VLAN exhausted still 409, illegal
> `ConfigError` still 500).

### Response fields: deriving access ports

`SessionResponse` flattens the session fields (`session_id`, `vlan`, endpoints,
`path`, `physical_ports`, `trunk_segments`) plus a top-level `status`.
Consistent with the ledger, the response does **not** carry `access_ports`; if
you need them, derive as
`physical_ports − {all trunk_segments' a_node/b_node}`.

### Consistency with the CLI

When the API and CLI point at the **same** `--state` / `TOPO_MANAGER_STATE`, the
file lock (`<state>.lock`) guarantees concurrent consistency: the API's
POST/DELETE and the CLI's `allocate`/`release` are mutually exclusive and
serialized, each re-reading the latest ledger under the lock. The API layer does
not bypass the file lock or substitute an in-memory cache for the allocator's
decisions.

### Interconnect endpoints

The same server also exposes the [interconnect fanout](#interconnect-fanout)
feature over REST, so an external system can create and release interconnect
sessions without the CLI. The endpoints work on the **interconnect ledger only**
(`TOPO_MANAGER_IC_STATE`, see [Configuration](#configuration-environment-variables)),
which is separate from the traffic ledger.

> **Scope: ledger operations only.** The REST API never connects to a device.
> Pushing the configuration to the interconnect fanout (`apply`, `park`,
> `release --apply`, `verify`) is **CLI-only**, just like pushing VLAN config for
> traffic sessions. `DELETE` is the equivalent of `release --ledger-only`: it
> removes the session from the ledger and leaves the device untouched.

| Method and path | CLI equivalent | Success | Purpose |
| --- | --- | --- | --- |
| `GET /interconnect/summary` | `summary` | 200 | Groups, the DUTs in each group (`d0`..`d3`) and the ports used on each interconnect fanout |
| `GET /interconnect/resources/available` | (new) | 200 | Per group: which ports are free (`occupied_by`) and which fixed-pairing links can still be built |
| `GET /interconnect/sessions` | `list` | 200 | All active sessions, ordered by `session_id` |
| `POST /interconnect/sessions` | `allocate` | 201 | Create a session; the response lists every link with its VLAN and both ports |
| `DELETE /interconnect/sessions/{session_id}` | `release --ledger-only` | 200 | Remove a session from the ledger (any phase); the response carries a `warning` |
| `POST /interconnect/plan` | `plan --group ...` | 200 | Preview the commands a new request would need. No side effect: nothing is reserved or written |
| `GET /interconnect/sessions/{session_id}/plan?action=apply\|release` | `plan --session [--release]` | 200 | Commands for an existing session |
| `GET /interconnect/plan/park?fanout=&ports=` | `plan --park` | 200 | Parking baseline, optionally narrowed with `fanout` and repeated `ports` |

#### Creating a session

The body takes a `group` plus **either** `duts` with a `profile` **or** `pairs`;
the rules are the same as the CLI's `--group`, `--duts`, `--profile` and `--pair`:

- `duts` (2 to 4 names, in the order you want) needs a `profile` of `mesh`,
  `line` or `ring`. `ring` needs at least 3 DUTs.
- `pairs` is a list of `{"a": "dut:port", "b": "dut:port"}` objects. `port` is
  either the DUT's interconnect port name or `p0`..`p3`. `p3` is only reachable
  this way. `pairs` cannot be combined with `profile`.
- Giving both `duts` and `pairs`, or neither, is a 400 error.

```bash
# Four DUTs as a ring
curl -s -X POST localhost:8000/interconnect/sessions \
    -H 'Content-Type: application/json' \
    -d '{"group": "A-g1", "duts": ["dut-A1", "dut-A2", "dut-A3", "dut-A4"], "profile": "ring"}'

# One explicit port pair (p3 is not in the fixed pairing table)
curl -s -X POST localhost:8000/interconnect/sessions \
    -H 'Content-Type: application/json' \
    -d '{"group": "A-g1", "pairs": [{"a": "dut-A1:p3", "b": "dut-A2:p3"}]}'
```

Response (201):

```json
{
  "session_id": "ic-0001",
  "group": "A-g1",
  "fanout": "ic-1-1",
  "profile": "explicit",
  "duts": ["dut-A1", "dut-A2"],
  "phase": "allocated",
  "links": [
    {"vlan": 100, "a_dut": "dut-A1", "a_port": "Eth11", "a_node": "ic-1-1:4",
     "b_dut": "dut-A2", "b_port": "Eth11", "b_node": "ic-1-1:8", "kind": "explicit"}
  ],
  "status": "allocated",
  "warning": null
}
```

Several sessions can live in the same group as long as they use different
ports: for example one session on `dut-A1`/`dut-A2` and another on
`dut-A3`/`dut-A4`. A request that needs a port that another session holds is
refused with 409 and nothing is written; release the other session first, after
which the same ports can be requested again.

#### Listing, planning and releasing

```bash
# Summary, free resources and active sessions
curl -s localhost:8000/interconnect/summary
curl -s localhost:8000/interconnect/resources/available
curl -s localhost:8000/interconnect/sessions

# Preview the commands (nothing is reserved; VLANs shown are illustrative)
curl -s -X POST localhost:8000/interconnect/plan \
    -H 'Content-Type: application/json' \
    -d '{"group": "A-g2", "duts": ["dut-A5", "dut-A6"], "profile": "mesh"}'

# Commands for an existing session, and the parking baseline for one fanout
curl -s 'localhost:8000/interconnect/sessions/ic-0001/plan?action=release'
curl -s 'localhost:8000/interconnect/plan/park?fanout=ic-1-1&ports=1&ports=2'

# Release from the ledger (the device is not touched)
curl -s -X DELETE localhost:8000/interconnect/sessions/ic-0001
```

A `plan` response lists the command batches per interconnect fanout, in the
order they would be sent:

```json
{
  "session_id": "(preview)",
  "action": "apply",
  "vlans": [100],
  "fanouts": [{"fanout": "ic-1-1", "batches": [["interface ethernet 1/17", "shutdown", "..."], ["..."]]}],
  "notes": ["..."],
  "preview": {"session_id": "(preview)", "status": "preview", "links": ["..."]}
}
```

`POST /interconnect/plan` uses POST only to carry a request body. Like the CLI's
`plan`, it does not check port conflicts or VLAN exhaustion, and the VLANs are
numbered from the bottom of the pool. The plan endpoints never read the
credentials file and never open a connection; they use the default interface
name template, so a fanout with a custom `interface_template` in the CLI
credentials may show different interface names in the CLI's own `plan`.

#### Errors

Errors use the same `{"error": {"type": ..., "message": ...}}` shape as the rest
of the API, with no traceback.

| Status | `error.type` | When |
| --- | --- | --- |
| 400 | `InterconnectRequestError` | Unknown group or DUT, DUTs from different groups, duplicate DUT or port, a 2-DUT `ring`, unknown profile or port, malformed pair, `duts`/`pairs`/`profile` combined wrongly, unknown fanout or port in `plan/park` |
| 400 | `ValidationError` | Missing or empty `group`, wrong types, extra fields, bad `action` or `ports` query values |
| 404 | `InterconnectSessionNotFoundError` | `DELETE` or a session `plan` for a `session_id` that does not exist |
| 404 | `InterconnectNotConfiguredError` | The topology has no `interconnect:` section |
| 409 | `InterconnectConflictError` | A port is already held by another session (the message lists the owners) |
| 409 | `VlanPoolExhaustedError` | The interconnect VLAN pool has too few free VLANs |
| 503 | `SessionStoreError` | The interconnect ledger cannot be read or written, is not an interconnect ledger, or the lock could not be taken |
| 500 | `InternalError` | Unexpected error (fixed message) |

If the topology has no `interconnect:` section the server still starts and the
traffic endpoints and the Web UI work as usual; every `/interconnect/...` call
answers 404:

```bash
curl -s localhost:8000/interconnect/summary
# => HTTP 404
# {"error": {"type": "InterconnectNotConfiguredError", "message": "..."}}
```

The interconnect ledger is not read at startup, so a broken interconnect ledger
does not stop the traffic endpoints; it shows up as 503 on the interconnect
endpoints only.

#### Consistency with the CLI

The CLI and the server can use the same interconnect ledger (`--state` and
`TOPO_MANAGER_IC_STATE`). Both take the same file lock (`<ledger>.lock`) and
reload the ledger under it, so a session created through one is visible through
the other and conflicts are detected across processes. The main-thread
deployment constraint and the event-loop blocking behavior described under
[Starting the server](#starting-the-server-two-equivalent-ways) apply to the
interconnect `POST` and `DELETE` endpoints as well.

The REST `DELETE` does not take the device lock that the CLI holds while it
configures a device. Do not release a session over REST while a CLI
`interconnect apply --apply` or `release --apply` for the same session is
running; if the device is left configured without a ledger entry, clean it up
with `interconnect park --apply`.

---

## Web UI

The same `topo-manager-api` server that exposes the REST API also serves a
lightweight browser UI on the side — open the server's root URL (`GET /`) in a
browser and you get a visual front end over the exact same engine and the exact
same JSON ledger as the REST API. It is not a second service: the Web UI, the
REST endpoints, and `/docs` are all served by one `topo-manager-api` (uvicorn)
process, sharing one state.

### Opening it

Start the server exactly as in [Starting the server](#starting-the-server-two-equivalent-ways)
(either `topo-manager-api` or `uvicorn "topo_manager.api:create_app" --factory`),
then point a browser at the server's root URL:

```
http://<host>:<port>/
```

where `<host>`/`<port>` are `TOPO_MANAGER_HOST` / `TOPO_MANAGER_PORT` (default
`0.0.0.0:8000`, so typically `http://localhost:8000/`). On one running server:

- `/` — the Web UI
- `/docs` — the Swagger / OpenAPI documentation (see [Documentation](#documentation-openapi))

### What it does

The UI is read-and-request oriented, mirroring what the REST API offers:

- **Topology view** — renders the fabric as a layered graph (generators on the
  left, fanout tiers in the middle, DUTs on the right). The layering is derived
  at runtime from the links (distance from the generators), not from the
  `role` label, so it draws two-tier, three-tier, and larger fabrics the same
  way. Hovering a device shows its connected ports.
- **Session highlight** — clicking a session highlights its end-to-end path
  (nodes and edges) on the topology graph; clicking it again clears the
  selection.
- **Filter sessions** — a text box filters the active-session list by id, VLAN,
  or port.
- **Create a session** — pick a generator port and a DUT port and allocate. The
  dropdowns list **only currently available ports** (those not already held by
  an active session), so you cannot pick a port that is taken.
- **Release a session** — release the selected session from its detail panel.
- **Auto-refresh (optional)** — a checkbox re-polls the server every 10 seconds;
  a manual **Refresh** button is always available.
- **Friendly errors** — API errors (e.g. a hard conflict on allocate) are shown
  as a readable banner rather than a raw status code or traceback.

### Scope: five endpoints, no new backend

The Web UI is a pure client of the **same five REST endpoints** documented in
[Endpoints and curl examples](#endpoints-and-curl-examples) (`GET /topology`,
`GET /resources/available`, `GET /sessions`, `POST /sessions`,
`DELETE /sessions/{id}`). It adds no new backend logic and no new endpoints.

Consequently, **pushing VLAN config to fanout switches** (`apply` / `verify` /
`setup`) is **not** in the Web UI — those remain **CLI-only**, consistent with
[Two independent entry points](#two-independent-entry-points). The UI can show
and request/release sessions, but device pushes stay on the CLI.
The [interconnect endpoints](#interconnect-endpoints) are likewise not used by
the UI: there is no interconnect tab, and the five endpoints above are the only
ones the page calls.

Writes triggered from the UI (allocate / release) go through the same POST /
DELETE handlers as the REST API, so the main-thread deployment constraint and
the single-process serialization behavior described under
[Starting the server](#starting-the-server-two-equivalent-ways) apply unchanged
— the UI does not add a separate write path or bypass the file lock.

---

## Design guarantees (universal multi-tier)

The tool does not hard-code the number of tiers, and it does not use the `role`
string to decide direction or layer:

- `fanout.role` (`core` / `spine` / `agg` / `leaf`, etc.) is only a free-form
  label and a summary grouping key. It takes no part in any direction or
  tier-level logic.
- Edge **direction** is derived from each link's distance to the generator;
  **tier level** is computed separately by a multi-source BFS on a contracted
  graph (all ports of one fanout contracted into a super-node). The two are
  independent.
- The same algorithm therefore handles two, three, or more tiers naturally (the
  `tiers` key sets above are `{0,1,2,3}` and `{0,1,2,3,4}`); adding a tier only
  means describing one more layer of links in the YAML.

Every node id is `<device>:<port>`. Each fanout also has a virtual hub
`<device>:#core` for intra-device connectivity, which the summary and path
outputs exclude.
