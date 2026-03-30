# EVPN-VXLAN DC Fabric - NetDevOps Lab

Building an automated EVPN-VXLAN data center fabric using containerlab, with NetBox as Source of Truth, a full CI/CD pipeline, configuration validation, security hardening, and multi-vendor DCI extension.

Goal: build a complete, repeatable workflow for managing DC network infrastructure using an Infrastructure as Code approach - from planning in NetBox to operational state validation.

---

## Phase 1 - NetBox as Source of Truth

A central source of truth for the entire infrastructure. Every piece of network information lives in NetBox - not in YAML files, not in someone's head, not in spreadsheets.

Scope:
- NetBox container in docker-compose (NetBox + PostgreSQL + Redis)
- Infrastructure modeling:
  - Sites: DC1 (DC2 added later in Phase 10)
  - Devices: 2x spine, 2x leaf (vJunos-switch), test hosts
  - Interfaces: physical spine-leaf links, loopbacks, IRB (prepared for future phases)
  - Connections: cables/links between devices (1:1 mapping with containerlab topology)
  - IP Addressing: management, loopback, P2P spine-leaf, host subnets
  - VLANs + VLAN Groups per site
  - Custom Fields: VNI per VLAN, L3VNI per VRF, ESI per LAG, anycast MAC per VRF
  - ASN modeled via native NetBox ASN objects (registry) + per-device underlay ASN in local_context_data
- NetBox population:
  - Python script (pynetbox) loading the initial state - no manual GUI clicking
  - Script is part of the repo, repeatable (idempotent)
- Documentation: design decisions (why this addressing scheme, why these ASNs, naming conventions)

Result: a complete network model in NetBox, ready for consumption by Nornir.

---

## Phase 2 - EVPN+VXLAN+ESI-LAG Fabric

Topology: 2x spine, 2x leaf on vJunos-switch in containerlab, 4x Linux hosts.

Scope:
- Containerlab topology file (`.clab.yml`) with device and link definitions - mapped to NetBox
- Juniper ERB (Edge-Routed Bridging) architecture:
  - Underlay eBGP in default routing instance (unique ASN per device)
  - Overlay iBGP with `family evpn signaling` in default instance (spines as route reflectors, AS 65000)
  - L2 overlay in `mac-vrf` routing instance with `service-type vlan-aware` (vlans, VNI-to-VLAN, vtep-source-interface)
  - L3 tenant routing in `vrf` instance (IRB anycast gateway, lo0.2, L3VNI, Type-5 routes)
  - OOB management in `mgmt_junos` instance (fxp0)
- VXLAN with VNI-to-VLAN mapping
- IRB anycast gateway on leaves (same IP + MAC on both leaves per VLAN)
- ESI-LAG (EVPN multihoming) between both leaves and test hosts (Linux containers)
  - ESI auto-derive from LACP PE system-id and admin-key
- EVPN core isolation: automatic ESI-LAG shutdown on overlay BGP loss
  - Explicit `network-isolation` profiles for faster failover (hard shutdown vs LACP timeout)
  - Hold-time tuning to prevent flapping during BGP reconvergence
- BGP operational features:
  - log-updown, graceful-restart, mtu-discovery
  - multipath multiple-as (underlay)
  - multihop no-nexthop-change, signaling loops 2 (overlay)
  - hold-time 30 (JVD timer, both UNDERLAY and OVERLAY groups)
  - graceful-restart dont-help-shared-fate-bfd-down
- BFD: multiplier 3, minimum-interval 1000 (single-hop underlay, multihop overlay)
- EVPN: duplicate-mac-detection, multicast-mode ingress-replication. Leave ARP suppression at the Junos default (ON) so leaves snoop host ARPs into the EVPN database and originate Type-2 (MAC+IP). `proxy-macip-advertisement` is intentionally NOT set: not supported on vJunos-switch (syntax error) and not needed in ERB anyway, where every leaf owns the IRB locally.
- Forwarding plane: chained-composite-next-hop ingress evpn, forwarding-table export LOAD-BALANCE policy with `load-balance per-packet` (mandatory for the PFE to install ECMP - without it BGP multipath shows multiple paths but only one next-hop is programmed)
- Chassis: aggregated-devices ethernet device-count
- IRB: `family inet mtu 9000`, `no-redirects`, virtual-gateway-address + virtual-gateway-v4-mac (anycast)
- LLDP on all interfaces (port-id-subtype interface-name)
- Jumbo MTU 9192 on fabric and host-facing interfaces (vJunos-switch caps at 9192, not 9216)
- Storm control profile on access ports
- Network-isolation profile on leaves with explicit core-isolation tracking
- 76-check smoke test suite (`smoke-tests.sh`) covering:
  - 3-stage pre-flight: BGP convergence, LLDP population, FIB programmed
  - Control plane: per-device BGP, EVPN routes, VTEP tunnels, LACP, BFD, LLDP, ESI, core-isolation
  - Data plane: L2 same-VLAN cross-leaf, L3 inter-VLAN, ESI-LAG, anycast gateway
  - Failover: ESI-LAG hard fail (docker pause), core isolation, spine failover, single-homed isolation
  - EVPN deep validation per leaf (mirrored): ECMP next-hop count in PFE, per-VNI Type-2/3, Type-5 in tenant VRF, jumbo MTU 8972 DF end-to-end, BFD diag, duplicate-MAC, EVPN database object asserts, BGP per-peer NLRI counters, interface error/drop counters
  - Cross-leaf invariants: ESI consistency, DF election agreement
  - Post-failure cleanup: VTEP withdrawal/reinstall after both ESI-LAG hard fail and core isolation
  - Poll-based waits (no hardcoded sleeps), JSON+jq parsers for fragile fields
  - Run time ~2 minutes on a converged fabric

Production-only features (NOT testable on vJunos-switch single-RE virtual platform - documented for completeness, deferred to real hardware):
- `nonstop-routing` and `layer2-control nonstop-bridging` (require dual RE)
- `network-services enhanced-ip` (QFX-only)
- `vxlan-routing overlay-ecmp` (PFE-only feature, not in vJunos)
- BFD sub-second timers (vJunos PFE-less BFD won't run faster than 1000ms)

Validated against two production EVPN-VXLAN fabrics. Earlier revisions of this lab carried `no-arp-suppression` per VLAN and a static-ARP workaround on hosts because vJunos-switch IRBs were assumed to not generate ARP replies. That assumption was wrong - the bug was `no-arp-suppression` itself, which disabled the EVPN ARP-snoop-and-reply mechanism. With ARP suppression at its Junos default (ON), leaves snoop local host ARPs into the EVPN database, originate Type-2 (MAC+IP) routes, and reply locally to gateway ARPs. Hosts learn the anycast gateway MAC dynamically without any host-side workaround.

Result: a fully operational fabric with L2 VXLAN bridging, L3 inter-VLAN routing (anycast gateway, dynamic ARP), ESI-LAG multihoming, per-packet ECMP across both spines, and a 76-check smoke test suite covering control plane, data plane, failover, EVPN deep validation, and post-failure cleanup.

---

## Phase 3 - Nornir IaC Framework

Replacing manual device configuration with an Infrastructure as Code framework - Nornir pulls data from NetBox, renders configurations, and deploys them.

Scope:
- Nornir with `nornir-netbox` plugin as inventory (hosts, groups, per-device data from NetBox API)
- Additional data from NetBox: VLANs, VNIs, ASNs, interfaces, addressing - fetched via pynetbox in tasks
- Jinja2 templates generating Junos configuration from NetBox data
- Nornir with NAPALM driver (junos) for configuration deployment
- `deploy.py` script as entry point: fetch from NetBox -> render -> deploy -> report
- Idempotency: re-running causes no changes if the NetBox intent hasn't changed

Result: `python deploy.py` builds the full fabric from zero to production. Change in NetBox -> re-deploy -> fabric updates.

---

## Phase 4 - Batfish Pre-Deployment Validation

Offline validation of generated configurations before deploying them to devices.

Scope:
- Batfish container in docker-compose
- Python validation script (pybatfish) running after config rendering, before deployment
- Tests:
  - Will all BGP sessions establish (topology and configuration analysis)
  - Are EVPN route-targets consistent across leaves
  - Are there any IP addressing conflicts
  - Do ACLs/firewall filters block control plane traffic (BGP, VXLAN UDP 4789, BFD)
- Differential analysis: before/after comparison on configuration changes
- Test output in CI-friendly format (exit code 0/1 + report)

Result: configuration errors caught before touching any device.

---

## Phase 5 - Suzieq Operational State Monitoring + NetBox Drift Detection

Re-scope rationale: the Phase 2 smoke test suite already covers the originally-planned assertions (BGP Established + per-peer EVPN NLRI, per-VNI Type-2/3, Type-5, TENANT-1 /32 object asserts, ESI-LAG with cross-leaf DF election consistency, LLDP neighbor counts, VXLAN tunnel state, MAC/IP entries against an expected host list). Re-implementing those in Suzieq would be duplication. Phase 5 instead focuses on what the smoke suite cannot do: continuous time-series state, vendor-neutral schema (needed for Phase 10 multi-vendor), and intent-vs-state diff against NetBox.

Smoke tests = deploy-time gate (one-shot, runs in CI after `containerlab deploy`).
Suzieq = runtime monitor (cron, dashboards, alerts).

Scope:
- Suzieq collector container in docker-compose, polling all DC1 devices via SSH every 60s, persisting to its Parquet store
- **NetBox-versus-Suzieq diff layer** - the killer use case:
  - Pull intent from NetBox API (devices, interfaces, BGP sessions, VLANs, VNIs, expected LLDP topology)
  - Pull state from Suzieq tables (`bgp`, `lldp`, `interfaces`, `evpnVni`, `routes`, `macs`)
  - Diff and report drift: missing BGP session, LLDP neighbor change vs NetBox cabling, VLAN-to-VNI mismatch, unexpected loopback advertised, etc.
  - This is "validation against intent" - the part that has zero overlap with the smoke suite
- **Time-series assertions** (things the smoke suite cannot answer because it is point-in-time):
  - BGP session flap count > 0 in last N minutes
  - LLDP neighbor change since baseline
  - EVPN Type-2/3/5 route count delta over time (sudden drops = silent withdraw)
  - VXLAN VTEP appear/disappear events
  - Interface error/drop counter rate (not just absolute value)
  - MAC mobility events (mac moved between VTEPs)
- **Strict assertions mirroring the smoke suite depth** (run by Suzieq on every poll, not just at deploy):
  - All BGP sessions Established AND each session has received-prefix-count > 0 per peer per AFI/SAFI
  - Per-VNI Type-2 / Type-3 route presence (not aggregate count)
  - ESI consistency: same ESI string seen on both leaves of a multi-homed group, identical DF election outcome
  - VTEP count per leaf == expected (from NetBox topology)
  - Specific tenant host /32s present in TENANT-1.inet.0 via EVPN
  - BFD session diag == None on every session
  - LLDP neighbor table matches NetBox cabling exactly (name + interface, not just count)
- Pass/fail report per assertion + structured output for the Phase 6 CI pipeline
- Optional: Suzieq REST API exposed for ad-hoc queries from operators

Result: automated drift detection between NetBox intent and live fabric state, plus a queryable time-series record of fabric behavior. Smoke and Suzieq are complementary: smoke says "this deploy landed cleanly," Suzieq says "is the fabric still in spec right now and how did it get there."

---

## Phase 6 - GitHub Actions CI/CD Pipeline

Connecting all components into an automated pipeline triggered on every PUSH/PR.

Scope:
- Workflow `.github/workflows/fabric-ci.yml`
- Pipeline stages:
  1. **Lint** - yamllint, flake8/ruff on Python, Jinja2 template validation
  2. **Render** - Nornir fetches intent from NetBox -> generates configurations
  3. **Batfish Validate** - offline analysis of generated configs + differential analysis
  4. **Batfish Results -> PR Comment** - bot posts to PR what will change (new BGP sessions, modified ACLs, affected prefixes)
  5. **Deploy** - containerlab up + Nornir deploy (optional, `workflow_dispatch` on self-hosted runner)
  6. **Smoke gate** - run `phase2-fabric/smoke-tests.sh` (~2 min). Hard fail = block merge.
  7. **Suzieq drift check** - NetBox-vs-state diff (Phase 5 Python harness). Soft fail = warn.
  8. **Teardown** - containerlab destroy
- Stages 5-7 may live in a separate workflow (spinning up the lab in CI is resource-intensive)
- Pipeline status badge in README

Result: every change to NetBox/templates is automatically validated. PRs include an impact analysis report.

---

## Phase 7 - Forwarding Scale + Convergence Tuning

Most originally-scoped Phase 7 items landed in Phase 2 during the JVD best-practice review:
- Per-packet ECMP via forwarding-table export LOAD-BALANCE - DONE in Phase 2 (was a critical bug fix; the PFE was installing only one next-hop until the policy was added)
- Anycast gateway, IRB interfaces, Type-5 routing - DONE in Phase 2
- ESI-LAG failover, leaf failure traffic takeover - DONE in Phase 2 (smoke tests Section 4)
- proxy-macip-advertisement - DROPPED. Not supported on vJunos-switch and not needed in ERB anyway (CRB construct for L2-only leaves)

Remaining scope - things that genuinely need a dedicated phase:
- VXLAN routing scale tuning: `interface-num`, `next-hop` table sizing, `shared-tunnels` (production-class scale, not relevant in a 4-device lab but worth modeling)
- Richer BGP export policy: per-subnet direct route advertisement instead of loopback-only export, with explicit allow/deny terms
- ECMP fast-reroute (BGP PIC + FRR) for sub-second failover under specific failure modes
- BGP add-path / multipath multiple-as tuning across spine RR boundary
- BFD micro-BFD on aggregated interfaces (would need physical hardware to validate)
- Selective route leaking between tenant VRFs (preview of multi-tenant work)

Most of these need real hardware to actually validate. May be re-scoped or merged into Phase 10 (multi-DC) where scale starts to matter.

Result: forwarding-plane scale knobs documented and where possible exercised; remaining items deferred to hardware lab.

---

## Phase 8 - CIS / PCI-DSS Hardening

Securing the fabric according to CIS Junos benchmarks and PCI-DSS v4.0 requirements.

Scope:
- Extended NetBox data with hardening parameters (config context or custom fields):
  - NTP servers + authentication keys
  - Syslog servers
  - RADIUS/TACACS+ servers + shared secrets
  - Allowed management subnets
  - Password policy
- New Jinja2 templates per hardening area:
  - Management ACL (restrict SSH/NETCONF access to management subnet)
  - NTP with authentication (MD5/SHA)
  - Syslog to external server (TCP + TLS if supported)
  - RADIUS/TACACS+ as authentication source
  - Disable unused services (finger, telnet, SNMP v1/v2c)
  - Enforce TLS 1.2+ on NETCONF/HTTPS
  - Login banner
  - Configuration change logging
- Extended Batfish validation for hardening controls (management ACL, protocol filtering)
- Helper container with NTP/syslog/RADIUS/TACACS+ - docker-compose, outside the main lab scope, deployed manually or via script
- Documentation: mapping of CIS/PCI-DSS controls -> Junos configuration -> NetBox data

Result: hardened fabric with documented security controls, hardening parameters managed from NetBox.

---

## Phase 9 - gNMI Monitoring

Streaming telemetry from devices to a monitoring stack.

Scope:
- gNMI configuration on vJunos (OpenConfig / Juniper-native YANG paths)
- Telegraf as gNMI collector (container in docker-compose)
- Subscriptions:
  - Interfaces: counters, errors, status
  - BGP: session state, prefixes received/sent
  - System: CPU, RAM, temperature (if available on vJunos)
  - EVPN: route count per instance
- Export to Prometheus (Prometheus outside lab scope, but endpoint exposed)
- Extended Nornir to deploy gNMI configuration to devices
- Sample Grafana dashboard as JSON (optional)

Result: devices streaming telemetry, ready for consumption by Prometheus/Grafana.

---

## Phase 10 - Second DC (Arista cEOS) + DCI

Extending with a second datacenter on a different vendor and inter-DC connectivity.

Scope:
- Extended NetBox: new site DC2, new devices (2x spine, 2x leaf cEOS), new interfaces, links, addressing, VLANs/VNIs per site
- DC2: 2x spine, 2x leaf on cEOS (Arista) in the same containerlab file
- Multi-vendor Nornir: per-vendor groups in NetBox, separate Jinja2 templates (Junos vs EOS), shared deployment logic
- EVPN Type-5 DCI between DC1 (Junos) and DC2 (EOS):
  - Option A: eBGP EVPN directly between border leaves
  - Option B: VXLAN-to-VXLAN with gateway on border leaves
- Extended Batfish with EOS configurations (Batfish supports both NOSes natively)
- Extended Suzieq with cross-DC assertions (EVPN routes from DC1 visible in DC2 and vice versa)
- Tests: L2 stretched traffic between DCs, L3 inter-DC traffic, border leaf failover
- DC2 hardening - same controls from Phase 8, different templates (EOS)

Result: multi-vendor, multi-DC EVPN-VXLAN with NetBox as single source of truth, full CI/CD pipeline, and end-to-end validation.

---

## Repository Structure (target)

```
├── clab/
│   └── topology.clab.yml
├── netbox/
│   ├── docker-compose.yml
│   ├── populate.py              # Idempotent NetBox population script
│   └── custom_fields.yml        # Custom field definitions (VNI, L3VNI, ESI, anycast MAC)
├── templates/
│   ├── junos/
│   └── eos/
├── nornir/
│   ├── config.yml
│   ├── deploy.py
│   └── tasks/
├── validation/
│   ├── batfish/
│   │   ├── validate.py
│   │   └── assertions/
│   └── suzieq/
│       ├── assert.py
│       └── checks/
├── docker-compose.yml            # Batfish, Telegraf, helper container
├── .github/
│   └── workflows/
│       ├── fabric-ci.yml         # Lint -> Render -> Batfish -> PR comment
│       └── fabric-deploy.yml     # Deploy -> Suzieq (workflow_dispatch)
├── docs/
│   ├── hardening-controls.md     # CIS/PCI-DSS controls -> configuration mapping
│   ├── architecture.md           # Topology diagram, design description
│   └── netbox-data-model.md      # NetBox data model description
└── README.md
```

---

## Pipeline - Full Workflow

```
NetBox (SoT)
    │
    ▼
Nornir: fetch intent from NetBox API
    │
    ▼
Jinja2: render configurations (Junos / EOS)
    │
    ▼
Batfish: offline validation (BGP, ACL, routing, diff)
    │
    ├── FAIL -> error report, stop
    │
    ▼ PASS
Nornir + NAPALM: deploy to devices
    │
    ▼
smoke-tests.sh: 76-check deploy-time gate (BGP, EVPN, failover, ECMP, MTU, ESI/DF, ...)
    │
    ├── FAIL -> block merge, dump diagnostics, teardown
    │
    ▼ PASS
Suzieq: continuous state monitor + NetBox drift check
    │
    ├── DRIFT -> warn (cron-driven, not deploy-blocking)
    │
    ▼ in-spec
✅ Fabric matches intent
```
