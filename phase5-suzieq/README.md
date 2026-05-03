# Phase 5 - Suzieq Operational State Monitoring + NetBox Drift Detection

Continuous runtime monitor of the DC1 fabric, complement to Phase 2's deploy-time smoke suite. Three goals: continuous state observation, NetBox-vs-live-state drift, and a queryable time-series record. This README covers **Part A only** (the SuzieQ stack on netdevops-srv). Drift harness, strict assertions, and time-window queries land in subsequent parts.

**Run from:** anywhere with `pip install pytest PyYAML pandas pyarrow pynetbox`. Live tests need `SUZIEQ_LIVE_PARQUET_DIR` pointing at a real parquet store. **Tests:** `cd phase5-suzieq && pytest` (362 default tests, ~4 s, fully offline). Add `-m live` (with the env var set) for the 12 live schema-guard tests. **Depends on:** the SuzieQ stack deployed on netdevops-srv (compose stack in this directory) for runtime monitoring; the test suite itself depends on nothing live.

## What Part A delivers

A docker-compose stack on the netdevops services VM running three SuzieQ processes against one shared parquet store:

| Service | Role |
|---|---|
| `sq-poller` | Polls DC1 devices over SSH, writes to parquet |
| `sq-coalescer` | Compacts small parquet files. Not optional - without it the store fragments |
| `sq-rest-server` | REST API for ad-hoc operator queries (lab-only `--no-https`, see banner below) |

Devices are NOT pulled from SuzieQ's native NetBox source plugin. Instead, [`gen-inventory.py`](gen-inventory.py) builds a static SuzieQ native inventory from NetBox at deploy time. The reason and trade-off are documented in the script's docstring; in short: SuzieQ's NetBox plugin reads `primary_ip4.address` (the loopback in this project, unreachable from netdevops-srv), and there is no override hook. The deploy-time generator reads `oob_ip` instead. NetBox stays the source of truth; device adds/removes need a script re-run + `restart sq-poller`.

Credentials come from `evpn-lab-env/env.sh` via SuzieQ's `env:VARNAME` syntax (the URL itself uses Python `urlparse()` and does not support `env:`, so the script writes the literal IP at generate time). Nothing committed in this directory contains a hostname, IP, password, or token.

## REST TLS - lab vs production

> **`--no-https` is set on the rest server's command line in [docker-compose.yml](docker-compose.yml) and is acceptable here ONLY because the REST port is bound on the netdevops-srv private mgmt interface inside the lab mgmt segment.** Upstream SuzieQ documentation explicitly discourages `--no-https` outside that exact scenario.
>
> Note: on the pinned digest, `no-https:` in `suzieq.cfg` is a no-op for the rest server - it only honors `--no-https` as a CLI flag. The TLS posture is therefore controlled in `docker-compose.yml`, not `suzieq.cfg`.
>
> Production migration path:
> - Set `rest-keyfile` and `rest-certfile` in `suzieq.cfg` (commented placeholders are already there) AND drop the `--no-https` flag from `docker-compose.yml`, OR
> - Front the REST server with a TLS-terminating reverse proxy (nginx, Caddy, Traefik) and remove the host port mapping from `docker-compose.yml` so the container is reachable only from the proxy.

## Required environment variables

All sourced from `evpn-lab-env/env.sh` (outside the repo, never committed):

| Variable | Used by | Purpose |
|---|---|---|
| `JUNOS_SSH_USER` | `inventory.yml` auth block (`env:` resolved at startup) | Junos SSH username (same as Phase 3 Nornir) |
| `JUNOS_SSH_PASSWORD` | `inventory.yml` auth block (`env:` resolved at startup) | Junos SSH password (same as Phase 3 Nornir) |
| `NETBOX_URL` | `gen-inventory.py` (NetBox API at deploy time) | NetBox base URL |
| `NETBOX_TOKEN` | `gen-inventory.py` (NetBox API at deploy time) | NetBox API token (read-only sufficient) |
| `SUZIEQ_API_KEY` | `suzieq.cfg` rest section (`env:` resolved at startup) | API key clients present to the REST server |

> **You MUST `source evpn-lab-env/env.sh` in the same shell that runs every `docker compose ...` command on netdevops-srv.** Compose substitutes `${VAR}` at command time, not at container runtime, so a missed `source` makes containers come up with empty credentials. The substitution warnings (`The "FOO" variable is not set`) on `docker compose ps` are harmless if the *up* invocation had the env, but that exact warning during `up` means you'll have non-functional containers.

## NetBox tag setup

Phase 1 [`netbox-data.yml`](../phase1-netbox/netbox-data.yml) now creates a `Suzieq` tag and applies it to `dc1-spine1`, `dc1-spine2`, `dc1-leaf1`, `dc1-leaf2`. If you populated NetBox from an older snapshot of `netbox-data.yml`, restore the clean baseline and re-run `populate.py` (see [phase1-netbox/README.md](../phase1-netbox/README.md)). The Phase 5 tag is queried by `gen-inventory.py`; without it the script exits with `ERROR: no devices with tag 'suzieq' in NetBox`.

When DC2 lands in Phase 10, tag its devices similarly (the Phase 1 model can either reuse `suzieq` or add a per-DC tag). `gen-inventory.py` will fan out automatically because it groups by `(site.slug, devtype)`.

## `junos-vjunos-switch` devtype (project-owned, added at image build time)

The lab runs vJunos-switch (the vrnetlab image `juniper_vjunos-switch:23.2R1.14`) which needs an unusual mix of SuzieQ service templates that no built-in devtype provides:

| SuzieQ service | What vJunos needs | Built-in devtype that has it |
|---|---|---|
| `device` (uptime/serial parsing) | single-RE JSON shape (no `multi-routing-engine-results` wrapper) | `junos-mx` only |
| `lldp` (neighbor table) | `show lldp neighbors **detail** \| display json` (the summary view omits `lldp-remote-port-id`) | `junos-qfx` only |
| Everything else | mostly identical to `junos-mx` | `junos-mx` |

Phase 5 Part A originally worked around this by setting `devtype: junos-mx` and accepting that LLDP would have empty `peerIfname` (with a Tier B fallback in the drift harness). Phase 5 Part B does it properly: a project-owned **`junos-vjunos-switch`** devtype, added to the SuzieQ service catalog at image build time by [suzieq-image/add-vjunos-switch.py](suzieq-image/add-vjunos-switch.py), running as a `RUN` step in [suzieq-image/Dockerfile](suzieq-image/Dockerfile).

### Why a project-owned devtype, not a mutation of `junos-mx`

Mutating `junos-mx` to use the LLDP detail view would pollute the meaning of the built-in devtype for any future real Juniper MX hardware in Phase 10+. The `junos-vjunos-switch` name is purely additive: stock `junos-mx`, `junos-qfx`, `junos-ex`, etc. are left byte-identical to upstream. The patcher is tested to verify this (`test_lldp_patch_does_not_mutate_junos_mx_block` and `test_preserves_upstream_devtype_blocks` in [tests/test_suzieq_patcher.py](tests/test_suzieq_patcher.py)).

### Why build-time and not runtime

The patcher runs ONCE inside `RUN python3 ...` at `docker compose build` time. The result is a new image layer (`evpn-lab/suzieq-patched:dev`) with the patches baked in. Container start does **nothing extra** - the upstream entrypoint is unchanged, no script runs at runtime, no entrypoint wrapper, no per-start re-application. Re-running `docker compose up -d` against the existing image is a zero-op for the patcher.

Upgrade procedure: bump the `FROM` digest in `suzieq-image/Dockerfile`, run `docker compose build`, run `docker compose up -d`. The patcher re-runs against the new upstream content; if upstream restructured a yaml or Python source in a way the patcher cannot handle, the build **FAILS LOUDLY** (every patch function has a "marker missing" hard-fail). Silent masking of upstream changes is the bug class this approach exists to avoid.

### What the patcher actually patches

Discovered during Phase 5 Part B by grepping the upstream image for every devtype validation, dispatch, and behavioral-branch site:

| Patch site | What it does | Why we need it |
|---|---|---|
| `config/lldp.yml` | Adds explicit `junos-vjunos-switch` block using `show lldp neighbors detail` and extracting `lldp-remote-port-id` | Makes `peerIfname` populate for LLDP rows; this is the load-bearing fix |
| `config/{12 other yamls}` | Adds `junos-vjunos-switch: copy: <resolved-base>` | All other services share `junos-mx`'s shape; the patcher walks the `copy:` chain at PATCH time because SuzieQ's resolver only follows ONE level (so a naive `copy: junos-mx` chained-through-junos-mx-itself would fail validation) |
| `shared/utils.py` `known_devtypes()` allowlist | Adds `'junos-vjunos-switch'` to the hardcoded list | Without this, every node init raises `ValueError("An unknown devtype...")` |
| `node.py:1969` multi-RE wrapper list | Adds `"junos-vjunos-switch"` to `["junos-mx", "junos-qfx10k", "junos-evo"]` | `JunosNode._parse_init_dev_data_devtype` walks `multi-routing-engine-results[0]/...` for everything NOT in this list; vJunos returns single-RE shape so we must opt out of wrapper extraction |

The class dispatch site `node.py:541 elif self.devtype.startswith("junos")` automatically routes `junos-vjunos-switch` to `JunosNode` because the name starts with `"junos"` - no patch needed there. Same goes for the 4 other `startswith("junos")` checks in `evpnVni.py`, `routes.py`, `service.py`, `devconfig.py`.

The naming is deliberate: `junos-vjunos-switch` exactly mirrors the vrnetlab image name (`juniper_vjunos-switch`), starts with `junos` to satisfy the dispatch checks without patching them, and clearly distinguishes from real `junos-ex` hardware.

### Tests

[tests/test_suzieq_patcher.py](tests/test_suzieq_patcher.py) - 25 unit tests covering:
- `resolve_base_devtype()` (the `copy:` chain walker that sidesteps SuzieQ's one-level resolver)
- `patch_simple_copy_yaml()` (the 12 simple-case service yamls)
- `patch_lldp_yaml()` (the load-bearing detail-view block)
- `patch_known_devtypes()` (`shared/utils.py` allowlist)
- `patch_node_multi_re_list()` (`node.py` single-RE wrapper list)
- `main()` orchestration
- Idempotency for every patch (re-running on already-patched files is a no-op)
- "Fail loudly" guard tests for every marker (if upstream restructures, the build dies with a clear FATAL error rather than silently producing a broken image)
- Regression guard `test_dereferences_chain_when_junos_mx_is_itself_a_copy` pinning the chain-resolution fix for the devconfig.yml / bgp.yml / fs.yml class of files where `junos-mx` is itself `copy: junos-qfx`

All run in <1 second, no docker, no SuzieQ install, no network - fixture yamls in `tmp_path`.

## Deployment

Run on the netdevops services VM (same host as NetBox and Batfish):

```bash
# 0. Always source the env file in the same shell that runs compose.
source /path/to/evpn-lab-env/env.sh

# 1. (One-time) Resolve and pin the image digest if you ever bump it.
#    The current pin is 6e4e955a... (juneneglabs/suzieq, april 2026).
docker pull netenglabs/suzieq:latest
docker image inspect netenglabs/suzieq:latest \
  --format '{{index .RepoDigests 0}}'
# Copy the sha256:... value into ALL THREE `image:` lines in
# docker-compose.yml (sq-poller, sq-coalescer, sq-rest-server).

# 2. Generate the inventory from NetBox.
#    Re-run this whenever devices are added/removed in NetBox.
#    Does not need network access from netdevops-srv to your dev box;
#    just needs NETBOX_URL/NETBOX_TOKEN in the environment.
python3 gen-inventory.py > inventory.yml

# 3. Copy the four files to the host.
mkdir -p /opt/suzieq && cd /opt/suzieq
# scp docker-compose.yml suzieq.cfg inventory.yml gen-inventory.py here

# 4. (One-time, first deploy ever) Fix volume permissions.
#    The image runs as uid 1000 (suzieq); a fresh docker volume
#    starts root-owned and the poller cannot write to it.
docker volume create suzieq_parquet
docker volume create suzieq_archive
docker run --user root \
  -v suzieq_parquet:/parquet -v suzieq_archive:/archive \
  --rm --entrypoint /bin/bash netenglabs/suzieq:latest \
  -c "chown -R 1000:1000 /parquet /archive"

# 5. Pinned-version syntax-check gate (MANDATORY before `up`).
#    Catches any drift between docs and the pinned digest BEFORE
#    you commit data. The pinned digest supports --syntax-check.
docker compose run --rm sq-poller \
  -I /suzieq/inventory.yml -c /suzieq/suzieq.cfg --syntax-check
# expect: "Inventory syntax check passed"

# 6. Bring the stack up.
docker compose up -d
docker compose ps   # all three services should reach (healthy)
```

## Verification gate

Part A is **not** considered complete until all of the following pass. All commands run from netdevops-srv:

```bash
# 1. All three services healthy
docker compose ps
# expect: sq-poller, sq-coalescer, sq-rest-server all "Up (healthy)"

# 2. NetBox-derived inventory resolved 4 devices into namespace dc1
docker exec sq-poller suzieq-cli device show
# expect: 4 rows (dc1-spine1/2, dc1-leaf1/2), status=alive, model=ex9214

# 3. Poller is keeping up - pollExcdPeriodCount must be 0 across all services
docker exec sq-poller python3 -c "
from suzieq.sqobjects.sqPoller import SqPollerObj
df = SqPollerObj().get(namespace=['dc1'])
print('max pollExcdPeriodCount:', df['pollExcdPeriodCount'].max(), '| rows:', len(df))
"
# expect: max 0, rows ~60

# 4. REST API reachable from a dev box on the lab mgmt segment.
#    Host port is 8443 (NOT 8000 - that's NetBox on the same VM).
curl -s -H "access_token: $SUZIEQ_API_KEY" \
  "http://$SUZIEQ_HOST:8443/api/v2/device/show?namespace=dc1" | head -c 500
# expect: JSON array with 4 device objects

# 5. Parquet store has data
docker exec sq-poller du -sh /suzieq/parquet
# expect: non-zero, growing across successive checks

# 6. (Bonus) BGP is healthy enough to prove the rest of the services
#    are also collecting correctly
docker exec sq-poller python3 -c "
from suzieq.sqobjects.bgp import BgpObj
df = BgpObj().get(namespace=['dc1'])
print('total sessions:', len(df), '| Established:', (df.state=='Established').sum())
"
# expect: 16 / 16 on a healthy DC1 fabric
```

If checks 2 or 3 fail on a 4-device fabric, **something is wrong**. Either the pinned image has a regression, the lab is reachable but slow, or the AAA knobs in `suzieq.cfg` are too aggressive. Do not declare Part A done until both are clean.

## Things that broke during Part A bring-up (and how)

Documented because the same potholes will trip the next operator:

| Symptom | Root cause | Fix |
|---|---|---|
| `bash sq-poller: import: command not found` | Image entrypoint is `/bin/bash`, not the binaries; `command: ["sq-poller", ...]` ran the python script through bash | Explicit `entrypoint: ["/usr/local/bin/sq-poller"]` per service in docker-compose.yml |
| `sq-coalescer` errors on `--run-once False` | `--run-once` is a boolean flag, not a key/value pair | Omit it for run-forever mode |
| `Invalid config file: Cannot load REST API KEY` on the *poller* | suzieq.cfg parsing happens in EVERY service, including the poller, even though only the rest server serves REST | `SUZIEQ_API_KEY` env var must be set on poller and coalescer too, not only the rest server |
| `Data directory /suzieq/parquet is not an accessible dir` | Fresh docker volume is root-owned; container runs as uid 1000 | One-time `chown -R 1000:1000` via `--user root` (see step 4 of Deployment) |
| `Unable to parse hostname env:NETBOX_URL` | SuzieQ NetBox source plugin's `url` field uses `urlparse()` directly and does NOT support `env:` syntax (only `token`/`username`/`password`/`API_KEY` do) | `gen-inventory.py` writes the literal URL at generate time |
| Empty `device show` despite parquet files on disk | `gen-inventory.py` writes the literal URL at generate time, but the SuzieQ NetBox source uses `primary_ip4` (the loopback in this project) which is unreachable from netdevops-srv | Switched to native source generated from NetBox, using `oob_ip` |
| `Host key is not trusted for host <device-oob-ip>` for 3 of 4 devices | First device's key gets accepted into a fresh known_hosts; the rest get rejected. vJunos containers regenerate keys on every cold boot anyway | Lab default: `ignore-known-hosts: true` via `gen-inventory.py` (lab convenience). Production: set `SUZIEQ_STRICT_HOST_KEYS=1` in the environment before running `gen-inventory.py` - the script will flip `ignore-known-hosts: false` and the operator is then responsible for provisioning known_hosts via configuration management. See "Production note" section below. |
| `Processing data failed for service device ... KeyError: 'bootupTimestamp'` | `junos-qfx` / `junos-ex` device template expects multi-routing-engine wrapper that vJunos-switch does not produce | Project-owned `junos-vjunos-switch` devtype added by build-time patcher (see "junos-vjunos-switch devtype" section); originally Phase 5 Part A worked around this with `junos-mx` but Part B did it properly |
| `device show` empty after re-deploy even though `pd.read_parquet` returns data | `suzieq-cli` reads `~/.suzieq/suzieq-cfg.yml` (default config) which has `data-directory: ./parquet`; our config lives at `/suzieq/suzieq.cfg` | Mount `./suzieq.cfg` at BOTH `/suzieq/suzieq.cfg` AND `/home/suzieq/.suzieq/suzieq-cfg.yml` |
| REST API returns "Connection reset by peer" | Default rest server bind address is `127.0.0.1`, not reachable through Docker port mapping | `address: 0.0.0.0` in `suzieq.cfg` rest section |
| Port 8000 collision on `docker compose up` | NetBox already runs on 8000 on the same VM | Host port mapped to 8443 instead |
| `sq-poller` reports `(unhealthy)` even though polling and parquet writes are working fine | Two bugs in the original parquet-based healthcheck: (a) wrong case `sqpoller` vs actual `sqPoller`, and (b) every coalescer cycle deletes raw files after compaction (confirmed at `pq_coalesce.py:71`) so the dir would be empty for ~minutes per hour, false-flapping the check | Switched to a `pgrep -f sq-poller` liveness check that mirrors the coalescer's pattern. Process alive = healthy |

## Production note (not lab guidance)

The lab cuts corners that production deployments should not:

- **Dedicated read-only user.** Lab reuses Phase 3's `JUNOS_SSH_USER` / `JUNOS_SSH_PASSWORD`. Production must create a dedicated user (e.g. `suzieq-ro`) bound to a Junos login class restricted to `view` permissions only. SuzieQ never needs configuration mode and never needs to commit anything.
- **AAA rate-limiting.** Lab uses local users so the `max-cmd-pipeline`, `retries-on-auth-fail`, and `per-cmd-auth` knobs in `suzieq.cfg` are protective rather than load-bearing. Production with TACACS+ or RADIUS must tune these against the AAA backend's rate limits - upstream SuzieQ ships a "Rate Limiting AAA Server Requests" document with the specific guidance.
- **Sizing.** Single worker, single namespace is correct here (4 devices). Upstream rule of thumb is "<= 40 devices per worker" and "workers <= namespaces"; multi-DC and multi-region deployments need a worker per namespace and possibly multiple workers per large namespace.
- **Coalescer storage budget.** Measured on the lab 2026-04-07 with `coalescer.period: 1h` and the archive directory enabled:
  - Live working set: ~150 MB peak (the 1h raw-buffer fills to ~120 MB before compaction, then drops back to ~30 MB)
  - Coalesced state tables (bgp/lldp/device/interfaces/routes): ~5-10 MB/day - grows only when state changes
  - Coalesced poll-stats (`sqPoller`): ~5 MB/day - grows linearly with poll cycles
  - Archive directory (`.tar.bz2` of pre-compacted raw files): ~20-30 MB/day - bz2 compression on poll-stats is excellent
  - **Lab steady-state: ~30-40 MB/day total. 30 days ~= 1 GB. 1 year ~= 12 GB.**
  - On netdevops-srv (51 GB free at last measurement) this leaves multi-year headroom for the 4-device lab.
  - **There is no built-in retention.** sq-coalescer compacts and deletes raw files (confirmed in `pq_coalesce.py:71` `os.remove(x)` after the tarball write), but the archive `.tar.bz2` files in `/suzieq/archive` accumulate forever. For long-running deployments, add a cron on netdevops-srv to enforce a retention window:
    ```
    # /etc/cron.daily/suzieq-archive-prune (chmod 755)
    docker exec sq-poller find /suzieq/archive -name '_archive-*.tar.bz2' -mtime +30 -delete
    ```
  - Production at hundreds of devices scales the daily growth roughly linearly (40+ devices = 300-400 MB/day) and needs the retention cron from day one plus a sized parquet volume on a dedicated disk.
- **REST TLS.** See the banner above. `--no-https` is a lab convenience, not a production posture.
- **Host key verification.** Lab default is `ignore-known-hosts: true` (via `gen-inventory.py`) because vJunos containers regenerate SSH keys on every `containerlab destroy/deploy` cycle. Production MUST keep verification on. The switch is a single env var read by `gen-inventory.py:_strict_host_keys_enabled()`:
    ```bash
    # Production deploy:
    export SUZIEQ_STRICT_HOST_KEYS=1
    python3 gen-inventory.py > inventory.yml
    # Produces: ignore-known-hosts: false  on every device block.
    # Operator then provisions known_hosts via configuration management
    # (Ansible/Salt/etc.) before the poller first connects.
    ```
    Accepts any truthy value (`1`, `true`, `yes`, `on`, case-insensitive). Default (unset/empty/falsy) stays permissive for lab compat. Regression tests in `tests/test_gen_inventory.py::TestStrictHostKeysEnvVar` pin both directions.
- **Static inventory regeneration.** Re-run `gen-inventory.py` and restart the poller after device adds/removes in NetBox. The proper fix is upstream - a SuzieQ PR adding `address-source: oob_ip|primary_ip4` to the NetBox source plugin - so that we can drop the script and use the native dynamic source.

## Comparison to commercial alternatives

Two adjacent products solve overlapping problems:

- **NetBox Assurance** (NetBox Labs) - intent-vs-state drift detection as a NetBox Enterprise feature.
- **SuzieQ Enterprise NetBox Sync** (Stardust Systems) - the reverse direction; pushes discovered state into NetBox.

The lab rolls its own thin Python harness (Parts B/C/D) for three reasons: it's OSS, it ties directly into the existing Phase 3 Nornir / Phase 4 Batfish plumbing, and the implementation surface is small enough to be educational rather than opaque. Neither commercial product is in scope here.

## Junos-specific notes

- **MX route-table scale issue** does not apply - the lab runs vJunos-switch (EX-class), and the upstream SuzieQ MX route caveat (only direct routes gathered to mitigate JSON parse latency on full Internet tables) is not in the failure surface.
- **Devtype semantics vs JSON shape**: see the ["`junos-vjunos-switch` devtype"](#junos-vjunos-switch-devtype-project-owned-added-at-image-build-time) section above. The lab uses EX9214 semantics with the project-owned `junos-vjunos-switch` SuzieQ devtype, added at image build time by the patcher. The `device` service under that devtype inherits from the `junos-mx` template via `SERVICE_BASE_OVERRIDES` because vJunos returns the single-RE JSON shape that `junos-mx`'s uptime parser expects.
- **MAC table EVPN vs VPLS** - SuzieQ treats Junos MAC entries with EVPN-VXLAN encapsulation correctly out of the box. Worth knowing if Phase 10 ever introduces a Junos MX with classic VPLS for comparison.

## Part B-min: NetBox-vs-Suzieq drift harness (DONE)

Catches the class of bugs that neither Phase 2 smoke nor Phase 4 Batfish can see: drift between what NetBox says the network is and what the network actually is, in real time.

### Architecture

A **sibling container** (`drift`) in [docker-compose.yml](docker-compose.yml), built from [drift/Dockerfile](drift/Dockerfile). Slim `python:3.11-slim` base + `pandas` + `pyarrow` + `pynetbox`. **Does NOT inherit from `netenglabs/suzieq`** - reads the parquet store directly via pyarrow hive partitioning, never imports the suzieq python package. Saves ~600 MB image size and decouples the drift harness from suzieq base image upgrade cadence.

The container mounts the `suzieq_parquet` docker volume **read-only** and the `drift/` source code as a separate read-only mount so iteration during development does not need an image rebuild. Invoked as a one-shot CLI:

```bash
docker compose run --rm drift              # JSON output for CI
docker compose run --rm drift --human      # human-readable table
docker compose run --rm drift --namespace dc1 --human
```

### Module boundaries

Strict separation so the unit tests are dependency-light:

| Module | Imports | Role |
|---|---|---|
| [drift/intent.py](drift/intent.py) | `pynetbox` only | NetBox -> dataclass intent |
| [drift/state.py](drift/state.py) | `pyarrow`, `pandas` only | parquet store -> pandas DataFrames |
| [drift/diff.py](drift/diff.py) | `pandas`, dataclasses | pure structured comparison, the comparison core |
| [drift/cli.py](drift/cli.py) | all of the above | I/O orchestration only |

`test_drift_diff.py` and `test_drift_cli.py` import nothing heavier than `pandas` and use hand-built dicts as fixtures for both intent and state. `test_drift_state.py` writes tiny real parquet files into a `tmp_path` fixture (hermetic, no SuzieQ container needed). `test_drift_intent.py` uses a small `FakeNb` test double (~50 lines) that returns hand-built objects shaped like `pynetbox.Record` - not a mock, a real read-only stub class.

### Drift dimensions (4)

| Dimension | Catches | Severity model |
|---|---|---|
| `device_presence` | NetBox-modeled device not seen by SuzieQ (or vice versa) | error if modeled-but-not-polled; warning if polled-but-not-modeled |
| `interface_admin` | NetBox `enabled` != SuzieQ `adminState` for NetBox-modeled interfaces | error on disagreement; warning if interface modeled but not yet seen by SuzieQ; SuzieQ-only interfaces (lo0.16384, jsrv, em0...) deliberately ignored |
| `lldp_topology` | mis-cabled fabric, missing LLDP neighbor, port flap | two-tier match - see "Junos LLDP limitation" below |
| `bgp_session` | cable-derived BGP session expectation not present, or present but not Established | error |

BGP session intent is **derived from NetBox cables + IPs**, not from a NetBox BGP plugin (Phase 1 does not install one). Each fabric P2P /31 cable produces one expected BGP session; the drift check looks for a matching SuzieQ row on EITHER side and asserts state=Established.

### Junos LLDP limitation (Tier B fallback)

Discovered during Part B bring-up against vJunos-switch 23.2R1.14: `show lldp neighbors | display json` (the summary view that SuzieQ's junos template uses) **does not include `lldp-remote-port-id`** at all. Only `lldp-remote-system-name` and `lldp-remote-port-description`. SuzieQ correctly stores empty `peerIfname` because the source data has nothing to put there.

`drift/diff.py` handles this via a **two-tier LLDP match**:

| Tier | Match shape | Result |
|---|---|---|
| **A** (strict) | LLDP row has both `peerHostname` AND `peerIfname` | Compare canonical `(devA, ifaceA) <-> (devB, ifaceB)` against NetBox cable graph. Catches interface-level miscabling within a device pair. |
| **B** (degraded) | `peerHostname` present, `peerIfname` empty | Falls back to checking that the LLDP row reports the right peer DEVICE; cannot verify the peer interface. **Emits a warning** so the operator knows the check is degraded. |

On the vJunos lab today every LLDP cable matches at Tier B and produces 4 warnings. A real fabric with EOS / IOS-XR / NX-OS would match at Tier A and produce zero warnings on a clean network. The Tier B fallback still catches **device-pair miscabling** - if a cable physically connects A to C while NetBox says A to B, that's still an error in either tier.

### Output contract

JSON (default, for Phase 6 CI):

```json
{
  "namespace": "dc1",
  "timestamp": "2026-04-07T20:08:11.234567+00:00",
  "drift_count": 4,
  "error_count": 0,
  "warning_count": 4,
  "drifts": [
    {
      "dimension": "lldp_topology",
      "severity": "warning",
      "subject": "dc1-leaf1:ge-0/0/0<->dc1-spine1:ge-0/0/0",
      "detail": "LLDP peer device matches but peer interface is unknown ...",
      "intent": {"a": {"device": "dc1-leaf1", "interface": "ge-0/0/0"}, "b": {...}},
      "state": {"degraded_match": "device-level only"}
    }
  ]
}
```

Exit codes - the contract Phase 6 CI relies on:

| Code | Meaning |
|---|---|
| 0 | No error-severity drift (warnings allowed) |
| 1 | One or more error-severity drifts found. Phase 6 `fabric-deploy.yml` drift-check hard-fails on this; persistent exit 1 past the retry budget triggers `rollback-on-failure`. (Earlier plan was soft-fail / warn; promoted to hard-fail when the marker-based outer rollback landed in Phase 6.3.) |
| 2 | Tooling error (NetBox unreachable, parquet path missing). Phase 6 should distinguish this from "drift found" - the second is a real failure of the harness itself. |

### Verification (run on netdevops-srv 2026-04-07)

Positive case (no drift on a clean fabric):

```
namespace=dc1: 4 drift(s)
  [WRN] lldp_topology  (4x, all Tier B fallback - documented limitation)
EXIT: 0
```

Negative case (injected `dc1-leaf1:ge-0/0/0 enabled=False` in NetBox while real interface is up):

```
namespace=dc1: 5 drift(s)
  [ERR] interface_admin  dc1-leaf1:ge-0/0/0
        admin state drift: NetBox enabled=False, SuzieQ adminState='up'
  [WRN] lldp_topology    (the 4 documented Tier B warnings)
EXIT: 1
```

Negative test exits 1, positive test exits 0. The drift was visible to the harness within ~1 second of the NetBox change being saved.

## Part B-full: 4 more drift dimensions (DONE)

Same module shape, four new dimensions covering EVPN VNI presence, overlay-via-underlay reachability, anycast gateway MAC, and EVPN Type-2 ARP advertisement. Required two layers of work: (a) inverting the patcher's default base from junos-mx to junos-qfx (because three of the four new tables either had wrong data or no data with junos-mx as the base), and (b) adding 4 new intent collectors / 4 new diff functions.

### Patcher inversion (junos-qfx as the default base)

Originally the patcher defaulted to `copy: junos-mx` for all simple-copy services with a chain resolver to walk junos-mx -> junos-qfx for the 7 services where junos-mx was itself a copy. Live verification of Part B-full revealed three real problems with that default:

1. **`macs.yml`** - junos-mx uses `show bridge mac-table` (an MX-only command, the bridge table is not present on EX/QFX/vJunos switches). junos-qfx uses `show ethernet-switching table detail` which works on vJunos. With the original default, the macs table was empty.

2. **`routes.yml`** - junos-mx uses `show route protocol direct` (the documented MX scale workaround for full-Internet RIBs - returns ONLY direct/connected routes). junos-qfx uses `show route` + `show evpn ip-prefix-database` (full RIB + EVPN learned). With the original default, the routes table had 26 connected-only entries; switching to junos-qfx took it to **86 rows** including bgp/evpn/vpn protocols.

3. **`evpnVni.yml`** - junos-mx is **completely absent** upstream. The patcher saw no junos-mx text and skipped the file, leaving suzieq with no evpnVni collector at all.

The clean architecture, validated empirically: **`junos-qfx` is REAL in every Junos service yaml upstream** (12 of 12). junos-mx has its own real definition in only 4/12 services, and 3 of those 4 (arpnd, macs, routes) are exactly the services where junos-mx is the **wrong** choice for vJunos. So:

```python
DEFAULT_BASE = "junos-qfx"
SERVICE_BASE_OVERRIDES = {"device.yml": "junos-mx"}  # the only one needed
```

`device.yml` is the lone override because it's the one service where junos-mx has the right definition for vJunos: the single-routing-engine uptime parser that vJunos's JSON shape requires. The corresponding `node.py:1969` Python source patch (`patch_node_multi_re_list`) keeps the parser path consistent.

### 4 new drift dimensions

| Dimension | Catches | Source of intent |
|---|---|---|
| `evpn_vni` | NetBox-modeled VNI not present in `evpnVni` table, or present but not `state=up` | L2 from `VLAN.custom_fields.vni`, L3 from `VRF.custom_fields.l3vni` |
| `loopback_route` | Underlay reachability broken: device's loopback /32 not in another device's RIB | `Device.primary_ip4` cross product, with **Clos topology rule** (spine-to-spine pairs excluded) |
| `anycast_mac` | Anycast gateway MAC not in a leaf's MAC table for a tenant VLAN it serves | `VRF.custom_fields.anycast_mac` walked via VLAN -> IRB interface -> IP -> VRF chain |
| `peer_irb_arp` | EVPN Type-2 ARP advertisement broken: peer leaf's IRB IP not resolved in this leaf's ARP table | per-leaf IRB IPs (non-anycast role) cross product across leaves serving the same VLAN |

### The Clos topology rule (regression guard)

`_collect_loopback_routes` skips pairs where both observer and target are role=spine. Discovered live on the lab: without the exclusion the harness produces 2 false-positive drifts on a clean fabric (`spine1->spine2(10.1.0.2/32)` and `spine2->spine1(10.1.0.1/32)`). In a 2-tier Clos, spines do not peer with each other and do not need each other's loopbacks - the architecturally correct statement. Pinned by `test_clos_rule_excludes_spine_to_spine_pairs` in [tests/test_drift_intent.py](tests/test_drift_intent.py).

### Live verification

| Check | Expected | Got |
|---|---|---|
| Clean state, all 8 dimensions | exit 0, 0 drifts | OK |
| Inject fake VLAN with vni=99099 in NetBox | 2 drifts (one per leaf), exit 1 | OK `[ERR] evpn_vni dc1-leaf1:vni99099` + `dc1-leaf2:vni99099` |
| Cleanup, re-run | exit 0, 0 drifts | OK |

### Test count growth across the parts

| Tier | Tests | Time |
|---|---|---|
| Part A only | 22 | 0.14s |
| + Part B-min | 75 | 1.4s |
| + Part B-full + patcher inversion | **128** | 1.83s |

The 53 new tests in B-full break down as: 1 patcher test rewrite + 2 new patcher tests (inverted defaults + override + evpnVni-no-junos-mx fixture), 11 intent collector tests (4 new collector classes + Clos rule guard), 1 state test for the new tables, 16 diff tests (4 new dimension classes), and a small number of cross-cutting test updates. All run in <2 seconds, no docker, no network, no SuzieQ install.

## Part C: strict assertions + systemd timer (DONE)

State-only invariant checks that run continuously via a systemd timer, complementary to the one-shot drift harness. The drift harness answers "does NetBox intent match state?" Assertions answer "does state satisfy these invariants?", require **no NetBox credentials**, and are meant for unattended scheduling.

### Non-overlap rule (non-negotiable)

Every assertion must answer a question Phase 2 smoke CANNOT answer. Phase 2 smoke runs once at deploy; assertions run continuously. The three angles that qualify:

1. **Continuous state** — is this still true *now*, between deploys?
2. **Property drift** — has a measurable property (e.g. pfxRx) left its valid range since the last check?
3. **Self-health** — is the harness itself keeping up?

Assertions that would read identically to a smoke-check docstring are rejected at PR review. See `drift/assertions/__init__.py` for the gate text.

### The four initial assertions

| Assertion | Reads | Catches | Drift doesn't catch because |
|---|---|---|---|
| `assert_bgp_all_established` | `bgp` table | Any session not in Established | Drift checks cable-derived sessions; this checks every session SuzieQ sees (including overlay iBGP to loopbacks not modeled as NetBox cables) |
| `assert_bgp_pfx_rx_positive` | `bgp` table | Established but `pfxRx=0` (flap + mid-converge, policy filter, peer announcing nothing) | Drift only checks `state==Established` |
| `assert_vtep_remote_count` | `evpnVni` table | L2 VNI with no remote VTEPs (EVPN Type-3 discovery broken) | Drift checks `state==up`, not discovery |
| `assert_poll_health` | `sqPoller` table | Poller falling behind (`pollExcdPeriodCount > 0`) | Drift has no opinion about the harness itself |

All four are **pure state-only** - none read NetBox. The assertion mode is therefore safe to run from a systemd timer that has no NetBox credential.

### Gotcha 1: engine-computed columns (two known cases)

`suzieq-cli` and the REST API expose some columns that the SuzieQ pandas engine COMPUTES at query time from raw parquet columns. They never exist in the parquet file. The drift state reader uses direct pyarrow reads that bypass the engine, so production code that reads one of these columns directly will silently see `None`/`NaN` and default to 0 / empty / wrong. Two documented cases today; a live schema-drift smoke test in Phase 5.1 catches new ones automatically on an image bump (see "Phase 5.1 live schema guards" below).

**Case 1: `evpnVni.remoteVtepCnt`** — computed as `len(remoteVtepList)` at query time. The raw source column `remoteVtepList` (list-typed) IS in parquet. Production code in `drift/assertions/vtep.py:_count_remote_vteps()` computes the count itself from the raw column. Discovered live on 2026-04-11: the first live run of `assert_vtep_remote_count` fired 4 false positives on a clean fully-converged fabric because the assertion was reading the non-existent `remoteVtepCnt` column and defaulting to 0. Fix + regression guard pinned in `test_assertions_vtep.py::test_l2_vni_with_missing_column_is_error`.

**Case 2: `sqPoller.statusStr`** — maps the integer `status` column (which IS in parquet) to a human string like `"OK"` or `"Command Not Found"` at query time. Production code does NOT currently read `statusStr` (`assert_poll_health` reads `pollExcdPeriodCount` instead), so this is a documented-but-unused case — the smoke test catches it so a future contributor adding a `statusStr` read would hit the guard first. Discovered live on 2026-04-11 by the Phase 5.1 REST schema-drift smoke test (`tests/fixtures/verify_rest_vs_raw.py`) on its FIRST live run. Allowlisted in `KNOWN_ENGINE_COMPUTED["sqPoller"]`. If a future assertion needs the human-readable status, read raw `status` (integer) and apply a local int-to-string map — NOT a direct `statusStr` read.

**How to avoid this class of bug when adding a new reader:** before reading a column directly via pyarrow in `drift/state.py` / `drift/diff.py` / `drift/assertions/*.py`, grep `tests/fixtures/verify_rest_vs_raw.py:KNOWN_ENGINE_COMPUTED` for the column name. If it's there, use the raw source column + local computation. If it isn't, run the REST schema-drift smoke test once against the live lab to confirm the column is in raw parquet (see "Phase 5.1 live schema guards" below).

### Gotcha 2: partial-view BGP rows during session transitions

First-run analysis of this bug was wrong — the user pushed back with a correct challenge and I had to re-investigate. The corrected explanation:

**Junos emits each BGP peer exactly ONCE** in `show bgp neighbor | display json` (verified directly against vJunos 23.2R1.14 — 4 peers, 4 entries, each with `peer-cfg-rti: master` and `peer-fwd-rti: master`). The earlier "Junos emits each peer twice" theory was wrong.

The real mechanism: **SuzieQ's Junos bgp normalize pipeline runs two commands**:
- `show bgp summary | display json` — extracts `vrf` (with fallback `"default"`) and iterates `bgp-rib/[*]` per AFI/SAFI
- `show bgp neighbor | display json` — extracts `state` and many per-session fields, but does NOT extract vrf or afi or safi in its normalize spec at all

In steady state, the suzieq engine merges the two command outputs into one row per `(vrf, peer, afi, safi)` with all fields populated. The coalescer keeps the merged rows. **During BGP session state transitions (fault → recovery)**, the pipeline writes partial-view rows to raw parquet before the merge completes. The partial rows have empty `vrf`, empty `afi`, empty `safi`, and `state=NotEstd`. They are visible to direct pyarrow reads (which bypass the engine merge) but NOT to `suzieq-cli` (which runs the engine pipeline).

The partial rows are a SuzieQ pipeline artifact, **not** a Junos artifact. They exist only until the next coalescer run compacts them.

Two independent fixes, both in `drift/state.py`:

1. **Use SuzieQ's actual bgp PK** per its own schema at `config/schema/bgp.avsc`: `(namespace, hostname, vrf, peer, afi, safi)`. The earlier 4-field PK was independently wrong — a single peer has **multiple legitimate rows**, one per AFI/SAFI (e.g. an overlay peer with `l2vpn/evpn` AND the underlay peer with `ipv4/unicast`), and the short PK was silently dropping distinct AFI/SAFI rows. Regression guard: `test_bgp_pk_distinguishes_same_peer_different_afi`.

2. **Drop partial-view rows** via a per-table cleanup hook (`_cleanup_bgp_phantom_rows` in `drift/state.py`). Any row where `vrf` OR `afi` OR `safi` is empty/NaN is dropped before dedup, because a BGP row missing any structural field is semantically meaningless (a session is always in a routing-instance and always negotiates a specific AFI/SAFI). Regression guards: `test_bgp_partial_view_rows_dropped`, `test_bgp_partial_view_any_empty_structural_field`, `test_bgp_cleanup_handles_all_partial`.

### CLI `--mode` flag

`drift/cli.py` now supports three modes:

```bash
docker compose run --rm drift --mode drift      # Part B: NetBox-vs-state diff (default)
docker compose run --rm drift --mode assertions # Part C: state-only invariants, NO NetBox needed
docker compose run --rm drift --mode all        # both, combined output
```

The `assertions` mode skips NetBox entirely - verified by `test_assertions_mode_skips_netbox_entirely` which monkeypatches `collect_intent` and asserts it's never called. That's what makes the mode safe for a systemd timer without a NetBox token in the environment.

Exit codes unchanged: 0 clean, 1 failure, 2 tooling error.

### systemd timer

Two unit files ship in `phase5-suzieq/systemd/`:

| File | Role |
|---|---|
| `suzieq-drift-assert.service` | `Type=oneshot` unit that runs `docker compose run --rm drift --mode assertions --human` from `WorkingDirectory=/opt/suzieq`. Reads `EnvironmentFile=/opt/evpn-lab-env/env.sh`. stdout/stderr -> journal. Non-zero exit marks the unit as failed. |
| `suzieq-drift-assert.timer` | `OnCalendar=*:0/5` (every 5 min) + `OnBootSec=2min` + `Persistent=true`. Fires the `.service` unit. |

Install on netdevops-srv:

```bash
sudo cp phase5-suzieq/systemd/suzieq-drift-assert.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suzieq-drift-assert.timer
systemctl status suzieq-drift-assert.timer        # shows next scheduled run
journalctl -u suzieq-drift-assert.service -n 50   # last run's output
```

Override the schedule (e.g. every 2 minutes) via drop-in:

```bash
sudo systemctl edit suzieq-drift-assert.timer
# Enter:
#   [Timer]
#   OnCalendar=
#   OnCalendar=*:0/2
```

### Live verification 2026-04-11

| Scenario | Expected | Result |
|---|---|---|
| Clean fabric, `--mode assertions` | 0 drifts, exit 0 | ✅ |
| Inject `spine1 ge-0/0/0 disable` | BGP assertions fire | ✅ 4 `assert_bgp_established` + 1 `assert_bgp_pfx_rx` (leaf2→spine1 EVPN session still Established but withdraw not propagated) |
| Restore interface, wait for reconvergence | 0 drifts, exit 0 | ✅ (see commit description) |

The negative case is interesting because it demonstrated BOTH the `assert_bgp_established` and `assert_bgp_pfx_rx` assertions firing on the same fault, each catching a different symptom. A session that had dropped to NotEstd on the affected ends, and a peer session that stayed Established briefly but with pfxRx=0 because the withdraw-all hadn't propagated yet.

## Part D: time-window queries (DONE)

Where Part B/C answer "what is the current state right now?", Part D answers "what happened over a window?". Same parquet store, different read pattern: read the historical snapshots over a time window and aggregate per-event metrics, instead of collapsing to `view='latest'`.

Three queries ship in the initial set, all running over the existing parquet history with no new infrastructure:

| Query | Reads | Answers |
|---|---|---|
| `bgp_flaps` | `bgp` table | How many BGP session state transitions happened in the window, per `(host, vrf, peer, afi, safi)`? Counts row-to-row state changes in the polled snapshots, NOT the per-row `numChanges` counter (which can reset and counts events between polls that the harness can't see anyway). |
| `route_churn` | `routes` table | Per `(host, vrf)`: how many distinct prefixes were touched in the window, and how many of those received >1 update (the "churned" subset)? Decomposed into `prefixes_touched` / `churned_prefixes` / `total_changes` so a clean window with no events shows zero churn even if the absolute route count is unchanged. |
| `mac_mobility` | `macs` table | Which MACs appeared on more than one distinct `(host, oif, remoteVtepIp)` during the window? Catches L2 moves between leaves, port flaps, and EVPN Type-2 advertisement migrations. |

### Architecture

`drift/timeseries/` mirrors `drift/`'s module-import-boundary rule:

```
drift/timeseries/
  partition.py     pure: filename parsing + directory walking + duration parsing
  reader.py        the ONLY pyarrow import in the package
  queries/
    __init__.py    QUERIES registry + TimeseriesResult dataclass
    bgp_flaps.py
    route_delta.py
    mac_mobility.py
  envelope.py      JSON shape (no pandas, no pyarrow imports needed at test time)
```

Tests for the query layer never import pyarrow — they build inline DataFrame fixtures and pass them to query functions. This is the same pattern that keeps `test_drift_diff.py` cheap.

### Windowing strategy

Coalesced parquet files are named `sqc-h1-0-<start_epoch>-<end_epoch>.parquet` where the two epochs encode the exact 1-hour window the file covers. Reader pre-filters files by name epoch, then filters rows inside the matching files by the row-level `timestamp` column (millisecond epoch). Raw uncoalesced files (the current hour's data) are always included and filtered only by row timestamp.

The smallest practically-useful window is **1 minute** — that's the poller cadence, so anything narrower returns at most one snapshot per session. Sub-minute windows are accepted but documented as low-resolution.

### CLI

```bash
# Last hour, JSON envelope to stdout (Phase 6 / log-shipper format)
docker compose run --rm drift --mode timeseries --window 1h --json

# Last 5 minutes, human-readable to terminal
docker compose run --rm drift --mode timeseries --window 5m --human

# Absolute window for replay or debugging
docker compose run --rm drift --mode timeseries \
  --from 1775904896 --to 1775908496 --json
```

`--window` and `--from/--to` are mutually exclusive — the CLI rejects bad combinations with a tooling-error exit code.

### Exit code semantics

Timeseries observations are **never pass/fail**. Even with 1000 BGP flaps in the result, `--mode timeseries` exits 0. The whole point of the mode is to surface neutral observations the operator interprets in context — a fabric mid-maintenance window has lots of flaps and that's expected. Pass/fail is the job of `--mode assertions`.

The only non-zero exit code timeseries can produce is `2` (tooling error): bad window arguments, parquet read failure, or a query crash.

This is pinned by `test_returns_zero_even_when_flaps_detected` and `test_json_envelope_has_timeseries_shape_not_drift_shape`.

### JSON envelope

Different shape from the drift/assertions envelope. Stable contract for Phase 6:

```json
{
  "namespace": "dc1",
  "generated_at": "2026-04-11T12:34:56+00:00",
  "window": {
    "start_epoch": 1775904896,
    "end_epoch":   1775908496,
    "start_iso":   "2026-04-11T11:34:56+00:00",
    "end_iso":     "2026-04-11T12:34:56+00:00",
    "duration_seconds": 3600
  },
  "queries": [
    {
      "name":       "bgp_flaps",
      "table":      "bgp",
      "files_read": 3,
      "summary":    {"total_flaps": 4, "sessions_with_flaps": 1, "sessions_seen": 16},
      "rows":       [
        {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.0.0.2",
         "afi": "ipv4", "safi": "unicast",
         "flap_count": 4, "snapshots": 12,
         "first_state": "established", "last_state": "established"}
      ]
    },
    { "name": "route_churn", ... },
    { "name": "mac_mobility", ... }
  ]
}
```

`files_read=N` with `rows=[]` distinguishes "the window had files but every row was filtered" (a quiet window) from "no files at all" (e.g. namespace doesn't exist, parquet path wrong). The drift envelope's `{result, total, passed, failed, drifts}` fields are deliberately absent — a future merger of the two output paths would lose that distinction.

### systemd timer

Same pattern as Part C, slower cadence. Two unit files in `phase5-suzieq/systemd/`:

| File | Role |
|---|---|
| `suzieq-drift-timeseries.service` | `Type=oneshot` unit that runs `docker compose run --rm drift --mode timeseries --window 1h --json` and redirects stdout to `/var/log/suzieq-drift/timeseries-latest.json`. Same `WorkingDirectory=/opt/suzieq` and `EnvironmentFile=/opt/evpn-lab-env/env.sh` as the assertions service. |
| `suzieq-drift-timeseries.timer` | `OnCalendar=hourly` + `OnBootSec=5min` + `Persistent=true`. Aligns the summary cadence with the coalescer's hourly file boundary. |

Install on netdevops-srv alongside the Part C units:

```bash
sudo cp phase5-suzieq/systemd/suzieq-drift-timeseries.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suzieq-drift-timeseries.timer
systemctl list-timers suzieq-drift-*                          # next scheduled runs
cat /var/log/suzieq-drift/timeseries-latest.json | jq .       # last summary
journalctl -u suzieq-drift-timeseries.service -n 50           # run history
```

The hourly cadence is the natural floor of the parquet partition scheme — a more frequent timer would just reread the same coalesced files and produce identical output. Override via `systemctl edit suzieq-drift-timeseries.timer` if a different cadence is needed.

### Wiring systemd OnFailure= to `status: degraded` (Phase 5.1 opt-in)

The default `--mode timeseries` exit code is always 0 (ADR-11), so a vanilla systemd timer never fires `OnFailure=` even when the envelope self-check reports `status: "degraded"`. The result: an operator who doesn't watch the JSON file could miss a broken-poller state for hours or days.

Phase 5.1 adds a strictly-opt-in flag that lets systemd pick up the degraded signal without waiting for a Phase 6 consumer:

```bash
docker compose run --rm drift --mode timeseries --window 1h --json --exit-nonzero-on-degraded
```

When the flag is set AND `envelope.status == "degraded"`, the command exits with `EXIT_DRIFT_FOUND` (1) instead of `EXIT_OK` (0). Tooling errors still exit 2. Any other run still exits 0. The default is **off** — the ADR-11 "timeseries observations are never pass/fail" contract stays intact for every run that does not opt in.

Install as a systemd drop-in override so the unit file stays pristine:

```bash
sudo systemctl edit suzieq-drift-timeseries.service
```

Enter:

```ini
[Service]
# Promote status=degraded to exit 1 so OnFailure= fires on it.
# See ADR-15 + Phase 5.1 review item #3.
ExecStart=
ExecStart=/bin/bash -c '/usr/bin/docker compose run --rm drift --mode timeseries --window 1h --json --exit-nonzero-on-degraded > /var/log/suzieq-drift/timeseries-latest.json'

# Fire an alerting unit when the timer's oneshot exits non-zero
# (which now happens on either tooling error OR degraded status).
OnFailure=suzieq-drift-timeseries-alert.service
```

Then define a trivial alert service (one-shot wrapper around your mailer / Slack-webhook / pagerduty-cli of choice). This gives operators a fire-immediately path for the degraded signal without any Phase 6 consumer infrastructure.

**Why a flag and not flip the default:** the ADR-11 rule is load-bearing for consumers that treat the JSON file as a data source (e.g. a dashboard polling `timeseries-latest.json`). Such a consumer depends on the file being readable even when the harness reports degradation. Flipping the default would break the fire-and-forget dashboard case to help the systemd-OnFailure case; making it an opt-in lets both patterns coexist.

### Why "touched/churned" instead of an absolute route delta

The natural framing of `route_churn` is "how did the route count change between window start and window end?". Answering it cleanly requires snapshots at exactly the window's start AND end, which we don't have — the parquet store has poll-cadence snapshots and a query window starts at an arbitrary time that almost never aligns with one. The "touched/churned" decomposition sidesteps the alignment problem by counting only what's observable inside the window.

There's a second reason: an absolute delta of zero is consistent with both "nothing happened" AND "100 routes flapped through the same state and ended up identical". The churn count distinguishes the two — operators looking for "did anything go wrong with route propagation in the last hour?" get a more useful answer.

### Why count BGP state transitions instead of using `numChanges`

`numChanges` is a per-row counter the device increments on each session state change. Two reasons we don't use it directly for flap counting:

1. **It resets when the daemon restarts.** A reset between two snapshots produces a negative delta and the naive max-min approach silently undercounts.
2. **It counts changes the harness cannot see.** A session that flaps and recovers between two polls bumps `numChanges` but produces no visible state change in the polled snapshots. The metric we want is "what state transitions did the harness OBSERVE?" — that's what an operator can correlate against assertions output and logs. The device's internal counter is invisible to the rest of the alerting pipeline.

Counting row-to-row state transitions in the polled snapshots gives us the operator-observable answer. It is conservative — fast flaps that complete inside one poll cycle are missed — but that's correct: if the harness can't see it, an alert on it would be unfounded.

## Recreating this phase on a different environment

The instructions below are the complete recipe for reproducing the Phase 5 stack (Parts A + B + C) on a different host. They assume a Linux VM with Docker and a reachable NetBox.

### Prerequisites

| Requirement | Why |
|---|---|
| Linux host with Docker 20.10+ and Docker Compose v2 | Runs the four-container Suzieq stack + the drift sibling container |
| ≥ 2 GB free RAM available for the Suzieq stack | Poller is the heaviest; measured ~400 MB steady-state for 4 devices |
| ≥ 5 GB free disk for the first year | Parquet store + archive grows ~30-40 MB/day for a 4-device lab (see "Coalescer storage budget" above) |
| NetBox 4.x instance reachable from the host | Intent source for drift. Must have the `dcim`, `ipam`, `vpn` APIs. |
| SSH reachability from the host to every fabric device | Suzieq polls via SSH. Credentials come from `$JUNOS_SSH_USER` / `$JUNOS_SSH_PASSWORD`; production must use a dedicated read-only user. |
| Python 3.10+ on the workstation that generates `inventory.yml` | `gen-inventory.py` uses stdlib urllib + pynetbox |
| systemd (for Part C timer) | Optional; without it assertions run on demand only |

### NetBox data model requirements

The drift harness depends on these NetBox objects existing. Phase 1's `populate.py` creates them all for the lab; adapt to your environment:

| NetBox object | Custom fields | Used by |
|---|---|---|
| `dcim.tag` named `suzieq` | — | `gen-inventory.py` filter; every device to be polled must have this tag |
| `dcim.device` | `primary_ip4` = loopback, `oob_ip` = mgmt | drift `loopback_route` dimension + inventory generation |
| `dcim.interface` | `cable` property set for fabric P2P links | drift `lldp_topology` + `bgp_session` dimensions |
| `ipam.ip_address` with role `anycast` | — | drift `peer_irb_arp` dimension (excluded from leaf-local IP list) |
| `ipam.vlan` | `vni` (int) for L2 VNIs | drift `evpn_vni` dimension |
| `ipam.vrf` | `l3vni` (int), `anycast_mac` (str), `tenant_id` (int) | drift `evpn_vni` + `anycast_mac` dimensions |
| `vpn.l2vpn` (type `vxlan-evpn`) + terminations on VLANs | — | L2VPN -> VLAN binding for tenant VLAN enumeration |

If any of the custom fields above are missing, the corresponding drift dimension silently emits no intent for that resource (not an error). Running `python gen-inventory.py 2>&1` and checking for `WARNING` lines is a quick sanity check.

### Environment variables

These live in a single file outside the repo (the project convention is `../evpn-lab-env/env.sh`). The operator sources it before any `docker compose` command. Required:

```bash
# SSH credentials used by Suzieq poller
export JUNOS_SSH_USER=admin
export JUNOS_SSH_PASSWORD=<your-junos-password>

# NetBox API
export NETBOX_URL=http://your-netbox:8000
export NETBOX_TOKEN=your-netbox-api-token

# Suzieq REST API access key (any string, rotate periodically)
export SUZIEQ_API_KEY=$(openssl rand -hex 32)
```

### One-time setup

```bash
# 1. Copy the phase5-suzieq/ directory to the host (e.g. /opt/suzieq/)
scp -r phase5-suzieq/* operator@host:/opt/suzieq/

# 2. Create a dedicated env file outside the repo (never commit)
sudo mkdir -p /opt/evpn-lab-env/
sudo vi /opt/evpn-lab-env/env.sh     # paste the exports above

# 3. (One-time) Permissions on the parquet volume. The suzieq
# container runs as uid 1000; fresh docker volumes are root-owned.
sudo docker volume create suzieq_parquet
sudo docker volume create suzieq_archive
sudo docker run --user root -v suzieq_parquet:/parquet -v suzieq_archive:/archive \
  --rm --entrypoint /bin/bash netenglabs/suzieq@sha256:6e4e955a... \
  -c "chown -R 1000:1000 /parquet /archive"

# 4. Build the patched Suzieq image (adds the junos-vjunos-switch
# devtype at BUILD TIME - see "junos-vjunos-switch devtype" section
# above for the full story).
cd /opt/suzieq/
source /opt/evpn-lab-env/env.sh
sudo docker compose build sq-poller

# 5. Tag the fabric devices with `suzieq` in NetBox (or let Phase 1
# populate.py do it via `netbox-data.yml`).

# 6. Generate the Suzieq inventory from NetBox
python gen-inventory.py > inventory.yml
grep devtype inventory.yml   # sanity check: junos-vjunos-switch for EX9214

# 7. Bring the stack up
sudo docker compose up -d

# 8. Verify per the "Verification gate" section above
```

### Deploy Part C systemd timer

```bash
# 9. Install and enable the timer
sudo cp /opt/suzieq/systemd/suzieq-drift-assert.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suzieq-drift-assert.timer

# Verify
systemctl status suzieq-drift-assert.timer
journalctl -u suzieq-drift-assert.service -n 20
```

### Regenerating the inventory after a fabric change

Any time a device is added/removed in NetBox or the `suzieq` tag is applied/removed, re-run from the workstation:

```bash
source evpn-lab-env/env.sh
python phase5-suzieq/gen-inventory.py > inventory.yml
scp inventory.yml operator@host:/opt/suzieq/inventory.yml
ssh operator@host "cd /opt/suzieq && docker compose restart sq-poller"
```

The inventory file is deliberately NOT generated in-container because the container has no NetBox credentials of its own.

### What to change for a non-vJunos deployment

If the fabric uses real hardware (QFX, EX, MX) instead of vJunos-switch:

1. **Remove the `junos-vjunos-switch` devtype mapping** from `gen-inventory.py` and map `EX9214` (or whatever model) directly to the stock upstream `junos-qfx` / `junos-mx` devtype. The build-time patcher in `suzieq-image/Dockerfile` becomes optional at that point - you can use the upstream `netenglabs/suzieq` image directly if you don't need the patch.
2. **Verify `show system uptime | display json` on one of the devices** returns the multi-RE-wrapped shape (`multi-routing-engine-results/[0]/...`) that the stock `junos-qfx` device service template expects. If it does, use `junos-qfx`. If it returns single-RE shape like vJunos does, use `junos-mx` (or run the patcher unchanged).
3. **LLDP detail view works on real Junos** without any patching (both `junos-qfx` and the patcher's `junos-vjunos-switch` use `show lldp neighbors detail`), so no adjustment needed.
4. **For non-Junos vendors** (Arista EOS, Cisco NX-OS/IOS-XR, Cumulus): change the devtype mapping in `gen-inventory.py::DEVTYPE_OVERRIDES` to the appropriate upstream devtype. The drift harness and assertions are vendor-neutral - they read the same column names from the SuzieQ tables regardless of which NOS populated them.

### Troubleshooting checklist

In the order you should check when something doesn't work:

1. `docker compose ps` — all four services up (sq-poller, sq-coalescer, sq-rest-server, drift)
2. `docker exec sq-poller suzieq-cli device show` — 4 rows, status=alive. If 0 rows, the NetBox tag is missing or `oob_ip` is wrong.
3. `docker exec sq-poller suzieq-cli bgp show` — 16 rows, all Established. If not, fabric itself has a problem - unrelated to Suzieq.
4. `docker compose run --rm drift --mode assertions --human` — expected output on a clean fabric is `namespace=<ns>: no drift`.
5. `journalctl -u suzieq-drift-assert.service -n 50` — the timer's last run. If "unit failed" shows up, tail the journal to see which assertion fired.
6. `docker exec sq-poller cat /tmp/sq-poller-0.log | tail -50` — poller-side errors (SSH auth failures, command timeouts).

## Phase 5.1 operational hardening (2026-04-11)

Five operator-facing changes landed after a review pass that focused on operational gaps the earlier architectural reviews had missed. All five are live-verified end-to-end on netdevops-srv.

### `sq-rest-server` real HTTP healthcheck

The earlier healthcheck was `["CMD-SHELL", "timeout 3 bash -c '</dev/tcp/127.0.0.1/8000' || exit 1"]` — a bash TCP connect probe that succeeds as long as the listening socket is alive. A wedged uvicorn worker still satisfies that, because the parent accept path is separate from the request-handling path. Result: the container reported `Up 3 days (healthy)` while every HTTP request got reset at the server.

Fixed by `sq-rest-healthcheck.py` (host-side script, bind-mounted into the container at `/usr/local/bin/sq-rest-healthcheck.py`). Makes an authenticated HTTP GET to `/api/v2/device/show?namespace=dc1` with the container's `SUZIEQ_API_KEY`, asserts HTTP 200 + JSON list body, fails non-zero on anything else. Docker marks the container unhealthy within the configured retry window instead of silently staying green.

`docker-compose.yml` healthcheck now:

```yaml
healthcheck:
  test: ["CMD", "python3", "/usr/local/bin/sq-rest-healthcheck.py"]
  interval: 30s
  timeout: 10s
  retries: 5
  start_period: 30s
```

Diagnostic recipe for a future wedge:

```bash
docker inspect sq-rest-server --format '{{.State.Health.Status}}: {{range .State.Health.Log}}exit={{.ExitCode}} [{{.Start.Format "15:04:05"}}] {{.Output}}{{end}}'
```

### `--exit-nonzero-on-degraded` opt-in for the hourly timer

Default `--mode timeseries` exit code is always 0 per ADR-11. Operators who want systemd `OnFailure=` to fire on `status: "degraded"` without waiting for a Phase 6 consumer can enable the opt-in flag via a `systemctl edit` drop-in. See the "Wiring systemd OnFailure= to `status: degraded` (Phase 5.1 opt-in)" section in the Part D block above for the full drop-in example.

### `SUZIEQ_STRICT_HOST_KEYS` env var

`gen-inventory.py` now reads `SUZIEQ_STRICT_HOST_KEYS` at deploy time. Default stays permissive for lab compat (containerlab destroy/deploy cycles); production sets the env var to any truthy value and gets `ignore-known-hosts: false`. See the "Production note" section above for the full explanation.

### Live schema guards (catch new engine-computed columns automatically)

Two new test files in `tests/`:

**`tests/test_live_schema_guards.py`** — 12 `@pytest.mark.live` tests parametrized over 9 SuzieQ tables. For each table, reads the coalesced parquet subtree via `pyarrow.dataset(partitioning='hive')` and asserts every column production code depends on is present. Plus three engine-computed-drift regression guards pinning the `remoteVtepCnt`, `bgp` 6-field PK, and `sqPoller.timestamp` heartbeat contracts. Skipped by default (`addopts = -m "not live"`); runs when `SUZIEQ_LIVE_PARQUET_DIR` is set and the `live` marker is enabled.

**`tests/fixtures/verify_live_schema.py`** — standalone Python mirror of the same logic that runs WITHOUT pytest. Needed because netdevops-srv is Debian 12 + PEP 668 blocks `pip install pytest` on system python, and installing pytest into the drift container would bloat the image. Run via:

```bash
docker run --rm \
    -v suzieq_parquet:/suzieq/parquet:ro \
    -v /tmp/verify_live_schema.py:/verify.py:ro \
    -e SUZIEQ_LIVE_PARQUET_DIR=/suzieq/parquet \
    --entrypoint python3 \
    evpn-lab/phase5-drift:dev /verify.py
```

First live run 2026-04-11: 10/10 PASS against the real lab parquet store. All 9 required-column checks succeed + the `remoteVtepCnt` engine-computed pin still holds.

### REST vs raw schema-drift smoke test (catches NEW engine-computed columns)

**`tests/fixtures/verify_rest_vs_raw.py`** — standalone script that diffs the REST `/api/v2/<table>/show` column set against the raw pyarrow column set for 8 SuzieQ tables. Columns in REST but NOT in raw parquet = engine-computed. Fails on any that aren't in the `KNOWN_ENGINE_COMPUTED` allowlist.

REST API table-name gotcha encoded in the script's `TABLES` tuple: REST uses singular `route` / `mac` / `interface` while parquet uses plural `routes` / `macs` / `interfaces`. Verified empirically via status-code probe (`200` vs `404`).

Run via:

```bash
docker run --rm --network host \
    -v /var/lib/docker/volumes/suzieq_parquet/_data:/parquet:ro \
    -v /tmp/verify_rest_vs_raw.py:/verify.py:ro \
    -e SUZIEQ_REST_URL=http://127.0.0.1:8443 \
    -e SUZIEQ_API_KEY=$(docker inspect sq-rest-server \
        --format '{{range .Config.Env}}{{println .}}{{end}}' \
        | awk -F= '/^SUZIEQ_API_KEY=/{print $2}') \
    -e SUZIEQ_LIVE_PARQUET_DIR=/parquet \
    -e SUZIEQ_NAMESPACE=dc1 \
    --entrypoint python3 \
    evpn-lab/phase5-drift:dev /verify.py
```

First live run 2026-04-11 caught `sqPoller.statusStr` as a previously-undocumented engine-computed column (see Gotcha 1 above). Final result after allowlisting: **8/8 passed, 0 failed, 0 skipped**.

### Coverage baseline 91.9%

`pytest-cov` is now a dev dependency (`requirements-dev.txt`) and `.coveragerc` is in the phase directory. Coverage is **opt-in** — the default `pytest` command skips it entirely so the default test runtime stays flat at ~2.5 s. Run the coverage report via:

```bash
cd phase5-suzieq
python -m pytest --cov --cov-config=.coveragerc --cov-report=term
```

Baseline as of this commit: **91.9% on 1214 statements, 98 missed.** Per-file: worst is `drift/timeseries/reader.py` at 86.3% (FileNotFoundError race paths during fresh poll cycles — hard to unit-test without a real tmp_path race); `gen-inventory.py` at 87.0% (`main()` not exercised by unit tests); `drift/cli.py` at 89.4% (NetBox exception handlers). Everything else is above 90%.

The `.coveragerc` uses `include =` (not `source =`) because two of the production modules live in hyphenated filenames (`gen-inventory.py`, `suzieq-image/add-vjunos-switch.py`) and coverage's auto-import heuristic can't track them by package name. `include` takes filesystem path patterns directly.

Coverage is **NOT a CI gate** — the point is to measure the claim that "362 tests exercise critical error paths", not to force writing tests-for-coverage. The number gives a reviewer a signal on where the gaps are; closing them is a Phase 5.2 follow-up task, not a merge blocker.
