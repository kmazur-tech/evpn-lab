"""Enrich Nornir hosts with data from NetBox via pynetbox.

Phase 3 templates need more than what NetBoxInventory2 provides out of
the box. This task hydrates host.data with structured fields the
Jinja2 templates consume directly.

Each new template stanza adds the fields it needs here. Keep field
names short and stable - templates depend on them.
"""

import os
import ipaddress

import pynetbox
from nornir.core.task import Result, Task


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

    # router_id: lo0.1 IPv4 without mask. Phase 1 sets lo0.1 as primary_ip4
    # for fabric devices, so primary_ip4 is the cleanest source.
    router_id = None
    if device.primary_ip4:
        router_id = str(ipaddress.ip_interface(device.primary_ip4.address).ip)
    task.host["router_id"] = router_id

    # Underlay ASN from local_context_data.bgp_asn (set by populate.py).
    lcd = device.local_context_data or {}
    task.host["asn"] = lcd.get("bgp_asn")

    return Result(
        host=task.host,
        result=f"router_id={router_id} asn={task.host['asn']}",
    )
