#!/usr/bin/env python3
"""Populate NetBox with EVPN-VXLAN lab data (Phase 1, Steps 1-13).

Reads structured data from netbox-data.yml and creates all objects in
dependency order. Idempotent - safe to re-run.

Environment variables required:
    NETBOX_URL      - NetBox base URL (e.g. http://netbox:8000)
    NETBOX_TOKEN    - API token (v2 format: nbt_xxx.yyy)
    MGMT_SUBNET     - Management subnet CIDR (e.g. 172.16.18.0/24)
    MGMT_dc1_spine1 - Management IP/mask for dc1-spine1
    MGMT_dc1_spine2 - Management IP/mask for dc1-spine2
    MGMT_dc1_leaf1  - Management IP/mask for dc1-leaf1
    MGMT_dc1_leaf2  - Management IP/mask for dc1-leaf2
"""

import os
import sys
from pathlib import Path

import pynetbox
import yaml


def load_config():
    """Load netbox-data.yml and resolve $VARIABLE placeholders from env."""
    config_path = Path(__file__).parent / "netbox-data.yml"
    raw = config_path.read_text()

    # Resolve $VARIABLE placeholders (only in values, not comments)
    import re
    # Remove comment lines before checking
    lines = raw.split('\n')
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith('#'):
            continue
        for key, value in os.environ.items():
            lines[i] = lines[i].replace(f"${key}", value)
    raw = '\n'.join(lines)

    # Check for unresolved variables in non-comment lines
    unresolved = []
    for line in raw.split('\n'):
        if line.lstrip().startswith('#'):
            continue
        unresolved.extend(re.findall(r'\$([A-Z_][A-Za-z0-9_]*)', line))
    if unresolved:
        print(f"ERROR: Unresolved env variables: {', '.join(set(unresolved))}")
        print("Set them in your environment. See .env.example in repo root.")
        sys.exit(1)

    return yaml.safe_load(raw)


def slugify(name):
    """Generate a slug from a name."""
    import re
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
    data = ensure_slug(data)
    lookup = {k: data[k] for k in lookup_keys if k in data}
    existing = endpoint.filter(**lookup)
    results = list(existing)
    if results:
        print(f"  EXISTS: {label or lookup}")
        return results[0], False

    try:
        obj = endpoint.create(data)
        print(f"  CREATED: {label or lookup}")
        return obj, True
    except pynetbox.RequestError as e:
        print(f"  ERROR creating {label or lookup}: {e}")
        raise


def main():
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
        # Map type names to NetBox API values
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
                {"name": child["name"], "slug": child["name"].lower(), "parent": parent.id},
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
        get_or_create(
            nb.dcim.sites, ["slug"],
            {"name": site["name"], "slug": site["slug"],
             "region": region.id if region else None,
             "tenant": tenant.id if tenant else None,
             "status": site["status"],
             "description": site.get("description", "")},
            site["name"],
        )

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

        # Create interface templates
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
        data = {
            "name": vrf["name"],
            "description": vrf.get("description", ""),
            "import_targets": [rt.id for rt in import_rts if rt],
            "export_targets": [rt.id for rt in export_rts if rt],
            "custom_fields": vrf.get("custom_fields", {}),
        }
        get_or_create(nb.ipam.vrfs, ["name"], data, vrf["name"])

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
                data["site"] = site.id
        if pfx.get("role"):
            role = nb.ipam.roles.get(name=pfx["role"])
            if role:
                data["role"] = role.id
        if pfx.get("vrf"):
            vrf = nb.ipam.vrfs.get(name=pfx["vrf"])
            if vrf:
                data["vrf"] = vrf.id
        get_or_create(nb.ipam.prefixes, ["prefix"], data, pfx["prefix"])

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
        # Check if VLAN exists in this group
        existing = list(nb.ipam.vlans.filter(vid=vlan["vid"], group_id=group.id if group else None))
        if existing:
            print(f"  EXISTS: VLAN {vlan['vid']}")
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
        iface = nb.dcim.interfaces.get(device_id=device.id, name=lo["interface"])
        if not iface:
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
            iface = nb.dcim.interfaces.get(device_id=device.id, name=intf_name)
            if not iface:
                print(f"  ERROR: Interface {intf_name} not found on {dev_name}")
                continue

            existing = list(nb.ipam.ip_addresses.filter(address=ip))
            if not existing:
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
        a_intf = nb.dcim.interfaces.get(device_id=a_dev.id, name=cable["a_interface"])
        z_dev = nb.dcim.devices.get(name=cable["z_device"])
        z_intf = nb.dcim.interfaces.get(device_id=z_dev.id, name=cable["z_interface"])

        if not a_intf or not z_intf:
            print(f"  ERROR: Interface not found for cable {cable['label']}")
            continue

        # Check if cable already exists by checking if interface is connected
        if a_intf.cable:
            print(f"  EXISTS: {cable['label']}")
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

    print("\n=== Population complete ===")

    # Summary
    print(f"\nDevices:    {len(list(nb.dcim.devices.all()))}")
    print(f"Interfaces: {len(list(nb.dcim.interfaces.all()))}")
    print(f"IPs:        {len(list(nb.ipam.ip_addresses.all()))}")
    print(f"Cables:     {len(list(nb.dcim.cables.all()))}")
    print(f"Prefixes:   {len(list(nb.ipam.prefixes.all()))}")
    print(f"VLANs:      {len(list(nb.ipam.vlans.all()))}")


if __name__ == "__main__":
    main()
