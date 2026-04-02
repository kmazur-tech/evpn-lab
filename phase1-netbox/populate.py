#!/usr/bin/env python3
"""Populate NetBox with EVPN-VXLAN lab data (Phase 1, Steps 1-13).

Reads structured data from netbox-data.yml and creates all objects in
dependency order. Uses create-only convergence - existing objects are
skipped, not updated or deleted.

Environment variables required:
    NETBOX_URL      - NetBox base URL (e.g. http://netbox:8000)
    NETBOX_TOKEN    - API token (v2 format: nbt_xxx.yyy)
    MGMT_SUBNET     - Management subnet CIDR
    MGMT_dc1_spine1 - Management IP/mask for dc1-spine1
    MGMT_dc1_spine2 - Management IP/mask for dc1-spine2
    MGMT_dc1_leaf1  - Management IP/mask for dc1-leaf1
    MGMT_dc1_leaf2  - Management IP/mask for dc1-leaf2
"""

import argparse
import os
import re
import sys
from pathlib import Path

import pynetbox
import yaml

# Only these env vars are substituted in netbox-data.yml.
# Prevents accidental injection from system vars like PATH, HOME, etc.
EXPECTED_ENV_VARS = [
    "NETBOX_URL", "NETBOX_TOKEN", "MGMT_SUBNET",
    "MGMT_dc1_spine1", "MGMT_dc1_spine2",
    "MGMT_dc1_leaf1", "MGMT_dc1_leaf2",
]

CHECK_MODE = False
missing_count = 0


def load_config():
    """Load netbox-data.yml and resolve $VARIABLE placeholders from env."""
    config_path = Path(__file__).parent / "netbox-data.yml"
    raw = config_path.read_text()

    # Resolve only expected $VARIABLE placeholders (skip comments)
    lines = raw.split('\n')
    for i, line in enumerate(lines):
        if line.lstrip().startswith('#'):
            continue
        for key in EXPECTED_ENV_VARS:
            value = os.environ.get(key, "")
            if value:
                lines[i] = lines[i].replace(f"${key}", value)
    raw = '\n'.join(lines)

    # Check for unresolved expected variables in non-comment lines
    unresolved = []
    for line in raw.split('\n'):
        if line.lstrip().startswith('#'):
            continue
        for match in re.findall(r'\$([A-Z_][A-Za-z0-9_]*)', line):
            if match in EXPECTED_ENV_VARS:
                unresolved.append(match)
    if unresolved:
        print(f"ERROR: Unresolved env variables: {', '.join(set(unresolved))}")
        print("Set them in your environment. See .env.example in repo root.")
        sys.exit(1)

    return yaml.safe_load(raw)


def slugify(name):
    """Generate a slug from a name."""
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')


def ensure_slug(data):
    """Add a slug field if the data has a name but no slug."""
    if "name" in data and "slug" not in data:
        data["slug"] = slugify(data["name"])
    return data


def get_or_create(endpoint, lookup_keys, data, label=""):
    """Get existing object or create new one. Returns (object, created)."""
    global missing_count
    data = ensure_slug(data)
    lookup = {k: data[k] for k in lookup_keys if k in data}
    existing = endpoint.filter(**lookup)
    results = list(existing)
    if results:
        print(f"  EXISTS: {label or lookup}")
        return results[0], False

    if CHECK_MODE:
        print(f"  MISSING: {label or lookup}")
        missing_count += 1
        return None, False

    try:
        obj = endpoint.create(data)
        print(f"  CREATED: {label or lookup}")
        return obj, True
    except pynetbox.RequestError as e:
        print(f"  ERROR creating {label or lookup}: {e}")
        raise


def main():
    global CHECK_MODE, missing_count

    parser = argparse.ArgumentParser(description="Populate NetBox with EVPN-VXLAN lab data")
    parser.add_argument("--check", action="store_true",
                        help="Check mode: verify all objects exist without creating anything")
    args = parser.parse_args()
    CHECK_MODE = args.check

    if CHECK_MODE:
        print("=== CHECK MODE - no changes will be made ===\n")

    # Connect to NetBox
    url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        print("ERROR: NETBOX_URL and NETBOX_TOKEN must be set")
        sys.exit(1)

    nb = pynetbox.api(url, token=token)
    config = load_config()

    # Step 1 - Custom Fields
    print("\n=== Step 1: Custom Fields ===")
    for cf in config["custom_fields"]:
        type_map = {"integer": "integer", "text": "text"}
        data = {
            "name": cf["name"],
            "type": type_map[cf["type"]],
            "object_types": cf["content_types"],
            "description": cf.get("description", ""),
        }
        get_or_create(nb.extras.custom_fields, ["name"], data, cf["name"])

    # Step 2 - Tags
    print("\n=== Step 2: Tags ===")
    for tag in config["tags"]:
        get_or_create(nb.extras.tags, ["slug"], tag, tag["name"])

    # Step 3 - Regions
    print("\n=== Step 3: Regions, Tenants ===")
    for region in config["regions"]:
        parent, _ = get_or_create(
            nb.dcim.regions, ["name"],
            {"name": region["name"], "slug": region["name"].lower()},
            region["name"],
        )
        for child in region.get("children", []):
            get_or_create(
                nb.dcim.regions, ["name"],
                {"name": child["name"], "slug": child["name"].lower(),
                 "parent": parent.id if parent else None},
                f"  {child['name']}",
            )

    # Tenant Groups
    for tg in config["tenant_groups"]:
        get_or_create(
            nb.tenancy.tenant_groups, ["name"],
            {"name": tg["name"], "slug": tg["name"].lower()},
            tg["name"],
        )

    # Tenants
    for t in config["tenants"]:
        group = nb.tenancy.tenant_groups.get(name=t["group"])
        get_or_create(
            nb.tenancy.tenants, ["name"],
            {"name": t["name"], "slug": t["name"].lower().replace(" ", "-"),
             "group": group.id if group else None,
             "description": t.get("description", "")},
            t["name"],
        )

    # Step 4 - Sites
    print("\n=== Step 4: Sites ===")
    for site in config["sites"]:
        region = nb.dcim.regions.get(name=site["region"])
        tenant = nb.tenancy.tenants.get(name=site["tenant"])
        data = {"name": site["name"], "slug": site["slug"],
                "region": region.id if region else None,
                "tenant": tenant.id if tenant else None,
                "status": site["status"],
                "description": site.get("description", "")}
        if site.get("custom_fields"):
            data["custom_fields"] = site["custom_fields"]
        site_obj, _ = get_or_create(nb.dcim.sites, ["slug"], data, site["name"])
        # Update custom_fields if site already existed and values differ.
        # get_or_create skips updates, so handle the drift case explicitly.
        if site_obj and site.get("custom_fields") and not CHECK_MODE:
            current = site_obj.custom_fields or {}
            desired = site["custom_fields"]
            if any(current.get(k) != v for k, v in desired.items()):
                site_obj.custom_fields = {**current, **desired}
                site_obj.save()
                print(f"    UPDATED custom_fields: {site['name']}")

    # Step 5 - Manufacturers, Platforms, Device Roles
    print("\n=== Step 5: Manufacturers, Platforms, Device Roles ===")
    for mfr in config["manufacturers"]:
        get_or_create(nb.dcim.manufacturers, ["slug"], mfr, mfr["name"])

    for plat in config["platforms"]:
        mfr = nb.dcim.manufacturers.get(name=plat["manufacturer"])
        data = {
            "name": plat["name"], "slug": plat["slug"],
            "manufacturer": mfr.id if mfr else None,
            "description": plat.get("description", ""),
        }
        if plat.get("napalm_driver"):
            data["napalm_driver"] = plat["napalm_driver"]
        get_or_create(nb.dcim.platforms, ["slug"], data, plat["name"])

    for role in config["device_roles"]:
        get_or_create(nb.dcim.device_roles, ["slug"], role, role["name"])

    # Step 6 - Device Types
    print("\n=== Step 6: Device Types ===")
    for dt in config["device_types"]:
        mfr = nb.dcim.manufacturers.get(name=dt["manufacturer"])
        data = {
            "manufacturer": mfr.id,
            "model": dt["name"], "slug": dt["slug"],
            "u_height": dt["u_height"],
            "is_full_depth": dt.get("is_full_depth", False),
            "description": dt.get("description", ""),
        }
        dtype, created = get_or_create(nb.dcim.device_types, ["slug"], data, dt["name"])

        if created and dt.get("interface_templates"):
            for it in dt["interface_templates"]:
                tmpl_data = {
                    "device_type": dtype.id,
                    "name": it["name"],
                    "type": it["type"],
                    "mgmt_only": it.get("mgmt_only", False),
                }
                if it.get("description"):
                    tmpl_data["description"] = it["description"]
                try:
                    nb.dcim.interface_templates.create(tmpl_data)
                    print(f"    TEMPLATE: {it['name']}")
                except pynetbox.RequestError as e:
                    print(f"    TEMPLATE ERROR {it['name']}: {e}")

    # Step 7 - RIR, ASN Ranges, ASNs
    print("\n=== Step 7: RIR, ASNs ===")
    for rir in config["rirs"]:
        get_or_create(nb.ipam.rirs, ["slug"], rir, rir["name"])

    for ar in config["asn_ranges"]:
        rir = nb.ipam.rirs.get(slug=ar["rir"].lower())
        slug = ar["name"].lower().replace(" ", "-")
        get_or_create(
            nb.ipam.asn_ranges, ["name"],
            {"name": ar["name"], "slug": slug, "rir": rir.id,
             "start": ar["start"], "end": ar["end"]},
            ar["name"],
        )

    for asn_entry in config["asns"]:
        rir = nb.ipam.rirs.get(slug="private")
        get_or_create(
            nb.ipam.asns, ["asn"],
            {"asn": asn_entry["asn"], "rir": rir.id,
             "description": asn_entry.get("description", "")},
            f"AS{asn_entry['asn']}",
        )

    # Step 8 - Route Targets, Roles, VRFs
    print("\n=== Step 8: Route Targets, Roles, VRFs ===")

    for rt in config["route_targets"]:
        get_or_create(nb.ipam.route_targets, ["name"], rt, rt["name"])

    for role in config["roles"]:
        get_or_create(nb.ipam.roles, ["slug"], role, role["name"])

    for vrf in config["vrfs"]:
        import_rts = [nb.ipam.route_targets.get(name=rt) for rt in vrf.get("import_targets", [])]
        export_rts = [nb.ipam.route_targets.get(name=rt) for rt in vrf.get("export_targets", [])]
        tenant = nb.tenancy.tenants.get(name=vrf["tenant"]) if vrf.get("tenant") else None
        data = {
            "name": vrf["name"],
            "description": vrf.get("description", ""),
            "tenant": tenant.id if tenant else None,
            "import_targets": [rt.id for rt in import_rts if rt],
            "export_targets": [rt.id for rt in export_rts if rt],
            "custom_fields": vrf.get("custom_fields", {}),
        }
        get_or_create(nb.ipam.vrfs, ["name"], data, vrf["name"])

    # Step 8b - L2VPN (EVPN MAC-VRF) instances
    # Models the L2 side of each tenant. NetBox L2VPN object holds the
    # identifier (used as the L2VNI base / cross-reference number), the
    # type (evpn), and the import/export RTs. Phase 3 templates render
    # this into a `routing-instances <name> instance-type mac-vrf` block.
    for l2vpn in config.get("l2vpns", []):
        import_rts = [nb.ipam.route_targets.get(name=rt) for rt in l2vpn.get("import_targets", [])]
        export_rts = [nb.ipam.route_targets.get(name=rt) for rt in l2vpn.get("export_targets", [])]
        tenant = nb.tenancy.tenants.get(name=l2vpn["tenant"]) if l2vpn.get("tenant") else None
        data = {
            "name": l2vpn["name"],
            "slug": l2vpn["slug"],
            "type": l2vpn.get("type", "vxlan-evpn"),
            "identifier": l2vpn.get("identifier"),
            "description": l2vpn.get("description", ""),
            "tenant": tenant.id if tenant else None,
            "import_targets": [rt.id for rt in import_rts if rt],
            "export_targets": [rt.id for rt in export_rts if rt],
        }
        get_or_create(nb.vpn.l2vpns, ["name"], data, l2vpn["name"])

    # Step 9 - Aggregates, Prefixes
    print("\n=== Step 9: Aggregates, Prefixes ===")
    for agg in config["aggregates"]:
        rir = nb.ipam.rirs.get(slug=agg["rir"].lower())
        get_or_create(
            nb.ipam.aggregates, ["prefix"],
            {"prefix": agg["prefix"], "rir": rir.id,
             "description": agg.get("description", "")},
            agg["prefix"],
        )

    for pfx in config["prefixes"]:
        data = {"prefix": pfx["prefix"], "description": pfx.get("description", "")}
        if pfx.get("site"):
            site = nb.dcim.sites.get(name=pfx["site"])
            if site:
                # NetBox 4.5 uses scope_type/scope_id instead of site
                data["scope_type"] = "dcim.site"
                data["scope_id"] = site.id
        if pfx.get("role"):
            role = nb.ipam.roles.get(name=pfx["role"])
            if role:
                data["role"] = role.id
        if pfx.get("vrf"):
            vrf = nb.ipam.vrfs.get(name=pfx["vrf"])
            if vrf:
                data["vrf"] = vrf.id
        if pfx.get("tenant"):
            tenant = nb.tenancy.tenants.get(name=pfx["tenant"])
            if tenant:
                data["tenant"] = tenant.id

        # Prefix lookup includes VRF and site scope to avoid false matches
        # when the same CIDR exists in different VRFs or sites.
        lookup = {"prefix": pfx["prefix"]}
        if data.get("vrf"):
            lookup["vrf_id"] = data["vrf"]
        if data.get("scope_id"):
            lookup["scope_id"] = data["scope_id"]

        existing = list(nb.ipam.prefixes.filter(**lookup))
        if existing:
            print(f"  EXISTS: {pfx['prefix']}")
        else:
            if CHECK_MODE:
                print(f"  MISSING: {pfx['prefix']}")
                missing_count += 1
            else:
                nb.ipam.prefixes.create(data)
                print(f"  CREATED: {pfx['prefix']}")

    # Step 10 - VLAN Groups, VLANs
    print("\n=== Step 10: VLAN Groups, VLANs ===")
    for vg in config["vlan_groups"]:
        scope_data = {}
        if vg.get("scope_type") == "dcim.site":
            site = nb.dcim.sites.get(name=vg["scope"])
            if site:
                scope_data["scope_type"] = "dcim.site"
                scope_data["scope_id"] = site.id
        get_or_create(
            nb.ipam.vlan_groups, ["name"],
            {"name": vg["name"], "description": vg.get("description", ""),
             **scope_data},
            vg["name"],
        )

    for vlan in config["vlans"]:
        group = nb.ipam.vlan_groups.get(name=vlan["group"])
        role = nb.ipam.roles.get(name=vlan["role"]) if vlan.get("role") else None
        data = {
            "vid": vlan["vid"], "name": vlan["name"],
            "group": group.id if group else None,
            "status": vlan.get("status", "active"),
            "role": role.id if role else None,
            "description": vlan.get("description", ""),
            "custom_fields": vlan.get("custom_fields", {}),
        }
        # VLANs require composite lookup (vid + group) because the same VID
        # can exist in different VLAN groups. get_or_create() uses simple
        # field equality which doesn't support foreign key filters like group_id.
        existing = list(nb.ipam.vlans.filter(vid=vlan["vid"], group_id=group.id if group else None))
        if existing:
            print(f"  EXISTS: VLAN {vlan['vid']}")
        else:
            if CHECK_MODE:
                print(f"  MISSING: VLAN {vlan['vid']}")
                missing_count += 1
            else:
                try:
                    nb.ipam.vlans.create(data)
                    print(f"  CREATED: VLAN {vlan['vid']}")
                except pynetbox.RequestError as e:
                    print(f"  ERROR VLAN {vlan['vid']}: {e}")

    # Step 11 - Devices
    print("\n=== Step 11: Devices ===")
    for dev in config["devices"]:
        dtype = nb.dcim.device_types.get(model=dev["device_type"])
        role = nb.dcim.device_roles.get(name=dev["role"])
        site = nb.dcim.sites.get(name=dev["site"])
        platform = nb.dcim.platforms.get(name=dev["platform"])
        tags = [nb.extras.tags.get(slug=t) for t in dev.get("tags", [])]

        # Store ASN in local_context_data if assigned
        # (NetBox ASN objects are site-level only, not device-level)
        local_ctx = {}
        if dev.get("asn"):
            local_ctx["bgp_asn"] = dev["asn"]

        data = {
            "name": dev["name"],
            "device_type": dtype.id,
            "role": role.id,
            "site": site.id,
            "platform": platform.id if platform else None,
            "status": dev.get("status", "active"),
            "tags": [t.id for t in tags if t],
            "local_context_data": local_ctx if local_ctx else None,
        }
        device, created = get_or_create(nb.dcim.devices, ["name"], data, dev["name"])
        if not device:
            continue

        # Assign OOB management IP to fxp0 (NOT primary_ip4 - that's loopback)
        if dev.get("oob_ip") and dev["oob_ip"] != "-":
            oob_ip = dev["oob_ip"]
            fxp0 = nb.dcim.interfaces.get(device_id=device.id, name="fxp0")
            if fxp0:
                existing_ip = list(nb.ipam.ip_addresses.filter(address=oob_ip))
                if not existing_ip:
                    ip_obj = nb.ipam.ip_addresses.create({
                        "address": oob_ip,
                        "assigned_object_type": "dcim.interface",
                        "assigned_object_id": fxp0.id,
                        "description": f"{dev['name']} OOB management",
                    })
                    print(f"    OOB IP: {oob_ip} -> fxp0")
                else:
                    ip_obj = existing_ip[0]
                    print(f"    OOB IP EXISTS: {oob_ip}")

                # Set as OOB IP, not primary
                device.oob_ip = ip_obj.id
                device.save()

    # Step 12 - Loopback and P2P IPs
    print("\n=== Step 12: IP Addresses ===")

    for lo in config["loopback_ips"]:
        device = nb.dcim.devices.get(name=lo["device"])
        if not device:
            print(f"  MISSING DEVICE: {lo['device']}")
            missing_count += 1
            continue

        iface = nb.dcim.interfaces.get(device_id=device.id, name=lo["interface"])
        if not iface:
            if CHECK_MODE:
                print(f"  MISSING INTERFACE: {lo['device']}:{lo['interface']}")
                missing_count += 1
                continue
            if lo.get("create_interface"):
                iface = nb.dcim.interfaces.create({
                    "device": device.id,
                    "name": lo["interface"],
                    "type": "virtual",
                    "description": lo.get("description", ""),
                })
                print(f"  CREATED INTERFACE: {lo['device']}:{lo['interface']}")
            else:
                print(f"  ERROR: Interface {lo['interface']} not found on {lo['device']}")
                continue

        existing = list(nb.ipam.ip_addresses.filter(address=lo["ip"]))
        if not existing:
            if CHECK_MODE:
                print(f"  MISSING: {lo['ip']} -> {lo['device']}:{lo['interface']}")
                missing_count += 1
                continue
            ip_obj = nb.ipam.ip_addresses.create({
                "address": lo["ip"],
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": iface.id,
                "description": lo.get("description", ""),
            })
            print(f"  CREATED: {lo['ip']} -> {lo['device']}:{lo['interface']}")
        else:
            ip_obj = existing[0]
            print(f"  EXISTS: {lo['ip']}")

        # Set loopback as primary_ip4 if flagged
        if lo.get("primary"):
            device.primary_ip4 = ip_obj.id
            device.save()
            print(f"    PRIMARY IP: {lo['ip']}")

    for link in config["p2p_links"]:
        for side in ("a", "z"):
            dev_name = link[f"{side}_device"]
            intf_name = link[f"{side}_interface"]
            ip = link[f"{side}_ip"]

            device = nb.dcim.devices.get(name=dev_name)
            if not device:
                print(f"  MISSING DEVICE: {dev_name}")
                missing_count += 1
                continue
            iface = nb.dcim.interfaces.get(device_id=device.id, name=intf_name)
            if not iface:
                print(f"  MISSING INTERFACE: {dev_name}:{intf_name}")
                missing_count += 1
                continue

            existing = list(nb.ipam.ip_addresses.filter(address=ip))
            if not existing:
                if CHECK_MODE:
                    print(f"  MISSING: {ip} -> {dev_name}:{intf_name}")
                    missing_count += 1
                    continue
                nb.ipam.ip_addresses.create({
                    "address": ip,
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": iface.id,
                    "description": f"P2P {link['a_device']} - {link['z_device']}",
                })
                print(f"  CREATED: {ip} -> {dev_name}:{intf_name}")
            else:
                print(f"  EXISTS: {ip}")

    # Step 13 - Cables
    print("\n=== Step 13: Cables ===")
    for cable in config["cables"]:
        a_dev = nb.dcim.devices.get(name=cable["a_device"])
        z_dev = nb.dcim.devices.get(name=cable["z_device"])
        if not a_dev or not z_dev:
            print(f"  MISSING DEVICE for cable {cable['label']}")
            missing_count += 1
            continue

        a_intf = nb.dcim.interfaces.get(device_id=a_dev.id, name=cable["a_interface"])
        z_intf = nb.dcim.interfaces.get(device_id=z_dev.id, name=cable["z_interface"])

        if not a_intf or not z_intf:
            print(f"  MISSING INTERFACE for cable {cable['label']}")
            missing_count += 1
            continue

        if a_intf.cable:
            print(f"  EXISTS: {cable['label']}")
            continue

        if CHECK_MODE:
            print(f"  MISSING: cable {cable['label']}")
            missing_count += 1
            continue

        try:
            nb.dcim.cables.create({
                "a_terminations": [{"object_type": "dcim.interface", "object_id": a_intf.id}],
                "b_terminations": [{"object_type": "dcim.interface", "object_id": z_intf.id}],
                "label": cable.get("label", ""),
            })
            print(f"  CREATED: {cable['label']}")
        except pynetbox.RequestError as e:
            print(f"  ERROR {cable['label']}: {e}")

    # Step 14 - LAGs, IRBs, anycast gateways, L2VPN terminations
    print("\n=== Step 14: LAGs, Access VLANs, IRBs, Anycast GWs, L2VPN terminations ===")

    # 14a - LAG parent interfaces (ae0/ae1) and bind physical members
    for lag in config.get("lag_interfaces", []):
        device = nb.dcim.devices.get(name=lag["device"])
        if not device:
            print(f"  MISSING DEVICE: {lag['device']}")
            missing_count += 1
            continue
        lag_iface = nb.dcim.interfaces.get(device_id=device.id, name=lag["name"])
        if lag_iface:
            print(f"  EXISTS: LAG {lag['device']}:{lag['name']}")
        else:
            if CHECK_MODE:
                print(f"  MISSING: LAG {lag['device']}:{lag['name']}")
                missing_count += 1
                continue
            lag_iface = nb.dcim.interfaces.create({
                "device": device.id,
                "name": lag["name"],
                "type": "lag",
                "description": lag.get("description", ""),
            })
            print(f"  CREATED: LAG {lag['device']}:{lag['name']}")

        # untagged_vlan on the LAG (NetBox VLAN must already exist)
        if lag.get("untagged_vlan"):
            vlan_match = list(nb.ipam.vlans.filter(vid=lag["untagged_vlan"]))
            if vlan_match:
                vlan_obj = vlan_match[0]
                current_vlan_id = lag_iface.untagged_vlan.id if lag_iface.untagged_vlan else None
                if current_vlan_id != vlan_obj.id and not CHECK_MODE:
                    lag_iface.mode = "access"
                    lag_iface.untagged_vlan = vlan_obj.id
                    lag_iface.save()
                    print(f"    VLAN {lag['untagged_vlan']} -> {lag['name']}")

        # Bind member physical interfaces to the LAG
        for member_name in lag.get("members", []):
            member = nb.dcim.interfaces.get(device_id=device.id, name=member_name)
            if not member:
                print(f"    MISSING MEMBER: {member_name}")
                missing_count += 1
                continue
            current_lag_id = member.lag.id if member.lag else None
            if current_lag_id == lag_iface.id:
                print(f"    MEMBER OK: {member_name} -> {lag['name']}")
                continue
            if not CHECK_MODE:
                member.lag = lag_iface.id
                member.save()
                print(f"    MEMBER BOUND: {member_name} -> {lag['name']}")

    # 14b - Access interfaces: untagged_vlan on single-homed host ports
    for ac in config.get("access_interfaces", []):
        device = nb.dcim.devices.get(name=ac["device"])
        if not device:
            continue
        iface = nb.dcim.interfaces.get(device_id=device.id, name=ac["interface"])
        if not iface:
            print(f"  MISSING INTERFACE: {ac['device']}:{ac['interface']}")
            missing_count += 1
            continue
        vlan_match = list(nb.ipam.vlans.filter(vid=ac["untagged_vlan"]))
        if not vlan_match:
            print(f"  MISSING VLAN: {ac['untagged_vlan']}")
            missing_count += 1
            continue
        vlan_obj = vlan_match[0]
        current_vlan_id = iface.untagged_vlan.id if iface.untagged_vlan else None
        if current_vlan_id == vlan_obj.id:
            print(f"  EXISTS: {ac['device']}:{ac['interface']} VLAN {ac['untagged_vlan']}")
        else:
            if CHECK_MODE:
                print(f"  MISSING: {ac['device']}:{ac['interface']} VLAN {ac['untagged_vlan']}")
                missing_count += 1
                continue
            iface.mode = "access"
            iface.untagged_vlan = vlan_obj.id
            iface.save()
            print(f"  SET: {ac['device']}:{ac['interface']} VLAN {ac['untagged_vlan']}")

    # 14c - IRB virtual interfaces with leaf-local /24 IPs in tenant VRF
    for irb in config.get("irb_interfaces", []):
        device = nb.dcim.devices.get(name=irb["device"])
        if not device:
            continue
        iface = nb.dcim.interfaces.get(device_id=device.id, name=irb["name"])
        if iface:
            print(f"  EXISTS: IRB {irb['device']}:{irb['name']}")
        else:
            if CHECK_MODE:
                print(f"  MISSING: IRB {irb['device']}:{irb['name']}")
                missing_count += 1
                continue
            iface = nb.dcim.interfaces.create({
                "device": device.id,
                "name": irb["name"],
                "type": "virtual",
                "description": irb.get("description", ""),
            })
            print(f"  CREATED: IRB {irb['device']}:{irb['name']}")

        vrf_obj = nb.ipam.vrfs.get(name=irb["vrf"]) if irb.get("vrf") else None
        existing_ip = list(nb.ipam.ip_addresses.filter(
            address=irb["ip"],
            interface_id=iface.id,
        ))
        if existing_ip:
            print(f"    IP EXISTS: {irb['ip']}")
        else:
            if CHECK_MODE:
                print(f"    MISSING IP: {irb['ip']}")
                missing_count += 1
                continue
            nb.ipam.ip_addresses.create({
                "address": irb["ip"],
                "vrf": vrf_obj.id if vrf_obj else None,
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": iface.id,
                "description": irb.get("description", ""),
            })
            print(f"    IP CREATED: {irb['ip']}")

    # 14d - Anycast gateway IPs (one IPAddress row per leaf, role=anycast).
    # Same address on both leaves = the shared virtual-gateway-address.
    # Templates query NetBox for the prefix's role=anycast IP and render it
    # as `virtual-gateway-address` on the IRB. Reserves .1 in IPAM so nobody
    # else can grab it for a host.
    for gw in config.get("anycast_gateways", []):
        vrf_obj = nb.ipam.vrfs.get(name=gw["vrf"]) if gw.get("vrf") else None
        for leaf_name in gw["leaves"]:
            device = nb.dcim.devices.get(name=leaf_name)
            if not device:
                continue
            iface = nb.dcim.interfaces.get(device_id=device.id, name=gw["interface"])
            if not iface:
                print(f"  MISSING IRB: {leaf_name}:{gw['interface']}")
                missing_count += 1
                continue
            existing = list(nb.ipam.ip_addresses.filter(
                address=gw["address"],
                interface_id=iface.id,
            ))
            if existing:
                print(f"  EXISTS: anycast {gw['address']} on {leaf_name}:{gw['interface']}")
                continue
            if CHECK_MODE:
                print(f"  MISSING: anycast {gw['address']} on {leaf_name}:{gw['interface']}")
                missing_count += 1
                continue
            nb.ipam.ip_addresses.create({
                "address": gw["address"],
                "vrf": vrf_obj.id if vrf_obj else None,
                "role": "anycast",
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": iface.id,
                "description": f"Anycast gateway VLAN{gw['vlan']}",
            })
            print(f"  CREATED: anycast {gw['address']} on {leaf_name}:{gw['interface']}")

    # 14e - L2VPN VLAN terminations: bind tenant VLANs into the EVPN MAC-VRF
    for term in config.get("l2vpn_terminations", []):
        l2vpn = nb.vpn.l2vpns.get(name=term["l2vpn"])
        if not l2vpn:
            print(f"  MISSING L2VPN: {term['l2vpn']}")
            missing_count += 1
            continue
        group = nb.ipam.vlan_groups.get(name=term["vlan_group"])
        vlan_match = list(nb.ipam.vlans.filter(
            vid=term["vlan"],
            group_id=group.id if group else None,
        ))
        if not vlan_match:
            print(f"  MISSING VLAN: {term['vlan']}")
            missing_count += 1
            continue
        vlan = vlan_match[0]
        existing = list(nb.vpn.l2vpn_terminations.filter(
            l2vpn_id=l2vpn.id,
            assigned_object_type="ipam.vlan",
            assigned_object_id=vlan.id,
        ))
        if existing:
            print(f"  EXISTS: termination VLAN {term['vlan']} -> {term['l2vpn']}")
            continue
        if CHECK_MODE:
            print(f"  MISSING: termination VLAN {term['vlan']} -> {term['l2vpn']}")
            missing_count += 1
            continue
        nb.vpn.l2vpn_terminations.create({
            "l2vpn": l2vpn.id,
            "assigned_object_type": "ipam.vlan",
            "assigned_object_id": vlan.id,
        })
        print(f"  CREATED: termination VLAN {term['vlan']} -> {term['l2vpn']}")

    if CHECK_MODE:
        print(f"\n=== CHECK COMPLETE: {missing_count} missing object(s) ===")
        sys.exit(1 if missing_count > 0 else 0)

    print("\n=== Population complete ===")

    print(f"\nDevices:    {len(list(nb.dcim.devices.all()))}")
    print(f"Interfaces: {len(list(nb.dcim.interfaces.all()))}")
    print(f"IPs:        {len(list(nb.ipam.ip_addresses.all()))}")
    print(f"Cables:     {len(list(nb.dcim.cables.all()))}")
    print(f"Prefixes:   {len(list(nb.ipam.prefixes.all()))}")
    print(f"VLANs:      {len(list(nb.ipam.vlans.all()))}")


if __name__ == "__main__":
    main()
