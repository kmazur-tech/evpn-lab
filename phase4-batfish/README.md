# Phase 4 - Batfish Pre-Deployment Validation

Offline validation of rendered Junos configs before they touch a real device. Catches a class of bugs (BGP topology errors, undefined policy references, parse failures, loopback unreachability) that the Phase 3 regression gate and on-disk guard don't cover - and catches them **without** spinning up the lab.

## What it validates

| Check | What it catches | Confidence |
|---|---|---|
| `init_issues` | Vendor-model conversion errors, feature-not-supported, red flags across the snapshot. Broadest catch-all - recommended by pybatfish docs as the FIRST check | High - hard fail on `Convert error`, info on `Convert warning` (incl. `redflag`), known Junos EVPN false positives filtered |
| `parse_status` | Batfish cannot parse the config at all | High - hard fail on FAILED, warning on PARTIALLY_UNRECOGNIZED (Junos features Batfish doesn't fully model) |
| `bgp_sessions` | ASN mismatch, missing peer config, unreachable peer, wrong family | High - the most useful single Batfish check for an EVPN lab |
| `bgp_edges_symmetric` | One side defines a peer the other doesn't (asymmetric template bug) | High |
| `undefined_references` | Template emits `vrf-import EVPN-IMPORT-X` but no policy named EVPN-IMPORT-X is defined | High - catches Phase 3 template typos |
| `overlay_loopback_reachability` | iBGP overlay peer loopbacks not present in BGP RIB (overlay can't establish) | High |

## What it does NOT validate (intentionally)

The Phase 2 smoke suite (~76 checks) covers the runtime side: actual BGP convergence, EVPN Type-2/3/5 propagation, ESI-LAG behavior, DF election, BFD timers, MAC learning, anycast gateway reachability. Batfish would either duplicate that work poorly (its EVPN/VXLAN modeling is partial) or simply can't simulate those behaviors at all. The split is deliberate:

- **Phase 4 Batfish** = "did the templates produce structurally valid configs?" Runs in seconds, no devices needed.
- **Phase 2 smoke** = "does the deployed fabric actually work?" Runs against the live lab.

## Environment configuration

The Batfish server hostname/IP is environment-specific and lives in the external env file (same one Phase 1/3 use):

```bash
# evpn-lab-env/env.sh (outside the repo, gitignored)
export BATFISH_HOST="<the IP or hostname of your netdevops services VM>"
```

`validate.py` reads `$BATFISH_HOST` from env. CLI flag `--bf-host` overrides env. Hard-fails with a clear pointer if neither is set.

The Batfish container itself runs on the same VM that hosts NetBox - no new infra, just a second container alongside the existing NetBox stack. If you put NetBox somewhere else, put Batfish there too and update `BATFISH_HOST` accordingly.

## Setup - one-time deployment of the Batfish container

```bash
# 1. SSH to the netdevops services VM (the host whose IP is in $BATFISH_HOST).
#    Substitute the right value below; the README does not pin a specific IP
#    because that's environment-specific:
source ../evpn-lab-env/env.sh   # picks up BATFISH_HOST from your env file
ssh root@"$BATFISH_HOST"

# 2. Create the deployment directory and copy the docker-compose.yml.
#    From a dev box (one-liner scp):
#    scp phase4-batfish/docker-compose.yml root@"$BATFISH_HOST":/opt/batfish/docker-compose.yml
#    On the server:
mkdir -p /opt/batfish && cd /opt/batfish
# (paste or scp the docker-compose.yml from this directory)

# 3. Pull the image (~1.5 GB, one-time)
docker compose pull

# 4. Start
docker compose up -d

# 5. Verify it's running and the API is reachable
docker compose ps
docker compose logs --tail=20 batfish

# 6. From your dev box, verify TCP/9996 + 9997 are reachable:
source ../evpn-lab-env/env.sh
nc -zv "$BATFISH_HOST" 9996  # expect: connection succeeded
nc -zv "$BATFISH_HOST" 9997  # expect: connection succeeded
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
# Source the env file (sets BATFISH_HOST, NETBOX_URL, etc)
source ../evpn-lab-env/env.sh

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

Probing Batfish at <BATFISH_HOST>:9996...
  reachable
Connecting to Batfish at <BATFISH_HOST>:9996...
Initializing snapshot 'rendered' (this can take 30-60s)...

============================================================
 Batfish validation report
============================================================
  [OK  ] parse_status                4 file(s) parsed
  [OK  ] bgp_sessions                16/16 session(s) ESTABLISHED
  [OK  ] bgp_edges_symmetric         16 BGP edge(s), all symmetric
  [OK  ] undefined_references        no undefined references
  [OK  ] overlay_loopback_reachability  8 iBGP overlay peer loopback(s) reachable
============================================================
 RESULT: PASS (5 check(s))
============================================================
```

If the Batfish container isn't reachable, validate.py fails fast with an actionable error before any pybatfish API call:

```
Probing Batfish at <BATFISH_HOST>:9996...
ERROR: Batfish coordinator unreachable at <BATFISH_HOST>:9996 (...).

Is the container running?
  ssh root@<BATFISH_HOST> 'docker compose -f /opt/batfish/docker-compose.yml ps'

Is BATFISH_HOST set correctly?
  source <repo-root>/../evpn-lab-env/env.sh
  echo $BATFISH_HOST

See phase4-batfish/README.md for one-time deployment instructions.
```

### Wired into Phase 3 deploy.py via opt-in flag

```bash
~/.venvs/evpn-lab/bin/python phase3-nornir/deploy.py --full --validate
```

The `--validate` flag is opt-in (off by default) so the inner-loop `--check` workflow stays fast. CI invokes both deploy.py and validate.py as separate stages so a Batfish failure blocks the merge before NAPALM is ever called.

### CLI options

```
--snapshot DIR              Path to rendered configs (typically phase3-nornir/build/)
--reference-snapshot DIR    Optional reference for differential analysis
                            (typically phase3-nornir/expected/, the renderer's
                            golden file). When set, validate.py initializes
                            both snapshots and runs differential analysis
                            after the regular checks. INFORMATIONAL ONLY -
                            differential output never affects exit code.
--bf-host IP                Batfish server (default: $BATFISH_HOST env var)
--network NAME              Batfish network name (default: evpn-lab)
--snapshot-name NAME        Batfish snapshot name (default: rendered)
--format text|json          Output format. text (default) is human-readable;
                            json is machine-readable for the Phase 6 PR-comment bot
--debug                     Verbose pybatfish logging
```

### Differential analysis (PR-style "what changed?")

The killer Batfish feature for CI: compare a candidate snapshot (this PR's render) against a reference snapshot (the last known-good golden file) and report what changed.

```bash
python validate.py \
  --snapshot ../phase3-nornir/build/ \
  --reference-snapshot ../phase3-nornir/expected/
```

Output (when `build/` == `expected/`, i.e. an idempotent re-render):

```
============================================================
 Batfish differential analysis (candidate vs reference)
============================================================
  [DIFF] devices                    no changes (4 device(s), identical to reference)
  [DIFF] bgp_edges                  no changes (16 BGP edges, identical to reference)
============================================================
```

When the candidate ADDS a BGP session (e.g. spine-spine link added in NetBox):

```
  [DIFF] bgp_edges                  1 added, 0 removed (candidate has 17, reference had 16)
    + dc1-spine1(10.1.4.8) -> dc1-spine2(10.1.4.9)
```

When a device is decommissioned:

```
  [DIFF] devices                    0 added, 1 removed (candidate has 3, reference had 4)
    - dc1-leaf2
```

The differential layer is **informational only** — exit code is unaffected by what it finds. Phase 6's PR-comment bot consumes the JSON output (`--format json` adds a `diffs` field to the top-level payload) and posts it as the "what does this PR change?" report on the PR.

### JSON output (for CI)

```bash
python validate.py --snapshot ../phase3-nornir/build/ --format json
```

Stable schema:

```json
{
  "result": "PASS",
  "total": 6,
  "passed": 6,
  "failed": 0,
  "checks": [
    {"name": "init_issues", "passed": true, "summary": "no init errors; 32 warning(s) - ...", "detail": ""},
    ...
  ]
}
```

The Phase 6 CI workflow will consume this and post it as a PR comment via a small renderer.

## Tests

Two test paths:

**Unit tests** (default, offline, ~1 second):

```bash
~/.venvs/evpn-lab/bin/python -m pytest
```

Pure-function tests with mocked pybatfish. No Batfish container needed. CI runs this on every PR.

**Integration tests** (require a real Batfish container):

```bash
source ../../evpn-lab-env/env.sh   # picks up BATFISH_HOST
~/.venvs/evpn-lab/bin/python -m pytest -m integration
```

Pytest fixtures with module scope (per Said van de Klundert's pytest+Batfish pattern) - the ~30s snapshot init cost is paid ONCE per fixture and amortized across the whole integration run. Skipped automatically if `$BATFISH_HOST` is unset or the container is unreachable.

Run both unit and integration: `pytest -m "integration or not integration"`.

Coverage:
- `test_questions.py` - 26 tests pinning the 6 checks against canned pandas DataFrames (init_issues severity matching + false-positive filter, parse status, BGP session counts, edge symmetry, undefined-ref ignore list, iBGP filter using Session_Type to avoid the Local_AS/Remote_AS dtype mismatch)
- `test_validate.py` - tests for `stage_snapshot()` filter logic (excludes pre-commit backups and per-stanza files), `check_reachable()` TCP probe behavior
- `test_json_format.py` - 6 tests pinning the JSON output schema (top-level result/total/passed/failed/checks fields, dataclass-asdict mapping, edge cases)
- `test_diffs.py` - 10 tests pinning the differential analysis layer (`diff_bgp_edges`, `diff_node_set`) with snapshot-aware mock sessions
- `test_integration.py` - 7 integration tests against a real Batfish container, including: full check suite passes against current Phase 3 render, init_issues clean after false-positive filter, BGP sessions 16/16, overlay loopbacks reachable (regression guard for the Session_Type dtype fix), differential self-compare reports zero changes (`build/` vs `expected/` identical state), JSON output round-trip

## CI integration (Phase 6)

Phase 6 wires Batfish as pipeline stage 6 (`Batfish Validate`) and stage 7 (`Batfish Results -> PR Comment`). The bot posts the per-check report inline on the PR. CI injects `BATFISH_HOST` from GitHub Actions secrets into the workflow env. PROJECT_PLAN.md Phase 6 has the full workflow.

## Layout

```
phase4-batfish/
  README.md             this file
  docker-compose.yml    Batfish container spec for the netdevops services VM
  requirements.txt      pybatfish + pandas
  requirements-dev.txt  + pytest
  pytest.ini            test runner config
  validate.py           entry point: snapshot -> reachability probe -> Batfish -> report -> exit 0/1
  questions.py          check definitions (one function per check, all in ALL_CHECKS list)
  tests/
    test_questions.py   unit tests for the 5 checks (mocked pybatfish)
    test_validate.py    unit tests for stage_snapshot() and check_reachable()
```
