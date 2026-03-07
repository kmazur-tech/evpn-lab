# Phase 1 - NetBox as Source of Truth

NetBox serves as the single source of truth for the entire EVPN-VXLAN fabric. Every piece of network data - devices, interfaces, IPs, VLANs, ASNs, cabling - lives here and is consumed by automation in later phases.

**Status:** Implemented. Population script tested and idempotent.

## Prerequisites

- NetBox 4.5+ running and accessible (see project root for deployment)
- Python 3.10+
- Environment file configured (see below)

## Environment Setup

Environment-specific values (IPs, tokens, credentials) are passed as environment variables. They never appear in the repo.

1. Copy `.env.example` from the repo root
2. Fill in your NetBox URL, API token, and management IPs
3. Export the variables before running `populate.py`

```bash
export NETBOX_URL=http://<your-netbox-host>:8000
export NETBOX_TOKEN=<your-api-token>
export MGMT_SUBNET=<your-mgmt-cidr>
export MGMT_dc1_spine1=<ip/mask>
export MGMT_dc1_spine2=<ip/mask>
export MGMT_dc1_leaf1=<ip/mask>
export MGMT_dc1_leaf2=<ip/mask>
```

## Design Decisions

### Why NetBox?

- REST API for programmatic access (Nornir, Ansible, CI/CD)
- Native support for L2VPN/VXLAN-EVPN objects, route targets, VRFs
- Custom fields for EVPN-specific data (VNI, ESI)
- Config contexts for structured, role-based configuration data

### Addressing Scheme

| Block | Purpose |
|-------|---------|
| `$MGMT_SUBNET` | Management (OOB, containerlab mgmt bridge) |
| 10.0.0.0/24 | Inter-DC P2P links (Phase 10) |
| 10.1.0.0/16 | DC1 infrastructure supernet |
| 10.1.0.0/22 | DC1 loopbacks (all routing instances) |
| 10.1.4.0/24 | DC1 P2P spine-leaf links (/31s) |
| 10.2.0.0/16 | DC2 infrastructure supernet (Phase 10) |
| 10.10.0.0/16 | Tenant supernet (stretched across DCs via VXLAN) |
| 10.11.0.0/16 | DC1 local tenant subnets |
| 10.12.0.0/16 | DC2 local tenant subnets (Phase 10) |

Each DC summarizes to a single /16. Loopbacks summarize to /22 (room for 4 VRFs).
Management addressing is environment-specific - see `.env.example` in the repo root.

### BGP ASN Allocation

eBGP underlay with unique ASN per device:

| ASN | Device |
|-----|--------|
| 65001 | dc1-spine1 |
| 65002 | dc1-spine2 |
| 65003 | dc1-leaf1 |
| 65004 | dc1-leaf2 |

ASN objects are created as the authoritative registry. Per-device ASN is stored in `local_context_data` (`{"bgp_asn": 65001}`) because NetBox ASN objects can only be assigned to sites, not individual devices. This keeps the ASN accessible in templates via `device.local_context_data.bgp_asn`.

### Juniper Routing Instance Model (ERB)

Follows Juniper's Edge-Routed Bridging architecture:

| Instance | Type | Scope | Purpose |
|----------|------|-------|---------|
| default (master) | - | All devices | Underlay eBGP + overlay eBGP (`family evpn signaling`) |
| EVPN-VXLAN | `virtual-switch` | Leaves only | L2 bridge domains, VLANs, VNIs, VXLAN encap |
| TENANT-1 | `vrf` | Leaves only | L3 inter-VLAN routing via IRB, L3VNI, Type-5 routes |
| mgmt_junos | built-in | All devices | OOB management (fxp0, mgmt default route) |

### Custom Fields vs Config Contexts vs Derived Values

- **Custom fields** - scalar per-object values for EVPN/VXLAN: `vni` (on VLANs), `l3vni` and `anycast_mac` (on VRFs), `esi` (on interfaces)
- **Config contexts** - structured data assigned by role: routing instance definitions, underlay/overlay BGP settings, hardening parameters
- **Derived at render time** - route distinguisher (from loopback IP)

### VXLAN / EVPN Modeling

- VNI-to-VLAN mapping via `vni` custom field on VLAN objects
- L2VPN objects (type `vxlan-evpn`) with route target terminations for the overlay
- L3VNI stored as custom field on VRF
- ESI stored as custom field on LAG interfaces (same ESI on both leaves = active-active multihoming)
- Anycast gateway: same IP on IRB interfaces across all leaves, IP role=`anycast`, shared MAC in VRF custom field
- Prefix role for tenant subnets is "Server" - the anycast nature is expressed at the IP address level, not the prefix level

## Data Model

See [NETBOX_DATA_MODEL.md](NETBOX_DATA_MODEL.md) for the complete ordered list of objects to populate.

The model separates:
- **Phase 1 scope** (Steps 1-13, ~80 objects) - core infrastructure populated now
- **Pre-staged for later** (Steps 14-17) - overlay, L2VPN, IRB, config contexts

## Files

| File | Description |
|------|-------------|
| `NETBOX_DATA_MODEL.md` | Complete NetBox object inventory (dependency-ordered, 17 steps) |
| `netbox-data.yml` | Structured YAML data consumed by `populate.py` (Steps 1-13) |
| `populate.py` | Idempotent Python script to populate NetBox via pynetbox |
| `requirements.txt` | Python dependencies |

## Usage

```bash
# 1. Set up environment variables
export NETBOX_URL=http://<your-netbox-host>:8000
export NETBOX_TOKEN=<your-api-token>

# 2. Install dependencies
pip install -r requirements.txt

# 3. Populate NetBox (idempotent - safe to re-run)
python populate.py
```

## Definition of Done

Phase 1 is complete when:

- [ ] All Step 1-13 objects exist in NetBox
- [ ] `populate.py` runs idempotently (second run creates no new objects)
- [ ] Every device has correct interfaces, IPs, and cables
- [ ] ASN objects exist and per-device ASN is in `local_context_data`
- [ ] Loopback IPs (lo0.1) are set as `primary_ip4` on network devices
- [ ] OOB IPs (fxp0) are set as `oob_ip` on network devices
- [ ] NetBox topology view shows correct spine-leaf connectivity
