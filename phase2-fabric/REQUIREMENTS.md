# Phase 2 - Runtime Requirements

Tools required on the **containerlab host** (the machine running the lab,
not the workstation editing the configs). For this project that's the
lab server documented in `memory/reference_lab_server.md`.

## Required packages

| Package | Used by | Purpose |
|---------|---------|---------|
| `containerlab` >= 0.74 | `dc1.clab.yml` | Topology orchestration |
| `docker` | clab + smoke tests | Container runtime, `docker exec/pause/unpause` for hosts |
| `sshpass` | `smoke-tests.sh` | Non-interactive SSH to vJunos devices (TestLabPass1) |
| `jq` >= 1.6 | `smoke-tests.sh` | Parse `| display json` output from Junos for fragile fields (BGP neighbor state, BFD diag, interface counters). Replaces brittle awk/sed positional parsing. |
| `python3` | optional | Fallback JSON parser if jq is unavailable |
| `nsenter` (util-linux) | `smoke-tests.sh` | Enter host network namespaces for ping tests without paying docker exec startup cost |

## Install on Ubuntu/Debian

```bash
apt-get install -y sshpass jq
```

`docker`, `containerlab`, `python3`, and `nsenter` come pre-installed on
the lab server image.

## Required vrnetlab images

Loaded into local Docker registry on the lab server:

| Image | Source |
|-------|--------|
| `vrnetlab/juniper_vjunos-switch:23.2R1.14` | Built from vrnetlab + Juniper free download |

See `memory/project_containerlab_state.md` for the build procedure.

## Required environment variables (containerlab deploy time)

Set before `containerlab deploy -t dc1.clab.yml`:

```bash
export CLAB_BRIDGE=br-clab
export MGMT_SUBNET=172.16.18.0/24
export MGMT_GATEWAY=clab-host.lab.local
export CLAB_IP_dc1_spine1=172.16.18.160
export CLAB_IP_dc1_spine2=172.16.18.161
export CLAB_IP_dc1_leaf1=172.16.18.162
export CLAB_IP_dc1_leaf2=172.16.18.163
```

These are referenced as `${VAR}` in `dc1.clab.yml`. See `.env.example`
in the repo root for the full list.
