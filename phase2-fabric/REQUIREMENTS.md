# Phase 2 - Runtime Requirements

Tools required on the **containerlab host** (the machine running the lab,
not the workstation editing the configs). Any Linux host with Docker +
containerlab + the packages below will do; a dedicated bare-metal box
with hardware KVM is recommended for vJunos images because they need
real CPU virtualisation (the vrnetlab build wraps the emulator in
qemu and expects `/dev/kvm`).

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

Built from the upstream [vrnetlab](https://github.com/hellt/vrnetlab) repo
plus a Juniper vJunos-switch image download (free, registration required).
The build procedure is standard vrnetlab: drop the qcow2 image into the
appropriate subdirectory, run `make`, push to your local Docker registry.
No project-specific steps.

## Required environment variables (containerlab deploy time)

All device addressing and credentials live in **`evpn-lab-env/env.sh`**
(outside the repo, sibling of the project root). Source it before
running `containerlab deploy` or `bash smoke-tests.sh`:

```bash
source ../../evpn-lab-env/env.sh
containerlab deploy -t dc1.clab.yml
```

The variables `dc1.clab.yml` and `smoke-tests.sh` consume:

| Variable | Used by | Purpose |
|----------|---------|---------|
| `CLAB_BRIDGE` | `dc1.clab.yml` mgmt | Linux bridge for mgmt network |
| `MGMT_SUBNET` | `dc1.clab.yml` mgmt | Mgmt CIDR (example shape: `<your-mgmt-net>/24`) |
| `MGMT_GATEWAY` | `dc1.clab.yml` mgmt | Mgmt gateway IP |
| `CLAB_IP_dc1_spine1`..`spine2` | `dc1.clab.yml` node mgmt-ipv4 | Spine mgmt IPs |
| `CLAB_IP_dc1_leaf1`..`leaf2` | `dc1.clab.yml` node mgmt-ipv4 | Leaf mgmt IPs |
| `CLAB_IP_dc1_host1`..`host4` | `dc1.clab.yml` node mgmt-ipv4 | Linux host container mgmt IPs |
| `MGMT_dc1_spine1`..`leaf2` | `smoke-tests.sh` SSH targets | Same IPs as CLAB_IP_*, but in CIDR form for pynetbox/NetBox |
| `JUNOS_SSH_USER` / `JUNOS_SSH_PASSWORD` | `smoke-tests.sh` `junos_cmd` | Login for Junos CLI sessions |
| `CLAB_HOST` | doc references | Lab server IP (where Docker + clab live) |

`smoke-tests.sh` auto-sources `evpn-lab-env/env.sh` if `MGMT_dc1_spine1`
isn't already set, and hard-fails with an actionable message if any
required variable is still missing. No literal IPs or credentials live
inside `smoke-tests.sh`, `dc1.clab.yml`, or any other repo file.

A different lab deployment (different DC, different IP plan, different
credentials) sets its own values in its own `evpn-lab-env/env.sh` and
the repo runs unchanged.
