#!/usr/bin/env python3
"""Generate a SuzieQ native inventory from NetBox.

Why not use SuzieQ's NetBox source plugin directly: that plugin reads
`primary_ip4.address` from NetBox, which in this project is the device
loopback (10.1.0.x) - unreachable from netdevops-srv. Phase 3 Nornir
hits the same problem and solves it via a transform_function that
overrides the hostname at runtime from the `oob_ip` field. SuzieQ has
no equivalent hook, so we generate a static native inventory at deploy
time using `oob_ip` as the connect address.

What this script pulls FROM NetBox (not hardcoded):
    - Per-device OOB IP        device.oob_ip.address
    - Per-device model         device.device_type.model
    - Per-site namespace name  device.site.slug   (lowercased)
    - Filter selection         tag = "suzieq"

What this script HARDCODES (and why):
    - transport: ssh           Junos lab is SSH-only; Phase 10 with
                               cEOS will need a per-platform map
    - devtype mapping          See DEVTYPE_OVERRIDES below: vJunos
                               maps to the project-owned
                               `junos-vjunos-switch` devtype added at
                               image build time by add-vjunos-switch.py

DEVTYPE MAPPING (the "what devtype does vJunos use" question):

    The lab maps NetBox device_type.model='EX9214' to the
    project-owned SuzieQ devtype `junos-vjunos-switch`, which is
    added to the SuzieQ service catalog at IMAGE BUILD TIME by
    suzieq-image/add-vjunos-switch.py. See the "junos-vjunos-switch
    devtype" section in phase5-suzieq/README.md for the full story.

    Why a project-owned devtype and not a built-in: vJunos-switch
    needs an unusual mix of upstream service templates:

      - `device`  service: junos-mx shape (single-RE uptime JSON,
                           no multi-routing-engine-results wrapper)
      - `lldp`    service: junos-qfx shape (detail view, has
                           lldp-remote-port-id)
      - everything else:   junos-qfx shape (REAL in 12/12 Junos
                           service yamls upstream)

    No built-in SuzieQ devtype matches this combination. The
    patcher's SERVICE_BASE_OVERRIDES = {"device.yml": "junos-mx"}
    expresses the one override needed; everything else inherits
    from junos-qfx via `copy:`. DEVTYPE_OVERRIDES at the top of
    this file is the source of truth for the NetBox-model to
    SuzieQ-devtype mapping.

Trade-off vs SuzieQ's native NetBox source: device adds/removes in
NetBox don't propagate live - re-run this script and `docker compose
restart sq-poller` to pick them up. Acceptable for a lab; if Phase
10 makes this painful, the right fix is upstream (SuzieQ PR adding
an `address-source: oob_ip|primary_ip4` knob to the NetBox source).

Usage:
    source ../../evpn-lab-env/env.sh
    python3 gen-inventory.py > inventory.yml
"""

import os
import sys
import urllib.request
import json
from collections import defaultdict
from io import StringIO

TAG = "suzieq"

# NetBox device-type model -> SuzieQ devtype.
#
# Keys are matched as lowercased substrings against the NetBox
# `device_type.model` field. First match wins. Add entries here when
# new device types appear in netbox-data.yml.
#
# Devtype naming:
#   - junos-vjunos-switch: a project-owned devtype added by the build-time
#     patcher in suzieq-image/add-junos-vjunos-switch.py. Combines
#     junos-mx's `device` service template (single-routing-engine
#     JSON shape that vJunos-switch produces) with junos-qfx's
#     `lldp` service template (detail view that includes the peer
#     port id). Required because no built-in SuzieQ devtype matches
#     this combination of upstream services. The patched image is
#     built from suzieq-image/Dockerfile and used by all three
#     suzieq services in docker-compose.yml.
#   - junos-mx: real Juniper MX devices. Built-in upstream devtype,
#     unmodified. Phase 10+ would route real MX hardware here.
#   - eos: Arista cEOS, Phase 10 (commented placeholder below).
DEVTYPE_OVERRIDES = [
    # (model substring, suzieq devtype, reason)
    ("ex9214", "junos-vjunos-switch", "vJunos-switch lab device, project-owned devtype"),
    ("qfx",    "junos-vjunos-switch", "vQFX containers also need the junos-vjunos-switch devtype"),
    ("mx",     "junos-mx",      "real Juniper MX, native upstream devtype"),
    ("srx",    "junos-mx",      "Junos SRX, single-RE shape"),
    # Phase 10 cEOS will need its own entry once added:
    # ("ceos", "eos",            "Arista cEOS lab container"),
]


def fetch_devices(netbox_url, netbox_token):
    """Pull all devices with the suzieq tag from NetBox.

    Thin wrapper over urllib so unit tests can call generate()
    without touching the network.
    """
    url = f"{netbox_url.rstrip('/')}/api/dcim/devices/?tag={TAG}&limit=200"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Token {netbox_token}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["results"]


def map_devtype(model):
    """Map a NetBox device-type model string to a SuzieQ devtype."""
    if not model:
        return None
    m = model.lower()
    for substr, devtype, _reason in DEVTYPE_OVERRIDES:
        if substr in m:
            return devtype
    return None


def _strict_host_keys_enabled():
    """Read SUZIEQ_STRICT_HOST_KEYS from the environment.

    Returns True when the env var is set to any truthy value
    ('1', 'true', 'yes', 'on', case-insensitive). Default False so
    lab deploys keep the ignore-known-hosts convenience that lets
    `containerlab destroy/deploy` cycles work without wiping a
    host-side known_hosts file.

    Production deployments MUST set SUZIEQ_STRICT_HOST_KEYS=1 and
    provision known_hosts via configuration management. The env var
    is the single switch that flips the behavior; there is no code
    path where strict host keys get disabled by accident.
    """
    raw = os.environ.get("SUZIEQ_STRICT_HOST_KEYS", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def generate(devices, netbox_url="", warn_stream=None, strict_host_keys=None):
    """Pure function: NetBox devices list -> SuzieQ inventory YAML.

    Split out from main() so tests can call it with hand-built device
    dicts and inspect the result without mocking urllib or NetBox.

    Args:
        devices:    list of NetBox device dicts (the `results` field
                    of /api/dcim/devices/?tag=suzieq).
        netbox_url: URL string for the YAML header comment only;
                    does not affect any logic.
        warn_stream: where to write WARNING lines for skipped
                    devices. Defaults to sys.stderr in the CLI path,
                    pytest passes capsys-friendly streams.
        strict_host_keys: force the ignore-known-hosts flag in the
                    generated YAML. None (default) reads the value
                    from the SUZIEQ_STRICT_HOST_KEYS env var via
                    _strict_host_keys_enabled(). True/False override
                    the env var (used by tests).

    Returns:
        YAML string suitable for SuzieQ's native inventory format.

    Raises:
        ValueError: if no devices remain after filtering. Caller
                    decides how to surface this (CLI exits 1).
    """
    if warn_stream is None:
        warn_stream = sys.stderr
    if strict_host_keys is None:
        strict_host_keys = _strict_host_keys_enabled()

    if not devices:
        raise ValueError(
            f"no devices with tag '{TAG}' in NetBox. Run "
            "phase1-netbox/populate.py against the current "
            "netbox-data.yml (which now applies the tag) before "
            "regenerating this inventory."
        )

    # Group hosts by (namespace, devtype) so each combination becomes
    # one SuzieQ source/device pair. Lab today has one combination
    # (dc1 + junos-mx) but Phase 10 multi-DC / multi-vendor will
    # naturally fan out without script changes.
    grouped = defaultdict(list)
    skipped = []

    for d in devices:
        name = d["name"]
        oob = d.get("oob_ip")
        model = (d.get("device_type") or {}).get("model")
        site = ((d.get("site") or {}).get("slug") or "default").lower()

        if not oob or not oob.get("address"):
            skipped.append((name, "no oob_ip"))
            continue
        devtype = map_devtype(model)
        if not devtype:
            skipped.append(
                (name, f"no DEVTYPE_OVERRIDES match for model={model!r}")
            )
            continue

        addr = oob["address"].split("/", 1)[0]
        grouped[(site, devtype)].append(
            {"name": name, "address": addr, "model": model}
        )

    for name, why in skipped:
        print(f"WARNING: skipping {name}: {why}", file=warn_stream)

    if not grouped:
        raise ValueError("no usable devices after filtering")

    # Emit YAML by hand to keep the script dependency-free (urllib +
    # json only - same posture as Phase 1 populate.py's stdlib core).
    out = StringIO()
    out.write(f"# Generated by gen-inventory.py from NetBox tag '{TAG}'.\n")
    if netbox_url:
        out.write(f"# Source of truth: {netbox_url}\n")
    out.write("# Re-run after device adds/removes in NetBox; static at runtime.\n")
    out.write(f"# Groups (namespace, devtype): {sorted(grouped.keys())}\n\n")

    out.write("sources:\n")
    for (site, devtype), hosts in sorted(grouped.items()):
        source_name = f"{site}-{devtype}"
        out.write(f"  - name: {source_name}\n")
        out.write(f"    hosts:\n")
        for h in hosts:
            out.write(
                f"      - url: ssh://{h['address']}  # {h['name']} ({h['model']})\n"
            )
    out.write("\n")

    out.write("devices:\n")
    # Lab default: ignore-known-hosts: true (vJunos containers come
    # up with a fresh SSH host key on every containerlab
    # destroy/deploy cycle, so a known_hosts file would have to be
    # wiped on every cold boot).
    #
    # Production: set SUZIEQ_STRICT_HOST_KEYS=1 in the environment
    # (or pass strict_host_keys=True directly) and the inventory
    # will emit ignore-known-hosts: false. Operators are then
    # responsible for provisioning known_hosts via configuration
    # management. See _strict_host_keys_enabled() for the env var
    # parsing; the default stays permissive so lab deploys do not
    # silently start failing on cold boots after this env var is
    # added.
    ignore_value = "false" if strict_host_keys else "true"
    for (_, devtype), _hosts in sorted(grouped.items()):
        out.write(f"  - name: dev-{devtype}\n")
        out.write(f"    transport: ssh\n")
        out.write(f"    devtype: {devtype}\n")
        out.write(f"    ignore-known-hosts: {ignore_value}\n")
    out.write("\n")

    out.write("auths:\n")
    out.write("  - name: junos-creds\n")
    out.write("    username: env:JUNOS_SSH_USER\n")
    out.write("    password: env:JUNOS_SSH_PASSWORD\n\n")

    out.write("namespaces:\n")
    for (site, devtype), _hosts in sorted(grouped.items()):
        source_name = f"{site}-{devtype}"
        out.write(f"  - name: {site}\n")
        out.write(f"    source: {source_name}\n")
        out.write(f"    device: dev-{devtype}\n")
        out.write(f"    auth: junos-creds\n")

    return out.getvalue()


def main():
    netbox_url = os.environ["NETBOX_URL"]
    netbox_token = os.environ["NETBOX_TOKEN"]
    devices = fetch_devices(netbox_url, netbox_token)
    try:
        sys.stdout.write(generate(devices, netbox_url=netbox_url))
    except ValueError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
