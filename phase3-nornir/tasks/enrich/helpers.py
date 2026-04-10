"""Pure helper functions used by multiple enrich collectors.

No NetBox, no Nornir, no env. Easy to test in isolation - see
tests/test_enrich_helpers.py.
"""

import re
from typing import Optional


def _lo0_unit_from_iface_name(name: str) -> Optional[int]:
    """`lo0.1` -> 1, `lo0.2` -> 2; returns None for non-lo0 interfaces."""
    m = re.fullmatch(r"lo0\.(\d+)", name)
    return int(m.group(1)) if m else None


def _loopback_description(unit: int, role_slug: str, vrf_name) -> str:
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
