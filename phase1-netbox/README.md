# Phase 1 - NetBox as Source of Truth

NetBox serves as the single source of truth for the entire EVPN-VXLAN fabric. Every piece of network data - devices, interfaces, IPs, VLANs, ASNs, cabling - lives here and is consumed by automation in later phases.

## Design Decisions

### Why NetBox?

- REST API for programmatic access (Nornir, Ansible, CI/CD)
- Native support for L2VPN/VXLAN-EVPN objects, route targets, VRFs
- Custom fields for EVPN-specific data (ASN, VNI, ESI)
- Config contexts for structured, role-based configuration data

### Addressing Scheme

| Block | Purpose |
|-------|---------|
| `$MGMT_SUBNET` | Management (OOB, containerlab mgmt bridge) |
| 10.0.0.0/24 | Loopbacks (router-ID, VTEP source) |
| 10.0.1.0/24 | DC1 point-to-point spine-leaf links (/31s) |
| 10.10.10.0/24 | VLAN 10 - tenant server subnet |
| 10.10.20.0/24 | VLAN 20 - tenant server subnet |

Management addressing is environment-specific - see `.env.example` in the repo root.

### BGP ASN Allocation

eBGP underlay with unique ASN per device:

| ASN | Device |
|-----|--------|
| 65001 | dc1-spine1 |
| 65002 | dc1-spine2 |
| 65003 | dc1-leaf1 |
| 65004 | dc1-leaf2 |

### Custom Fields vs Config Contexts

- **Custom fields** - scalar per-object values consumed directly in templates: `bgp_asn`, `vni`, `esi`, `vtep_source`, `route_distinguisher`, `l3vni`, `anycast_mac`
- **Config contexts** - structured data assigned by role/site, merged at query time: underlay BGP settings, overlay EVPN settings, hardening parameters

### VXLAN / EVPN Modeling

- VNI-to-VLAN mapping via `vni` custom field on VLAN objects
- L2VPN objects (type `vxlan-evpn`) with route target terminations for the overlay
- L3VNI stored as custom field on VRF
- ESI stored as custom field on LAG interfaces (same ESI on both leaves = active-active multihoming)
- Anycast gateway: same IP on IRB interfaces across all leaves, IP role=`anycast`, shared MAC in VRF custom field

## Data Model

See [NETBOX_DATA_MODEL.md](NETBOX_DATA_MODEL.md) for the complete ordered list of ~85 objects to populate.

## Files

| File | Description |
|------|-------------|
| `NETBOX_DATA_MODEL.md` | Complete NetBox object inventory (dependency-ordered, 15 phases) |
| `populate.py` | Idempotent Python script to populate NetBox via pynetbox (TODO) |

## Usage

```bash
# 1. Set up environment variables (see .env.example in repo root)
export NETBOX_URL=http://<your-netbox-host>:8000
export NETBOX_TOKEN=<your-api-token>

# 2. Install dependencies
pip install pynetbox

# 3. Populate NetBox (idempotent - safe to re-run)
python populate.py
```
