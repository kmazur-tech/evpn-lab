#!/usr/bin/env python3
"""Build-time patcher for the suzieq image.

Runs ONCE inside the Dockerfile via `RUN python3 ...`. Reads each
SuzieQ service yaml in the upstream config directory and adds a
`junos-vjunos-switch:` devtype block, then writes back. The result is a
new image layer with `junos-vjunos-switch` as a first-class SuzieQ
devtype, alongside the upstream `junos-qfx`, `junos-ex`, `junos-mx`,
etc. - all of which are left structurally unchanged.

Why a new devtype name instead of mutating `junos-mx`:
  vJunos-switch (the vrnetlab image the lab uses to emulate EX9214)
  needs an unusual combination of upstream service templates:
    - device   service: junos-mx shape (single-routing-engine JSON)
    - lldp     service: junos-qfx shape (detail view, has port id)
    - all else: junos-mx
  No built-in SuzieQ devtype matches that combination. Mutating
  `junos-mx` directly would pollute its meaning for any hypothetical
  future real Juniper MX in the project. The honest fix is to add a
  new devtype name that the lab owns: junos-vjunos-switch.

Why build-time and not runtime:
  Runtime patching means a script runs at every container start;
  one mistake silently breaks the poller and the operator has to
  trace why. Build-time means the patch is part of the immutable
  image layer - if the build fails, the image fails (loud), and
  if the build succeeds, every container started from it is
  guaranteed to have the patches in place. There is no script
  running at runtime; the container's entrypoint is unchanged.

Why a Python script and not COPY of pre-patched yamls:
  - 13 service yamls = 13 files to keep in sync against upstream
  - The script mutates upstream content programmatically, so
    upstream-edited yamls flow through automatically on the next
    image bump
  - If upstream restructures the yaml shape in a way the script
    cannot handle, the build FAILS LOUDLY, which is exactly what
    we want - silently masking upstream changes is the bug class
    we are trying to avoid.

Service-by-service mapping:
  Most services: junos-vjunos-switch is identical to junos-mx, so the
                 patcher adds `junos-vjunos-switch: {copy: junos-mx}`.
                 SuzieQ's internal `copy:` resolution does the
                 rest at service-load time.
  lldp.yml:      Special-cased. The patcher adds an EXPLICIT
                 junos-vjunos-switch block that uses the detail-view
                 command (the same shape junos-qfx uses upstream)
                 because junos-mx's lldp template uses the summary
                 view which omits lldp-remote-port-id.

Test contract (see tests/test_suzieq_image_patches.py):
  - Every service yaml in the config dir has a `junos-vjunos-switch`
    entry after the patcher runs
  - lldp.yml's junos-vjunos-switch entry uses the `detail` view
  - All other services' junos-vjunos-switch entries are `copy: junos-mx`
  - junos-mx, junos-qfx, junos-ex, junos-evo, junos-es,
    junos-qfx10k blocks are byte-identical to upstream (we
    must not pollute the built-in devtype namespace)
"""
import sys
from pathlib import Path

# Service yamls that get a simple `copy: <base>` shim. lldp is
# excluded because it gets a special-cased explicit block below.
# mlag/time/topcpu have no junos-* block in upstream and are
# excluded at the POLLER level via sq-poller's --exclude-services
# flag in docker-compose.yml -- not here. Adding a yaml stub for
# them would still cost an SSH per poll cycle to run a no-op
# command; the poller flag avoids the SSH entirely.
SIMPLE_COPY_SERVICES = [
    "arpnd.yml",
    "bgp.yml",
    "devconfig.yml",
    "device.yml",
    "evpnVni.yml",
    "fs.yml",
    "ifCounters.yml",
    "interfaces.yml",
    "inventory.yml",
    "macs.yml",
    "ospfIf.yml",
    "ospfNbr.yml",
    "routes.yml",
]

# Default base devtype: junos-qfx.
#
# vJunos-switch IS architecturally a switch (the vrnetlab image
# emulates EX9214, a switch). junos-qfx is the canonical Junos
# switch base in upstream SuzieQ - in fact, junos-qfx is the ONLY
# Junos devtype that has a real definition in EVERY service yaml
# (verified empirically). All the other Junos variants (junos-ex,
# junos-es, junos-qfx10k, junos-evo, AND junos-mx in 7 of 12
# services) inherit from junos-qfx via `copy:` chains.
#
# Why this is the right default (and not junos-mx):
#   - junos-qfx is REAL in 12/12 service yamls. junos-mx is
#     `copy: junos-qfx` in 7/12, ABSENT in 1/12, and has its own
#     real definition in only 4/12 (arpnd, device, macs, routes).
#   - For 3 of those 4 (arpnd, macs, routes), junos-mx is
#     specifically the WRONG base for vJunos:
#       * arpnd: identical command, no advantage
#       * macs:  junos-mx uses `show bridge mac-table` (MX-only)
#                while junos-qfx uses `show ethernet-switching
#                table detail` (which vJunos-switch has)
#       * routes: junos-mx uses `show route protocol direct`
#                (the documented MX scale workaround - returns
#                ONLY direct/connected routes, no BGP, no EVPN).
#                junos-qfx uses `show route` + `show evpn
#                ip-prefix-database` (full RIB + EVPN learned).
#                Verified live: switching routes to junos-qfx took
#                the table from 26 connected-only rows to 86 rows
#                including bgp, evpn, vpn, static, local protocols.
#   - The single service where junos-mx is correct for vJunos is
#     device.yml - junos-mx uses the single-RE uptime parser that
#     vJunos's JSON shape requires. That's the lone override below.
DEFAULT_BASE = "junos-qfx"

# Per-service base override. ONLY used when junos-qfx is the wrong
# base. Currently exactly one entry: device.yml needs the junos-mx
# single-RE uptime parser because vJunos returns the single-RE
# `system-uptime-information/*` JSON shape (no
# `multi-routing-engine-results` wrapper). The corresponding Python
# source patch in patch_node_multi_re_list() adds vJunos to the
# allowlist for the same parser path - both must agree.
SERVICE_BASE_OVERRIDES = {
    "device.yml": "junos-mx",
}

# The explicit junos-vjunos-switch lldp block. Identical shape to upstream
# junos-qfx (detail view), only the devtype label is different.
# Indentation matches the existing yaml: two spaces under `apply:`.
LLDP_VJUNOS_SWITCH_BLOCK = """
  junos-vjunos-switch:
    version: all
    command: show lldp neighbors detail | display json | no-more
    normalize: 'lldp-neighbors-information/[0]/lldp-neighbor-information/*/[
    "lldp-local-port-id/[0]/data: ifname?|",
    "lldp-local-interface/[0]/data: ifname?|ifname",
    "lldp-remote-system-name/[0]/data: peerHostname",
    "lldp-remote-port-id/[0]/data: peerIfname?|",
    "lldp-remote-port-id-subtype/[0]/data: subtype?|",
    "lldp-remote-port-description/[0]/data: description?|",
    "lldp-remote-management-address/[0]/data: mgmtIP?|",
    "lldp-system-description/[0]/lldp-remote-system-description/[0]/data: description?|description",
    ]'
"""

SIMPLE_COPY_BLOCK_TEMPLATE = """
  junos-vjunos-switch:
    copy: {base}
"""

# Marker we leave at the end of every patched file so we can detect
# (and refuse to re-patch) an already-processed yaml. Idempotent
# build steps are good docker citizens.
PATCH_MARKER = "# junos-vjunos-switch added by add-junos-vjunos-switch.py"


def resolve_base_devtype(yml_text: str, start: str = "junos-mx") -> str:
    """Walk the `apply:` dict and follow `copy:` chains until we
    find a devtype with a real definition (not another copy).

    Why this matters: SuzieQ's service-loader copy resolver only
    follows ONE level. If we naively emit `junos-vjunos-switch: copy:
    junos-mx` and junos-mx is itself `copy: junos-qfx` (which is
    the case in devconfig.yml, bgp.yml, fs.yml, interfaces.yml,
    inventory.yml, ospfIf.yml, ospfNbr.yml), the resolver gets
    `{copy: junos-qfx}` back and rejects it as missing
    command/normalize.

    By walking the chain at PATCH time we always emit a
    `junos-vjunos-switch: copy: <X>` where X is a devtype with a real
    definition. This sidesteps the resolver bug entirely.
    """
    import yaml as _yaml
    try:
        doc = _yaml.safe_load(yml_text)
    except _yaml.YAMLError:
        return start
    apply = (doc or {}).get("apply") or {}

    seen = set()
    cur = start
    while cur not in seen:
        seen.add(cur)
        block = apply.get(cur)
        if block is None:
            # Devtype not present in this file at all
            return start
        if isinstance(block, dict) and "copy" in block and len(block) == 1:
            cur = block["copy"]
            continue
        # Real definition (dict with command/normalize, or a list of
        # versioned entries) - this is our base
        return cur
    # Cycle - shouldn't happen on upstream content but be safe
    return start


def patch_simple_copy_yaml(path: Path) -> None:
    """Append `junos-vjunos-switch: {copy: <resolved_base>}` to a service
    yaml. The base is whichever devtype junos-mx ultimately points
    at (after walking the copy chain). See resolve_base_devtype()
    for the rationale - SuzieQ's copy resolver is one-level only."""
    text = path.read_text()
    if PATCH_MARKER in text:
        print(f"  SKIP: {path.name} (already patched)")
        return

    # Pick the base: per-service override (currently only device.yml)
    # or DEFAULT_BASE (junos-qfx for everything else).
    base = SERVICE_BASE_OVERRIDES.get(path.name, DEFAULT_BASE)

    # Verify the chosen base actually has a block in this file.
    # Services that don't target Junos at all (mlag, time, topcpu)
    # have no junos-* blocks - skip them silently. This is a
    # NORMAL outcome, not an error.
    if f"{base}:" not in text:
        # If the chosen base is missing, optionally walk a `copy:`
        # chain from any other Junos devtype to find a real
        # definition. In current upstream this never fires
        # (junos-qfx is REAL in every Junos service file) but the
        # fallback is here so a future upstream restructure
        # doesn't silently skip services it shouldn't.
        fallback = resolve_base_devtype(text, start=base)
        if fallback == base or f"{fallback}:" not in text:
            print(f"  SKIP: {path.name} (no {base!r} block - "
                  f"service does not target Junos)")
            return
        base = fallback
        reason = "chain-fallback"
    elif path.name in SERVICE_BASE_OVERRIDES:
        reason = "override"
    else:
        reason = "default"

    block = SIMPLE_COPY_BLOCK_TEMPLATE.format(base=base)
    new_text = text.rstrip() + "\n" + block + "\n" + PATCH_MARKER + "\n"
    path.write_text(new_text)

    label = {
        "default": f"junos-vjunos-switch -> copy: {base}",
        "override": f"junos-vjunos-switch -> copy: {base} (per SERVICE_BASE_OVERRIDES)",
        "chain-fallback": f"junos-vjunos-switch -> copy: {base} (chain fallback)",
    }[reason]
    print(f"  PATCH: {path.name} ({label})")


def patch_lldp_yaml(path: Path) -> None:
    """Append the explicit junos-vjunos-switch detail-view block to
    lldp.yml. This is the load-bearing patch for the drift harness."""
    text = path.read_text()
    if PATCH_MARKER in text:
        print(f"  SKIP: {path.name} (already patched)")
        return
    new_text = text.rstrip() + "\n" + LLDP_VJUNOS_SWITCH_BLOCK + "\n" + PATCH_MARKER + "\n"
    path.write_text(new_text)
    print(f"  PATCH: {path.name} (junos-vjunos-switch -> detail view)")


def patch_node_multi_re_list(node_py: Path) -> None:
    """Patch the hardcoded single-RE list in JunosNode._parse_init_dev_data_devtype.

    The upstream code in poller/worker/nodes/node.py has:

        if self.devtype not in ["junos-mx", "junos-qfx10k", "junos-evo"]:
            jdata = (jdata['multi-routing-engine-results'][0]
                     ['multi-routing-engine-item'][0])

    Devtypes in that list have their `show system uptime | display
    json` output parsed as single-RE shape (no wrapper). Everything
    else is assumed to be wrapped in multi-routing-engine-results.

    vJunos-switch returns the SINGLE-RE shape (verified in Phase 5
    Part A bring-up - this is the same reason gen-inventory.py
    originally forced devtype=junos-mx). Our junos-vjunos-switch
    devtype must therefore be in this list, or the JunosNode
    parser fails on every poll cycle when it tries to walk the
    nonexistent multi-routing-engine-results path.

    Idempotent: checks if 'junos-vjunos-switch' is already in the
    file before patching."""
    if not node_py.is_file():
        sys.exit(f"FATAL: {node_py} not found")
    text = node_py.read_text()
    # Idempotency check: look for the bare devtype string (without
    # quotes), because upstream uses double quotes in this list and
    # the patched content is double-quoted too. Quote-style matching
    # would false-negative on the patched-already case.
    if "junos-vjunos-switch" in text:
        print(f"  SKIP: {node_py.name} (already has junos-vjunos-switch in multi-RE list)")
        return
    needle = '["junos-mx", "junos-qfx10k",\n                                        "junos-evo"]'
    replacement = (
        '["junos-mx", "junos-qfx10k",\n'
        '                                        "junos-evo",\n'
        '                                        "junos-vjunos-switch"]'
    )
    if needle not in text:
        sys.exit(
            f"FATAL: could not find the multi-RE list in {node_py} - "
            "the upstream image may have restructured "
            "JunosNode._parse_init_dev_data_devtype. Investigate."
        )
    new_text = text.replace(needle, replacement, 1)
    node_py.write_text(new_text)
    print(f"  PATCH: {node_py.name} (junos-vjunos-switch added to single-RE list)")


def patch_known_devtypes(utils_py: Path) -> None:
    """Inject 'junos-vjunos-switch' into the hardcoded known_devtypes()
    list in suzieq/shared/utils.py.

    Why this is needed: SuzieQ has TWO devtype validation paths.
    The service yamls (which we patch above) define which command
    runs for each devtype. But the node-init code in
    poller/worker/nodes/node.py also validates the inventory
    devtype against a hardcoded `known_devtypes()` allowlist in
    shared/utils.py. Without this allowlist entry, the poller
    raises ValueError on every inventory entry that uses
    devtype: junos-vjunos-switch, regardless of what the service yamls
    say.

    Verified during Phase 5 Part B junos-vjunos-switch bring-up: the
    chain-resolution fix made the service yaml validation pass,
    but every node still failed at init because the devtype
    name was rejected at this second validation point.

    The patch is a literal-string substitution: find the closing
    `'panos']` of the known_devtypes() return value and replace
    with `'panos', 'junos-vjunos-switch']`. Idempotent via the
    'junos-vjunos-switch' substring check.
    """
    if not utils_py.is_file():
        sys.exit(f"FATAL: {utils_py} not found")
    text = utils_py.read_text()
    if "'junos-vjunos-switch'" in text:
        print(f"  SKIP: {utils_py.name} (already has junos-vjunos-switch in known_devtypes)")
        return
    needle = "'panos'])"
    if needle not in text:
        sys.exit(
            f"FATAL: could not find {needle!r} in {utils_py} - "
            "the upstream image may have restructured "
            "known_devtypes(). Investigate before rebuilding."
        )
    new_text = text.replace(needle, "'panos', 'junos-vjunos-switch'])", 1)
    utils_py.write_text(new_text)
    print(f"  PATCH: {utils_py.name} (junos-vjunos-switch added to known_devtypes())")


def main(config_dir: Path, suzieq_pkg_dir: Path = None) -> None:
    if not config_dir.is_dir():
        sys.exit(f"FATAL: config dir {config_dir} does not exist")

    print(f"Patching SuzieQ service yamls in {config_dir}")

    lldp = config_dir / "lldp.yml"
    if not lldp.is_file():
        sys.exit(
            f"FATAL: {lldp} not found - the upstream image may have "
            "restructured the config layout. Investigate before "
            "rebuilding."
        )
    patch_lldp_yaml(lldp)

    for name in SIMPLE_COPY_SERVICES:
        path = config_dir / name
        if not path.is_file():
            print(f"  ABSENT: {name} (not in this image version)")
            continue
        patch_simple_copy_yaml(path)

    # Two more validation/behavior gates that live in Python source.
    # Tests can call main() with just config_dir to test the yaml
    # patching in isolation.
    if suzieq_pkg_dir is not None:
        # Gate 1: shared/utils.py known_devtypes() allowlist.
        # Without this, every node init raises ValueError("An
        # unknown devtype...") regardless of what the service
        # yamls say.
        patch_known_devtypes(suzieq_pkg_dir / "shared" / "utils.py")

        # Gate 2: poller/worker/nodes/node.py multi-RE wrapper list.
        # Without this, JunosNode._parse_init_dev_data_devtype
        # tries to walk a multi-routing-engine-results path that
        # vJunos does not produce, and bootupTimestamp parsing
        # crashes silently (logged as a warning, but the device
        # row never gets a real timestamp).
        patch_node_multi_re_list(
            suzieq_pkg_dir / "poller" / "worker" / "nodes" / "node.py"
        )

    print("done.")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(
            f"usage: {sys.argv[0]} <suzieq-config-dir> [<suzieq-pkg-dir>]\n"
            f"  config-dir: e.g. /usr/local/lib/python3.9/site-packages/suzieq/config\n"
            f"  pkg-dir:    e.g. /usr/local/lib/python3.9/site-packages/suzieq\n"
            f"              (omit to skip the known_devtypes() patch - useful in tests)"
        )
    pkg_dir = Path(sys.argv[2]) if len(sys.argv) == 3 else None
    main(Path(sys.argv[1]), pkg_dir)
