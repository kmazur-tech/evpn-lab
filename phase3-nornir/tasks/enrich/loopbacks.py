"""lo0 unit collection.

Walks every lo0.* interface on the device, pulls each non-anycast IP,
and emits a LoopbackUnit with the Junos display description string
the template will render verbatim.
"""

from typing import List

import pynetbox

from .helpers import _lo0_unit_from_iface_name, _loopback_description
from .models import LoopbackUnit


def collect_loopbacks(nb: pynetbox.api, device, role_slug: str) -> List[LoopbackUnit]:
    """Return all lo0.N units sorted by unit number."""
    loopbacks: List[LoopbackUnit] = []

    for iface in nb.dcim.interfaces.filter(device_id=device.id, name__isw="lo0."):
        unit = _lo0_unit_from_iface_name(iface.name)
        if unit is None:
            continue
        for ip in nb.ipam.ip_addresses.filter(interface_id=iface.id):
            # Skip anycast (IRB-side) and other non-loopback roles. lo0 IPs
            # have role=None in NetBox.
            if ip.role:
                continue
            vrf_name = ip.vrf.name if ip.vrf else None
            loopbacks.append(LoopbackUnit(
                unit=unit,
                address=ip.address,
                description=_loopback_description(unit, role_slug, vrf_name),
            ))

    loopbacks.sort(key=lambda u: u.unit)
    return loopbacks
