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
    -> NAPALM load_replace_candidate + commit
    -> phase2-fabric/smoke-tests.sh  (deploy gate)
```

## Success criterion

Rendered configs match `../phase2-fabric/configs/*.conf` byte-for-byte (ignoring `version` and `## Last changed` lines). Any diff is either a template bug or a NetBox modeling gap and must be resolved before deploy.

## Layout

```
phase3-nornir/
  nornir.yml             Inventory plugin = NetBoxInventory2
  vars/junos_defaults.yml Platform/hardware constants (chassis, MTU, BGP timers)
  tasks/
    enrich.py            pynetbox -> host.data hydration
    render.py            Jinja2 template -> build/<host>.conf
    diff_baseline.py     Unified diff vs phase2-fabric/configs/
    deploy.py            NAPALM load_replace + commit
  templates/junos/
    main.j2              Top-level: includes all partials
    routing_options.j2   First template (router-id, ASN, LOAD-BALANCE)
    ...                  More partials added incrementally
  deploy.py              Entry point
  build/                 Rendered output (gitignored)
```

## Running

Run from WSL2 Debian (the venv `~/.venvs/evpn-lab` already has `pynetbox`+`pyyaml`; the nornir stack gets installed on top of it):

```
source ../../evpn-lab-env/env.sh        # NETBOX_URL, NETBOX_TOKEN, MGMT_*
~/.venvs/evpn-lab/bin/python deploy.py --check     # render + baseline diff, no devices
~/.venvs/evpn-lab/bin/python deploy.py --dry-run   # NAPALM compare, no commit
~/.venvs/evpn-lab/bin/python deploy.py --commit    # render + deploy + smoke
```

## Secrets

`JUNOS_ROOT_HASH` and `JUNOS_ADMIN_HASH` are read from the env file. Never hardcoded in templates or NetBox.
