"""Tenant + MAC-VRF enrichment.

Two leaf-only collectors:

- collect_tenants() walks all VRFs in NetBox, picks the ones with
  l3vni + tenant_id custom fields set, and builds Tenant models with
  the route-target string and Type-5 prefix list each tenant exports.

- collect_mac_vrf() builds the data the leaf mac-vrf routing-instance
  template needs: the list of access-side interfaces, the list of
  VLANs (via L2VPN terminations), and the extended-vni list.

Spines call neither - they have no tenant VRFs and no mac-vrf instance.
"""

from typing import Dict, List

import pynetbox

from .models import AccessPort, Lag, Tenant, VlanInMacVrf


# Overlay ASN for the route-target. Matches vars/junos_defaults.yml
# bgp.overlay_asn. Hardcoded here because the RT scheme is a fabric-
# wide convention, not per-host data.
OVERLAY_ASN = 65000


def collect_tenants(nb: pynetbox.api) -> List[Tenant]:
    """Build Tenant models from NetBox VRFs that have l3vni+tenant_id."""
    tenants: List[Tenant] = []
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
        tenants.append(Tenant(
            name=vrf.name,
            tenant_id=tenant_id,
            l3vni=l3vni,
            anycast_mac=anycast_mac,
            rt=f"target:{OVERLAY_ASN}:{l3vni}",
            t5_prefixes=t5_prefixes,
        ))
    return tenants


def collect_mac_vrf(
    nb: pynetbox.api,
    access_ports: List[AccessPort],
    lags: List[Lag],
) -> Dict[str, list]:
    """Return mac_vrf_interfaces, vlans_in_mac_vrf, extended_vni_list.

    mac_vrf_interfaces: access ports first (sorted), then LAG parents
    (sorted), each rendered as <name>.0. Phase 2 baselines list
    physical-then-LAG; we match.

    vlans_in_mac_vrf: VLANs bound to the (single, by Phase 3 convention)
    leaf MAC-VRF via the NetBox L2VPN terminations table.
    """
    mac_vrf_interfaces: List[str] = []
    for ap in sorted(access_ports, key=lambda x: x.name):
        mac_vrf_interfaces.append(f"{ap.name}.0")
    for lag in sorted(lags, key=lambda x: x.name):
        if lag.vlan_name:
            mac_vrf_interfaces.append(f"{lag.name}.0")

    vlans_in_mac_vrf: List[VlanInMacVrf] = []
    for l2vpn in nb.vpn.l2vpns.all():
        for term in nb.vpn.l2vpn_terminations.filter(l2vpn_id=l2vpn.id):
            v = term.assigned_object
            if v is None:
                continue
            vlans_in_mac_vrf.append(VlanInMacVrf(
                name=v.name,
                vid=v.vid,
                vni=(v.custom_fields or {}).get("vni"),
                l3_interface=f"irb.{v.vid}",
            ))
    vlans_in_mac_vrf.sort(key=lambda x: x.vid)

    extended_vni_list = [v.vni for v in vlans_in_mac_vrf if v.vni is not None]

    return {
        "mac_vrf_interfaces": mac_vrf_interfaces,
        "vlans_in_mac_vrf": vlans_in_mac_vrf,
        "extended_vni_list": extended_vni_list,
    }
