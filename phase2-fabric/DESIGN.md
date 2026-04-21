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

## 5. Tenant RT/RD scheme (Option C - tenant-id encoding) + explicit Type-5 policies

**Decision:** Use a deterministic, NetBox-driven scheme that derives every
RT and RD from a single `tenant_id` (and the corresponding L3VNI) custom
field. Use explicit `vrf-import` / `vrf-export` policies (not `vrf-target`)
so the import/export decision is visible in policy-options. Use explicit
Type-5 export and import allowlist policies in the tenant VRF so the
advertised prefix set is a deliberate decision, not a side-effect of
`advertise direct-nexthop`.

**RT/RD encoding:**
| Object | Field | Encoding |
|---|---|---|
| Tenant identifier | NetBox `vrf.custom_fields.tenant_id` | Sequential int: T1=1, T2=2, ... |
| L3VNI | NetBox `vrf.custom_fields.l3vni` | `5000 + (tenant_id - 1)` (T1 -> 5000) |
| L3 VRF RD | derived | `<router_id>:<L3VNI>` (e.g. `10.1.0.3:5000`) |
| L3 VRF RT | derived | `target:<overlay_asn>:<L3VNI>` (e.g. `target:65000:5000`) |
| L2 MAC-VRF RD | derived | `<router_id>:<tenant_id>` (e.g. `10.1.0.3:1`) |
| L2 MAC-VRF RT | derived | `target:<overlay_asn>:<L3VNI>` (same as L3 VRF) |

L2 and L3 share the same RT for the same tenant: a tenant's MAC routes
and IP-prefix routes import together. The L2 RD uses `tenant_id` rather
than `L3VNI` because Junos rejects duplicate RDs across instances on the
same device, and the L3 VRF already owns `<lo>:<L3VNI>`. For multi-tenant
separation later, the L2 RT can be split off with an offset (e.g.
`target:65000:25000` = "L2 layer of L3VNI 5000").

**`FABRIC-TENANT-RT-RANGE` community:** A regex extended community
`target:65000:5...` (Junos POSIX-style: `.` matches any single character,
so this matches the entire 5000-5999 L3VNI range). Defined in
`policy-options` for use by future fabric-wide ops policies and as
design documentation. Not currently referenced by an active term.

**Type-5 export filter (`TENANT-1-T5-EXPORT`):**
Without this, `advertise direct-nexthop` would export EVERY direct
route in `TENANT-1.inet.0` - including future IRB subnets, the per-VRF
`lo0.2`, or any accidentally-leaked Direct route. The whitelist makes
the intent explicit and forces a policy edit before any new tenant
subnet starts being advertised. The lab whitelist is exactly the two
configured subnets:

```
policy-statement TENANT-1-T5-EXPORT {
    term tenant-subnets {
        from {
            route-filter 10.10.10.0/24 exact;
            route-filter 10.10.20.0/24 exact;
        }
        then accept;
    }
    term reject { then reject; }
}
```

**Type-5 import filter (`TENANT-1-T5-IMPORT`):** Symmetric. Even with a
single tenant in the lab, the explicit allowlist hardens against stray
Type-5 imports if a misconfigured neighbor ever tags a wrong RT.

**Why the RT/RD scheme is a separate decision from the policy:**
The encoding tells you HOW to compute an RT for a given tenant (Phase 3
Nornir templates use it). The policy tells you WHAT to do with routes
that carry that RT (current Phase 2 configs use it). Both layers exist
because either alone is insufficient.

**What would change in production:**
- The whitelist would be NetBox-templated per tenant (`{% for prefix in
  vrf.prefixes %}route-filter {{ prefix }} exact;{% endfor %}`) so adding
  a subnet in NetBox automatically updates the whitelist on the next
  Nornir push.
- For multi-tenant separation, the L2 RT can be split off with an
  offset (`target:65000:25000` = "L2 layer of L3VNI 5000"), or move to
  per-policy import/export with multiple RT communities per tenant.

**Knobs that implement it:**
- `policy-options community TENANT-1-RT members target:65000:5000`
- `policy-options community FABRIC-TENANT-RT-RANGE members "target:65000:5..."`
- `policy-options policy-statement EVPN-IMPORT-TENANT-1` / `EVPN-EXPORT-TENANT-1`
- `policy-options policy-statement TENANT-1-T5-EXPORT` / `TENANT-1-T5-IMPORT`
- `routing-instances EVPN-VXLAN { vrf-import EVPN-IMPORT-TENANT-1; vrf-export EVPN-EXPORT-TENANT-1; route-distinguisher <lo>:<tenant_id>; }`
- `routing-instances TENANT-1 { vrf-import EVPN-IMPORT-TENANT-1; vrf-export EVPN-EXPORT-TENANT-1; route-distinguisher <lo>:<L3VNI>; protocols evpn ip-prefix-routes { advertise direct-nexthop; export TENANT-1-T5-EXPORT; import TENANT-1-T5-IMPORT; } }`

**See also:** The full encoding table and the Python expressions that
Phase 3 templates will use are in
[`../phase1-netbox/NETBOX_DATA_MODEL.md`](../phase1-netbox/NETBOX_DATA_MODEL.md)
Step 8.

---

## 6. Explicit `network-isolation` profile with hard link-down action

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
- **No `forwarding-options vxlan-routing` hierarchy**. Verified 2026-04-11
  on dc1-leaf1 vJunos 23.2R1.14 via `configure private; set forwarding-options
  vxlan-routing; commit check` - the parser returns `syntax error` with
  the caret pointing at `vxlan-routing`, meaning the entire hierarchy
  (`overlay-ecmp`, `next-hop` scaling, `shared-tunnels`) is absent on
  this platform, not just one leaf knob. Underlay ECMP still works via
  the LOAD-BALANCE policy + `multipath multiple-as` (that path lives
  under `routing-options`, unrelated). The `vxlan-routing` hierarchy is
  for PFE-level VXLAN-to-VXLAN routing scale tuning and is irrelevant
  in this lab regardless.
- **No operational view of `forwarding-options storm-control-profiles`**
  on vJunos-switch (`show forwarding-options storm-control-profiles`
  and `show ethernet-switching storm-control` both return "syntax
  error"). The profile config and the per-interface binding both
  commit cleanly, so they are kept as design intent for the day this
  fabric is rendered onto real hardware. Runtime enforcement on vJunos
  cannot be verified.

---

## Appendix - review responses (decisions deliberately NOT taken)

This section captures items that have come up in code reviews but were
intentionally rejected, so future reviewers do not re-raise them. Each
item links the proposal to the reasoning behind the rejection.

### `vpn-apply-export` on the spine OVERLAY group - rejected

**Proposal:** Re-add `vpn-apply-export` to `protocols bgp group OVERLAY`
on both spines so the spine RR applies an export policy to reflected
EVPN routes (vs blindly reflecting everything).

**Why rejected:** `vpn-apply-export` is an L3VPN-era knob that is a
no-op on `family evpn signaling` *on a route reflector*. The spine has
no routing-instances and no `vrf-export` policy to "apply" - it
reflects routes at the `bgp.evpn.0` table level, which is the normal
behavior of an iBGP RR and what JVD recommends for small/medium
fabrics. Leaf-side `vrf-import` policies (the existing
`EVPN-IMPORT-TENANT-1`) drop unwanted RTs at import time.

The legitimate underlying concern (the RR sending traffic that the
recipient will discard) is real at multi-tenant scale. The correct
mechanism is **BGP Route Target Constrain (RFC 4684 / Junos `family
route-target`)**, where each leaf signals which RTs it wants and the
spine sends only matching routes. RTC is the production answer when
the spine fan-out × the tenant count starts producing measurable
wasted reflection. For 2 leaves and 1 tenant it is overkill and
adds an extra address family that the smoke suite would have to
validate.

If the lab grows past 4 tenants on 4+ leaves, revisit and add
`family route-target` to the OVERLAY group on both spines and
leaves. Until then, leave the spine RR as-is.

### Static `fxp0` address `10.0.0.15/24` on every device - leave as-is

**Observation:** Every device's startup-config carries the same
literal `interfaces fxp0 unit 0 family inet address 10.0.0.15/24`.
This is the vJunos-switch placeholder address.

**Why kept:** Containerlab overrides the actual management IP at
runtime via the `mgmt-ipv4` field in `dc1.clab.yml`. The literal in
the startup-config is never the live address. Junos requires
`family inet` to have *some* address before the config commits, so
removing the line risks breaking the cold-boot flow. Cost of
removal is real, benefit is purely cosmetic. Documented here so
the next reviewer does not flag it.

### Phase 8 hardening backlog (deliberately deferred)

Items raised in Phase 2 reviews that belong to the Phase 8 hardening
scope, kept on the backlog instead of bolted onto Phase 2:

- **BGP authentication** (MD5 or TCP-AO) on UNDERLAY and OVERLAY
  groups. CIS / PCI-DSS control. Plain text BGP is fine for a lab,
  not fine for prod.
- **Storm control rate thresholds** on the `sc-default` profile
  (currently the profile is bound to host-facing interfaces but only
  declares `all;` with no `bandwidth-percentage`). Bind exists,
  threshold does not - effectively a placeholder until Phase 8 sets
  the real percentage.
- **`commit synchronize`** / **`commit confirmed`** workflow knobs.
- **`protect-re` firewall filter** on `lo0` (full CoPP).
- **SSH hardening** (deny root, specific ciphers, rate-limit).
- **Custom login classes** with idle-timeout, deny-commands.

All of these are tracked in `PROJECT_PLAN.md` Phase 8 ("CIS/PCI-DSS
Hardening"). Adding them in Phase 2 would muddy the architecture
work and force the smoke suite to grow assertions for things that
have nothing to do with EVPN-VXLAN behavior.
