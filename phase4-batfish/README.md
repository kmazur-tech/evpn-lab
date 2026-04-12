# Phase 4 - Batfish Pre-Deployment Validation

Offline validation of rendered Junos configs before they touch a real device. Catches a class of bugs (BGP topology errors, undefined policy references, parse failures, loopback unreachability) that the Phase 3 regression gate and on-disk guard don't cover - and catches them **without** spinning up the lab.

## What it validates

| Check | What it catches | Confidence |
|---|---|---|
| `parse_status` | Batfish cannot parse the config at all | High - hard fail on FAILED, warning on PARTIALLY_UNRECOGNIZED (Junos features Batfish doesn't fully model) |
| `bgp_sessions` | ASN mismatch, missing peer config, unreachable peer, wrong family | High - the most useful single Batfish check for an EVPN lab |
| `bgp_edges_symmetric` | One side defines a peer the other doesn't (asymmetric template bug) | High |
| `undefined_references` | Template emits `vrf-import EVPN-IMPORT-X` but no policy named EVPN-IMPORT-X is defined | High - catches Phase 3 template typos |
| `loopback_reachability` | Fabric loopbacks not propagated via BGP underlay | High |

## What it does NOT validate (intentionally)

The Phase 2 smoke suite (~76 checks) covers the runtime side: actual BGP convergence, EVPN Type-2/3/5 propagation, ESI-LAG behavior, DF election, BFD timers, MAC learning, anycast gateway reachability. Batfish would either duplicate that work poorly (its EVPN/VXLAN modeling is partial) or simply can't simulate those behaviors at all. The split is deliberate:

- **Phase 4 Batfish** = "did the templates produce structurally valid configs?" Runs in seconds, no devices needed.
- **Phase 2 smoke** = "does the deployed fabric actually work?" Runs against the live lab.

## Setup - one-time deployment of the Batfish container on netdevops-srv

The Batfish server runs as a Docker container on `netdevops-srv` (netdevops-srv.lab.local), the same VM that hosts NetBox. Reuses the existing Docker daemon, no new VM, no lab impact.

```bash
# 1. SSH to netdevops-srv
ssh root@netdevops-srv.lab.local

# 2. Create the deployment directory and copy the docker-compose.yml
mkdir -p /opt/batfish && cd /opt/batfish

# Either scp from a dev box:
#   scp phase4-batfish/docker-compose.yml root@netdevops-srv.lab.local:/opt/batfish/
# Or paste it inline. The contents are:
cat > docker-compose.yml <<'YAML'
services:
  batfish:
    image: batfish/allinone:latest
    container_name: batfish
    restart: unless-stopped
    ports:
      - "9996:9996"
      - "9997:9997"
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost:9996/ || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 60s
YAML

# 3. Pull the image (~1.5 GB, one-time)
docker compose pull

# 4. Start
docker compose up -d

# 5. Verify it's running and the API is reachable
docker compose ps
docker compose logs --tail=20 batfish

# 6. From your dev box, verify TCP/9996 is reachable:
nc -zv netdevops-srv.lab.local 9996  # expect: connection succeeded
nc -zv netdevops-srv.lab.local 9997  # expect: connection succeeded
```

The container is **stateless** - validate.py uploads a fresh snapshot on every run. No persistent volumes, no backup needed. To restart cleanly: `docker compose restart batfish`.

## Usage

### Prerequisites on the dev box

Same WSL2 venv as Phase 3 (`~/.venvs/evpn-lab`), plus pybatfish:

```bash
cd phase4-batfish
~/.venvs/evpn-lab/bin/pip install -r requirements.txt
```

### Standalone validation

```bash
# Render the configs first
cd phase3-nornir
~/.venvs/evpn-lab/bin/python deploy.py --full

# Then validate
cd ../phase4-batfish
~/.venvs/evpn-lab/bin/python validate.py --snapshot ../phase3-nornir/build/
```

Expected output for a clean render against the current Phase 3 templates:

```
Staged 4 config(s) -> /tmp/bf-snap-XXXX
  dc1-leaf1.cfg
  dc1-leaf2.cfg
  dc1-spine1.cfg
  dc1-spine2.cfg

Connecting to Batfish at netdevops-srv.lab.local:9996...
Initializing snapshot 'rendered' (this can take 30-60s)...

============================================================
 Batfish validation report
============================================================
  [OK  ] parse_status                4 file(s) parsed
  [OK  ] bgp_sessions                12/12 session(s) ESTABLISHED
  [OK  ] bgp_edges_symmetric         12 BGP edge(s), all symmetric
  [OK  ] undefined_references        no undefined references
  [OK  ] loopback_reachability       4 fabric loopback(s) reachable from 4 device(s)
============================================================
 RESULT: PASS (5 check(s))
============================================================
```

### Wired into Phase 3 deploy.py via opt-in flag

```bash
~/.venvs/evpn-lab/bin/python phase3-nornir/deploy.py --full --validate
```

The `--validate` flag is opt-in (off by default) so the inner-loop `--check` workflow stays fast. CI invokes both deploy.py and validate.py as separate stages so a Batfish failure blocks the merge before NAPALM is ever called.

### CLI options

```
--snapshot DIR        Path to rendered configs (typically phase3-nornir/build/)
--bf-host IP          Batfish server (default: netdevops-srv.lab.local)
--network NAME        Batfish network name (default: evpn-lab)
--snapshot-name NAME  Batfish snapshot name (default: rendered)
--debug               Verbose pybatfish logging
```

## Tests

Pure-function unit tests under `tests/` use mocked pybatfish - no Batfish container needed. The container is only required for end-to-end validation against real rendered configs.

```bash
~/.venvs/evpn-lab/bin/python -m pytest
```

## CI integration (Phase 6)

Phase 6 wires Batfish as pipeline stage 6 (`Batfish Validate`) and stage 7 (`Batfish Results -> PR Comment`). The bot posts the per-check report inline on the PR. PROJECT_PLAN.md Phase 6 has the full workflow.

## Layout

```
phase4-batfish/
  README.md             this file
  docker-compose.yml    Batfish container spec for netdevops-srv
  requirements.txt      pybatfish + pandas
  requirements-dev.txt  + pytest
  pytest.ini            test runner config
  validate.py           entry point: snapshot -> Batfish -> report -> exit 0/1
  questions.py          check definitions (one function per check, all in ALL_CHECKS list)
  tests/
    test_questions.py   unit tests with mocked pybatfish
```
