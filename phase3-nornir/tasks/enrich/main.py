"""Nornir task entry point: orchestrate every collector + validate.

Each domain collector returns pydantic model instances. main()
assembles them into a HostData, which is the SINGLE place where
schema validation runs - any drift in NetBox or any collector bug
that produces bad data raises ValidationError here, BEFORE templates
render. Templates see plain dicts via .model_dump().
"""

import os
from pathlib import Path

import pynetbox
import yaml
from nornir.core.task import Result, Task

from .bgp import collect_overlay_neighbors, collect_underlay_neighbors
from .interfaces import collect_interfaces
from .loopbacks import collect_loopbacks
from .models import HostData
from .tenants import collect_mac_vrf, collect_tenants


# vars/junos_defaults.yml is the single source of truth for fabric-
# wide constants (BGP timers, overlay ASN, MTU caps, etc). Loaded
# once at module import. Both Python collectors AND Jinja templates
# read from this file - no value lives in two places.
_DEFAULTS_PATH = Path(__file__).resolve().parents[2] / "vars" / "junos_defaults.yml"
_DEFAULTS = yaml.safe_load(_DEFAULTS_PATH.read_text(encoding="utf-8"))


def enrich_from_netbox(task: Task) -> Result:
    """Populate task.host with NetBox-derived facts.

    Validates the assembled HostData with pydantic before writing
    anything to task.host. Validation failure raises (Nornir
    surfaces the exception as a task failure with the host name).
    """
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
        import ipaddress
        router_id = str(ipaddress.ip_interface(device.primary_ip4.address).ip)

    asn = (device.local_context_data or {}).get("bgp_asn")

    # ----- Per-domain collectors -----
    iface_buckets = collect_interfaces(nb, device)
    fabric_links = iface_buckets["fabric_links"]
    access_ports = iface_buckets["access_ports"]
    lags = iface_buckets["lags"]

    loopbacks = collect_loopbacks(nb, device, role_slug)
    underlay_neighbors = collect_underlay_neighbors(fabric_links)
    overlay_neighbors = collect_overlay_neighbors(nb, device, role_slug)

    # Tenants + MAC-VRF data: leaves only. Spines have neither.
    overlay_asn = _DEFAULTS["bgp"]["overlay_asn"]
    tenants = collect_tenants(nb, overlay_asn) if role_slug == "leaf" else []
    mac_vrf = (collect_mac_vrf(nb, access_ports, lags)
               if role_slug == "leaf"
               else {"mac_vrf_interfaces": [], "vlans_in_mac_vrf": [],
                     "extended_vni_list": []})

    # Site mgmt gateways (mgmt_junos routing-instance template needs these).
    site = nb.dcim.sites.get(id=device.site.id) if device.site else None
    site_cf = (site.custom_fields if site else {}) or {}

    # ----- Single validation point -----
    host_data = HostData(
        role_slug=role_slug,
        router_id=router_id,
        asn=asn,
        fabric_links=fabric_links,
        access_ports=access_ports,
        lag_members=iface_buckets["lag_members"],
        lags=lags,
        irbs=iface_buckets["irbs"],
        loopbacks=loopbacks,
        tenants=tenants,
        mgmt_gw_v4=site_cf.get("mgmt_gw_v4"),
        mgmt_gw_v6=site_cf.get("mgmt_gw_v6"),
        mac_vrf_interfaces=mac_vrf["mac_vrf_interfaces"],
        vlans_in_mac_vrf=mac_vrf["vlans_in_mac_vrf"],
        extended_vni_list=mac_vrf["extended_vni_list"],
        underlay_neighbors=underlay_neighbors,
        overlay_neighbors=overlay_neighbors,
    )

    # ----- Dump to task.host as plain dicts/lists -----
    # Templates use bracket access on lists of dicts; pydantic
    # .model_dump() converts every nested model recursively.
    dumped = host_data.model_dump()
    for key, value in dumped.items():
        task.host[key] = value

    return Result(
        host=task.host,
        result=(
            f"router_id={router_id} asn={asn} "
            f"lo0={[u.unit for u in loopbacks]} "
            f"fabric={len(fabric_links)} access={len(access_ports)} "
            f"lag_members={len(host_data.lag_members)} lags={len(lags)} "
            f"irbs={len(host_data.irbs)}"
        ),
    )
