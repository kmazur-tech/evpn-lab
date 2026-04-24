"""Structured intent-vs-state comparison.

The pure-function core of the drift harness. Imports neither
pynetbox nor pyarrow - takes intent.FabricIntent and state.FabricState
as inputs, returns a list of structured Drift records. This is what
makes the unit tests cheap: every test in test_drift_diff.py builds
intent and state from inline dicts, no external systems involved.

Drift dimensions in this module map 1:1 to the intent and state
collectors:

  device_presence    - NetBox-modeled device not seen by SuzieQ
                       (or vice versa)
  interface_admin    - NetBox `enabled` does not match SuzieQ
                       `adminState` for a NetBox-modeled interface
  lldp_topology      - NetBox cable graph does not match the LLDP
                       neighbor table observed live
  bgp_session        - a BGP session that NetBox cabling implies
                       should exist is not seen by SuzieQ as
                       Established (or no row at all)

Each Drift record has a stable shape so the Phase 6 CI consumer can
match on `dimension` and `severity` without parsing strings.
"""
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from .intent import (
    AnycastMacIntent,
    BgpSessionIntent,
    Cable,
    DeviceIntent,
    FabricIntent,
    InterfaceIntent,
    LoopbackRouteIntent,
    PeerIrbArpIntent,
    VniIntent,
)
from .state import FabricState


# ---------------------------------------------------------------------------
# Drift record - the unit of output
# ---------------------------------------------------------------------------

SEVERITY_ERROR = "error"      # CI should soft-fail (warn loudly, do not block merge per Phase 6 plan)
SEVERITY_WARNING = "warning"  # CI should log

# Drift categories. A coarser axis than dimension: a Phase 6 consumer
# can filter/prioritize by category without having to enumerate every
# dimension name. Each diff function in this module and each assertion
# in drift/assertions/ picks exactly one category per emitted Drift.
CATEGORY_INVENTORY    = "inventory"     # device presence, interface admin state
CATEGORY_TOPOLOGY     = "topology"      # LLDP cabling
CATEGORY_CONTROL_PLANE = "control_plane" # BGP sessions, underlay reachability
CATEGORY_OVERLAY      = "overlay"       # EVPN VNIs, anycast MAC, VTEP discovery
CATEGORY_ARP_ND       = "arp_nd"        # ARP / ND / Type-2 advertisements
CATEGORY_META         = "meta"          # harness self-health (sqPoller)

_VALID_CATEGORIES = {
    CATEGORY_INVENTORY,
    CATEGORY_TOPOLOGY,
    CATEGORY_CONTROL_PLANE,
    CATEGORY_OVERLAY,
    CATEGORY_ARP_ND,
    CATEGORY_META,
}


@dataclass
class Drift:
    dimension: str
    severity: str
    category: str          # one of CATEGORY_* constants
    subject: str           # human-readable identifier ("dc1-leaf1", "dc1-spine1:ge-0/0/0", "10.1.4.0<->10.1.4.1")
    detail: str            # one-line explanation
    intent: Optional[Dict[str, Any]] = None
    state:  Optional[Dict[str, Any]] = None

    def __post_init__(self):
        # Loud failure if a caller passes an unknown category. Typos
        # in category strings would silently break Phase 6 filtering,
        # so enforce the allowlist at construction time.
        if self.category not in _VALID_CATEGORIES:
            raise ValueError(
                f"Drift category {self.category!r} not in {sorted(_VALID_CATEGORIES)}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "severity": self.severity,
            "category": self.category,
            "subject": self.subject,
            "detail": self.detail,
            "intent": self.intent,
            "state": self.state,
        }


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def compare(intent: FabricIntent, state: FabricState) -> List[Drift]:
    """Run all eight dimension comparisons. Returns a flat list of
    Drift records, sorted by dimension then subject so output is
    stable across runs (important for golden-file tests in Phase 6
    and for human readability)."""
    drifts: List[Drift] = []
    # Part B-min dimensions
    drifts.extend(_diff_devices(intent.devices, state.devices))
    drifts.extend(_diff_interfaces(intent.interfaces, state.interfaces))
    drifts.extend(_diff_lldp(intent.cables, state.lldp))
    drifts.extend(_diff_bgp(intent.bgp_sessions, state.bgp))
    # Part B-full dimensions
    drifts.extend(_diff_evpn_vnis(intent.vnis, state.evpn_vnis))
    drifts.extend(_diff_loopback_routes(intent.loopback_routes, state.routes))
    drifts.extend(_diff_anycast_macs(intent.anycast_macs, state.macs))
    drifts.extend(_diff_peer_irb_arp(intent.peer_irb_arps, state.arpnd))
    return sorted(drifts, key=lambda d: (d.dimension, d.subject))


# ---------------------------------------------------------------------------
# Dimension 1: device presence
# ---------------------------------------------------------------------------

def _diff_devices(intent_devs: List[DeviceIntent], state_df: pd.DataFrame) -> List[Drift]:
    """NetBox-modeled-but-not-polled is an error (poller config drift
    or device truly missing). Polled-but-not-modeled is a warning
    (could be a stale Suzieq row from a removed device, or a real
    operator action that hasn't been reflected in NetBox yet)."""
    out: List[Drift] = []
    intent_names = {d.name for d in intent_devs}
    state_names = set(state_df["hostname"].tolist()) if not state_df.empty else set()

    for d in intent_devs:
        if d.name not in state_names:
            out.append(Drift(
                dimension="device_presence",
                severity=SEVERITY_ERROR,
                category=CATEGORY_INVENTORY,
                subject=d.name,
                detail="modeled in NetBox (status={}) but not seen by SuzieQ".format(d.status),
                intent=asdict(d),
                state=None,
            ))

    for hostname in sorted(state_names - intent_names):
        out.append(Drift(
            dimension="device_presence",
            severity=SEVERITY_WARNING,
            category=CATEGORY_INVENTORY,
            subject=hostname,
            detail="seen by SuzieQ but not tagged 'suzieq' in NetBox dc1 site",
            intent=None,
            state={"hostname": hostname},
        ))

    return out


# ---------------------------------------------------------------------------
# Dimension 2: interface admin state
# ---------------------------------------------------------------------------

def _diff_interfaces(
    intent_ifaces: List[InterfaceIntent],
    state_df: pd.DataFrame,
) -> List[Drift]:
    """Compare NetBox `enabled` vs SuzieQ `adminState` for the
    interfaces NetBox actually models. We deliberately do NOT report
    on interfaces SuzieQ sees that NetBox does not model - the
    fabric devices have many internal interfaces (lo0.16384, jsrv,
    em0, esi, ...) that NetBox correctly does not track."""
    out: List[Drift] = []

    state_lookup = {}
    if not state_df.empty:
        for _, row in state_df.iterrows():
            state_lookup[(row["hostname"], row["ifname"])] = row

    for ii in intent_ifaces:
        key = (ii.device, ii.name)
        row = state_lookup.get(key)
        if row is None:
            # Interface modeled in NetBox but not seen by SuzieQ on
            # this device. Could be a Junos unit suffix mismatch
            # (NetBox: ge-0/0/0, SuzieQ: ge-0/0/0.0) - downgrade to
            # warning so we don't drown in noise on first run.
            out.append(Drift(
                dimension="interface_admin",
                severity=SEVERITY_WARNING,
                category=CATEGORY_INVENTORY,
                subject=f"{ii.device}:{ii.name}",
                detail="modeled in NetBox but not in SuzieQ interface table",
                intent=asdict(ii),
                state=None,
            ))
            continue

        # SuzieQ adminState convention: "up" / "down". NetBox
        # convention: bool enabled. Translate then compare.
        suzieq_admin_up = str(row.get("adminState", "")).lower() == "up"
        if suzieq_admin_up != ii.enabled:
            out.append(Drift(
                dimension="interface_admin",
                severity=SEVERITY_ERROR,
                category=CATEGORY_INVENTORY,
                subject=f"{ii.device}:{ii.name}",
                detail=(
                    f"admin state drift: NetBox enabled={ii.enabled}, "
                    f"SuzieQ adminState={row.get('adminState')!r}"
                ),
                intent=asdict(ii),
                state={
                    "adminState": row.get("adminState"),
                    "state": row.get("state"),
                },
            ))

    return out


# ---------------------------------------------------------------------------
# Dimension 3: LLDP cabling
# ---------------------------------------------------------------------------

def _diff_lldp(intent_cables: List[Cable], state_df: pd.DataFrame) -> List[Drift]:
    """Each NetBox cable should appear as an LLDP neighbor in SuzieQ.

    Two-tier match strategy:

      Tier A (strict, interface-level): the LLDP row reports BOTH
        peerHostname AND peerIfname. We compare the canonical
        (devA, ifaceA) <-> (devB, ifaceB) edge against NetBox's
        cable graph. Catches interface-level miscabling within a
        device pair (e.g. A:ge-0/0/0 went to B:ge-0/0/2 instead of
        B:ge-0/0/1).

      Tier B (degraded, device-level): the LLDP row has
        peerHostname but peerIfname is empty. This is the case for
        vJunos `show lldp neighbors | display json` which omits
        `lldp-remote-port-id` entirely (verified against
        vJunos-switch 23.2R1.14 - the remote port id is only in
        the `detail` view, which SuzieQ's junos template does not
        use). Falls back to checking that the LLDP row reports
        the right peer DEVICE; cannot verify the peer interface.

    Severity:
      - cable matched at Tier A     -> no drift
      - cable matched at Tier B     -> warning (peer iface unknown)
      - cable not matched at all    -> error (real drift or LLDP gone)

    Catches:
      - mis-cabled device pairs (cable physically connects A to C,
        NetBox says A to B) -> error in either tier
      - mis-cabled interfaces within a device pair -> error in
        Tier A only; not detectable in Tier B by definition
      - missing LLDP neighbor (cable broken, port flap)
      - LLDP timer not yet converged (would also produce a missing-
        neighbor error - we can't distinguish that from real drift
        on a first poll cycle, but a re-run after 60s clears it
        if it was just timing)
    """
    out: List[Drift] = []

    # Build TWO indices over the LLDP table.
    strict_edges = set()  # (device, iface) <-> (device, iface) canonical
    device_adjacency = set()  # (deviceA, ifaceA) -> set of peer device names
    device_adj_map: Dict[Any, set] = {}

    if not state_df.empty:
        for _, row in state_df.iterrows():
            local_host = row.get("hostname")
            local_if = _strip_unit(row.get("ifname"))
            peer_host = row.get("peerHostname")
            peer_if = _strip_unit(row.get("peerIfname"))

            if not local_host or not local_if or not peer_host:
                continue

            # Tier B index: every (local_dev, local_iface) -> peer_dev
            device_adj_map.setdefault((local_host, local_if), set()).add(peer_host)

            # Tier A index: only when both interfaces are present
            if peer_if:
                edge = tuple(sorted([
                    (local_host, local_if),
                    (peer_host, peer_if),
                ]))
                strict_edges.add(edge)

    for cable in intent_cables:
        canonical = tuple(sorted([
            (cable.a.device, cable.a.interface),
            (cable.b.device, cable.b.interface),
        ]))

        # Tier A: strict interface-level match
        if canonical in strict_edges:
            continue

        # Tier B: device-level match. Check that EITHER end of the
        # cable shows the OTHER end's device as a peer.
        a_peers = device_adj_map.get((cable.a.device, cable.a.interface), set())
        b_peers = device_adj_map.get((cable.b.device, cable.b.interface), set())
        if cable.b.device in a_peers or cable.a.device in b_peers:
            out.append(Drift(
                dimension="lldp_topology",
                severity=SEVERITY_WARNING,
                category=CATEGORY_TOPOLOGY,
                subject=f"{cable.a.device}:{cable.a.interface}<->{cable.b.device}:{cable.b.interface}",
                detail=(
                    "LLDP peer device matches but peer interface is unknown "
                    "(SuzieQ Junos LLDP template uses summary view which "
                    "omits lldp-remote-port-id). Interface-level miscabling "
                    "within this device pair cannot be detected."
                ),
                intent={
                    "a": asdict(cable.a),
                    "b": asdict(cable.b),
                },
                state={"degraded_match": "device-level only"},
            ))
            continue

        # Neither tier matched -> real drift
        out.append(Drift(
            dimension="lldp_topology",
            severity=SEVERITY_ERROR,
            category=CATEGORY_TOPOLOGY,
            subject=f"{cable.a.device}:{cable.a.interface}<->{cable.b.device}:{cable.b.interface}",
            detail="NetBox cable not present in SuzieQ LLDP neighbor table",
            intent={
                "a": asdict(cable.a),
                "b": asdict(cable.b),
            },
            state=None,
        ))

    return out


def _strip_unit(ifname: Optional[str]) -> Optional[str]:
    """Junos interfaces in LLDP appear with their unit suffix:
    'ge-0/0/0' physical, 'ge-0/0/0.0' logical unit. NetBox cables
    bind to the physical interface name. Strip the unit so the
    comparison matches."""
    if not ifname or "." not in ifname:
        return ifname
    return ifname.split(".", 1)[0]


# ---------------------------------------------------------------------------
# Dimension 4: BGP session presence
# ---------------------------------------------------------------------------

def _diff_bgp(
    intent_sessions: List[BgpSessionIntent],
    state_df: pd.DataFrame,
) -> List[Drift]:
    """For each cable-derived BGP session expectation, look for a
    matching SuzieQ row on EITHER side of the session (a session
    appears in the bgp table once per device, with the local device
    as `hostname` and the remote IP as `peer`).

    The check uses the `peer` IP, not the `peerHostname` field,
    because Phase 1 NetBox does not auto-resolve the peer IP back
    to a device name in the bgp table - SuzieQ does that lookup
    only when both sides have been polled and the loopback IPs are
    in NetBox primary_ip4. We rely on IP equality instead, which
    is what gen-inventory.py / cable IP modeling guarantees.
    """
    out: List[Drift] = []

    # Build observed-sessions index keyed by (local_hostname, peer_ip)
    observed = {}  # (host, peer) -> row dict
    if not state_df.empty:
        for _, row in state_df.iterrows():
            observed[(row["hostname"], row["peer"])] = row

    for s in intent_sessions:
        # The session should appear from BOTH ends. We check end A.
        # If end B is also missing, that's a duplicate report - the
        # symmetric check is intentional because either side could
        # be down independently.
        for local_dev, local_ip, peer_ip in (
            (s.device_a, s.ip_a, s.ip_b),
            (s.device_b, s.ip_b, s.ip_a),
        ):
            row = observed.get((local_dev, peer_ip))
            if row is None:
                out.append(Drift(
                    dimension="bgp_session",
                    severity=SEVERITY_ERROR,
                    category=CATEGORY_CONTROL_PLANE,
                    subject=f"{local_dev}({local_ip})->{peer_ip}",
                    detail="cable-derived BGP session not present in SuzieQ bgp table",
                    intent=asdict(s),
                    state=None,
                ))
                continue
            state_str = str(row.get("state", "")).lower()
            if state_str != "established":
                out.append(Drift(
                    dimension="bgp_session",
                    severity=SEVERITY_ERROR,
                    category=CATEGORY_CONTROL_PLANE,
                    subject=f"{local_dev}({local_ip})->{peer_ip}",
                    detail=f"BGP session not Established: state={row.get('state')!r}",
                    intent=asdict(s),
                    state={
                        "state": row.get("state"),
                        "afi": row.get("afi"),
                        "safi": row.get("safi"),
                    },
                ))

    return out


# ---------------------------------------------------------------------------
# Dimension 5: EVPN VNI presence (Part B-full)
# ---------------------------------------------------------------------------

def _diff_evpn_vnis(intent_vnis: List[VniIntent], state_df: pd.DataFrame) -> List[Drift]:
    """Each NetBox-modeled VNI (L2 from VLAN custom field, L3 from
    VRF custom field) should appear in the SuzieQ evpnVni table on
    every leaf, with state == up.

    Catches:
      - VNI not configured at all (NetBox model says it should be,
        Phase 3 render didn't ship it - rare but possible during
        a partial deploy)
      - VNI configured but not up (typically a VLAN-to-VNI mapping
        broken in the templates, or an irb interface admin-down)
    """
    out: List[Drift] = []
    if state_df.empty:
        # The whole table is empty - emit one drift per intent VNI
        for v in intent_vnis:
            out.append(Drift(
                dimension="evpn_vni",
                severity=SEVERITY_ERROR,
                category=CATEGORY_OVERLAY,
                subject=f"{v.device}:vni{v.vni}",
                detail=f"{v.vni_type} VNI {v.vni} expected on {v.device} but evpnVni table is empty",
                intent=asdict(v),
                state=None,
            ))
        return out

    # Build {(hostname, vni) -> row} from state
    state_idx = {}
    for _, row in state_df.iterrows():
        try:
            state_idx[(row["hostname"], int(row["vni"]))] = row
        except (KeyError, TypeError, ValueError):
            continue

    for v in intent_vnis:
        row = state_idx.get((v.device, v.vni))
        if row is None:
            out.append(Drift(
                dimension="evpn_vni",
                severity=SEVERITY_ERROR,
                category=CATEGORY_OVERLAY,
                subject=f"{v.device}:vni{v.vni}",
                detail=f"{v.vni_type} VNI {v.vni} expected on {v.device} but not in SuzieQ evpnVni table",
                intent=asdict(v),
                state=None,
            ))
            continue
        state_str = str(row.get("state", "")).lower()
        if state_str != "up":
            out.append(Drift(
                dimension="evpn_vni",
                severity=SEVERITY_ERROR,
                category=CATEGORY_OVERLAY,
                subject=f"{v.device}:vni{v.vni}",
                detail=f"{v.vni_type} VNI {v.vni} on {v.device} not up: state={row.get('state')!r}",
                intent=asdict(v),
                state={"state": row.get("state"), "type": row.get("type")},
            ))
    return out


# ---------------------------------------------------------------------------
# Dimension 6: Loopback routes (overlay reachability via underlay)
# ---------------------------------------------------------------------------

def _diff_loopback_routes(
    intent_routes: List[LoopbackRouteIntent],
    state_df: pd.DataFrame,
) -> List[Drift]:
    """Each device's loopback /32 should be present in every other
    device's route table. The check is per (observer, target) pair.

    Catches:
      - underlay BGP session up but loopback not exported
      - eBGP next-hop-self missing on a spine -> leaf cannot resolve
      - one device's underlay completely broken (no foreign routes
        at all in its table)
      - typo in loopback IP modeling vs rendered config
    """
    out: List[Drift] = []
    if state_df.empty:
        for r in intent_routes:
            out.append(Drift(
                dimension="loopback_route",
                severity=SEVERITY_ERROR,
                category=CATEGORY_CONTROL_PLANE,
                subject=f"{r.observer_device}->{r.target_device}({r.prefix})",
                detail=f"routes table empty - cannot verify {r.target_device} loopback reachability from {r.observer_device}",
                intent=asdict(r),
                state=None,
            ))
        return out

    # Build {hostname -> set(prefixes)} from state. We use the
    # default VRF only - underlay loopbacks live in the global RIB.
    routes_by_host = {}
    for _, row in state_df.iterrows():
        if str(row.get("vrf", "")).lower() not in ("default", ""):
            continue
        host = row.get("hostname")
        prefix = row.get("prefix")
        if host is None or prefix is None:
            continue
        routes_by_host.setdefault(host, set()).add(prefix)

    for r in intent_routes:
        prefixes = routes_by_host.get(r.observer_device, set())
        if r.prefix not in prefixes:
            out.append(Drift(
                dimension="loopback_route",
                severity=SEVERITY_ERROR,
                category=CATEGORY_CONTROL_PLANE,
                subject=f"{r.observer_device}->{r.target_device}({r.prefix})",
                detail=f"{r.observer_device} has no /32 route to {r.target_device}'s loopback {r.prefix}",
                intent=asdict(r),
                state=None,
            ))
    return out


# ---------------------------------------------------------------------------
# Dimension 7: Anycast gateway MAC presence (Part B-full)
# ---------------------------------------------------------------------------

def _diff_anycast_macs(
    intent_macs: List[AnycastMacIntent],
    state_df: pd.DataFrame,
) -> List[Drift]:
    """The anycast gateway MAC for each tenant VLAN should appear in
    every leaf's MAC table for that VLAN. With ESI multi-homing both
    leaves use the same anycast MAC and learn the peer's via EVPN.

    Catches:
      - anycast MAC not configured (NetBox VRF custom field changed
        but Phase 3 render didn't pick up - rare)
      - VLAN-to-MAC-VRF binding broken (the MAC table for that VLAN
        is empty or missing the gateway entry)
      - EVPN MAC advertisement broken between leaves
    """
    out: List[Drift] = []
    if state_df.empty:
        for m in intent_macs:
            out.append(Drift(
                dimension="anycast_mac",
                severity=SEVERITY_ERROR,
                category=CATEGORY_OVERLAY,
                subject=f"{m.device}:vlan{m.vlan}({m.anycast_mac})",
                detail=f"anycast MAC {m.anycast_mac} expected on {m.device} vlan{m.vlan} but macs table is empty",
                intent=asdict(m),
                state=None,
            ))
        return out

    # Build {(hostname, vlan, macaddr_lower) -> row}
    state_idx = set()
    for _, row in state_df.iterrows():
        try:
            host = row.get("hostname")
            vlan = int(row.get("vlan", 0))
            mac = str(row.get("macaddr", "")).lower()
        except (TypeError, ValueError):
            continue
        if host and mac:
            state_idx.add((host, vlan, mac))

    for m in intent_macs:
        if (m.device, m.vlan, m.anycast_mac.lower()) not in state_idx:
            out.append(Drift(
                dimension="anycast_mac",
                severity=SEVERITY_ERROR,
                category=CATEGORY_OVERLAY,
                subject=f"{m.device}:vlan{m.vlan}({m.anycast_mac})",
                detail=f"anycast MAC {m.anycast_mac} not in {m.device} mac table for vlan {m.vlan}",
                intent=asdict(m),
                state=None,
            ))
    return out


# ---------------------------------------------------------------------------
# Dimension 8: Peer leaf IRB ARP (EVPN Type-2 ARP advertisement)
# ---------------------------------------------------------------------------

def _diff_peer_irb_arp(
    intent_arps: List[PeerIrbArpIntent],
    state_df: pd.DataFrame,
) -> List[Drift]:
    """Each leaf-local IRB IP should appear in every peer leaf's
    arpnd table. EVPN Type-2 routes carry the ARP binding so the
    peer learns the local-IRB-IP -> local-IRB-MAC mapping without
    a real ARP exchange.

    Catches:
      - EVPN Type-2 ARP extended community not advertised
      - peer leaf's import policy filtering the route
      - IRB interface down on the source leaf (no IP to advertise)
    """
    out: List[Drift] = []
    if state_df.empty:
        for a in intent_arps:
            out.append(Drift(
                dimension="peer_irb_arp",
                severity=SEVERITY_ERROR,
                category=CATEGORY_ARP_ND,
                subject=f"{a.observer_device}->{a.target_device}({a.target_ip})",
                detail=f"arpnd table empty - cannot verify peer IRB resolution",
                intent=asdict(a),
                state=None,
            ))
        return out

    # Build {(hostname, ipAddress) -> row}
    state_idx = {}
    for _, row in state_df.iterrows():
        host = row.get("hostname")
        ip = row.get("ipAddress")
        if host and ip:
            state_idx[(host, str(ip))] = row

    for a in intent_arps:
        row = state_idx.get((a.observer_device, a.target_ip))
        if row is None:
            out.append(Drift(
                dimension="peer_irb_arp",
                severity=SEVERITY_ERROR,
                category=CATEGORY_ARP_ND,
                subject=f"{a.observer_device}->{a.target_device}({a.target_ip})",
                detail=f"{a.observer_device} has no ARP entry for peer {a.target_device}'s IRB IP {a.target_ip}",
                intent=asdict(a),
                state=None,
            ))
    return out
