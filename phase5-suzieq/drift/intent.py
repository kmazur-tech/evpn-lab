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
class FabricIntent:
    """Top-level container - one of these is what intent.collect()
    returns. Everything downstream consumes this."""
    namespace: str
    devices:      List[DeviceIntent]      = field(default_factory=list)
    interfaces:   List[InterfaceIntent]   = field(default_factory=list)
    cables:       List[Cable]             = field(default_factory=list)
    bgp_sessions: List[BgpSessionIntent]  = field(default_factory=list)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

# Tag (Phase 5 convention) that the drift harness restricts itself
# to. Mirrors gen-inventory.py - same NetBox tag pulls the same set
# of devices into both the Suzieq inventory and the drift harness.
DRIFT_TAG = "suzieq"


def collect(nb: pynetbox.api, namespace: str) -> FabricIntent:
    """Pull the four intent dimensions from NetBox.

    The `namespace` arg is the SuzieQ namespace name, which in this
    project equals the NetBox site slug (gen-inventory.py emits the
    site slug as the namespace name). Filtering by site_slug here
    keeps the drift harness aligned with what the poller actually
    polls.
    """
    devices = _collect_devices(nb, namespace)
    device_names = {d.name for d in devices}

    interfaces = _collect_interfaces(nb, device_names)
    cables = _collect_cables(nb, device_names)
    bgp_sessions = _derive_bgp_sessions(nb, cables)

    return FabricIntent(
        namespace=namespace,
        devices=devices,
        interfaces=interfaces,
        cables=cables,
        bgp_sessions=bgp_sessions,
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
