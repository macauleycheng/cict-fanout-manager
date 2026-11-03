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
| `TOPO_MANAGER_HOST` | `0.0.0.0` | `topo-manager-api` bind host |
| `TOPO_MANAGER_PORT` | `8000` | `topo-manager-api` bind port |

If `TOPO_MANAGER_TOPOLOGY` is missing, the topology is invalid, or the VLAN
range is illegal, the app **fails fast at startup** (one stderr line, non-zero
exit code, no traceback) rather than returning 500 on every request.

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
> thread, or the writing endpoints (POST/DELETE) will fail to acquire the lock.
> To use multiple cores, scale out horizontally with multiple processes (e.g.
> several uvicorn/gunicorn worker processes, each with its own main thread);
> cross-process consistency is guaranteed by the file lock.

> **Liveness characteristic (event-loop blocking):** the write endpoints'
> `async` handlers call `allocator.allocate/release` directly on the main-thread
> event loop, where `fcntl.flock(LOCK_EX)` is a **blocking** system call. The
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
