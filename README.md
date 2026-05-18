# SAIL — SRX / Morpheus Reconciliation System
## Technical Documentation

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Repository Structure](#3-repository-structure)
4. [Prerequisites & Installation](#4-prerequisites--installation)
5. [Playbook Reference — `sail_sync.yml`](#5-playbook-reference--sail_syncyml)
6. [Filter Plugin Reference — `topology_filters.py`](#6-filter-plugin-reference--topology_filterspy)
7. [Callback Plugin Reference — `loki_push.py`](#7-callback-plugin-reference--loki_pushpy)
8. [Configuration](#8-configuration)
9. [Test Suite](#9-test-suite)
10. [Observability](#10-observability)
11. [Security Considerations](#11-security-considerations)
12. [Troubleshooting](#13-troubleshooting)

---

## 1. Overview

**SAIL** (SRX Automated IP Lifecycle) is an Ansible-based reconciliation system that keeps a Juniper vSRX firewall continuously in sync with the desired network segmentation state defined in a Morpheus cloud management platform.

### Problem it solves

In a dynamic cloud environment, VMs are provisioned and decommissioned constantly. Without automation, firewall address-books drift from reality — stale IPs accumulate, new VMs are never added, and security zones become meaningless. SAIL closes this loop automatically.

### What it does — end to end

```
Morpheus (desired state)
        │
        │  REST API  ─── AppTier tags → zone_map
        ▼
  [SAIL playbook]
        │
        │  NETCONF   ─── address-book XML → srx_zone_map
        ▼
     vSRX (current state)
        │
        │  compute_deltas → to_add / to_remove per zone
        ▼
  junos_config SET/DELETE lines
        │
        ├── topology.json  ──► Grafana node-graph
        └── audit log      ──► fileserver /history
```

### Key design principles

- **Idempotent** — running the playbook multiple times when nothing has changed produces zero config lines and a no-op confirmation message.
- **Tag-driven** — zone membership is derived entirely from the `AppTier` Morpheus tag; no hardcoded zone lists.
- **Deny-by-default** — brand-new zones get an explicit deny-all policy in both directions before any permit rules are ever added.
- **HA-safe** — the Loki callback uses only Python stdlib and fires in a background thread; it never blocks playbook execution.
- **Observable** — every run publishes a topology snapshot to Grafana and an audit record to the fileserver history endpoint.

---

## 2. Architecture

### Component map

```
┌─────────────────────────────────────────────────────────────────┐
│  Morpheus CMP                                                   │
│  • Triggers SAIL as a workflow task                             │
│  • Provides vsrx_ip, vsrx_password, morpheus_token via inputs  │
│  • Tags VMs with AppTier=Web|DB|App|…                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │ REST (HTTPS)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Ansible control node (Morpheus agent / AWX / local)           │
│                                                                 │
│  sail_sync.yml                                                  │
│  ├── filter_plugins/topology_filters.py                        │
│  │     parse_srx_zone_map · compute_deltas · build_topology    │
│  └── callback_plugins/loki_push.py                             │
│        streams every task event → Loki                         │
└──────────┬──────────────────────────┬───────────────────────────┘
           │ NETCONF (port 830)       │ HTTP PUT/POST
           ▼                          ▼
┌──────────────────┐      ┌──────────────────────────────────────┐
│  Juniper vSRX    │      │  Fileserver (Python HTTP or nginx)   │
│  address-book    │      │  /topology.json  ← Grafana polls     │
│  MORPHEUS_MANAGED│      │  /history        ← audit log         │
└──────────────────┘      └──────────────────────────────────────┘
                                       │
                                       ▼
                          ┌──────────────────────────┐
                          │  Grafana                 │
                          │  Node Graph panel        │
                          │  (reads topology.json)   │
                          └──────────────────────────┘
                                       ▲
                          ┌──────────────────────────┐
                          │  Grafana Loki            │
                          │  (task-level log stream) │
                          └──────────────────────────┘
```

### Data flow summary

| Step | Source | Destination | Protocol |
|------|--------|-------------|----------|
| Fetch desired state | Morpheus `/api/instances` | `zone_map` fact | HTTPS REST |
| Fetch current state | vSRX running config | `srx_zone_map` fact | NETCONF XML |
| Enforce state | `delta_map` | vSRX config | NETCONF (junos_config) |
| Publish topology | `zone_map` + `delta_map` | fileserver `/topology.json` | HTTP PUT |
| Audit log | run metadata | fileserver `/history` | HTTP POST |
| Task telemetry | every task result | Grafana Loki | HTTP POST (async) |

---

## 3. Repository Structure

```
sail-ansible/
├── sail_sync.yml               # Main playbook (4 phases)
├── ansible.cfg                 # Ansible configuration (collections path, callback enable)
├── group_vars/
│   └── all.yml                 # Global variable defaults
├── collections/
│   └── requirements.yml        # ansible.netcommon + junipernetworks.junos
├── filter_plugins/
│   └── topology_filters.py     # Custom Jinja2 filters
├── callback_plugins/
│   └── loki_push.py            # Loki streaming callback
├── pytest.ini                  # pytest configuration
├── requirements-test.txt       # Test dependencies
└── tests/
    ├── __init__.py
    ├── conftest.py             # Shared fixtures
    └── unit/
        ├── __init__.py
        ├── test_topology_filters.py
        └── test_loki_push.py
```

---

## 4. Prerequisites & Installation

### Control node requirements

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | ≥ 3.9 | 3.14 tested |
| Ansible | ≥ 2.14 | `pip install ansible` |
| junipernetworks.junos | ≥ 5.3 | see below |
| ansible.netcommon | ≥ 5.0 | see below |
| ncclient | ≥ 0.6 | NETCONF transport for Junos |

### Install Ansible collections

```bash
ansible-galaxy collection install -r collections/requirements.yml
```

`collections/requirements.yml`:
```yaml
collections:
  - name: junipernetworks.junos
  - name: ansible.netcommon
```

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LOKI_URL` | No | Base URL of Loki instance, e.g. `http://10.202.52.109:3100` |

### vSRX requirements

- NETCONF must be enabled on port 830
- The `root` user (or a dedicated service account) must have NETCONF access
- The `MORPHEUS_MANAGED` address-book must exist (SAIL creates entries within it; it does not create the book itself on first run — add it manually or via day-0 automation)

---

## 5. Playbook Reference — `sail_sync.yml`

### Input variables

These are passed as task/workflow inputs from Morpheus (or as extra vars for manual runs):

| Variable | Type | Example | Description |
|----------|------|---------|-------------|
| `vsrx_ip` | string | `10.202.52.10` | Management IP of the vSRX |
| `vsrx_password` | string | `***` | Root password for the vSRX |
| `morpheus_ip` | string | `morpheus.example.com` | Morpheus appliance hostname or IP |
| `morpheus_token` | string | `eyJ…` | Morpheus Bearer token |
| `fileserver_url` | string | `http://10.202.52.109:8880` | Base URL of the topology fileserver |

Manual test run:
```bash
ansible-playbook sail_sync.yml \
  -e vsrx_ip=10.202.52.10 \
  -e vsrx_password=secret \
  -e morpheus_ip=morpheus.example.com \
  -e morpheus_token=eyJ... \
  -e fileserver_url=http://10.202.52.109:8880
```

---

### Phase 1 — Gather desired state from Morpheus

**Hosts:** `localhost`

Queries the Morpheus REST API for all instances tagged with `AppTier` and builds a `zone_map` dictionary.

#### Task: Register vSRX in inventory

Dynamically registers the vSRX host into the in-memory inventory so Phase 2 can target it by name (`vsrx`) without a static inventory file.

```yaml
ansible_connection:   netconf
ansible_network_os:   junos
ansible_netconf_port: 830
```

#### Task: Query Morpheus for AppTier-tagged instances

```
GET https://{morpheus_ip}/api/instances?tagName=AppTier&max=500
Authorization: Bearer {morpheus_token}
```

Returns up to 500 instances. Increase `max` if your environment has more.

#### Task: Build dynamic zone map

Produces `zone_map` — a dict where each key is an uppercased `AppTier` tag value and each value is a list of primary IPs:

```python
{
  "WEB": ["10.0.0.1", "10.0.0.2"],
  "DB":  ["10.0.1.1"],
  "APP": ["10.0.2.1", "10.0.2.2"]
}
```

Instances are skipped if they have no `AppTier` tag or no `connectionInfo[0].ip`.

---

### Phase 2 — Read actual state from SRX

**Hosts:** `vsrx`

Fetches the running configuration of the `MORPHEUS_MANAGED` address-book via NETCONF `get-config` and parses it into a dict matching the `zone_map` shape.

#### Task: Fetch address-book config via NETCONF

Issues a filtered `get-config` RPC scoped to:
```xml
<security>
  <address-book>
    <name>MORPHEUS_MANAGED</name>
  </address-book>
</security>
```

#### Task: Parse XML into current zone map

Calls the `parse_srx_zone_map` filter (see §6). Produces `srx_zone_map` with the same shape as `zone_map`.

#### Task: Compute per-zone deltas

Calls `compute_deltas` filter. Produces `delta_map`:

```python
{
  "WEB": { "to_add": ["10.0.0.3"], "to_remove": ["10.0.0.99"] },
  "DB":  { "to_add": [],           "to_remove": [] }
}
```

---

### Phase 2.5 — Promote vsrx facts to localhost

**Hosts:** `localhost`

Copies `delta_map` and `srx_zone_map` from `hostvars['vsrx']` back to localhost scope so Phases 3 and 4 (which run on localhost) can reference them.

This phase exists because Ansible facts set on one host are not automatically visible to tasks running on a different host.

---

### Phase 3 — Enforce desired state on SRX

**Hosts:** `vsrx`

Applies `delta_map` to the vSRX via `junos_config` SET/DELETE lines.

#### Task: Add IPs to address-book and zone set

For each `(zone, ip)` pair in `to_add`:

```
set security address-book MORPHEUS_MANAGED address {ip} {ip}/32
set security address-book MORPHEUS_MANAGED address-set SET_{ZONE} address {ip}
```

This is idempotent — Junos silently accepts duplicate SET lines.

#### Task: Apply deny-all policy for brand-new zones

Only fires when a zone is present in `delta_map` but was **completely absent** from `srx_zone_map` (i.e. first time this zone is seen). Applies:

```
set security policies from-zone {ZONE}_ZONE to-zone untrust policy DENY_ALL_OUT_{ZONE} …
set security policies from-zone untrust to-zone {ZONE}_ZONE policy DENY_ALL_IN_{ZONE} …
```

This ensures a misconfigured tag never exposes a VM before a human explicitly adds a permit policy.

#### Task: Remove stale IPs

For each `(zone, ip)` pair in `to_remove`:

```
delete security address-book MORPHEUS_MANAGED address-set SET_{ZONE} address {ip}
delete security address-book MORPHEUS_MANAGED address {ip}
```

#### Task: No-op confirmation

Emits a debug message if both `to_add` and `to_remove` are empty across all zones — confirms the run was truly zero-impact.

---

### Phase 4 — Publish topology snapshot

**Hosts:** `localhost`

#### Task: Build topology dict

Calls `build_topology(zone_map, delta_map, vsrx_ip)` to produce a Grafana node-graph compatible JSON structure (see §6).

#### Task: Push topology.json

```
PUT {fileserver_url}/topology.json
Content-Type: application/json
```

Grafana's node-graph panel polls this file. Accepts HTTP 200, 201, or 204.

#### Task: Send audit history log

```
POST {fileserver_url}/history
Content-Type: application/json

{
  "ts":         1716000000,
  "run_id":     "job-123456",
  "vsrx_ip":    "10.202.52.10",
  "changed":    true,
  "delta_map":  { … },
  "new_zones":  ["APP"],
  "duration_s": 2.5
}
```

---

## 6. Filter Plugin Reference — `topology_filters.py`

Location: `filter_plugins/topology_filters.py`

All filters are pure Python functions with no Ansible runtime dependency, making them independently testable.

---

### `parse_srx_address_book(xml_text, set_name)`

Parses a Junos NETCONF `get-config` XML response and returns the list of IP address names belonging to a single named address-set.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `xml_text` | `str \| None` | Raw XML string from NETCONF output |
| `set_name` | `str` | Exact address-set name, e.g. `SET_WEB` |

**Returns:** `List[str]` — IP address names (e.g. `["10.0.0.1", "10.0.0.2"]`), or `[]` on empty/malformed input.

**Jinja2 usage:**
```jinja2
{{ srx_xml_raw.output | parse_srx_address_book('SET_WEB') }}
```

**Error handling:** Returns `[]` (never raises) for `None`, empty string, whitespace-only, or malformed XML. Safe for first runs against a clean SRX.

---

### `parse_srx_zone_map(xml_text)`

Parses the full `MORPHEUS_MANAGED` address-book XML and returns a complete zone → IPs mapping. Strips the `SET_` prefix from address-set names and uppercases zone names to match Morpheus tag values.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `xml_text` | `str \| None` | Raw XML string from NETCONF output |

**Returns:** `Dict[str, List[str]]`

```python
{
  "WEB": ["10.0.0.1", "10.0.0.2"],
  "DB":  ["10.0.1.1"]
}
```

**Jinja2 usage:**
```jinja2
{{ srx_xml_raw.output | parse_srx_zone_map }}
```

**Error handling:** Returns `{}` on any error. A brand-new SRX with no address-book entries will cause all desired IPs to land in `to_add`.

---

### `compute_deltas(desired, current)`

Compares the desired state (from Morpheus) against the current state (from the SRX) and computes per-zone add/remove lists.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `desired` | `Dict[str, List[str]]` | `zone_map` from Morpheus |
| `current` | `Dict[str, List[str]]` | `srx_zone_map` from NETCONF |

**Returns:** `Dict[str, Dict[str, List[str]]]`

```python
{
  "WEB": { "to_add": ["10.0.0.3"], "to_remove": ["10.0.0.99"] },
  "DB":  { "to_add": [],           "to_remove": [] },
  "APP": { "to_add": ["10.0.2.1"], "to_remove": [] }  # brand-new zone
}
```

**Behaviour:**
- Zones in `desired` but not in `current` → brand-new zone, all IPs in `to_add`
- Zones in `current` but not in `desired` → orphaned zone, all IPs in `to_remove`
- Output lists are always sorted for deterministic Junos config line ordering
- Duplicate IPs in input are deduplicated (set semantics)

**Jinja2 usage:**
```jinja2
{{ zone_map | compute_deltas(srx_zone_map) }}
```

---

### `build_topology(args)`

Builds a Grafana node-graph compatible topology dict from the current zone map and delta information.

**Parameters** (passed as a tuple via Jinja2 pipe):

| Position | Name | Type | Description |
|----------|------|------|-------------|
| `args[0]` | `zone_map` | `Dict[str, List[str]]` | Desired zone state from Morpheus |
| `args[1]` | `delta_map` | `Dict[str, Dict]` | Per-zone add/remove lists |
| `args[2]` | `vsrx_ip` | `str` | vSRX management IP (shown as node subtitle) |

**Returns:**
```python
{
  "nodes": [ { "id": …, "title": …, "subTitle": …, "mainStat": …, "color": … }, … ],
  "edges": [ { "id": …, "source": …, "target": … }, … ]
}
```

**Node types and colours:**

| Node | ID format | Color | mainStat |
|------|-----------|-------|----------|
| vSRX anchor | `srx` | orange | `Firewall` |
| Zone tier | `zone_{lower}` | blue/purple/green/yellow/semi-dark-red (cycled) | `{n} VMs` |
| Active VM (unchanged) | `{ip}` | green | `IN SYNC` |
| Active VM (just added) | `{ip}` | red | `DRIFT FIXED` |
| Removed VM ghost | `removed-{zone}-{ip}` | red | `REMOVED` |

Ghost nodes for removed IPs are included so the Grafana panel shows what was just cleaned up, making the run's effect visible at a glance.

**Jinja2 usage:**
```jinja2
{{ (zone_map, delta_map, vsrx_ip) | build_topology }}
```

---

## 7. Callback Plugin Reference — `loki_push.py`

Location: `callback_plugins/loki_push.py`

A notification-type Ansible callback plugin that streams task-level telemetry to Grafana Loki's HTTP push API. Uses only Python stdlib — no `requests` or other third-party dependencies.

### Configuration

Via `ansible.cfg`:
```ini
[defaults]
callback_plugins    = ./callback_plugins
callbacks_enabled   = loki_push

[callback_loki_push]
loki_url    = http://10.202.52.109:3100
loki_timeout = 5
```

Or via environment variable (takes precedence over `ansible.cfg` during `__init__`):
```bash
export LOKI_URL=http://10.202.52.109:3100
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `loki_url` | string | — | Base URL of the Loki instance, no trailing slash |
| `loki_timeout` | int | `5` | HTTP request timeout in seconds |

### Proxy bypass

Morpheus injects corporate proxy settings (`http_proxy`, `HTTP_PROXY`) into the environment before Ansible starts. Python's `urllib` caches proxy config early, which would cause Loki pushes to be routed through the proxy and fail.

SAIL patches this by:
1. Reading `LOKI_URL` in `__init__` before `super().__init__()` runs
2. Extracting the bare Loki IP from the URL
3. Appending it to both `no_proxy` and `NO_PROXY` environment variables

This patch fires at the earliest possible moment and is re-applied in `set_options()` as a belt-and-suspenders measure.

### Async push

All Loki pushes are fire-and-forget: `_push()` spawns a daemon thread that calls `_push_sync()`. If Loki is unavailable or the push fails, the exception is caught, logged at `-vvv` verbosity, and the playbook continues unaffected.

### Loki stream labels

Every log line is pushed with these labels:

| Label | Value | Always present |
|-------|-------|---------------|
| `job` | `ansible` | ✓ |
| `project` | `SAIL` | ✓ |
| `playbook` | playbook filename | ✓ |
| `play` | current play name | ✓ |
| `task` | task name | ✓ |
| `status` | `ok` / `changed` / `failed` / `skipped` / `unreachable` / `started` | ✓ |
| `host` | target host name | when available |
| `role` | role name | when task belongs to a role |

Empty-string label values are excluded from the stream to keep Loki cardinality low.

### Hooks implemented

| Hook | Trigger | Status label |
|------|---------|-------------|
| `v2_playbook_on_start` | Playbook begins | `started` |
| `v2_playbook_on_play_start` | Each play begins | *(sets internal state only)* |
| `v2_playbook_on_stats` | Playbook ends | `ok` or `failed` |
| `v2_runner_on_ok` | Task succeeds | `ok` or `changed` |
| `v2_runner_on_failed` | Task fails | `failed` |
| `v2_runner_on_skipped` | Task skipped | `skipped` |
| `v2_runner_on_unreachable` | Host unreachable | `unreachable` |

### Canary log line

On playbook start, a canary line is pushed:
```
[SAIL] loki_push loaded OK | target=http://…:3100 | playbook=sail_sync.yml
```
If this appears in Grafana, the plugin loaded correctly and the proxy bypass is working end-to-end.

---

## 8. Configuration

### `ansible.cfg` (minimal working example)

```ini
[defaults]
collections_path    = ./collections
callback_plugins    = ./callback_plugins
callbacks_enabled   = loki_push
filter_plugins      = ./filter_plugins
host_key_checking   = False

[callback_loki_push]
loki_url    = http://10.202.52.109:3100
loki_timeout = 5
```

### `group_vars/all.yml`

Holds variable defaults shared across all hosts. Sensitive values (tokens, passwords) should never be stored here in plaintext — use Morpheus workflow inputs or Ansible Vault.

---

## 9. Test Suite

### Philosophy

The test suite targets the two custom Python components — `topology_filters.py` and `loki_push.py` — which contain all the project-specific logic and are the most likely source of regressions. Ansible built-in modules (`uri`, `junos_config`, `netconf_get`) and third-party collections are not tested; they are maintained by their respective projects.

### Requirements

```bash
pip install -r requirements-test.txt
# pytest>=8.0, pytest-mock>=3.14
```

A virtual environment is strongly recommended:

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows
source .venv/bin/activate     # Linux / macOS
pip install -r requirements-test.txt
```

### Running the tests

```bash
# All tests
pytest

# Verbose output
pytest -v

# Specific file
pytest tests/unit/test_topology_filters.py

# Specific class
pytest tests/unit/test_topology_filters.py::TestComputeDeltas

# Specific test
pytest tests/unit/test_topology_filters.py::TestComputeDeltas::test_brand_new_zone

# Stop on first failure
pytest -x
```

### No Ansible install required

Both test files are designed to run without Ansible installed:

- `topology_filters.py` has zero Ansible imports — tests import it directly via `sys.path`
- `loki_push.py` depends on `ansible.plugins.callback.CallbackBase` — the test file stubs this out via `sys.modules` before importing the plugin

This makes the test suite runnable in any CI environment or developer machine with just Python and pytest.

### Test coverage

#### `test_topology_filters.py` — 41 tests

| Class | Tests | What is covered |
|-------|-------|-----------------|
| `TestParseSrxAddressBook` | 8 | Happy path, missing set, empty/None/malformed/whitespace XML, no XML declaration |
| `TestParseSrxZoneMap` | 8 | All zones returned, SET_ prefix stripping, correct IPs, empty/None/malformed XML, no sets, empty set |
| `TestComputeDeltas` | 9 | No change, add, remove, brand-new zone, orphaned zone, empty maps, mixed multi-zone, sorted output, duplicate IP dedup |
| `TestBuildTopology` | 14 | Return shape, SRX anchor node, zone tier nodes, VM nodes, drift/in-sync colour, ghost removed nodes, all edge types, empty zone map, VM count stat, no duplicate IDs |
| `TestFilterModule` | 2 | All 4 filters registered, all are callable |

#### `test_loki_push.py` — 34 tests

| Class | Tests | What is covered |
|-------|-------|-----------------|
| `TestExtractIpFromUrl` | 6 | IP+port, hostname, no port, HTTPS, empty string, garbage input |
| `TestPatchNoProxy` | 4 | Add to empty, no dedup, append to existing, empty IP noop |
| `TestPushSync` | 5 | Correct JSON + endpoint, None URL noop, network error swallowed, extra labels merged, empty labels excluded |
| `TestPushAsync` | 1 | Background thread fires |
| `TestTaskLabels` | 3 | Basic labels, role present, role absent |
| `TestResultMsg` | 6 | `msg` field, `stdout_lines`, stdout_lines over stdout priority, stderr, empty dict, non-dict result |
| `TestCallbackHooks` | 9 | All 7 hooks fire `_push`, correct status labels, per-host stats, failed host detection |

### Adding new tests

Follow the existing class-per-function convention. Place new filter tests in `test_topology_filters.py` and new callback tests in `test_loki_push.py`. Shared fixtures go in `tests/conftest.py`.

---

## 10. Observability

### Grafana node-graph panel

The topology panel visualises the current zone structure and the effect of the last run:

- **Green nodes** — VMs in sync with Morpheus
- **Red DRIFT FIXED nodes** — VMs that were just added this run
- **Red REMOVED nodes** — VMs that were just removed this run
- **Zone tier nodes** — coloured by zone index (blue → purple → green → yellow → dark-red, cycling)
- **Orange SRX node** — the vSRX anchor

Data source: `GET {fileserver_url}/topology.json`

### Grafana Loki log stream

Query examples:

```logql
# All SAIL events
{job="ansible", project="SAIL"}

# Failed tasks only
{job="ansible", project="SAIL", status="failed"}

# Specific playbook run
{job="ansible", project="SAIL", playbook="sail_sync.yml"}

# Changes on a specific host
{job="ansible", project="SAIL", host="vsrx", status="changed"}
```

### Audit history endpoint

`GET {fileserver_url}/history` returns a list of run records for drift trending and compliance reporting. Each record includes `ts`, `run_id`, `changed`, `delta_map`, `new_zones`, and `duration_s`.

---

## 11. Security Considerations

### Credentials

- `vsrx_password` and `morpheus_token` are passed as Morpheus workflow inputs and never written to disk
- Use Ansible Vault if running outside Morpheus: `ansible-vault encrypt_string 'secret' --name vsrx_password`
- The `MORPHEUS_MANAGED` address-book is namespaced — SAIL never touches any other address-book or policy set

### Certificate validation

Both the Morpheus API call and the fileserver push use `validate_certs: no`. This is intentional for internal-only infrastructure with self-signed certificates. **Enable certificate validation** (`validate_certs: yes`) if your Morpheus instance has a trusted certificate.

### Deny-by-default for new zones

Any zone that appears for the first time receives an explicit deny-all policy in both directions before any permit rules can be added. This prevents a misconfigured `AppTier` tag from silently opening traffic.

### Loki transport

Loki pushes are fire-and-forget over plain HTTP. If your Loki instance requires authentication or TLS, extend `_push_sync()` to add the appropriate headers.

---

## 12. Troubleshooting

### Loki canary line not appearing in Grafana

1. Check `LOKI_URL` is set and reachable from the control node
2. Run with `-vvv` — `_push_sync` logs failures at that verbosity
3. Verify `callbacks_enabled = loki_push` in `ansible.cfg`
4. Check whether a corporate proxy is intercepting the connection — the canary line confirms the `no_proxy` patch worked

### `parse_srx_zone_map` returns `{}`

- The NETCONF `get-config` response may be empty if the `MORPHEUS_MANAGED` address-book doesn't exist yet on the SRX — create it manually for the first run
- Check `srx_xml_raw.output` with a debug task to inspect the raw XML

### Zone IPs not being added

- Confirm the Morpheus instances have both an `AppTier` tag and a populated `connectionInfo[0].ip`
- Run with `-e ansible_verbosity=3` to see the raw Morpheus API response
- Check `zone_map` in the Phase 1 debug output

### `junos_config` task fails

- Verify NETCONF is enabled on the vSRX: `show system services`
- Confirm port 830 is reachable: `Test-NetConnection {vsrx_ip} -Port 830`
- Check the `ncclient` package is installed in the Ansible Python environment

### Tests fail after modifying filter code

Run `pytest -v --tb=long` for full tracebacks. The most common causes are:

- Changed return type (e.g. `None` instead of `[]` on error)
- Changed label key name in `_task_labels`
- Modified the Loki push payload structure