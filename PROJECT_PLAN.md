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
  - L2 overlay in `virtual-switch` routing instance (vlans, VNI-to-VLAN)
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
  - vpn-apply-export, multihop no-nexthop-change, signaling loops 2 (overlay)
  - graceful-restart dont-help-shared-fate-bfd-down
- BFD: multiplier 3, session-mode (single-hop underlay, multihop overlay)
- EVPN: duplicate-mac-detection, multicast-mode ingress-replication, no-arp-suppression per VLAN
- Forwarding plane: chained-composite-next-hop ingress evpn, vxlan-routing overlay-ecmp
- Chassis: aggregated-devices ethernet device-count, network-services enhanced-ip
- nonstop-routing, layer2-control nonstop-bridging
- LLDP on all interfaces (port-id-subtype interface-name)
- Jumbo frames (mtu 9216 on fabric and host-facing interfaces)
- Storm control profiles on external-facing interfaces
- Manual baseline verification: `show bgp summary`, `show evpn instance`, `show ethernet-switching table`
- Traffic tests: L2 within VLAN, L3 inter-VLAN, ESI-LAG failover

Known vjunos-switch limitation: IRB L3 gateway (inter-VLAN routing) does not work on the simulated EX9214 data plane. The bridge-to-IRB punt path is not functional in the simulator - hosts cannot ARP the gateway despite correct config, routing tables, and EVPN routes. L2 VXLAN bridging, ESI-LAG, and EVPN route exchange all work correctly. The config is validated against two production EVPN-VXLAN fabrics and is correct for real hardware. Consider vjunos-router (MX simulation) for L3 testing if needed.

Result: a working fabric with L2 VXLAN bridging, ESI-LAG multihoming, and production-grade operational features. L3 inter-VLAN routing requires real hardware or vjunos-router.

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

## Phase 5 - Suzieq Post-Deployment Validation

Operational state validation after deployment - does what's running match the intent in NetBox.

Scope:
- Suzieq collecting data from devices (SSH/NETCONF) after deployment
- Assertions in Python or Suzieq CLI:
  - All BGP sessions in Established state
  - EVPN routes present on all leaves
  - ESI-LAG active on both leaves
  - LLDP neighbors matching the topology (and NetBox)
  - VXLAN interfaces up
  - MAC/IP entries in EVPN tables matching expectations
- Pass/fail report per assertion

Result: automated verification that the fabric operates according to intent.

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
  6. **Suzieq Assert** - operational state validation after deployment
  7. **Teardown** - containerlab destroy
- Stages 5-7 may live in a separate workflow (spinning up the lab in CI is resource-intensive)
- Pipeline status badge in README

Result: every change to NetBox/templates is automatically validated. PRs include an impact analysis report.

---

## Phase 7 - ECMP + Forwarding Optimization

Optimizing traffic forwarding and load balancing across the fabric.

Note: anycast gateway, IRB interfaces, and Type-5 routing moved to Phase 2.

Scope:
- ECMP load balancing: forwarding-table export PFE-LB (per-packet)
- ECMP fast-reroute for sub-second failover
- VXLAN routing scale tuning (interface-num, next-hop count, shared-tunnels)
- Richer BGP export policy (direct routes per subnet, not just loopback)
- proxy-macip-advertisement on IRB interfaces
- Extended Suzieq assertions: ECMP working (traceroute from hosts), both leaves as gateway
- Failover tests: shutting down one leaf, verifying traffic takeover

Result: optimized forwarding with ECMP load balancing and fast convergence.

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
Suzieq: operational state validation
    │
    ├── FAIL -> discrepancy report
    │
    ▼ PASS
✅ Fabric matches intent
```
