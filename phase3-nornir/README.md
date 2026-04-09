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

## Success criterion

Rendered configs match `../phase2-fabric/configs/*.conf` byte-for-byte (ignoring `version`, `## Last changed`, and salted `encrypted-password` text). Any diff is either a template bug or a NetBox modeling gap and must be resolved before deploy.

## Layout

```
phase3-nornir/
  nornir.yml                 Inventory plugin = NetBoxInventory2
  vars/junos_defaults.yml    Platform/hardware constants ONLY (chassis, MTU,
                             BGP timers, fxp0 lab quirk). NEVER any auth-adjacent
                             material - that lives in env (see Secrets section).
  tasks/
    enrich.py                pynetbox -> host.data hydration; derives the
                             deterministic SHA-512 login hash from env
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

## Phased rollout

For a first commit on a fresh template (or after a recovery), commit one device at a time and verify health between each. The Nornir `nr.filter()` API supports this:

```python
single = nr.filter(name="dc1-spine2")    # least-impactful spine
single.run(task=napalm_deploy, ...)
```

Smoke + manual SSH check between rollout steps. Only after the first device is confirmed do you fan out to the rest.
