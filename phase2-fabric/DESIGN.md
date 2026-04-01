# Phase 2 - Design Rationale

This document captures the intentional design choices behind the Phase 2
EVPN-VXLAN fabric. It is the answer to "why is it built this way" - the
counterpart to `README.md` (which describes WHAT) and `smoke-tests.sh`
(which proves the fabric WORKS).

The intent is to make it possible for a future reader (or future me) to
distinguish:

1. **Deliberate design choices** that should survive any rewrite.
2. **Lab simplifications** that were chosen for fast iteration but would
   change at production scale.
3. **vJunos-switch limitations** that forced a specific knob and would
   simply go away on real hardware.

When something looks "off" in the configs, this document is the first
place to check before changing it.

---

## 1. Edge-Routed Bridging (ERB) with anycast gateway on every leaf

**Decision:** Each leaf owns the IRB for every tenant VLAN. Both leaves
present the same `virtual-gateway-address` and `virtual-gateway-v4-mac`
on each IRB unit, so any host on any access port reaches its default
gateway in one hop with no L2 traversal across the fabric.

**Why:**
- JVD reference design for small-to-medium DCs. Centralised Routed
  Bridging (CRB) puts the L3 gateway on a single border leaf or spine
  pair, which forces hairpin routing for any inter-VLAN traffic and
  becomes the main scale bottleneck. ERB scales horizontally.
- All inter-VLAN routing happens locally on the ingress leaf, so the
  spines stay pure L3 transport (no IRB, no VTEP, no MAC learning).
  This is what makes it possible to use the spines as iBGP route
  reflectors without complicating the data plane.
- ESI-LAG dual-homing only works cleanly in ERB because both leaves
  have to make the same forwarding decision for the same flow.

**What would change in production:** Nothing. ERB is the right call
at this scale and well above it.

**Knobs that implement it:**
- `interfaces irb unit X virtual-gateway-address` (per VLAN, identical
  on both leaves)
- `interfaces irb unit X virtual-gateway-v4-mac 00:00:5e:00:01:01`
- `interfaces irb unit X virtual-gateway-accept-data` (lets the leaf
  process L3 traffic destined to the virtual gateway)

---

## 2. eBGP underlay + iBGP EVPN overlay with spines as route reflectors

**Decision:**
- **Underlay**: eBGP, unique private ASN per device (spine1=65001,
  spine2=65002, leaf1=65003, leaf2=65004), `multipath multiple-as`,
  carries only loopback /32s.
- **Overlay**: iBGP EVPN, single shared AS 65000 across the entire
  fabric, multihop loopback-to-loopback, spines act as route
  reflectors with `cluster 10.1.0.X`.

**Why:**
- This is the JVD reference design for EVPN-VXLAN. eBGP underlay
  gives clean per-device failure domains and trivial ECMP via
  `multipath multiple-as`. iBGP overlay gives a single AS for the
  EVPN family, which means RT import/export filtering does not need
  to deal with AS_PATH rewriting at every hop.
- Spine RR vs full-mesh: at 2 leaves it does not matter, but the
  topology was deliberately built RR-shaped so adding a 3rd or 4th
  leaf is a one-line config change instead of a full-mesh expansion.
  The cost of doing it the wrong way is much higher than the cost of
  doing it the right way from day one.
- Putting overlay BGP on the loopback (not the P2P link) is what
  makes the overlay path-independent of any single underlay link and
  is what lets BFD-on-BGP detect spine failure without losing the
  EVPN session as long as one underlay path survives.
- `multihop ttl 2 + no-nexthop-change` is mandatory in this design:
  the spine RR reflects EVPN routes from leaf to leaf, but the
  next-hop must remain the originating leaf loopback (the actual
  VTEP), not be rewritten to the spine. The spine is NOT a VTEP.

**What would change in production:**
- ASN allocation: real production fabrics typically use 4-byte ASNs
  from the 4200000000/8 private range, with a structured allocation
  scheme (per-rack, per-pod). Lab uses 2-byte private for readability.
- Hold timers: lab uses JVD `hold-time 30`. Some shops keep the
  default 90 to reduce flap noise during maintenance.
- Authentication: production runs MD5 (or BGP-over-TCP-AO on newer
  Junos) with key rotation. Lab is unauthenticated.

**Knobs that implement it:**
- `protocols bgp group UNDERLAY` - eBGP, family inet unicast,
  per-device `local-as`
- `protocols bgp group OVERLAY` - iBGP, family evpn signaling,
  multihop ttl 2, no-nexthop-change, hold-time 30
- `protocols bgp group OVERLAY cluster <id>` (spines only)
- `routing-options forwarding-table export LOAD-BALANCE` with
  `policy-statement LOAD-BALANCE then load-balance per-packet` -
  MANDATORY for the PFE to install ECMP. Without this policy BGP
  multipath shows multiple next-hops in `inet.0` but only ONE is
  programmed in the forwarding table. This was a real bug in the
  original lab configs and is the kind of thing that quietly costs
  half the fabric capacity in production.

---

## 3. Ingress replication for BUM (broadcast / unknown / multicast)

**Decision:** EVPN BUM traffic is replicated at the ingress VTEP and
unicast-tunneled to every other VTEP that signaled interest in the
VNI via a Type-3 IMET route. No PIM, no underlay multicast, no
assisted replication.

**Why:**
- The lab has 2 leaves. With 2 VTEPs, ingress replication produces
  exactly 1 copy per BUM packet - identical cost to any multicast
  scheme. There is nothing to optimise.
- Underlay multicast (PIM-SM, PIM-SSM) requires running RP/MSDP/anycast
  RP, configuring (S,G)/(S,*) joins on every device, and adds a
  separate failure domain that has to be monitored. For a 2-leaf lab
  this is pure overhead.
- Assisted replication (AR) is a SP-class optimisation for very wide
  fabrics (dozens of VTEPs) where head-end replication starts to
  saturate the ingress link. Not relevant at this scale.

**What would change in production:**
- A 32-leaf fabric carrying multicast tenant applications (video
  conferencing, market data, IPTV) would benefit from PIM underlay.
- A 64-leaf fabric with a heavy ARP flood profile might need assisted
  replication on dedicated AR-leaf devices.
- Both decisions are driven by measured BUM rate per VNI, not by leaf
  count alone. Lab does not generate enough BUM to make either
  worthwhile.

**Knob that implements it:**
- `routing-instances EVPN-VXLAN protocols evpn multicast-mode
  ingress-replication`

---

## 4. EVPN ARP suppression at Junos default (ON)

**Decision:** Do NOT set `no-arp-suppression` per VLAN. Leave ARP
suppression at its Junos default-on state. Hosts learn the anycast
gateway MAC dynamically without any host-side workaround.

**Why:**
- With suppression on, the leaf snoops local host ARPs into the EVPN
  database, originates Type-2 (MAC+IP) routes for them, and
  proxy-replies locally to ARPs targeting the gateway IP. This is
  exactly the mechanism EVPN ARP suppression was designed for.
- Earlier revisions of this lab carried `no-arp-suppression` per VLAN
  and a static-ARP workaround on hosts. The story was "vJunos-switch
  IRB doesn't generate ARP replies." That story was wrong - the bug
  was `no-arp-suppression` itself, which disabled the snoop-and-reply
  path. Removing the knob restored Junos default behavior and dynamic
  ARP works without any host-side workaround.
- This is now a regression test in the smoke suite (Section 8 EVPN
  database object asserts): if `no-arp-suppression` ever gets
  re-introduced, the test fails because the EVPN database stops
  carrying host MAC+IP entries.

**What would change in production:** Nothing. ARP suppression at
default-on is the JVD ERB recommendation.

**`proxy-macip-advertisement` is intentionally NOT set.** Two reasons:
1. It is not a valid knob at `routing-instances <mac-vrf> protocols
   evpn` on vJunos-switch / EX9200 (syntax error).
2. It is a CRB construct: it lets a centralised L3 gateway advertise
   MAC+IP on behalf of L2-only leaves. In ERB every leaf is its own L3
   gateway and snoops ARP locally, so there is nothing to proxy.

**Knob that implements it:** Absence of `no-arp-suppression` under
`routing-instances EVPN-VXLAN vlans VLAN10` and `VLAN20`.

---

## 5. Explicit `network-isolation` profile with hard link-down action

**Decision:** Each leaf carries an explicit `protocols network-isolation
group EVPN-CORE` block that hard-shuts ESI-LAG access interfaces when
overlay BGP is lost, with a 60-second hold-time-up to prevent flapping
during normal BGP reconvergence.

**Why:**
- The default Junos behaviour - relying on LACP to time out the
  partner across an isolated leaf - is correct but slow. LACP slow
  rate is 30 seconds; even LACP fast (1s PDU, 3s timeout) is slower
  than what we get by hard-bringing-down the AE on the leaf side.
- The hard shutdown is what makes "leaf becomes isolated -> traffic
  fails over to the other leaf in <5s" actually work for ESI-LAG
  multihomed hosts. Without the explicit profile, the host bond
  keeps sending half its frames into a black hole until LACP gives
  up.
- The 60-second `hold-time up` window after BGP comes back is the
  damping that prevents AEs from flapping during normal BGP
  convergence (e.g. after a planned config commit). The smoke test
  Section 5 explicitly covers the recovery cycle.

**What would change in production:** Hold-times tuned to match the
specific BGP/BFD timer set and the operational change cadence. The
shape of the configuration is correct as-is.

**Knobs that implement it:**
- `protocols network-isolation group EVPN-CORE detection
  service-tracking core-isolation`
- `protocols network-isolation group EVPN-CORE detection hold-time
  up 60`
- `protocols network-isolation group EVPN-CORE service-tracking-action
  link-down`

---

## Appendix - lab simplifications NOT covered above

These are decisions that exist purely because this is a lab on vJunos:

- **Single tenant** (TENANT-1 only). The RT/RD scheme in NETBOX_DATA_MODEL.md
  is built to scale to N tenants mechanically; the lab just does not
  exercise multi-tenant.
- **Two leaves only.** Adding leaf3, leaf4 needs zero overlay BGP
  changes (RR design), only new underlay sessions on the spines and
  new clab links.
- **Hosts are Linux containers.** Real hosts would do GARP/RA tuning
  that the test hosts skip. setup-hosts.sh fakes the bare minimum.
- **mgmt over fxp0 in clab br-clab bridge.** Production uses dedicated
  out-of-band switches with VRF isolation; lab uses the linux bridge.

## Appendix - vJunos-switch limitations that forced specific knobs

- **Maximum MTU 9192**, not 9216. Underlay and host-facing interfaces
  use 9192; IRB unit MTU is 9000 to leave VXLAN encap headroom.
- **`proxy-macip-advertisement` not in the schema** at the mac-vrf
  protocols evpn hierarchy (syntax error). Documented in Section 4
  above.
- **No `nonstop-routing` / `nonstop-bridging`** - vJunos is single-RE
  by definition. Documented in PROJECT_PLAN.md Phase 2 "Production-only
  features (NOT testable on vJunos-switch)".
- **No PFE-level BFD**, so BFD intervals cannot go below 1000ms. The
  lab uses 1000x3 (3s detection); production-class hardware would use
  300x3 (900ms) or smaller.
- **No `vxlan-routing overlay-ecmp`** (PFE-only feature, not in
  vJunos). Underlay ECMP works via the LOAD-BALANCE policy; the
  overlay-ecmp knob is for VXLAN-to-VXLAN routing scale and is
  irrelevant in this lab.
