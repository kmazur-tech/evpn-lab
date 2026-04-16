# Phase 5 - Suzieq Operational State Monitoring + NetBox Drift Detection

Continuous runtime monitor of the DC1 fabric, complement to Phase 2's deploy-time smoke suite. Three goals: continuous state observation, NetBox-vs-live-state drift, and a queryable time-series record. This README covers **Part A only** (the SuzieQ stack on netdevops-srv). Drift harness, strict assertions, and time-window queries land in subsequent parts.

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

## Junos devtype override (the "EX devices but `junos-mx` devtype" thing)

SuzieQ ships per-devtype templates in `device.yml`. The `device` service collects model/version/serial/uptime via `show system uptime | display json`. The expected JSON shape varies:

| SuzieQ devtype | Expected shape | vJunos-switch returns |
|---|---|---|
| `junos-qfx` | `multi-routing-engine-results/[0]/...` | ❌ KeyError on bootupTimestamp |
| `junos-ex` | `copy: junos-qfx` (same multi-RE) | ❌ KeyError on bootupTimestamp |
| `junos-mx` | `system-uptime-information/*/...` (single-RE) | ✅ Works |

vJunos-switch (the vrnetlab image the lab uses to emulate EX9214) returns the **single-RE** shape, regardless of the fact that real EX9214 hardware would not. So the only built-in SuzieQ devtype whose `device` service template parses correctly is `junos-mx`. With `junos-ex` (the semantically correct choice) the `device` service raises `KeyError: 'bootupTimestamp'` on every poll cycle and `device show` stays empty - even though every other service (bgp, lldp, interfaces, evpnVni, routes, macs, arpnd) populates fine.

The `gen-inventory.py` `DEVTYPE_OVERRIDES` map encodes this: NetBox model `EX9214` -> SuzieQ devtype `junos-mx`. Comments in [gen-inventory.py](gen-inventory.py) explain the mapping inline. Upstream fix would be to add a vJunos-aware shape to `junos-ex`, or to auto-detect the wrapper at parse time - neither is tracked anywhere I could find.

## Deployment

Run on the netdevops services VM (netdevops-srv.lab.local - same host as NetBox and Batfish):

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
  "http://netdevops-srv.lab.local:8443/api/v2/device/show?namespace=dc1" | head -c 500
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
| `Host key is not trusted for host 172.16.18.161` for 3 of 4 devices | First device's key gets accepted into a fresh known_hosts; the rest get rejected. vJunos containers regenerate keys on every cold boot anyway | `ignore-known-hosts: true` in the device block (lab convenience; production must keep verification on) |
| `Processing data failed for service device ... KeyError: 'bootupTimestamp'` | `junos-qfx` / `junos-ex` device template expects multi-routing-engine wrapper that vJunos-switch does not produce | Override to `junos-mx` devtype (see "Junos devtype override" section above) |
| `device show` empty after re-deploy even though `pd.read_parquet` returns data | `suzieq-cli` reads `~/.suzieq/suzieq-cfg.yml` (default config) which has `data-directory: ./parquet`; our config lives at `/suzieq/suzieq.cfg` | Mount `./suzieq.cfg` at BOTH `/suzieq/suzieq.cfg` AND `/home/suzieq/.suzieq/suzieq-cfg.yml` |
| REST API returns "Connection reset by peer" | Default rest server bind address is `127.0.0.1`, not reachable through Docker port mapping | `address: 0.0.0.0` in `suzieq.cfg` rest section |
| Port 8000 collision on `docker compose up` | NetBox already runs on 8000 on the same VM | Host port mapped to 8443 instead |

## Production note (not lab guidance)

The lab cuts corners that production deployments should not:

- **Dedicated read-only user.** Lab reuses Phase 3's `JUNOS_SSH_USER` / `JUNOS_SSH_PASSWORD`. Production must create a dedicated user (e.g. `suzieq-ro`) bound to a Junos login class restricted to `view` permissions only. SuzieQ never needs configuration mode and never needs to commit anything.
- **AAA rate-limiting.** Lab uses local users so the `max-cmd-pipeline`, `retries-on-auth-fail`, and `per-cmd-auth` knobs in `suzieq.cfg` are protective rather than load-bearing. Production with TACACS+ or RADIUS must tune these against the AAA backend's rate limits - upstream SuzieQ ships a "Rate Limiting AAA Server Requests" document with the specific guidance.
- **Sizing.** Single worker, single namespace is correct here (4 devices). Upstream rule of thumb is "<= 40 devices per worker" and "workers <= namespaces"; multi-DC and multi-region deployments need a worker per namespace and possibly multiple workers per large namespace.
- **Coalescer storage budget.** Default `coalescer.period: 1h` with the archive directory enabled keeps weeks of history for 4 devices in well under a GB. Production at hundreds of devices needs an explicit retention policy and a sized parquet volume.
- **REST TLS.** See the banner above. `--no-https` is a lab convenience, not a production posture.
- **Host key verification.** Lab uses `ignore-known-hosts: true` because vJunos containers regenerate SSH keys on every cold boot. Production must keep verification on and provision known_hosts via configuration management.
- **Static inventory regeneration.** Re-run `gen-inventory.py` and restart the poller after device adds/removes in NetBox. The proper fix is upstream - a SuzieQ PR adding `address-source: oob_ip|primary_ip4` to the NetBox source plugin - so that we can drop the script and use the native dynamic source.

## Comparison to commercial alternatives

Two adjacent products solve overlapping problems:

- **NetBox Assurance** (NetBox Labs) - intent-vs-state drift detection as a NetBox Enterprise feature.
- **SuzieQ Enterprise NetBox Sync** (Stardust Systems) - the reverse direction; pushes discovered state into NetBox.

The lab rolls its own thin Python harness (Parts B/C/D) for three reasons: it's OSS, it ties directly into the existing Phase 3 Nornir / Phase 4 Batfish plumbing, and the implementation surface is small enough to be educational rather than opaque. Neither commercial product is in scope here.

## Junos-specific notes

- **MX route-table scale issue** does not apply - the lab runs vJunos-switch (EX-class), and the upstream SuzieQ MX route caveat (only direct routes gathered to mitigate JSON parse latency on full Internet tables) is not in the failure surface.
- **Devtype semantics vs JSON shape**: see "Junos devtype override" above. The lab uses EX9214 semantics but `junos-mx` SuzieQ devtype because that is the only built-in template whose `device` service parses vJunos-switch's JSON correctly.
- **MAC table EVPN vs VPLS** - SuzieQ treats Junos MAC entries with EVPN-VXLAN encapsulation correctly out of the box. Worth knowing if Phase 10 ever introduces a Junos MX with classic VPLS for comparison.

## What lands in Parts B/C/D

| Part | Scope |
|---|---|
| B-min | Drift harness for `device` / `interfaces` / `lldp` / `bgp` (smallest viable end-to-end loop) |
| B-full | Drift extended to `evpnVni` / `routes` / `macs` / `arpnd` |
| C | Strict assertions, gated by a non-overlap rule against Phase 2 smoke |
| D | Time-window queries via SuzieQ's native `view='all'` + `start_time`/`end_time` |

Parts B onward use SuzieQ's Python API (`get_sqobject`) directly against the parquet volume, not the REST server. REST stays exposed for ad-hoc operator queries only.
