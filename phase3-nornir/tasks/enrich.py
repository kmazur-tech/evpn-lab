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
    task.host["role_slug"] = role_slug

    # router_id: lo0.1 IPv4 without mask. Phase 1 sets lo0.1 as primary_ip4
    # for fabric devices, so primary_ip4 is the cleanest source.
    router_id = None
    if device.primary_ip4:
        router_id = str(ipaddress.ip_interface(device.primary_ip4.address).ip)
    task.host["router_id"] = router_id

    # Underlay ASN from local_context_data.bgp_asn (set by populate.py).
    lcd = device.local_context_data or {}
    task.host["asn"] = lcd.get("bgp_asn")

    # ----- Interfaces -----
    # Pull all interfaces once and bucket them by purpose so the template
    # iterates simple lists rather than re-querying NetBox.
    all_ifaces = list(nb.dcim.interfaces.filter(device_id=device.id))

    fabric_links = []   # ge-0/0/0..N P2P with peer description
    access_ports = []   # single-homed host ports
    lag_members = []    # physical members of an ESI-LAG
    lags = []           # ae0/ae1
    irbs = []           # irb.<vlan>

    for iface in all_ifaces:
        name = iface.name
        # Fabric P2P: physical interface, has an IP, has a cable to a
        # spine/leaf peer (not a host). Cable termination tells us the peer.
        if name.startswith("ge-") and iface.cable and iface.lag is None:
            ip_objs = list(nb.ipam.ip_addresses.filter(interface_id=iface.id))
            peer_dev_name = None
            cable = nb.dcim.cables.get(iface.cable.id)
            for term in (cable.a_terminations + cable.b_terminations):
                obj = term.object
                if obj and getattr(obj, "device", None) and obj.device.name != device.name:
                    peer_dev_name = obj.device.name
                    break
            if ip_objs and peer_dev_name and peer_dev_name.startswith(("dc1-spine", "dc1-leaf")):
                local_addr = ip_objs[0].address
                # Compute peer P2P IP as the other host of the /31 and look
                # up the peer's underlay ASN from local_context_data.
                net = ipaddress.ip_interface(local_addr).network
                hosts = [str(h) for h in net.hosts()] or [
                    str(ipaddress.ip_interface(local_addr).ip),
                ]
                local_ip = str(ipaddress.ip_interface(local_addr).ip)
                peer_ips = [h for h in hosts if h != local_ip]
                peer_ip = peer_ips[0] if peer_ips else None
                peer_dev = nb.dcim.devices.get(name=peer_dev_name)
                peer_asn = (peer_dev.local_context_data or {}).get("bgp_asn") if peer_dev else None
                fabric_links.append({
                    "name": name,
                    "description": f"to {peer_dev_name}",
                    "address": local_addr,
                    "peer_ip": peer_ip,
                    "peer_asn": peer_asn,
                    "peer_name": peer_dev_name,
                })
                continue
            # Access port: physical, has untagged_vlan, no IP, no LAG parent.
            if iface.untagged_vlan and iface.lag is None:
                access_ports.append({
                    "name": name,
                    "vlan_name": iface.untagged_vlan.name,
                })
                continue

        # LAG member: physical with iface.lag set.
        if name.startswith("ge-") and iface.lag is not None:
            lag_members.append({
                "name": name,
                "lag_name": iface.lag.name,
            })
            continue

        # LAG parent: type=lag, name like ae0/ae1.
        if iface.type and getattr(iface.type, "value", None) == "lag":
            ae_index = int(re.fullmatch(r"ae(\d+)", name).group(1))
            lags.append({
                "name": name,
                "ae_index": ae_index,
                "vlan_name": iface.untagged_vlan.name if iface.untagged_vlan else None,
                # Deterministic ESI-LAG knobs (matches Phase 2 baselines):
                # admin-key = ae_index + 1; system-id encodes ae_index in
                # the second-to-last octet. See phase2-fabric/DESIGN.md.
                "admin_key": ae_index + 1,
                "system_id": f"00:00:00:00:0{ae_index + 3}:00",
            })
            continue

        # IRB: virtual interface named irb.N
        m = re.fullmatch(r"irb\.(\d+)", name)
        if m:
            unit = int(m.group(1))
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
            irbs.append({
                "unit": unit,
                "leaf_ip": leaf_ip,
                "gateway_ip": gateway_ip,
                "anycast_mac": anycast_mac,
            })
            continue

    fabric_links.sort(key=lambda x: x["name"])
    access_ports.sort(key=lambda x: x["name"])
    lag_members.sort(key=lambda x: x["name"])
    lags.sort(key=lambda x: x["name"])
    irbs.sort(key=lambda x: x["unit"])

    task.host["fabric_links"] = fabric_links
    task.host["access_ports"] = access_ports
    task.host["lag_members"] = lag_members
    task.host["lags"] = lags
    task.host["irbs"] = irbs

    # ----- Tenants (leaves only need this) -----
    # One entry per tenant VRF this device serves. The template uses
    # name, l3vni, rt (community string), and t5_prefixes (route-filter
    # list for the Type-5 EXPORT/IMPORT policies).
    tenants = []
    if role_slug == "leaf":
        overlay_asn = 65000   # matches vars/junos_defaults.yml bgp.overlay_asn
        for vrf in nb.ipam.vrfs.all():
            cf = vrf.custom_fields or {}
            l3vni = cf.get("l3vni")
            tenant_id = cf.get("tenant_id")
            anycast_mac = cf.get("anycast_mac")
            if l3vni is None or tenant_id is None:
                continue
            t5_prefixes = sorted(
                str(p.prefix) for p in nb.ipam.prefixes.filter(vrf_id=vrf.id)
            )
            tenants.append({
                "name": vrf.name,
                "tenant_id": tenant_id,
                "l3vni": l3vni,
                "anycast_mac": anycast_mac,
                "rt": f"target:{overlay_asn}:{l3vni}",
                "t5_prefixes": t5_prefixes,
            })
    task.host["tenants"] = tenants

    # ----- Site mgmt gateways (for mgmt_junos routing-instance) -----
    site = nb.dcim.sites.get(id=device.site.id) if device.site else None
    site_cf = (site.custom_fields if site else {}) or {}
    task.host["mgmt_gw_v4"] = site_cf.get("mgmt_gw_v4")
    task.host["mgmt_gw_v6"] = site_cf.get("mgmt_gw_v6")

    # ----- MAC-VRF data (leaves only) -----
    # interfaces: access ports first (sorted), then LAG parents (sorted),
    # rendered as <name>.0. Phase 2 baselines list physical-then-LAG.
    mac_vrf_interfaces = []
    if role_slug == "leaf":
        for ap in sorted(access_ports, key=lambda x: x["name"]):
            mac_vrf_interfaces.append(f"{ap['name']}.0")
        for lag in sorted(lags, key=lambda x: x["name"]):
            if lag["vlan_name"]:
                mac_vrf_interfaces.append(f"{lag['name']}.0")
    task.host["mac_vrf_interfaces"] = mac_vrf_interfaces

    # VLANs bound to the (single) MAC-VRF on this leaf, via the L2VPN
    # terminations table. Each entry: name, vid, vni, l3_interface.
    vlans_in_mac_vrf = []
    if role_slug == "leaf":
        for l2vpn in nb.vpn.l2vpns.all():
            for term in nb.vpn.l2vpn_terminations.filter(l2vpn_id=l2vpn.id):
                v = term.assigned_object
                if v is None:
                    continue
                vlans_in_mac_vrf.append({
                    "name": v.name,
                    "vid": v.vid,
                    "vni": (v.custom_fields or {}).get("vni"),
                    "l3_interface": f"irb.{v.vid}",
                })
    vlans_in_mac_vrf.sort(key=lambda x: x["vid"])
    task.host["vlans_in_mac_vrf"] = vlans_in_mac_vrf

    # VNI list for `extended-vni-list [ ... ]`
    task.host["extended_vni_list"] = [v["vni"] for v in vlans_in_mac_vrf if v["vni"]]

    # ----- BGP neighbors -----
    # Underlay: one neighbor per fabric P2P link (peer IP + peer ASN).
    # Sorted by neighbor IP to match Phase 2 baseline ordering.
    underlay_neighbors = sorted(
        ({"ip": l["peer_ip"], "asn": l["peer_asn"]} for l in fabric_links),
        key=lambda n: ipaddress.ip_address(n["ip"]),
    )
    task.host["underlay_neighbors"] = underlay_neighbors

    # Overlay: spines see leaves, leaves see spines. Both peer over lo0.1.
    overlay_role = "leaf" if role_slug == "spine" else "spine"
    overlay_neighbors = []
    for d in nb.dcim.devices.filter(site_id=device.site.id, role=overlay_role):
        if d.primary_ip4:
            overlay_neighbors.append(str(ipaddress.ip_interface(d.primary_ip4.address).ip))
    overlay_neighbors.sort(key=ipaddress.ip_address)
    task.host["overlay_neighbors"] = overlay_neighbors

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
        result=(
            f"router_id={router_id} asn={task.host['asn']} "
            f"lo0={[u['unit'] for u in loopbacks]} "
            f"fabric={len(fabric_links)} access={len(access_ports)} "
            f"lag_members={len(lag_members)} lags={len(lags)} irbs={len(irbs)}"
        ),
    )
