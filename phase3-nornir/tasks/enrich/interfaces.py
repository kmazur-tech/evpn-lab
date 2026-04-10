"""Interface bucketing - the largest enrich domain.

Walks every interface on the device once and sorts them into:
- fabric_links   (P2P spine-leaf, with peer IP/ASN derived from /31)
- access_ports   (single-homed host ports with untagged_vlan)
- lag_members    (physical members bound to an ae interface)
- lags           (ae0/ae1 with deterministic ESI-LAG knobs)
- irbs           (anycast gateway interfaces with leaf-local + virtual IP)

Returns a dict with the five lists. Each list contains pydantic
model instances; the orchestrator validates and dumps them to plain
dicts before assigning to task.host.
"""

import ipaddress
import re
from typing import Dict, List

import pynetbox

from .models import AccessPort, FabricLink, Irb, Lag, LagMember


# Devices we treat as fabric peers (have an underlay ASN, are part of
# the EVPN fabric). Anything else cabled to a ge- port is treated as
# a host (handled via untagged_vlan -> AccessPort).
FABRIC_PEER_PREFIXES = ("dc1-spine", "dc1-leaf")


def collect_interfaces(nb: pynetbox.api, device) -> Dict[str, List]:
    """Bucket every interface on `device` by purpose.

    Returns dict with keys: fabric_links, access_ports, lag_members,
    lags, irbs - each a list of validated pydantic model instances,
    sorted for stable rendering.
    """
    all_ifaces = list(nb.dcim.interfaces.filter(device_id=device.id))

    fabric_links: List[FabricLink] = []
    access_ports: List[AccessPort] = []
    lag_members: List[LagMember] = []
    lags: List[Lag] = []
    irbs: List[Irb] = []

    for iface in all_ifaces:
        name = iface.name

        # Fabric P2P or access port: physical (ge-) interface, no LAG parent.
        if name.startswith("ge-") and iface.lag is None:
            link = _try_fabric_link(nb, device, iface)
            if link is not None:
                fabric_links.append(link)
                continue
            # Not a fabric P2P. If it has untagged_vlan, it's an access port.
            if iface.untagged_vlan:
                access_ports.append(AccessPort(
                    name=name,
                    vlan_name=iface.untagged_vlan.name,
                ))
                continue

        # LAG member: physical with iface.lag set.
        if name.startswith("ge-") and iface.lag is not None:
            lag_members.append(LagMember(
                name=name,
                lag_name=iface.lag.name,
            ))
            continue

        # LAG parent: type=lag, name like ae0/ae1.
        if iface.type and getattr(iface.type, "value", None) == "lag":
            lags.append(_build_lag(iface))
            continue

        # IRB: virtual interface named irb.N
        m = re.fullmatch(r"irb\.(\d+)", name)
        if m:
            irbs.append(_build_irb(nb, iface, int(m.group(1))))
            continue

    fabric_links.sort(key=lambda x: x.name)
    access_ports.sort(key=lambda x: x.name)
    lag_members.sort(key=lambda x: x.name)
    lags.sort(key=lambda x: x.name)
    irbs.sort(key=lambda x: x.unit)

    return {
        "fabric_links": fabric_links,
        "access_ports": access_ports,
        "lag_members": lag_members,
        "lags": lags,
        "irbs": irbs,
    }


def _try_fabric_link(nb, device, iface):
    """If iface is cabled to another fabric device AND has an IP,
    return a FabricLink with peer IP/ASN derived. Else None."""
    if not iface.cable:
        return None
    ip_objs = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
    if not ip_objs:
        return None

    cable = nb.dcim.cables.get(iface.cable.id)
    peer_dev_name = None
    for term in (cable.a_terminations + cable.b_terminations):
        obj = term.object
        if obj and getattr(obj, "device", None) and obj.device.name != device.name:
            peer_dev_name = obj.device.name
            break

    if peer_dev_name is None or not peer_dev_name.startswith(FABRIC_PEER_PREFIXES):
        return None

    local_addr = ip_objs[0].address
    # Peer P2P IP = the OTHER /31 host. Peer ASN = peer's bgp_asn LCD field.
    net = ipaddress.ip_interface(local_addr).network
    hosts = [str(h) for h in net.hosts()] or [
        str(ipaddress.ip_interface(local_addr).ip),
    ]
    local_ip = str(ipaddress.ip_interface(local_addr).ip)
    peer_ips = [h for h in hosts if h != local_ip]
    peer_ip = peer_ips[0] if peer_ips else None

    peer_dev = nb.dcim.devices.get(name=peer_dev_name)
    peer_asn = (peer_dev.local_context_data or {}).get("bgp_asn") if peer_dev else None

    return FabricLink(
        name=iface.name,
        description=f"to {peer_dev_name}",
        address=local_addr,
        peer_ip=peer_ip,
        peer_asn=peer_asn,
        peer_name=peer_dev_name,
    )


def _build_lag(iface) -> Lag:
    """Construct a Lag with deterministic ESI-LAG knobs.

    The ae index drives both system-id and admin-key per the Phase 2
    convention (admin-key = ae_index + 1; system-id encodes ae_index
    in the second-to-last octet). See phase2-fabric/DESIGN.md.
    """
    ae_index = int(re.fullmatch(r"ae(\d+)", iface.name).group(1))
    return Lag(
        name=iface.name,
        ae_index=ae_index,
        vlan_name=iface.untagged_vlan.name if iface.untagged_vlan else None,
        admin_key=ae_index + 1,
        system_id=f"00:00:00:00:0{ae_index + 3}:00",
    )


def _build_irb(nb, iface, unit: int) -> Irb:
    """Pull leaf-local + anycast IPs and the VRF's anycast MAC."""
    leaf_ip = None
    gateway_ip = None
    vrf_name = None
    for ip in nb.ipam.ip_addresses.filter(interface_id=iface.id):
        if ip.role and ip.role.value == "anycast":
            gateway_ip = str(ipaddress.ip_interface(ip.address).ip)
        else:
            leaf_ip = ip.address
        if ip.vrf:
            vrf_name = ip.vrf.name

    anycast_mac = None
    if vrf_name:
        vrf = nb.ipam.vrfs.get(name=vrf_name)
        anycast_mac = (vrf.custom_fields or {}).get("anycast_mac")

    return Irb(
        unit=unit,
        leaf_ip=leaf_ip,
        gateway_ip=gateway_ip,
        anycast_mac=anycast_mac,
    )
