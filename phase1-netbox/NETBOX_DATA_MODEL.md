# NetBox Data Model - EVPN-VXLAN DC Fabric Lab

This document defines every object that must be populated in NetBox to serve as Source of Truth for the EVPN-VXLAN lab. Objects are listed in dependency order (steps) - each step can only be created after all previous steps.

Environment-specific values (NetBox URL, token, management IPs) are passed as environment variables. See `.env.example` in the repo root for the required variables.

## Scope

Objects are split into two groups:

- **Phase 1 scope** (Steps 1-13) - core infrastructure model: custom fields, tags, regions, sites, device types, devices, interfaces, IPs, cables. Populated now.
- **Pre-staged for later phases** (Steps 14-17) - overlay, L2VPN, IRB, config contexts. Defined here for completeness but populated when the corresponding project phase begins.

## Lab Topology

```
                        DC1 (Juniper vJunos)

                +--------------+  +--------------+
                |  dc1-spine1  |  |  dc1-spine2  |
                +------+---+--+  +--+---+-------+
                       |   |        |   |
                   +---+   +----+---+   +---+
                   |            X           |
                   +---+   +----+---+   +---+
                       |   |        |   |
                +------+---+--+  +--+---+-------+
                |  dc1-leaf1  |  |  dc1-leaf2   |
                +-+--+--+--+-+  +-+--+--+--+---+
                  |  |  |  |      |  |  |  |
                  |  |  |  +--++--+  |  |  |
                  |  |  +--+--||--+--+  |  |
                  |  |     |  ||  |     |  |
               +--+-++ +--+--++--+--+ ++-+--+
               |host1| |    host3   | |host2|
               +-----+ +---(ae0)---+ +-----+
              (leaf1    (ESI-LAG,      (leaf2
              only)     dual-homed)    only)

                       +------------+
                       |   host4    |
                       +---(ae0)----+
                       (ESI-LAG,
                       dual-homed)

                DC2 (Arista cEOS - project Phase 10)
```

- 2x spine (vJunos-switch, EX9214 model)
- 2x leaf (vJunos-switch, EX9214 model)
- 4x Linux containers as test hosts:
  - host1: single-homed to leaf1
  - host2: single-homed to leaf2
  - host3, host4: dual-homed ESI-LAG to both leaves
- eBGP underlay (unique ASN per device), EVPN overlay, VXLAN, ESI-LAG between leaves

---

# Phase 1 Scope

## Step 1 - Custom Fields

| Name | Content Type | Type | Required | Description |
|------|-------------|------|----------|-------------|
| `vni` | ipam.vlan | Integer | No | VXLAN Network Identifier mapped to this VLAN |
| `l3vni` | ipam.vrf | Integer | No | L3 VNI for symmetric IRB routing |
| `esi` | dcim.interface | Text | No | Ethernet Segment Identifier (10-byte, colon-separated) |
| `anycast_mac` | ipam.vrf | Text | No | Shared anycast gateway MAC per VRF |

> ASN is modeled using native NetBox ASN objects (Step 7), not a custom field - single source of truth.
> VTEP source interface is a convention (lo0.0 on all leaves) defined in config context, not a per-device field.
> Route distinguisher is derived from loopback IP at config render time, not stored.

---

## Step 2 - Tags

| Name | Slug | Color | Description |
|------|------|-------|-------------|
| Spine | `spine` | Blue | Spine-layer device |
| Leaf | `leaf` | Green | Leaf-layer device |
| Underlay | `underlay` | Cyan | Underlay network (eBGP, P2P) |
| Overlay | `overlay` | Purple | Overlay network (EVPN, VXLAN) |
| ESI-LAG | `esi-lag` | Teal | ESI-LAG multihomed interface |
| Anycast-GW | `anycast-gw` | Yellow | Anycast gateway IRB |
| Management | `mgmt` | Gray | Management network |
| P2P | `p2p` | Cyan | Point-to-point link |
| Loopback | `loopback` | Indigo | Loopback interface |
| DC1 | `dc1` | Dark Blue | Data Center 1 (Juniper) |
| DC2 | `dc2` | Dark Green | Data Center 2 (Arista, project Phase 10) |

---

## Step 3 - Regions, Tenant Groups, Tenants

### Regions

```
Lab
 +-- DC1
 +-- DC2 (project Phase 10)
```

### Tenant

| Tenant Group | Tenant | Description |
|-------------|--------|-------------|
| Lab | Lab Operations | Owner of all lab devices |

---

## Step 4 - Sites

| Name | Slug | Region | Status | Tenant | Description |
|------|------|--------|--------|--------|-------------|
| DC1 | `dc1` | DC1 | Active | Lab Operations | Primary DC - Juniper fabric |

> DC2 (Arista) added in project Phase 10.

---

## Step 5 - Manufacturers, Platforms, Device Roles

### Manufacturers

| Name | Slug |
|------|------|
| Juniper Networks | `juniper` |
| Arista Networks | `arista` |
| Linux | `linux` |

### Platforms

| Name | Slug | Manufacturer | NAPALM Driver | Description |
|------|------|-------------|---------------|-------------|
| Junos | `junos` | Juniper | junos | Juniper JunOS |
| EOS | `eos` | Arista | eos | Arista EOS (project Phase 10) |
| Linux | `linux` | Linux | - | Linux container host |

### Device Roles

| Name | Slug | Color | Description |
|------|------|-------|-------------|
| Spine | `spine` | Blue | Spine / aggregation layer |
| Leaf | `leaf` | Green | Leaf / access layer (ToR) |
| Server | `server` | Teal | Compute / test host |

---

## Step 6 - Device Types (with Interface Templates)

### Juniper EX9214 (Spines & Leaves)

vjunos-switch emulates an EX9214 chassis (SMBIOS product=VM-VEX). U height set to 1 instead of real 16U - this is a simulation lab and all devices need to fit in a single 42U rack in NetBox.

| Field | Value |
|-------|-------|
| Manufacturer | Juniper Networks |
| Model | EX9214 |
| Slug | `ex9214` |
| U Height | 1 |
| Full Depth | Yes |

**Interface Templates:**

| Name | Type | Mgmt Only | Description |
|------|------|-----------|-------------|
| fxp0 | 1000BASE-T | Yes | Management |
| ge-0/0/0 | 10GBASE-X-SFP+ | No | |
| ge-0/0/1 | 10GBASE-X-SFP+ | No | |
| ge-0/0/2 | 10GBASE-X-SFP+ | No | |
| ge-0/0/3 | 10GBASE-X-SFP+ | No | |
| ge-0/0/4 | 10GBASE-X-SFP+ | No | |
| ge-0/0/5 | 10GBASE-X-SFP+ | No | |
| lo0 | Virtual | No | Loopback |

> Only lab-used interfaces templated. vjunos-switch maps eth1=ge-0/0/0, eth2=ge-0/0/1, etc.

### Linux Container Host

| Field | Value |
|-------|-------|
| Manufacturer | Linux |
| Model | Container Host |
| Slug | `container-host` |
| U Height | 0 |

**Interface Templates:**

| Name | Type |
|------|------|
| eth0 | 1000BASE-T |
| eth1 | 1000BASE-T |
| bond0 | Link Aggregation Group (LAG) |

---

## Step 7 - RIR, ASN Ranges, ASNs

### RIR

| Name | Slug | Private |
|------|------|---------|
| Private | `private` | Yes |

### ASN Range

| Name | RIR | Start | End |
|------|-----|-------|-----|
| Lab ASN Range | Private | 64512 | 65534 |

### ASN Objects

ASN objects serve as the authoritative registry. Per-device ASN is stored in `local_context_data` on each device because NetBox native ASN objects can only be assigned to sites, not individual devices.

| ASN | Description |
|-----|-------------|
| 65001 | dc1-spine1 |
| 65002 | dc1-spine2 |
| 65003 | dc1-leaf1 |
| 65004 | dc1-leaf2 |

Device `local_context_data` example: `{"bgp_asn": 65001}`

> eBGP underlay: unique ASN per device. ASN registry is in IPAM ASN objects. Per-device value is in local context for direct template access.

---

## Step 8 - VRFs, Route Targets, Prefix/VLAN Roles

### VRFs

| Name | RD | Description | Custom Fields |
|------|-----|-------------|---------------|
| TENANT-1 | auto | Tenant VRF (symmetric IRB) | l3vni=5000, anycast_mac=00:00:5e:00:01:01 |

### Route Targets

| Name | Description |
|------|-------------|
| 65000:5000 | TENANT-1 L3VNI import/export |
| 65000:10010 | VLAN 10 L2VNI |
| 65000:10020 | VLAN 20 L2VNI |

### Prefix & VLAN Roles

| Name | Slug | Description |
|------|------|-------------|
| Loopback | `loopback` | Router-ID and VTEP loopbacks |
| P2P Link | `p2p-link` | Point-to-point inter-switch links |
| Management | `management` | OOB management network |
| Server | `server` | Server/host/tenant subnets |
| VXLAN | `vxlan` | VXLAN-extended L2 segments |

> Removed "Anycast Gateway" as a prefix/VLAN role. Tenant subnets use the "Server" role. The anycast nature is expressed by the IP address role=`anycast` on the IRB interface, not by the prefix role.

---

## Step 9 - Aggregates, Prefixes

### Aggregates

| Prefix | RIR | Description |
|--------|-----|-------------|
| 10.0.0.0/8 | Private | Lab address space |
| 172.16.0.0/12 | Private | Management address space (env-specific) |

### Prefix Hierarchy

| Prefix | Site | VRF | Role | Description |
|--------|------|-----|------|-------------|
| **Management** | | | | |
| `$MGMT_SUBNET` | - | - | Management | Lab management network |
| **Loopbacks** | | | | |
| 10.0.0.0/24 | - | - | Loopback | Loopback addresses |
| **DC1 P2P** | | | | |
| 10.0.1.0/24 | DC1 | - | P2P Link | DC1 spine-leaf P2P links |
| 10.0.1.0/31 | DC1 | - | P2P Link | spine1 - leaf1 |
| 10.0.1.2/31 | DC1 | - | P2P Link | spine1 - leaf2 |
| 10.0.1.4/31 | DC1 | - | P2P Link | spine2 - leaf1 |
| 10.0.1.6/31 | DC1 | - | P2P Link | spine2 - leaf2 |
| **Tenant subnets** | | | | |
| 10.10.10.0/24 | - | TENANT-1 | Server | VLAN 10 - Server subnet 1 |
| 10.10.20.0/24 | - | TENANT-1 | Server | VLAN 20 - Server subnet 2 |

---

## Step 10 - VLAN Groups, VLANs

### VLAN Groups

| Name | Scope | Description |
|------|-------|-------------|
| DC1-VLANs | Site: DC1 | DC1 VLAN namespace |

### VLANs

| VID | Name | Group | Status | Role | Custom Fields | Description |
|-----|------|-------|--------|------|---------------|-------------|
| 10 | SERVER-10 | DC1-VLANs | Active | VXLAN | vni=10010 | Server subnet 1 |
| 20 | SERVER-20 | DC1-VLANs | Active | VXLAN | vni=10020 | Server subnet 2 |

---

## Step 11 - Devices

| Name | Device Type | Role | Site | Platform | Status | OOB IP (fxp0) | Tags | Homing |
|------|------------|------|------|----------|--------|---------------|------|--------|
| dc1-spine1 | EX9214 | Spine | DC1 | Junos | Active | `$MGMT_dc1_spine1` | spine, dc1 | - |
| dc1-spine2 | EX9214 | Spine | DC1 | Junos | Active | `$MGMT_dc1_spine2` | spine, dc1 | - |
| dc1-leaf1 | EX9214 | Leaf | DC1 | Junos | Active | `$MGMT_dc1_leaf1` | leaf, dc1 | - |
| dc1-leaf2 | EX9214 | Leaf | DC1 | Junos | Active | `$MGMT_dc1_leaf2` | leaf, dc1 | - |
| dc1-host1 | Container Host | Server | DC1 | Linux | Active | - | dc1 | Single-homed leaf1 |
| dc1-host2 | Container Host | Server | DC1 | Linux | Active | - | dc1 | Single-homed leaf2 |
| dc1-host3 | Container Host | Server | DC1 | Linux | Active | - | dc1 | Dual-homed ESI-LAG |
| dc1-host4 | Container Host | Server | DC1 | Linux | Active | - | dc1 | Dual-homed ESI-LAG |

> OOB IPs are environment-specific, assigned to `fxp0`, set as device `oob_ip`.
> **Primary IP** (`primary_ip4`) is the loopback address on `lo0` (see Step 12).
> ASN stored in `local_context_data` as `{"bgp_asn": <asn>}` (see Step 7).
> Hosts connect to leaves for traffic testing. No mgmt IP (accessed via containerlab).

---

## Step 12 - Interfaces & IP Addresses

### Loopback IPs (assigned to lo0)

| Device | Interface | IP | Description |
|--------|-----------|-----|-------------|
| dc1-spine1 | lo0 | 10.0.0.1/32 | Router-ID |
| dc1-spine2 | lo0 | 10.0.0.2/32 | Router-ID |
| dc1-leaf1 | lo0 | 10.0.0.3/32 | Router-ID / VTEP |
| dc1-leaf2 | lo0 | 10.0.0.4/32 | Router-ID / VTEP |

### P2P Link IPs (assigned to ge-0/0/x)

Full mesh: each spine connects to each leaf.

| A-Device | A-Interface | A-IP | Z-Device | Z-Interface | Z-IP |
|----------|-------------|------|----------|-------------|------|
| dc1-spine1 | ge-0/0/0 | 10.0.1.0/31 | dc1-leaf1 | ge-0/0/0 | 10.0.1.1/31 |
| dc1-spine1 | ge-0/0/1 | 10.0.1.2/31 | dc1-leaf2 | ge-0/0/0 | 10.0.1.3/31 |
| dc1-spine2 | ge-0/0/0 | 10.0.1.4/31 | dc1-leaf1 | ge-0/0/1 | 10.0.1.5/31 |
| dc1-spine2 | ge-0/0/1 | 10.0.1.6/31 | dc1-leaf2 | ge-0/0/1 | 10.0.1.7/31 |

---

## Step 13 - Cables

| A-Device | A-Interface | Z-Device | Z-Interface | Label | Type |
|----------|-------------|----------|-------------|-------|------|
| dc1-spine1 | ge-0/0/0 | dc1-leaf1 | ge-0/0/0 | sp1-lf1 | Fabric |
| dc1-spine1 | ge-0/0/1 | dc1-leaf2 | ge-0/0/0 | sp1-lf2 | Fabric |
| dc1-spine2 | ge-0/0/0 | dc1-leaf1 | ge-0/0/1 | sp2-lf1 | Fabric |
| dc1-spine2 | ge-0/0/1 | dc1-leaf2 | ge-0/0/1 | sp2-lf2 | Fabric |
| dc1-leaf1 | ge-0/0/2 | dc1-host1 | eth0 | lf1-h1 | Single-homed |
| dc1-leaf2 | ge-0/0/2 | dc1-host2 | eth0 | lf2-h2 | Single-homed |
| dc1-leaf1 | ge-0/0/3 | dc1-host3 | eth0 | lf1-h3 | ESI-LAG |
| dc1-leaf2 | ge-0/0/3 | dc1-host3 | eth1 | lf2-h3 | ESI-LAG |
| dc1-leaf1 | ge-0/0/4 | dc1-host4 | eth0 | lf1-h4 | ESI-LAG |
| dc1-leaf2 | ge-0/0/4 | dc1-host4 | eth1 | lf2-h4 | ESI-LAG |

**Total: 10 cables**

---

# Pre-staged for Later Phases

The following objects are defined here for design completeness. They will be populated when the corresponding project phase begins.

## Step 14 - Host & Leaf LAG Interfaces (project Phase 2)

### Host Interfaces (dual-homed only - host3, host4)

| Device | Interface | Type | Parent LAG | Connected to |
|--------|-----------|------|------------|-------------|
| dc1-host3 | eth0 | 1000BASE-T | bond0 | dc1-leaf1 ge-0/0/3 |
| dc1-host3 | eth1 | 1000BASE-T | bond0 | dc1-leaf2 ge-0/0/3 |
| dc1-host3 | bond0 | LAG | - | - |
| dc1-host4 | eth0 | 1000BASE-T | bond0 | dc1-leaf1 ge-0/0/4 |
| dc1-host4 | eth1 | 1000BASE-T | bond0 | dc1-leaf2 ge-0/0/4 |
| dc1-host4 | bond0 | LAG | - | - |

> host1 and host2 are single-homed (eth0 only, no LAG).

### Leaf ESI-LAG Interfaces

| Device | Interface | Type | ESI | Members | Description |
|--------|-----------|------|-----|---------|-------------|
| dc1-leaf1 | ae0 | LAG | 00:11:11:11:11:11:11:11:11:01 | ge-0/0/3 | ESI-LAG to host3 |
| dc1-leaf2 | ae0 | LAG | 00:11:11:11:11:11:11:11:11:01 | ge-0/0/3 | ESI-LAG to host3 |
| dc1-leaf1 | ae1 | LAG | 00:11:11:11:11:11:11:11:11:02 | ge-0/0/4 | ESI-LAG to host4 |
| dc1-leaf2 | ae1 | LAG | 00:11:11:11:11:11:11:11:11:02 | ge-0/0/4 | ESI-LAG to host4 |

> Same ESI on both leaves = EVPN multihoming active-active.

---

## Step 15 - IRB Interfaces (project Phase 7)

| Device | Interface | Type | VRF | IP | Description |
|--------|-----------|------|-----|-----|-------------|
| dc1-leaf1 | irb.10 | Virtual | TENANT-1 | 10.10.10.1/24 (anycast) | VLAN 10 GW |
| dc1-leaf1 | irb.20 | Virtual | TENANT-1 | 10.10.20.1/24 (anycast) | VLAN 20 GW |
| dc1-leaf2 | irb.10 | Virtual | TENANT-1 | 10.10.10.1/24 (anycast) | VLAN 10 GW |
| dc1-leaf2 | irb.20 | Virtual | TENANT-1 | 10.10.20.1/24 (anycast) | VLAN 20 GW |

> Same IP on both leaves, NetBox IP role=`anycast`. Anycast MAC in VRF custom field.

---

## Step 16 - L2VPN (project Phase 2)

| Name | Slug | Type | Identifier (VNI) | Import Targets | Export Targets |
|------|------|------|-------------------|---------------|----------------|
| VLAN10-VXLAN | `vlan10-vxlan` | VXLAN-EVPN | 10010 | 65000:10010 | 65000:10010 |
| VLAN20-VXLAN | `vlan20-vxlan` | VXLAN-EVPN | 10020 | 65000:10020 | 65000:10020 |

**Terminations:** Each L2VPN terminates on the corresponding VLAN.

---

## Step 17 - Config Contexts (project Phases 2, 8)

Config contexts encode the Juniper ERB (Edge-Routed Bridging) routing instance model:

```
Leaf routing instances:
  default (master)    - underlay eBGP + overlay eBGP (family evpn signaling)
  EVPN-VXLAN          - virtual-switch: bridge domains, VLANs, VNIs
  TENANT-1            - vrf: IRB interfaces, L3 inter-VLAN routing, L3VNI
  mgmt_junos          - OOB management (fxp0, mgmt default route)

Spine routing instances:
  default (master)    - underlay eBGP + overlay eBGP (route reflector)
  mgmt_junos          - OOB management
```

### 1. Routing instances - Leaf (assigned to role: Leaf) - project Phase 2

```json
{
  "routing_instances": {
    "evpn_vxlan": {
      "instance_type": "virtual-switch",
      "vtep_source": "lo0.0",
      "encapsulation": "vxlan"
    },
    "mgmt_junos": {
      "description": "OOB management"
    }
  },
  "underlay": {
    "bgp_type": "ebgp",
    "multipath": true,
    "bfd": true,
    "timers": {"hold": 90, "keepalive": 30}
  },
  "overlay": {
    "bgp_type": "ebgp",
    "family": "evpn signaling",
    "multihop": true,
    "local_address": "lo0.0"
  }
}
```

### 2. Routing instances - Spine / Route Reflector (assigned to role: Spine) - project Phase 2

```json
{
  "routing_instances": {
    "mgmt_junos": {
      "description": "OOB management"
    }
  },
  "underlay": {
    "bgp_type": "ebgp",
    "multipath": true,
    "bfd": true,
    "timers": {"hold": 90, "keepalive": 30}
  },
  "overlay": {
    "bgp_type": "ebgp",
    "family": "evpn signaling",
    "route_reflector": true,
    "cluster_id_from": "loopback"
  }
}
```

### 3. Hardening (global) - project Phase 8

```json
{
  "hardening": {
    "ntp_servers": ["$SERVICES_HOST"],
    "syslog_servers": ["$SERVICES_HOST"],
    "dns_servers": ["1.1.1.1", "8.8.8.8"],
    "banner": "Authorized access only.",
    "disable_services": ["telnet", "finger", "snmpv1", "snmpv2c"]
  }
}
```

---

# Summary

### Phase 1 scope (Steps 1-13)

| Object Type | Count |
|-------------|-------|
| Custom Fields | 4 |
| Tags | 11 |
| Regions | 2 |
| Tenants | 1 |
| Sites | 1 |
| Manufacturers | 3 |
| Platforms | 3 |
| Device Roles | 3 |
| Device Types | 2 |
| ASNs | 4 |
| VRFs | 1 |
| Route Targets | 3 |
| VLAN Groups | 1 |
| VLANs | 2 |
| Devices | 8 |
| Prefixes | ~12 |
| IP Addresses | ~12 |
| Cables | 10 |
| **Phase 1 total** | **~80 objects** |

### Pre-staged for later (Steps 14-17)

| Object Type | Phase | Count |
|-------------|-------|-------|
| LAG Interfaces | 2 | ~10 |
| IRB Interfaces + IPs | 7 | ~8 |
| L2VPN Instances | 2 | 2 |
| Config Contexts | 2, 8 | 3 |

---

## Notes

- **Idempotency:** Population script must use `get_or_create` pattern.
- **Step ordering:** Create objects in step order (dependencies).
- **ASN modeling:** Native ASN objects as registry + `local_context_data.bgp_asn` on each device for template access. NetBox ASN objects are site-level only (no device relationship), so local context bridges the gap.
- **Routing instances:** Follows Juniper ERB model. Underlay + overlay BGP in default instance. L2 overlay in `virtual-switch` instance. L3 tenant routing in `vrf` instance. OOB management in `mgmt_junos`.
- **Derived values:** Route distinguisher derived from loopback IP at config render time. VTEP source is `lo0.0` on all leaves (in config context).
- **Prefix roles:** Tenant subnets have role "Server". The anycast nature is on the IP address (role=`anycast`), not the prefix.
- **DC2 (Arista):** Added in project Phase 10 - new site, cEOS device types, EOS platform.
