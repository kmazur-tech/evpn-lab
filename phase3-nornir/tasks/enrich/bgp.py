"""BGP neighbor derivation.

Underlay neighbors come from the fabric P2P links collected by
tasks/enrich/interfaces.py - one neighbor per link, sorted by IP.

Overlay neighbors are derived by topology role: spines see leaves,
leaves see spines. Both peer over lo0.1 (the device's primary_ip4
in NetBox).
"""

import ipaddress
from typing import List

import pynetbox

from .models import BgpUnderlayNeighbor, FabricLink


def collect_underlay_neighbors(fabric_links: List[FabricLink]) -> List[BgpUnderlayNeighbor]:
    """One neighbor per fabric P2P link, sorted by IP."""
    return sorted(
        (BgpUnderlayNeighbor(ip=link.peer_ip, asn=link.peer_asn) for link in fabric_links),
        key=lambda n: ipaddress.ip_address(n.ip),
    )


def collect_overlay_neighbors(nb: pynetbox.api, device, role_slug: str) -> List[str]:
    """Spines see leaves, leaves see spines. Returns a sorted list of
    peer lo0.1 addresses (each device's primary_ip4 in NetBox)."""
    overlay_role = "leaf" if role_slug == "spine" else "spine"
    overlay_neighbors: List[str] = []
    for d in nb.dcim.devices.filter(site_id=device.site.id, role=overlay_role):
        if d.primary_ip4:
            overlay_neighbors.append(
                str(ipaddress.ip_interface(d.primary_ip4.address).ip)
            )
    overlay_neighbors.sort(key=ipaddress.ip_address)
    return overlay_neighbors
