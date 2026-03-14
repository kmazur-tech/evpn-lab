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

Some production features cannot be enabled on vjunos-switch (single virtual RE):

| Feature | Reason | Production recommendation |
|---------|--------|--------------------------|
| `nonstop-routing` | Requires `chassis redundancy graceful-switchover` and `system commit synchronize` (dual-RE only) | Enable on production devices with dual REs for hitless RE switchover |
| `nonstop-bridging` | Same dual-RE dependency | Enable alongside nonstop-routing |
| `network-services enhanced-ip` | Not supported on vjunos | Required on some QFX platforms for VXLAN |
| `vxlan-routing overlay-ecmp` | Not supported on vjunos | Enables ECMP across VXLAN tunnels on hardware platforms |

## Platform-Specific Syntax (vjunos-switch)

Discovered during deployment - differs from some Juniper documentation examples:

- Uses `vlans` with `l3-interface` (not `bridge-domains` with `routing-interface`)
- `vtep-source-interface` goes inside the `virtual-switch` routing instance (not global `switch-options`)
- `routing-options router-id` must be set explicitly (defaults to 0.0.0.0)
- Maximum MTU is 9192 (not 9216)

## Files

| File | Description |
|------|-------------|
| `dc1.clab.yml` | Containerlab topology (4 switches + 4 hosts) |
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

# Verify BGP sessions
ssh admin@$CLAB_IP_dc1_spine1  # password: TestLabPass1
show bgp summary
show evpn instance
show lldp neighbors
```
