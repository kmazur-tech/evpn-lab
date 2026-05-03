"""NetBox intent collection for the drift harness.

The ONLY module in drift/ that imports pynetbox. Everything else
consumes the dataclasses defined here, never raw pynetbox objects.
This isolation means:

  - test_drift_diff.py / test_drift_cli.py never need pynetbox
  - The intent contract for Phase 6 CI is the dataclass shape, not
    NetBox's API surface (which can drift across NetBox versions)
  - A future swap of NetBox for a different SoT (Nautobot, custom
    YAML, etc.) only touches this file

Four intent dimensions, matching the four state dimensions in
state.py and the four diff dimensions in diff.py:

  1. devices       - which devices NetBox says exist in the namespace
  2. interfaces    - admin state per (device, ifname) for the subset
                     of interfaces NetBox actually models
  3. cables        - the cable graph: (devA, ifaceA) <-> (devB, ifaceB)
                     edges, used to derive expected LLDP topology
  4. bgp_sessions  - expected BGP sessions, derived from the P2P /31
                     fabric cables (NOT from a BGP plugin in NetBox -
                     Phase 1's NetBox does not install one)

The "intent" framing matters: this module never reaches a device.
It reads NetBox and returns "what should be true." The state.py
side reads Suzieq and returns "what IS true." diff.py compares them.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pynetbox


# ---------------------------------------------------------------------------
# Dataclass shapes - the contract between intent.py and the rest of drift/
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeviceIntent:
    name: str
    status: str        # NetBox device status: "active", "planned", "offline", etc.
    site_slug: str
    role_slug: str     # spine | leaf | server | ...


@dataclass(frozen=True)
class InterfaceIntent:
    device: str
    name: str          # e.g. "ge-0/0/0", "ae0", "lo0"
    enabled: bool      # NetBox `enabled` field == admin-up


@dataclass(frozen=True)
class CableEdge:
    """One side of a cable as a (device, interface) pair. The full
    edge is two CableEdges grouped into a Cable. Frozen for set use."""
    device: str
    interface: str


@dataclass(frozen=True)
class Cable:
    a: CableEdge
    b: CableEdge

    def normalized(self) -> Tuple[CableEdge, CableEdge]:
        """Return the two endpoints in a canonical (sorted) order so
        cable equality is direction-independent. dc1-leaf1:ge-0/0/0
        <-> dc1-spine1:ge-0/0/1 must equal dc1-spine1:ge-0/0/1 <->
        dc1-leaf1:ge-0/0/0."""
        return tuple(sorted([self.a, self.b], key=lambda e: (e.device, e.interface)))


@dataclass(frozen=True)
class BgpSessionIntent:
    """An expected BGP session derived from a fabric P2P /31 cable.
    Both endpoints are local IPs (no peer IP because both sides
    appear in the suzieq bgp table from each device's perspective)."""
    device_a: str
    ip_a: str          # local IP on device_a (the /32 part of its /31)
    device_b: str
    ip_b: str          # local IP on device_b (the other /32 of the /31)


@dataclass(frozen=True)
class VniIntent:
    """An EVPN VNI that should be configured on a device. L2 VNIs
    come from NetBox VLAN custom field `vni`; L3 VNIs come from
    VRF custom field `l3vni`. vni_type is 'L2' or 'L3'."""
    device: str
    vni: int
    vni_type: str  # "L2" or "L3"


@dataclass(frozen=True)
class LoopbackRouteIntent:
    """Each device's loopback (primary_ip4 in NetBox) should be
    reachable from every other device as a /32 route. This is the
    overlay-reachability-via-underlay check that proves the BGP
    underlay is actually doing its job."""
    observer_device: str  # the device whose route table we check
    target_device: str    # the device whose loopback we expect to see
    prefix: str           # e.g. "10.1.0.3/32"


@dataclass(frozen=True)
class AnycastMacIntent:
    """The anycast gateway MAC (from VRF custom field anycast_mac)
    should appear in each tenant leaf's MAC table for each VLAN
    bound to the tenant L2VPN. With ESI multi-homing both leaves
    use the same anycast MAC and learn the peer's via EVPN."""
    device: str
    vlan: int
    anycast_mac: str


@dataclass(frozen=True)
class PeerIrbArpIntent:
    """The leaf-local IRB IP (the per-leaf address on irb.<vid>,
    distinct from the anycast gateway IP) should appear as an ARP
    entry on every peer leaf that serves the same VLAN. Catches
    EVPN Type-2 ARP advertisement breakage."""
    observer_device: str  # the device whose arpnd table we check
    target_device: str    # the device whose IRB IP we expect to see
    target_ip: str        # the IRB IP without mask


@dataclass(frozen=True)
class FabricIntent:
    """Top-level container - one of these is what intent.collect()
    returns. Everything downstream consumes this."""
    namespace: str
    devices:      List[DeviceIntent]      = field(default_factory=list)
    interfaces:   List[InterfaceIntent]   = field(default_factory=list)
    cables:       List[Cable]             = field(default_factory=list)
    bgp_sessions: List[BgpSessionIntent]  = field(default_factory=list)
    # Part B-full additions
    vnis:             List[VniIntent]         = field(default_factory=list)
    loopback_routes:  List[LoopbackRouteIntent] = field(default_factory=list)
    anycast_macs:     List[AnycastMacIntent]    = field(default_factory=list)
    peer_irb_arps:    List[PeerIrbArpIntent]    = field(default_factory=list)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

# Tag (Phase 5 convention) that the drift harness restricts itself
# to. Mirrors gen-inventory.py - same NetBox tag pulls the same set
# of devices into both the Suzieq inventory and the drift harness.
DRIFT_TAG = "suzieq"


def collect(nb: pynetbox.api, namespace: str) -> FabricIntent:
    """Pull the eight intent dimensions from NetBox.

    The `namespace` arg is the SuzieQ namespace name, which in this
    project equals the NetBox site slug (gen-inventory.py emits the
    site slug as the namespace name). Filtering by site_slug here
    keeps the drift harness aligned with what the poller actually
    polls.

    Part B-min dimensions: devices, interfaces, cables, bgp_sessions.
    Part B-full dimensions: vnis, loopback_routes, anycast_macs,
    peer_irb_arps.
    """
    devices = _collect_devices(nb, namespace)
    device_names = {d.name for d in devices}
    leaf_names = {d.name for d in devices if d.role_slug == "leaf"}

    interfaces = _collect_interfaces(nb, device_names)
    cables = _collect_cables(nb, device_names)
    bgp_sessions = _derive_bgp_sessions(nb, cables)

    vnis = _collect_vnis(nb, leaf_names)
    loopback_routes = _collect_loopback_routes(nb, devices)
    anycast_macs = _collect_anycast_macs(nb, leaf_names)
    peer_irb_arps = _collect_peer_irb_arps(nb, leaf_names)

    return FabricIntent(
        namespace=namespace,
        devices=devices,
        interfaces=interfaces,
        cables=cables,
        bgp_sessions=bgp_sessions,
        vnis=vnis,
        loopback_routes=loopback_routes,
        anycast_macs=anycast_macs,
        peer_irb_arps=peer_irb_arps,
    )


def _collect_devices(nb, site_slug: str) -> List[DeviceIntent]:
    out = []
    for d in nb.dcim.devices.filter(site=site_slug, tag=DRIFT_TAG):
        # status comes back as either a string ("active") in older
        # NetBox or a dict-like with .value in newer (4.x). Coerce.
        status = _coerce_status(d.status)
        role = d.role.slug if d.role else "unknown"
        out.append(DeviceIntent(
            name=d.name,
            status=status,
            site_slug=site_slug,
            role_slug=role,
        ))
    return sorted(out, key=lambda x: x.name)


def _collect_interfaces(nb, device_names) -> List[InterfaceIntent]:
    if not device_names:
        return []
    out = []
    # NetBox interface filter accepts device__n=<name>; iterate to
    # avoid one massive query for fabric-scale generality.
    for dname in sorted(device_names):
        for iface in nb.dcim.interfaces.filter(device=dname):
            out.append(InterfaceIntent(
                device=dname,
                name=iface.name,
                enabled=bool(iface.enabled),
            ))
    return out


def _collect_cables(nb, device_names) -> List[Cable]:
    """Walk every cable that touches a tagged device. Reject any
    cable whose other side is not in the tagged set (host-facing
    cables to alpine containers, for example) - those would be
    false positives in the drift report because Suzieq does not
    poll the hosts."""
    if not device_names:
        return []

    seen = set()  # dedupe by normalized form
    out = []
    for dname in sorted(device_names):
        for iface in nb.dcim.interfaces.filter(device=dname):
            cable_obj = getattr(iface, "cable", None)
            if not cable_obj:
                continue
            far = _far_endpoint(iface)
            if far is None:
                continue
            far_device, far_iface = far
            if far_device not in device_names:
                # Cable goes to a host or to something outside the
                # drift scope; skip with no warning - LLDP from a
                # tagged device to an untagged peer is expected.
                continue
            cable = Cable(
                a=CableEdge(device=dname, interface=iface.name),
                b=CableEdge(device=far_device, interface=far_iface),
            )
            key = cable.normalized()
            if key in seen:
                continue
            seen.add(key)
            out.append(cable)
    return sorted(out, key=_cable_sort_key)


def _cable_sort_key(cable: "Cable") -> tuple:
    """Stringify the cable's normalized form for stable sort. We
    cannot sort directly on `cable.normalized()` because that returns
    a tuple of CableEdge dataclasses, and CableEdge is `frozen=True`
    but NOT `order=True` (intentional - we want set membership but
    not arbitrary ordering on the dataclass itself)."""
    a, b = cable.normalized()
    return (a.device, a.interface, b.device, b.interface)


def _far_endpoint(iface) -> Optional[Tuple[str, str]]:
    """Return (device_name, interface_name) for the OTHER side of
    iface's cable, or None if not connected to a single interface
    (could be terminating on a circuit, a power port, or unknown).

    NetBox 4.x uses `link_peers` (a list, since cables can fan out
    to multiple terminations on the same side); older versions used
    `connected_endpoint`. Try both shapes so the harness survives
    minor NetBox version drift."""
    # NetBox 4.x: list of peer terminations on the OTHER side of the cable
    peers = getattr(iface, "link_peers", None)
    if peers:
        peer = peers[0]
        peer_device = getattr(peer, "device", None)
        if peer_device is None:
            return None
        return (peer_device.name, peer.name)

    # Pre-4.x fallback
    endpoint = getattr(iface, "connected_endpoint", None)
    if endpoint and getattr(endpoint, "device", None):
        return (endpoint.device.name, endpoint.name)
    return None


def _derive_bgp_sessions(nb, cables: List[Cable]) -> List[BgpSessionIntent]:
    """For each fabric P2P cable, look up the IPs assigned to the
    two interfaces and emit a BgpSessionIntent if both sides have an
    IP in the same /31. Phase 1 does NOT install a NetBox BGP
    plugin - the modeling source of truth for "what BGP sessions
    should exist" is the cable + IP graph."""
    out = []
    seen = set()
    for cable in cables:
        ips_a = _interface_primary_ip(nb, cable.a.device, cable.a.interface)
        ips_b = _interface_primary_ip(nb, cable.b.device, cable.b.interface)
        if not ips_a or not ips_b:
            continue
        ip_a = ips_a.split("/")[0]
        ip_b = ips_b.split("/")[0]
        # Canonical order so a session derived from cable A->B equals
        # one derived from B->A
        pair = tuple(sorted([(cable.a.device, ip_a), (cable.b.device, ip_b)]))
        if pair in seen:
            continue
        seen.add(pair)
        out.append(BgpSessionIntent(
            device_a=pair[0][0], ip_a=pair[0][1],
            device_b=pair[1][0], ip_b=pair[1][1],
        ))
    return out


def _interface_primary_ip(nb, device_name: str, ifname: str) -> Optional[str]:
    """Return the first IP address assigned to (device, interface)
    in NetBox, or None. Phase 1 P2P fabric links each have a single
    /31 IP per side."""
    iface_list = list(nb.dcim.interfaces.filter(
        device=device_name, name=ifname,
    ))
    if not iface_list:
        return None
    iface = iface_list[0]
    ip_list = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
    if not ip_list:
        return None
    return ip_list[0].address


def _coerce_status(status_field) -> str:
    """NetBox status is a string in old versions and a dict-like
    in 4.x. Return the lowercase string form."""
    if status_field is None:
        return "unknown"
    if hasattr(status_field, "value"):
        return str(status_field.value).lower()
    return str(status_field).lower()


def _custom_field(obj, name):
    """Read a NetBox custom_fields entry. NetBox 4.x exposes
    custom_fields as a dict-like attribute on the record."""
    cf = getattr(obj, "custom_fields", None) or {}
    return cf.get(name) if isinstance(cf, dict) else None


# ---------------------------------------------------------------------------
# Part B-full: VNIs (L2 from VLANs, L3 from VRFs)
# ---------------------------------------------------------------------------

def _collect_vnis(nb, leaf_names) -> List[VniIntent]:
    """Walk NetBox VLANs and VRFs for vni / l3vni custom fields and
    emit one VniIntent per (leaf, vni). The lab assumption is that
    every leaf in the namespace participates in every tenant VLAN
    and every tenant VRF - true for the Phase 1+2 single-tenant
    fabric. Phase 10 multi-tenant work will need to filter by
    per-leaf participation."""
    out: List[VniIntent] = []
    if not leaf_names:
        return out

    l2_vnis = []
    for vlan in nb.ipam.vlans.all():
        vni = _custom_field(vlan, "vni")
        if vni is None:
            continue
        try:
            l2_vnis.append(int(vni))
        except (TypeError, ValueError):
            continue

    l3_vnis = []
    for vrf in nb.ipam.vrfs.all():
        l3vni = _custom_field(vrf, "l3vni")
        if l3vni is None:
            continue
        try:
            l3_vnis.append(int(l3vni))
        except (TypeError, ValueError):
            continue

    for leaf in sorted(leaf_names):
        for vni in sorted(l2_vnis):
            out.append(VniIntent(device=leaf, vni=vni, vni_type="L2"))
        for vni in sorted(l3_vnis):
            out.append(VniIntent(device=leaf, vni=vni, vni_type="L3"))

    return out


# ---------------------------------------------------------------------------
# Part B-full: loopback routes (overlay reachability via underlay)
# ---------------------------------------------------------------------------

def _collect_loopback_routes(nb, devices: List[DeviceIntent]) -> List[LoopbackRouteIntent]:
    """For each device with a primary_ip4 (lo0.1 in this project),
    every OTHER device that NEEDS to reach it should have a /32
    route. Catches:
      - underlay BGP not exporting loopbacks
      - eBGP next-hop-self missing on spines
      - one device's underlay totally broken (no routes anywhere)

    Topology rule (Clos-aware): in a 2-tier Clos, spines do not
    peer with each other and do not need each other's loopbacks.
    The valid (observer, target) pairs are:

      leaf  -> leaf   (transit through spines, BGP-learned)
      leaf  -> spine  (direct eBGP peer)
      spine -> leaf   (direct eBGP peer)
      spine -> spine  EXCLUDED - no path in Clos, no requirement

    Without the spine-spine exclusion the harness produces false-
    positive drift on every clean Clos fabric. Verified live on
    the lab: spine1 has 10.1.0.3 (leaf1) and 10.1.0.4 (leaf2) as
    BGP routes but no 10.1.0.2 (spine2) - architecturally correct.
    """
    out: List[LoopbackRouteIntent] = []
    # Pull the loopback IP for each device by re-querying NetBox.
    loopback_by_device = {}
    role_by_device = {d.name: d.role_slug for d in devices}
    for d in devices:
        nb_dev_list = list(nb.dcim.devices.filter(name=d.name))
        if not nb_dev_list:
            continue
        primary = getattr(nb_dev_list[0], "primary_ip4", None)
        if primary is None:
            continue
        addr = primary.address
        ip_only = addr.split("/")[0]
        # Force /32 for loopback regardless of NetBox mask
        loopback_by_device[d.name] = f"{ip_only}/32"

    sorted_devs = sorted(loopback_by_device.keys())
    for observer in sorted_devs:
        for target in sorted_devs:
            if observer == target:
                continue
            # Clos rule: skip spine -> spine pairs
            if (role_by_device.get(observer) == "spine"
                    and role_by_device.get(target) == "spine"):
                continue
            out.append(LoopbackRouteIntent(
                observer_device=observer,
                target_device=target,
                prefix=loopback_by_device[target],
            ))
    return out


# ---------------------------------------------------------------------------
# Part B-full: anycast gateway MAC (per leaf, per tenant VLAN)
# ---------------------------------------------------------------------------

def _collect_anycast_macs(nb, leaf_names) -> List[AnycastMacIntent]:
    """For each VRF with an anycast_mac custom field, the anycast
    MAC should appear in every tenant leaf's MAC table for every
    VLAN bound to the tenant L2VPN. The lab assumption is that
    every leaf serves every tenant VLAN - same simplification as
    _collect_vnis(). Phase 10 multi-tenant work will refine this.
    """
    out: List[AnycastMacIntent] = []
    if not leaf_names:
        return out

    # Find all (vlan_id, anycast_mac) pairs from NetBox.
    # Walk VLANs that have a vni custom field (== tenant L2 VLANs).
    # For each, find the VRF that owns the IRB / anycast IP for
    # that VLAN, and read its anycast_mac.
    pairs = []  # list of (vlan_vid, anycast_mac)
    for vlan in nb.ipam.vlans.all():
        if _custom_field(vlan, "vni") is None:
            continue
        # Find the anycast IP for this VLAN's IRB. The IRB
        # interface name pattern is irb.<vid>.
        irb_name = f"irb.{vlan.vid}"
        # Get any device's irb.<vid> - they all share the same VRF
        # for this VLAN in the lab's single-tenant model.
        irb_ifaces = list(nb.dcim.interfaces.filter(name=irb_name))
        if not irb_ifaces:
            continue
        anycast_mac = None
        for iface in irb_ifaces:
            ips = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
            for ip in ips:
                if ip.vrf is None:
                    continue
                vrf = nb.ipam.vrfs.get(name=ip.vrf.name)
                mac = _custom_field(vrf, "anycast_mac")
                if mac:
                    anycast_mac = mac
                    break
            if anycast_mac:
                break
        if anycast_mac:
            pairs.append((vlan.vid, anycast_mac.lower()))

    for leaf in sorted(leaf_names):
        for vlan_vid, mac in sorted(pairs):
            out.append(AnycastMacIntent(
                device=leaf,
                vlan=vlan_vid,
                anycast_mac=mac,
            ))
    return out


# ---------------------------------------------------------------------------
# Part B-full: peer leaf IRB ARP (Type-2 EVPN ARP advertisement)
# ---------------------------------------------------------------------------

def _collect_peer_irb_arps(nb, leaf_names) -> List[PeerIrbArpIntent]:
    """For each leaf's leaf-local IRB IP (the per-leaf address on
    irb.<vid>, role != anycast), every PEER leaf in the same VLAN
    should have an arpnd entry resolving that IP. Catches EVPN
    Type-2 ARP-extended-community advertisement breakage.

    The leaf-local IP is distinguished from the anycast gateway IP
    by the NetBox `role` field (anycast vs unset)."""
    out: List[PeerIrbArpIntent] = []
    if len(leaf_names) < 2:
        return out

    # Build {leaf -> [(ip_no_mask, vlan_vid)]} for leaf-local
    # (non-anycast) IRB IPs.
    leaf_irb_ips = {}
    for leaf in sorted(leaf_names):
        leaf_irb_ips[leaf] = []
        irb_ifaces = list(nb.dcim.interfaces.filter(device=leaf))
        for iface in irb_ifaces:
            if not iface.name.startswith("irb."):
                continue
            try:
                vid = int(iface.name.split(".", 1)[1])
            except ValueError:
                continue
            ips = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
            for ip in ips:
                role_val = getattr(ip.role, "value", None) if ip.role else None
                if role_val == "anycast":
                    continue  # skip the anycast gateway IP
                ip_only = ip.address.split("/")[0]
                leaf_irb_ips[leaf].append((ip_only, vid))

    # Cross-product: for each (this_leaf, ip, vlan) emit one intent
    # per peer leaf that also serves the same VLAN
    for this_leaf, entries in leaf_irb_ips.items():
        peer_leaves = sorted(set(leaf_irb_ips.keys()) - {this_leaf})
        for ip, vid in entries:
            for peer in peer_leaves:
                if any(p_vid == vid for _, p_vid in leaf_irb_ips.get(peer, [])):
                    out.append(PeerIrbArpIntent(
                        observer_device=peer,
                        target_device=this_leaf,
                        target_ip=ip,
                    ))
    return out
