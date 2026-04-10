# Phase 3 - Nornir IaC

NetBox-driven Junos configuration rendering and deployment for the DC1 EVPN-VXLAN fabric.

## Pipeline

```
NetBox (SoT)
    -> Nornir NetBoxInventory2 (hosts, platform, primary_ip)
    -> tasks/enrich.py (pynetbox: VLANs, VNIs, VRFs, anycast GW, cables, interfaces)
    -> Jinja2 templates (templates/junos/)
    -> build/<host>.conf
    -> diff vs phase2-fabric/configs/<host>.conf  (regression gate)
    -> on-disk deploy guard (independent grep, see Safety section)
    -> NAPALM load_replace_candidate + commit
    -> phase2-fabric/smoke-tests.sh  (run separately, wired into CI in Phase 6)
```

## Success criterion - the regression gate authority

Rendered configs match **`phase3-nornir/expected/*.conf`** byte-for-byte (ignoring `version`, `## Last changed`, and salted `encrypted-password` text). Any diff is either a template bug or a NetBox modeling gap and must be resolved before deploy.

`phase3-nornir/expected/` is the **renderer's contract with itself**: a snapshot of the last known-good output of `main.j2` for each device, committed to git. When a template changes, the workflow is:

```
python deploy.py --full       # render into build/
cp build/*.conf expected/     # refresh golden files
git add expected/ templates/  # commit template + baseline together
```

This is the **golden-file testing pattern** ([Nautobot Golden Config](https://docs.nautobot.com/projects/golden-config/en/latest/) uses the same idea). It catches accidental template changes between commits without coupling Phase 3 to any specific historic config.

### What about `phase2-fabric/configs/*.conf`?

`phase2-fabric/configs/*.conf` are the **clab startup configs** Phase 2 hand-wrote. They are loaded into vJunos at boot when `containerlab deploy` runs, then `python deploy.py --commit` overwrites the running config with the canonical-order rendered output. They are **not** the Phase 3 regression authority and do NOT need to match `expected/`. The two diverge intentionally:

- Phase 2 baselines have hand-written ordering and per-device random-salt password hashes (one-shot Junos-generated)
- Phase 3 expected/ has Junos canonical ordering and the deterministic `$6$evpnlab1$` hash from env

**Semantic validation** of intent (does this BGP session converge? are these prefixes reachable? do these ACLs block control plane?) is NOT done by the byte-diff regression gate — that's **Phase 4 (Batfish)** territory. The byte-diff catches structural template drift; Batfish will catch semantic intent drift.

## Layout

```
phase3-nornir/
  nornir.yml                 Inventory plugin = NetBoxInventory2
  vars/junos_defaults.yml    Platform/hardware constants ONLY (chassis, MTU,
                             BGP timers, fxp0 lab quirk). NEVER any auth-adjacent
                             material - that lives in env (see Secrets section).
  tasks/
    enrich/                  Per-domain NetBox enrichment, package layout:
      __init__.py              Re-exports public API (enrich_from_netbox,
                               derive_login_hash, helpers used by tests)
      models.py                Pydantic models for HostData and every
                               sub-shape (FabricLink, Lag, Irb, Tenant,
                               LoopbackUnit, BgpUnderlayNeighbor, ...).
                               Single validation point catches NetBox
                               schema drift before templates render.
      helpers.py               Pure helpers (lo0 unit parser, lo0
                               description mapper)
      auth.py                  derive_login_hash (env -> $6$ via passlib)
      interfaces.py            Fabric P2P + access + LAG members + ESI-LAG
                               parents + IRB collection
      loopbacks.py             lo0.* unit collection
      bgp.py                   Underlay + overlay neighbor derivation
      tenants.py               Tenant VRFs + MAC-VRF VLAN bindings
      main.py                  Nornir task entry point - calls each
                               collector, validates HostData, dumps to
                               task.host as plain dicts
    deploy.py                NAPALM napalm_configure (load_replace_candidate)
  templates/junos/
    main.j2                  Top-level: includes all partials in Junos order
    system.j2                hostname + auth (env-supplied real hash) + services
    chassis.j2               platform constants
    interfaces.j2            fabric P2P, access, LAG members, ESI-LAG, fxp0,
                             irb (anycast + leaf-local), lo0
    forwarding_options.j2    storm-control profile (leaves only)
    policy_options.j2        EXPORT-LOOPBACK, LOAD-BALANCE, EVPN community/policies
    routing_instances.j2     EVPN-VXLAN mac-vrf, tenant VRF, mgmt_junos
    routing_options.j2       router-id, graceful-restart, forwarding-table
    protocols.j2             BGP underlay/overlay, network-isolation, LLDP
  deploy.py                  Entry point with --check / --full / --dry-run / --commit
  build/                     Rendered output (gitignored, wiped at start of every run)
```

## Running

Run from WSL2 Debian. The venv `~/.venvs/evpn-lab` has nornir, nornir-netbox, nornir-napalm, nornir-jinja2, pynetbox, napalm, junos-eznc:

```
source ../../evpn-lab-env/env.sh        # all required env vars (see below)
~/.venvs/evpn-lab/bin/python deploy.py --check     # per-stanza diff vs baseline, no devices touched
~/.venvs/evpn-lab/bin/python deploy.py --full      # full main.j2 diff vs entire baseline file
~/.venvs/evpn-lab/bin/python deploy.py --dry-run   # full + on-disk guard + NAPALM compare_config
~/.venvs/evpn-lab/bin/python deploy.py --commit    # full + on-disk guard + NAPALM commit
```

`--commit` does NOT run smoke tests automatically. Run smoke separately:
```
bash ../phase2-fabric/smoke-tests.sh
```
Smoke is wired in as a CI stage in Phase 6 (`fabric-ci.yml`), not inside `deploy.py`.

## Secrets and credential material

The lab reads four credential-related env vars from `../../evpn-lab-env/env.sh` (outside the repo, gitignored):

| Variable | Purpose |
|----------|---------|
| `NETBOX_TOKEN` | NetBox API auth (intent fetch) |
| `JUNOS_SSH_USER` / `JUNOS_SSH_PASSWORD` | SSH login NAPALM uses to reach each device |
| `JUNOS_LOGIN_PASSWORD` | Plaintext password rendered into the device login config |
| `JUNOS_LOGIN_SALT` | Fixed crypt(3) salt for deterministic SHA-512 hash derivation |

`JUNOS_LOGIN_PASSWORD` and `JUNOS_LOGIN_SALT` are fed to `crypt.crypt()` at render time to produce a stable `$6$<salt>$<86char>` hash. The hash is deterministic - re-rendering produces the same bytes - so deploys are idempotent.

The salt is NOT cryptographically secret (it appears in clear inside the rendered hash), but it IS environment-specific. Different deployments of this lab pick different salts and store them as part of their credential bundle.

### PRODUCTION: pull credentials from a vault, not an env file

For lab use we keep `evpn-lab-env/env.sh` outside the repo and source it manually. **For production this is NOT acceptable.** Replace the env-file approach with:

- **HashiCorp Vault**: `vault kv get -format=json secret/junos/login` -> shell exports, or use the `hvac` Python client directly inside `deploy.py` and `tasks/enrich.py` to fetch values at task time.
- **AWS Secrets Manager / GCP Secret Manager / Azure Key Vault**: same pattern via the corresponding cloud SDK.
- **sops + age/PGP** or **git-crypt**: encrypted secrets in the repo, decrypted on demand.

The contract `deploy.py` expects is: at task entry time, `os.environ["JUNOS_LOGIN_PASSWORD"]` and `os.environ["JUNOS_LOGIN_SALT"]` are populated with real values. Where they came from is up to the operator. A vault-backed entry script wraps `deploy.py`:

```bash
#!/usr/bin/env bash
# vault-deploy.sh - production wrapper
set -euo pipefail
eval "$(vault kv get -format=json secret/evpn-lab/junos | \
  jq -r 'to_entries[] | "export \(.key)=\(.value | @sh)"')"
exec python /opt/evpn-lab/phase3-nornir/deploy.py "$@"
```

The repo never sees the values; `deploy.py` never reads files; rotation is a vault update plus a re-deploy.

## Safety - the two-layer guard

`deploy.py` has two independent safety layers because **Junos `compare_config` masks SECRET-DATA fields** (encrypted passwords, keys, certificates). A bad value in a secret field will pass NAPALM dry-run with "no diff" - we discovered this the hard way after a placeholder hash got committed to all 4 devices and locked everyone out.

| Layer | What it checks | Purpose |
|-------|----------------|---------|
| **Regression gate** (`render_full_and_diff`) | Rendered config == Phase 2 baseline (with normalized noise: salted hashes, version line, timestamps) | "Templates produce structurally identical output to the hand-built reference" |
| **On-disk deploy guard** (`assert_safe_to_deploy`) | `build/<host>.conf` contains no sentinel strings (`PLACEHOLDER`, `render-time-only`, `<HASH>`, etc.) AND every `encrypted-password` line matches `^encrypted-password "\$6\$[^$]+\$[A-Za-z0-9./]{86}"$` | "The bytes that will be loaded onto the device contain real, valid credential material" |

Both must pass before NAPALM is called. The regression gate normalizes salted-hash text because the content is opaque noise, not structure - but the deploy guard scans the unmodified rendered file independently.

If you add a new template that emits a secret field, you MUST extend the guard's shape regex (or sentinel list) to validate it. Trusting `compare_config` alone for secret fields is the same trap that locked us out before. See [feedback_never_normalize_secrets_into_deploy](../../../.claude/projects/c--Users-tasior-Projects-evpn-lab/memory/feedback_never_normalize_secrets_into_deploy.md) for the incident postmortem.

## Tests

Pure-function unit tests under `tests/`. No NetBox, no devices, no env vars (each test that needs env uses `monkeypatch`). Run from WSL2:

```
cd phase3-nornir
~/.venvs/evpn-lab/bin/pip install -r requirements-dev.txt   # one-time
~/.venvs/evpn-lab/bin/python -m pytest
```

Coverage as of Phase 3 close (60 tests, ~2 sec):

| File | What it pins | Why it matters |
|------|--------------|----------------|
| `test_deploy_guard.py` | `assert_safe_to_deploy()` rejects every sentinel + every malformed hash shape (placeholder, truncated, cleartext, MD5, empty); accepts a clean config | This is the layer whose absence caused the credential lockout. Every regression here is a deploy that could lock out the lab. |
| `test_extract_stanza.py` | Brace-balanced Junos stanza extraction: top-level, nested, indented, missing, substring-no-match, first-match | Used by the regression gate for every per-stanza diff. Bugs here = false PASS or false FAIL. Documents the known limitation of `}` inside string literals. |
| `test_normalize.py` | Diff normalizer rules + idempotence + non-secret-fields-untouched | Pins the boundary between "regression-diff noise" and "deploy-critical content". Any change to this function MUST come with deploy guard tests proving placeholder hashes still get caught. |
| `test_enrich_helpers.py` | `_lo0_unit_from_iface_name()`, `_loopback_description()`, `derive_login_hash()` (deterministic, hard-fail on missing env) | Pure mappers easy to break on refactor; the hash derivation is the postmortem fix verified to fail-fast. |
| `test_transform.py` | `fabric_inventory_transform()` mgmt-IP/platform/credential mutation | Idiomatic Nornir contract; broken transform = unreachable deploy. |

What's intentionally NOT tested at Phase 3:
- Full template rendering (would need a complete `host.data` fixture; Phase 6 scope)
- `enrich_from_netbox()` end-to-end (needs NetBox or vcrpy cassettes; Phase 6)
- NAPALM tasks (needs devices or mocked NAPALM; Phase 6)
- Live deploy / smoke (the existing manual deploy + lab-server smoke covers this)

## Phased rollout

For a first commit on a fresh template (or after a recovery), commit one device at a time and verify health between each. The Nornir `nr.filter()` API supports this:

```python
single = nr.filter(name="dc1-spine2")    # least-impactful spine
single.run(task=napalm_deploy, ...)
```

Smoke + manual SSH check between rollout steps. Only after the first device is confirmed do you fan out to the rest.
