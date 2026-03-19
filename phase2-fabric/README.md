# Phase 2 - EVPN+VXLAN+ESI-LAG Fabric

A fully operational EVPN-VXLAN leaf-spine fabric running on containerlab with Juniper vJunos-switch (EX9214).

**Status:** Base fabric operational (BGP/EVPN up). ESI-LAG and host connectivity in progress.

## Architecture

Juniper ERB (Edge-Routed Bridging):

| Routing Instance | Type | Scope | Purpose |
|-----------------|------|-------|---------|
| default (master) | - | All devices | Underlay eBGP + overlay iBGP (family evpn signaling) |
| EVPN-VXLAN | virtual-switch | Leaves | L2 bridge domains, VLANs, VNIs, VXLAN encapsulation |
| TENANT-1 | vrf | Leaves | L3 inter-VLAN routing via IRB, L3VNI 5000, Type-5 routes |
| mgmt_junos | built-in | All devices | OOB management (fxp0) |

## Operational Features

Validated on vjunos-switch 23.2R1.14:

| Category | Features |
|----------|----------|
| BGP | log-updown, graceful-restart, mtu-discovery, dont-help-shared-fate-bfd-down |
| BGP underlay | eBGP, multipath multiple-as, BFD single-hop |
| BGP overlay | iBGP AS 65000, vpn-apply-export, no-nexthop-change, signaling loops 2, BFD automatic |
| EVPN | duplicate-mac-detection, multicast-mode ingress-replication, no-arp-suppression |
| Forwarding | chained-composite-next-hop ingress evpn |
| Chassis | aggregated-devices ethernet device-count 48 |
| LLDP | port-id-subtype interface-name, interface all |
| MTU | 9192 (vjunos max) on fabric and host-facing interfaces |
| Storm control | sc-default profile on leaf host-facing interfaces |

## vjunos-switch Limitations

### IRB ARP limitation

vjunos-switch (EX9214 simulation) does not generate ARP replies from IRB interfaces. The data forwarding path works correctly - once the host knows the gateway MAC, all L2/L3 traffic flows through IRB including inter-VLAN routing.

**Workaround:** Set static ARP on test hosts before traffic tests:
```bash
# On each host, set gateway MAC (virtual-gateway-v4-mac from IRB config)
docker exec <host> arp -s 10.10.10.1 00:00:5e:00:01:01  # VLAN 10 gateway
docker exec <host> arp -s 10.10.20.1 00:00:5e:00:01:01  # VLAN 20 gateway
```

**Verified working with static ARP:**
- L2 within VLAN (host1 -> host2 across VXLAN)
- L3 inter-VLAN (host1 VLAN10 -> host3 VLAN20, ttl=63)
- ESI-LAG (host3 -> host4, both dual-homed)

### Other limitations (single virtual RE)

| Feature | Reason | Production recommendation |
|---------|--------|--------------------------|
| `nonstop-routing` | Requires dual-RE graceful-switchover | Enable on production devices |
| `nonstop-bridging` | Same dual-RE dependency | Enable alongside nonstop-routing |
| `network-services enhanced-ip` | Not supported on vjunos | Required on some QFX platforms |
| `vxlan-routing overlay-ecmp` | Not supported on vjunos | Enables ECMP across VXLAN tunnels |

### Alternative virtual platforms considered

| Platform | IRB L3 | Status | Issue |
|----------|--------|--------|-------|
| vjunos-switch | Works (static ARP) | Active, free | ARP replies not generated |
| vjunos-router (vMX) | Full support | Active, free | Different config syntax (bridge-domains) |
| vPTX (vJunosEvolved) | Partial | Active, free | Anycast MAC ignored |
| vQFX | Full support | Abandoned | Last version ~2020 (Junos 19.4) |

## Platform-Specific Syntax (vjunos-switch)

Discovered during deployment - differs from some Juniper documentation examples:

- Uses `mac-vrf` instance type with `service-type vlan-aware` (or `virtual-switch`)
- Uses `vlans` with `l3-interface` (not `bridge-domains` with `routing-interface`)
- `vtep-source-interface` goes inside the routing instance (not global `switch-options`)
- IRB uses `virtual-gateway-address` + `virtual-gateway-v4-mac` + `virtual-gateway-accept-data`
- `routing-options router-id` must be set explicitly (defaults to 0.0.0.0)
- Maximum MTU is 9192 (not 9216)

## Files

| File | Description |
|------|-------------|
| `dc1.clab.yml` | Containerlab topology (4 switches + 4 hosts) |
| `setup-hosts.sh` | Host setup: IPs, 802.3ad bonding (LACP fast), static ARP, traffic tests |
| `configs/dc1-spine1.conf` | Validated spine config |
| `configs/dc1-spine2.conf` | Validated spine config |
| `configs/dc1-leaf1.conf` | Validated leaf config |
| `configs/dc1-leaf2.conf` | Validated leaf config |

## Usage

```bash
# Source environment variables
source ../evpn-lab-env/env.sh

# Deploy the fabric
cd phase2-fabric
sudo containerlab deploy -t dc1.clab.yml

# Wait for switches to boot (~2-4 min), then configure hosts
bash setup-hosts.sh

# Verify
ssh admin@$CLAB_IP_dc1_spine1  # password: TestLabPass1
show bgp summary
show evpn instance
show lldp neighbors
```

## Smoke Tests

All tests automated in `smoke-tests.sh`. Run after `setup-hosts.sh`:

```bash
bash smoke-tests.sh
```

### 1. Control plane

| Test | Verification | Expected |
|------|-------------|----------|
| BGP underlay | `show bgp summary` on all 4 devices | 0 down peers |
| BGP overlay (EVPN) | `show bgp summary` on spines | 2 EVPN peers Established |
| EVPN routes | `show route table EVPN-VXLAN.evpn.0` | Type-2 MAC/IP + Type-3 IM routes |
| VTEP tunnel | `show ethernet-switching vxlan-tunnel-end-point remote` | Remote VTEP 10.1.0.3/4 with VNI 10010/10020 |
| Remote MAC learning | `show ethernet-switching table` | DR (Dynamic Remote) entries present |
| LACP | `show lacp interfaces ae0` | Collecting/Distributing |
| BFD sessions | `show bfd session` | Sessions Up (may not show on vjunos) |
| LLDP neighbors | `show lldp neighbors` | >= 2 per device |
| ESI state | `show evpn instance extensive` | all-active entries present |
| Core isolation | `show configuration protocols network-isolation` | core-isolation configured |

### 2. Underlay reachability

| Test | Verification | Expected |
|------|-------------|----------|
| Leaf-to-leaf loopback | Ping all loopbacks from each leaf | All reachable via ECMP |
| Leaf-to-spine loopback | Ping spine loopbacks from each leaf | Direct reachability |

### 3. Data plane

| Test | Path | Expected |
|------|------|----------|
| L2 same VLAN cross-leaf | host1 (leaf1) -> host2 (leaf2) VLAN 10 | Pass (via VXLAN) |
| L3 inter-VLAN | host1 (VLAN10) -> host3 (VLAN20) | Pass (ttl=63, via IRB) |
| L3 cross-VLAN cross-leaf | host2 (leaf2 VLAN10) -> host4 (leaf1+2 VLAN20) | Pass |
| ESI-LAG same VLAN | host3 -> host4 (both dual-homed VLAN20) | Pass |
| Gateway reachability | host1 -> 10.10.10.1 | Pass (static ARP) |
| Gateway reachability | host3 -> 10.10.20.1 | Pass (static ARP) |

### 4. Failover: ESI-LAG (hard failure)

| Test | Action | Expected |
|------|--------|----------|
| Leaf crash simulation | `docker pause` leaf1 container | LACP fast detects failure within ~3s |
| Bond slave removal | Check host3 `/proc/net/bonding/bond0` | Leaf1 slave MII down, bond degrades |
| Traffic continuity | host3 -> host4 while leaf1 paused | Traffic continues via leaf2 |
| Recovery | `docker unpause` leaf1 (or restart if unresponsive) | LACP re-establishes, both paths active |

### 5. Failover: Spine

| Test | Action | Expected |
|------|--------|----------|
| Spine failover | Disable spine1 ge-0/0/0+ge-0/0/1 | L2+L3 traffic via spine2 |
| Spine restore | Re-enable spine1 interfaces | Traffic via both spines |

### 6. Expected failures

| Test | Action | Expected |
|------|--------|----------|
| Single-homed isolation | Disable leaf1 ge-0/0/2 | host1 loses all connectivity (correct - no redundancy) |

### Not testable (vjunos limitations)

| Test | Reason |
|------|--------|
| Dynamic ARP from hosts to IRB | IRB does not generate ARP replies (static ARP workaround) |
| Core isolation trigger | Would require killing all overlay BGP - hard to test cleanly on vjunos |
| nonstop-routing failover | Requires dual-RE |
| ECMP overlay | vxlan-routing overlay-ecmp not supported |
