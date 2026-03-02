# NetBox Data Model - EVPN-VXLAN DC Fabric Lab

This document defines every object that must be populated in NetBox to serve as Source of Truth for the EVPN-VXLAN lab. Objects are listed in dependency order - each section can only be created after all previous sections exist.

Environment-specific values (NetBox URL, token, management IPs) are defined in `env.yml` outside this repo. See `.env.example` in the repo root.

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
                +----+--+--+-+  +-+--+--+-------+
                     |  |  |      |  |  |
                     |  |  +--++--+  |  |
                     |  +--+--||--+--+  |
                     |     |  ||  |     |
                  +--+-+ +-+--++--+-+ +-+--+
                  |host1| |  host2  | |host3|
                  +-----+ +--(ae0)-+ +-----+
                           (ESI-LAG,
                        dual-homed to
                        both leaves)

                DC2 (Arista cEOS - Phase 10)
```

- 2x spine (vJunos-switch, EX9214 model)
- 2x leaf (vJunos-switch, EX9214 model)
- 2-3x Linux containers as test hosts
- eBGP underlay, EVPN overlay, VXLAN, ESI-LAG between leaves

---

## Phase 1 - Custom Fields

| Name | Content Type | Type | Required | Description |
|------|-------------|------|----------|-------------|
| `bgp_asn` | dcim.device | Integer | No | BGP Autonomous System Number |
| `vni` | ipam.vlan | Integer | No | VXLAN Network Identifier mapped to this VLAN |
| `l3vni` | ipam.vrf | Integer | No | L3 VNI for symmetric IRB routing |
| `esi` | dcim.interface | Text | No | Ethernet Segment Identifier (10-byte, colon-separated) |
| `vtep_source` | dcim.device | Text | No | VTEP source interface (e.g. lo0.0) |
| `route_distinguisher` | dcim.device | Text | No | BGP route distinguisher |
| `anycast_mac` | ipam.vrf | Text | No | Shared anycast gateway MAC per VRF |

---

## Phase 2 - Tags

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
| DC2 | `dc2` | Dark Green | Data Center 2 (Arista, Phase 10) |

---

## Phase 3 - Regions, Tenant Groups, Tenants

### Regions

```
Lab
 └── DC1
 └── DC2 (Phase 10)
```

### Tenant

| Tenant Group | Tenant | Description |
|-------------|--------|-------------|
| Lab | Lab Operations | Owner of all lab devices |

---

## Phase 4 - Sites

| Name | Slug | Region | Status | Tenant | Description |
|------|------|--------|--------|--------|-------------|
| DC1 | `dc1` | DC1 | Active | Lab Operations | Primary DC - Juniper fabric |

> DC2 (Arista) added in Phase 10.

---

## Phase 5 - Manufacturers, Platforms, Device Roles

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
| EOS | `eos` | Arista | eos | Arista EOS (Phase 10) |
| Linux | `linux` | Linux | - | Linux container host |

### Device Roles

| Name | Slug | Color | Description |
|------|------|-------|-------------|
| Spine | `spine` | Blue | Spine / aggregation layer |
| Leaf | `leaf` | Green | Leaf / access layer (ToR) |
| Server | `server` | Teal | Compute / test host |

---

## Phase 6 - Device Types (with Interface Templates)

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

## Phase 7 - RIR, ASN Ranges, ASNs

### RIR

| Name | Slug | Private |
|------|------|---------|
| Private | `private` | Yes |

### ASN Range

| Name | RIR | Start | End |
|------|-----|-------|-----|
| Lab ASN Range | Private | 64512 | 65534 |

### ASN Objects

| ASN | Description |
|-----|-------------|
| 65001 | dc1-spine1 |
| 65002 | dc1-spine2 |
| 65003 | dc1-leaf1 |
| 65004 | dc1-leaf2 |

> eBGP underlay: unique ASN per device.

---

## Phase 8 - VRFs, Route Targets, Prefix/VLAN Roles

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
| Server | `server` | Server/host subnets |
| VXLAN | `vxlan` | VXLAN-extended L2 segments |
| Anycast Gateway | `anycast-gw` | IRB gateway subnets |

---

## Phase 9 - Aggregates, Prefixes

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
| 10.0.1.0/31 | DC1 | - | P2P Link | spine1 ↔ leaf1 |
| 10.0.1.2/31 | DC1 | - | P2P Link | spine1 ↔ leaf2 |
| 10.0.1.4/31 | DC1 | - | P2P Link | spine2 ↔ leaf1 |
| 10.0.1.6/31 | DC1 | - | P2P Link | spine2 ↔ leaf2 |
| **Tenant subnets** | | | | |
| 10.10.10.0/24 | - | TENANT-1 | Anycast GW | VLAN 10 - Server subnet 1 |
| 10.10.20.0/24 | - | TENANT-1 | Anycast GW | VLAN 20 - Server subnet 2 |

---

## Phase 10 - VLAN Groups, VLANs

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

## Phase 11 - Devices

| Name | Device Type | Role | Site | Platform | Status | bgp_asn | vtep_source | mgmt IP | Tags |
|------|------------|------|------|----------|--------|---------|-------------|---------|------|
| dc1-spine1 | EX9214 | Spine | DC1 | Junos | Active | 65001 | - | `$MGMT_dc1_spine1` | spine, dc1 |
| dc1-spine2 | EX9214 | Spine | DC1 | Junos | Active | 65002 | - | `$MGMT_dc1_spine2` | spine, dc1 |
| dc1-leaf1 | EX9214 | Leaf | DC1 | Junos | Active | 65003 | lo0.0 | `$MGMT_dc1_leaf1` | leaf, dc1 |
| dc1-leaf2 | EX9214 | Leaf | DC1 | Junos | Active | 65004 | lo0.0 | `$MGMT_dc1_leaf2` | leaf, dc1 |
| dc1-host1 | Container Host | Server | DC1 | Linux | Active | - | - | - | dc1 |
| dc1-host2 | Container Host | Server | DC1 | Linux | Active | - | - | - | dc1 |
| dc1-host3 | Container Host | Server | DC1 | Linux | Active | - | - | - | dc1 |

> Management IPs are environment-specific (defined in `env.yml`), assigned to `fxp0`, set as device `primary_ip4`.
> Hosts connect to leaves for traffic testing. No mgmt IP (accessed via containerlab).

---

## Phase 12 - Interfaces & IP Addresses

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

### Host Interfaces

ESI-LAG: each host dual-homed to both leaves via bond0.

| Device | Interface | Type | Parent LAG | Connected to |
|--------|-----------|------|------------|-------------|
| dc1-host1 | eth0 | 1000BASE-T | bond0 | dc1-leaf1 ge-0/0/2 |
| dc1-host1 | eth1 | 1000BASE-T | bond0 | dc1-leaf2 ge-0/0/2 |
| dc1-host1 | bond0 | LAG | - | - |
| dc1-host2 | eth0 | 1000BASE-T | bond0 | dc1-leaf1 ge-0/0/3 |
| dc1-host2 | eth1 | 1000BASE-T | bond0 | dc1-leaf2 ge-0/0/3 |
| dc1-host2 | bond0 | LAG | - | - |
| dc1-host3 | eth0 | 1000BASE-T | - | dc1-leaf1 ge-0/0/4 |

### Leaf ESI-LAG Interfaces

| Device | Interface | Type | ESI | Members | Description |
|--------|-----------|------|-----|---------|-------------|
| dc1-leaf1 | ae0 | LAG | 00:11:11:11:11:11:11:11:11:01 | ge-0/0/2 | ESI-LAG to host1 |
| dc1-leaf2 | ae0 | LAG | 00:11:11:11:11:11:11:11:11:01 | ge-0/0/2 | ESI-LAG to host1 |
| dc1-leaf1 | ae1 | LAG | 00:11:11:11:11:11:11:11:11:02 | ge-0/0/3 | ESI-LAG to host2 |
| dc1-leaf2 | ae1 | LAG | 00:11:11:11:11:11:11:11:11:02 | ge-0/0/3 | ESI-LAG to host2 |

> Same ESI on both leaves = EVPN multihoming active-active.

### IRB Interfaces (on leaves, for anycast gateway)

| Device | Interface | Type | VRF | IP | Description |
|--------|-----------|------|-----|-----|-------------|
| dc1-leaf1 | irb.10 | Virtual | TENANT-1 | 10.10.10.1/24 (anycast) | VLAN 10 GW |
| dc1-leaf1 | irb.20 | Virtual | TENANT-1 | 10.10.20.1/24 (anycast) | VLAN 20 GW |
| dc1-leaf2 | irb.10 | Virtual | TENANT-1 | 10.10.10.1/24 (anycast) | VLAN 10 GW |
| dc1-leaf2 | irb.20 | Virtual | TENANT-1 | 10.10.20.1/24 (anycast) | VLAN 20 GW |

> Same IP on both leaves, NetBox IP role=`anycast`. Anycast MAC in VRF custom field.

---

## Phase 13 - Cables

| A-Device | A-Interface | Z-Device | Z-Interface | Label |
|----------|-------------|----------|-------------|-------|
| dc1-spine1 | ge-0/0/0 | dc1-leaf1 | ge-0/0/0 | sp1-lf1 |
| dc1-spine1 | ge-0/0/1 | dc1-leaf2 | ge-0/0/0 | sp1-lf2 |
| dc1-spine2 | ge-0/0/0 | dc1-leaf1 | ge-0/0/1 | sp2-lf1 |
| dc1-spine2 | ge-0/0/1 | dc1-leaf2 | ge-0/0/1 | sp2-lf2 |
| dc1-leaf1 | ge-0/0/2 | dc1-host1 | eth0 | lf1-h1 |
| dc1-leaf2 | ge-0/0/2 | dc1-host1 | eth1 | lf2-h1 |
| dc1-leaf1 | ge-0/0/3 | dc1-host2 | eth0 | lf1-h2 |
| dc1-leaf2 | ge-0/0/3 | dc1-host2 | eth1 | lf2-h2 |
| dc1-leaf1 | ge-0/0/4 | dc1-host3 | eth0 | lf1-h3 |

**Total: 9 cables**

---

## Phase 14 - L2VPN (VXLAN-EVPN instances)

| Name | Slug | Type | Identifier (VNI) | Import Targets | Export Targets |
|------|------|------|-------------------|---------------|----------------|
| VLAN10-VXLAN | `vlan10-vxlan` | VXLAN-EVPN | 10010 | 65000:10010 | 65000:10010 |
| VLAN20-VXLAN | `vlan20-vxlan` | VXLAN-EVPN | 10020 | 65000:10020 | 65000:10020 |

**Terminations:** Each L2VPN terminates on the corresponding VLAN.

---

## Phase 15 - Config Contexts

### 1. Underlay BGP (assigned to roles: Spine, Leaf)

```json
{
  "underlay": {
    "bgp_type": "ebgp",
    "multipath": true,
    "bfd": true,
    "timers": {"hold": 90, "keepalive": 30}
  }
}
```

### 2. Overlay EVPN - Leaf (assigned to role: Leaf)

```json
{
  "overlay": {
    "evpn_type": "ibgp",
    "route_reflector_client": true,
    "encapsulation": "vxlan",
    "vtep_source": "lo0.0"
  }
}
```

### 3. Overlay EVPN - Spine / Route Reflector (assigned to role: Spine)

```json
{
  "overlay": {
    "evpn_type": "ibgp",
    "route_reflector": true,
    "cluster_id_from": "loopback"
  }
}
```

### 4. Hardening (global - Phase 8 prep)

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

## Summary

| Object Type | Count |
|-------------|-------|
| Custom Fields | 7 |
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
| Devices | 7 |
| Prefixes | ~12 |
| IP Addresses | ~20 |
| Cables | 9 |
| L2VPN Instances | 2 |
| Config Contexts | 4 |
| **Total** | **~85 objects** |

---

## Notes

- **Idempotency:** Population script must use `get_or_create` pattern.
- **Ordering:** Create objects in phase order (dependencies).
- **DC2 (Arista):** Added in Phase 10 - new site, cEOS device types, EOS platform, same IP/VLAN structure.
- **Naming:** Generic `dc1-spine1` convention, no company-specific names.
- **Overlaid.net learnings:** Minimal custom fields (scalars), config contexts for structured role-wide data.
