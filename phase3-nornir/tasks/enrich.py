"""Enrich Nornir hosts with data from NetBox via pynetbox.

Phase 3 templates need more than what NetBoxInventory2 provides out of
the box. This task hydrates host.data with structured fields the
Jinja2 templates consume directly.

Each new template stanza adds the fields it needs here. Keep field
names short and stable - templates depend on them.
"""

import os
import ipaddress
import re

import pynetbox
from nornir.core.task import Result, Task


def _lo0_unit_from_iface_name(name: str):
    """`lo0.1` -> 1, `lo0.2` -> 2; returns None for non-lo0 interfaces."""
    m = re.fullmatch(r"lo0\.(\d+)", name)
    return int(m.group(1)) if m else None


def _loopback_description(unit: int, role_slug: str, vrf_name):
    """Map (lo0 unit, device role, VRF) -> Junos lo0 unit description.

    Spines don't act as VTEP, leaves do, so lo0.1 description differs.
    Anything in a VRF is named "VRF <name>" (matches Junos routing
    instance name). Keeps presentation logic out of NetBox.
    """
    if unit == 1:
        return "Router-ID / VTEP" if role_slug == "leaf" else "Router-ID"
    if vrf_name:
        return f"VRF {vrf_name}"
    return f"lo0.{unit}"


def enrich_from_netbox(task: Task) -> Result:
    """Populate task.host.data with NetBox-derived facts."""
    nb = pynetbox.api(
        os.environ["NETBOX_URL"],
        token=os.environ["NETBOX_TOKEN"],
    )

    device = nb.dcim.devices.get(name=task.host.name)
    if device is None:
        return Result(host=task.host, failed=True,
                      result=f"Device {task.host.name} not found in NetBox")

    role_slug = device.role.slug if device.role else None

    # router_id: lo0.1 IPv4 without mask. Phase 1 sets lo0.1 as primary_ip4
    # for fabric devices, so primary_ip4 is the cleanest source.
    router_id = None
    if device.primary_ip4:
        router_id = str(ipaddress.ip_interface(device.primary_ip4.address).ip)
    task.host["router_id"] = router_id

    # Underlay ASN from local_context_data.bgp_asn (set by populate.py).
    lcd = device.local_context_data or {}
    task.host["asn"] = lcd.get("bgp_asn")

    # All lo0 units with their IPs, sorted by unit number. Each entry has
    # the unit number, address (CIDR as stored in NetBox), and the Junos
    # description string the template will emit verbatim.
    loopbacks = []
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
            loopbacks.append({
                "unit": unit,
                "address": ip.address,
                "description": _loopback_description(unit, role_slug, vrf_name),
            })
    loopbacks.sort(key=lambda u: u["unit"])
    task.host["loopbacks"] = loopbacks

    return Result(
        host=task.host,
        result=f"router_id={router_id} asn={task.host['asn']} lo0_units={[u['unit'] for u in loopbacks]}",
    )
