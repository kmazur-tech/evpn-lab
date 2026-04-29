# Phase 2 - EVPN+VXLAN+ESI-LAG Fabric

A fully operational EVPN-VXLAN leaf-spine fabric running on containerlab with Juniper vJunos-switch (EX9214).

**Run from:** lab server (containerlab host - the one running Docker). `smoke-tests.sh` uses `nsenter` into Docker netns and only works where the daemon lives. **Tests:** `bash smoke-tests.sh` after `containerlab deploy` and `setup-hosts.sh` (76 checks, ~2 min). **Depends on:** Phase 1 (the clab startup configs are hand-built but device IDs match NetBox).

> This README describes WHAT was built and how to run it.
> For WHY each design choice was made, see [DESIGN.md](DESIGN.md).
> For the production-class features that are intentionally NOT in
> this lab (because vJunos cannot run them, not because they were
> forgotten), see the "Production-only features (NOT testable on
> vJunos-switch)" block in [../PROJECT_PLAN.md](../PROJECT_PLAN.md).

## Architecture

Juniper ERB (Edge-Routed Bridging):

| Routing Instance | Type | Scope | Purpose |
|-----------------|------|-------|---------|
| default (master) | - | All devices | Underlay eBGP + overlay iBGP (family evpn signaling) |
| EVPN-VXLAN | mac-vrf (vlan-aware) | Leaves | L2 VLANs, VLAN-to-VNI mapping, VXLAN encapsulation |
| TENANT-1 | vrf | Leaves | L3 inter-VLAN routing via IRB, L3VNI 5000, Type-5 routes |
| mgmt_junos | built-in | All devices | OOB management (fxp0) |

## Operational Features

Validated on vjunos-switch 23.2R1.14:

| Category | Features |
|----------|----------|
| BGP | log-updown, graceful-restart, mtu-discovery, dont-help-shared-fate-bfd-down |
| BGP underlay | eBGP, multipath multiple-as, BFD single-hop |
| BGP overlay | iBGP AS 65000, vpn-apply-export, no-nexthop-change, signaling loops 2, BFD automatic |
| EVPN | duplicate-mac-detection, multicast-mode ingress-replication, ARP suppression (default-on) |
| Forwarding | chained-composite-next-hop ingress evpn, forwarding-table export LOAD-BALANCE (per-packet ECMP) |
| Chassis | aggregated-devices ethernet device-count 48 |
| LLDP | port-id-subtype interface-name, interface all |
| MTU | 9192 (vjunos max) on fabric and host-facing interfaces |
| Storm control | sc-default profile on leaf host-facing interfaces |

## vjunos-switch Limitations

### Dynamic ARP works (no workaround needed)

Earlier revisions of this lab carried `no-arp-suppression` per VLAN and a static ARP workaround on hosts. That was the bug, not a vJunos limitation. With ARP suppression at its Junos default (ON), the leaf snoops local host ARPs into the EVPN database, originates Type-2 (MAC+IP) routes, and replies locally to gateway ARPs. Hosts learn the anycast gateway MAC dynamically.

Note: `proxy-macip-advertisement` is **not** supported on vJunos-switch / EX9200 (syntax error at `routing-instances <mac-vrf> protocols evpn`). It is also not needed in ERB - it is a CRB construct for L2-only leaves. ERB leaves snoop ARP locally because they own the IRB.

### Limitations (single virtual RE)

| Feature | Reason | Production recommendation |
|---------|--------|--------------------------|
| `nonstop-routing` | Requires dual-RE graceful-switchover | Enable on production devices |
| `nonstop-bridging` | Same dual-RE dependency | Enable alongside nonstop-routing |
| `forwarding-options vxlan-routing` hierarchy | Parser rejects the whole hierarchy (`set forwarding-options vxlan-routing` -> `syntax error` pointing at `vxlan-routing`, verified via `commit check` on dc1-leaf1 vJunos 23.2R1.14 2026-04-11). Means `overlay-ecmp`, `next-hop` scaling, and `shared-tunnels` tuning cannot be expressed on this platform. | Enables ECMP across VXLAN tunnels and PFE tunnel scale tuning on real QFX/MX hardware |

> Note: `chassis network-services enhanced-ip` is deliberately NOT in this limitations table. It was listed here in an earlier revision, but `commit check` on dc1-leaf1 vJunos 23.2R1.14 accepts the knob (`configuration check succeeds`). Runtime effect on the simulated PFE is untested because the lab does not use the feature; a future phase that needs it should verify live.

### Virtual platform choice

This lab uses **vjunos-switch** (the vrnetlab image `juniper_vjunos-switch:23.2R1.14`, emulating EX9214). The alternative free virtual Junos platform that is verified to work with mac-vrf EVPN-VXLAN in this project is **vjunos-router** (vMX), but its config tree uses `bridge-domains` with `routing-interface`, incompatible with the Phase 2/3 templates that emit `mac-vrf` with `vlans { l3-interface ... }`.

Other Junos virtual platforms (vPTX / vJunosEvolved, vQFX) were not tested for this lab. Trade-off analyses between them are out of scope here - a future phase that actually runs one of them should document verified behavior, not secondhand claims.

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
| `setup-hosts.sh` | Host setup: IPs, 802.3ad bonding (LACP fast), traffic tests |
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
ssh $JUNOS_SSH_USER@$CLAB_IP_dc1_spine1  # password from $JUNOS_SSH_PASSWORD
show bgp summary
show evpn instance
show lldp neighbors
```

## Smoke Tests

All tests automated in `smoke-tests.sh`. Run **on the containerlab host** (the machine whose IP is in `$CLAB_HOST` from `evpn-lab-env/env.sh`) after `setup-hosts.sh`:

```bash
source ../../evpn-lab-env/env.sh   # device mgmt IPs + JUNOS_SSH_*
bash smoke-tests.sh
```

`smoke-tests.sh` auto-sources `../../evpn-lab-env/env.sh` if `$MGMT_dc1_spine1` isn't already set, and hard-fails with an actionable message if any required env var is missing. No literal device IPs or credentials live inside the script.

### Where to run smoke (and why)

`smoke-tests.sh` has two classes of checks with different location requirements:

1. **Control-plane (~58 checks)** talk to Junos via `sshpass $JUNOS_SSH_USER@<device-mgmt-ip>`. These run from anywhere with TCP/22 reach to the fabric mgmt IPs and `sshpass`+`jq` installed.

2. **Data-plane / failover (~18 checks)** use `ping_test()` → `docker inspect -f '{{.State.Pid}}' clab-dc1-<host>` → `nsenter -t $PID -n ping ...`. Both `docker inspect` and `nsenter` require the Docker daemon AND the target containers to be on the **local** machine. They silently fail (every ping reports FAIL) from any host that isn't the containerlab host.

**Running from elsewhere wraps it via SSH** - never try to replicate Docker into your dev box. From a Phase 3 / Phase 6 orchestrator:

```bash
source ../../evpn-lab-env/env.sh   # picks up CLAB_HOST + CLAB_SSH_KEY
ssh -i "$CLAB_SSH_KEY" root@"$CLAB_HOST" 'cd /opt/evpn-lab && bash smoke-tests.sh'
```

Phase 6 CI/CD will run smoke from a self-hosted runner **on** the lab server for the same reason: tests run where the system under test runs.

**Symptom of running from the wrong place:** ~18 FAILs clustered in L2/L3/ESI-LAG/gateway/failover/withdrawal sections; control-plane (BGP/EVPN/BFD/LACP/ESI/DF) all PASS. The split IS the diagnostic.

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
| Gateway reachability | host1 -> 10.10.10.1 | Pass (dynamic ARP via EVPN) |
| Gateway reachability | host3 -> 10.10.20.1 | Pass (dynamic ARP via EVPN) |

### 4. Failover: ESI-LAG (hard failure)

| Test | Action | Expected |
|------|--------|----------|
| Leaf crash simulation | `docker pause` leaf1 container | LACP fast detects failure within ~3s |
| Bond slave removal | Check host3 `/proc/net/bonding/bond0` | Leaf1 slave MII down, bond degrades |
| Traffic continuity | host3 -> host4 while leaf1 paused | Traffic continues via leaf2 |
| Recovery | `docker unpause` leaf1 (or restart if unresponsive) | LACP re-establishes, both paths active |

### 5. Failover: Core Isolation

| Test | Action | Expected |
|------|--------|----------|
| Overlay BGP loss | `deactivate protocols bgp group OVERLAY` on leaf1 | Core isolation brings ae0/ae1 link down |
| Traffic continuity | host3 -> host4 while leaf1 isolated | Traffic via leaf2 |
| Recovery | `activate protocols bgp group OVERLAY` on leaf1 | BGP re-establishes, ae0/ae1 come back up |

### 6. Failover: Spine

| Test | Action | Expected |
|------|--------|----------|
| Spine failover | Disable spine1 ge-0/0/0+ge-0/0/1 | L2+L3 traffic via spine2 |
| Spine restore | Re-enable spine1 interfaces | Traffic via both spines |

### 7. Expected failures

| Test | Action | Expected |
|------|--------|----------|
| Single-homed isolation | Disable leaf1 ge-0/0/2 | host1 loses all connectivity (correct - no redundancy) |

### 8. EVPN deep validation (per leaf, mirrored across leaf1 + leaf2)

| Test | Verification | Expected |
|------|-------------|----------|
| ECMP next-hop count | `show route forwarding-table destination <remote-lo>/32` | `ulst` with 2 next-hops (one per spine) - regression test for `forwarding-table export LOAD-BALANCE` |
| Type-2 per VNI | `show route table bgp.evpn.0 match-prefix 2:*::<vni>::*` | At least 1 MAC/IP per L2VNI (10010, 10020) - catches single-VNI outage |
| Type-3 per VNI | `show route table bgp.evpn.0 match-prefix 3:*::<vni>::*` | At least 1 IMET per L2VNI |
| Type-5 IP-prefix | `show route table bgp.evpn.0 match-prefix 5:*` | >= 1 (tenant L3VNI advertisement) |
| Specific Type-5 in TENANT-1 | `show route table TENANT-1.inet.0 <expected-/32> protocol evpn` | Expected remote host /32 installed via EVPN (object-based, not threshold) |
| EVPN database host coverage | `show evpn database` | All 4 lab host IPs (10.10.10.11/12, 10.10.20.13/14) present with MAC+IP - regression test for `no-arp-suppression` |
| Per-peer overlay BGP NLRI | `show bgp neighbor <spine-lo>` | State=Established AND Received prefixes > 0 on each spine peer |
| Jumbo MTU end-to-end | `ping <remote-lo> source <local-lo> size 8972 do-not-fragment` | 0% loss (proves underlay MTU >= 9050 for VXLAN) |
| Duplicate-MAC clean | `show evpn database state duplicate` | Empty (no loops or mis-cabling) |
| BFD session health | `show bfd session extensive` | Every session Up AND Local diagnostic None |
| Underlay counters | `show interfaces ge-0/0/X extensive` | Errors=0, Drops=0 on fabric ports (carrier transitions allowed) |
| ESI consistency (cross-leaf) | `show evpn instance designated-forwarder` on both leaves | Same ESI count, same DF elected per ESI |

### Post-failure cleanup checks (woven into sections 4 + 5)

| Test | When | Expected |
|------|------|----------|
| VTEP withdrawal (ESI-LAG hard fail) | After leaf1 paused, BGP hold expired | Remote VTEP for 10.1.0.3 disappears from leaf2 |
| VTEP reinstall (ESI-LAG recovery) | After leaf1 unpaused + BGP converged | Remote VTEP for 10.1.0.3 reinstated on leaf2 |
| VTEP withdrawal (core isolation) | After overlay BGP deactivated, hold expired | Remote VTEP for 10.1.0.3 disappears from leaf2 |
| VTEP reinstall (core isolation recovery) | After overlay BGP reactivated, AEs back up | Remote VTEP for 10.1.0.3 reinstated on leaf2 |
| ae0 + ae1 both down on isolation | After 15s settle | Both AE link states down (not just ae0) |
| ae0 + ae1 both up on restore | After core-isolation hold-time up | Both AE link states up |
| DF election still consistent post-restore | After overlay BGP recovers | Same DF elected on both leaves, no drift |

### Not testable (vjunos limitations)

| Test | Reason |
|------|--------|
| nonstop-routing failover | Requires dual-RE |
| ECMP overlay (vxlan-routing overlay-ecmp) | `forwarding-options vxlan-routing` hierarchy not in vJunos parser (syntax error, verified 2026-04-11) |
