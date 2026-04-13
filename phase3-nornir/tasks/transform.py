"""Nornir inventory transform_function.

NetBoxInventory2 sets host.hostname to primary_ip4 (the loopback,
unreachable from outside the fabric) and host.platform to the NetBox
platform name "Junos" rather than the NAPALM driver "junos". Both
need overriding before any NAPALM task runs.

Wired in via nornir.yml:
    inventory:
      transform_function: tasks.transform.fabric_inventory_transform

This is the idiomatic Nornir way to mutate inventory data; doing it
in main() with a for-loop after InitNornir is a code smell.
"""

import os

from nornir.core.inventory import Host


def fabric_inventory_transform(host: Host) -> None:
    """Mutate a Host in place: real mgmt IP, NAPALM driver, creds."""
    # Per-device OOB mgmt IP from MGMT_<name with - as _> env var.
    # MGMT_* values are stored CIDR (e.g. <ip>/<mask>); strip mask.
    env_key = f"MGMT_{host.name.replace('-', '_')}"
    mgmt = os.environ.get(env_key, "")
    if "/" in mgmt:
        mgmt = mgmt.split("/", 1)[0]
    if mgmt:
        host.hostname = mgmt

    # NAPALM expects the driver name lowercase. NetBox stores the
    # platform display name "Junos".
    host.platform = "junos"

    # SSH credentials NAPALM uses to reach the device.
    host.username = os.environ.get("JUNOS_SSH_USER")
    host.password = os.environ.get("JUNOS_SSH_PASSWORD")
