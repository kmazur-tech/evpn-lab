"""MAC mobility query.

Detects MAC moves: a (vlan, macaddr) seen on more than one
distinct location during the window. A "location" here is the
tuple (hostname, oif, remoteVtepIp), so the query catches both:

  - L2 moves: same MAC seen on a different host's local oif
    (host migration between leaves, or a misbehaving server with
    the same MAC plugged into two ports)
  - VXLAN remote-VTEP changes: same MAC's remoteVtepIp changed
    from one leaf's VTEP to another (the EVPN Type-2 advertisement
    moved)

## Why this exists

In a stable EVPN-VXLAN fabric a host's MAC should appear in
exactly one place: either as a LOCAL row on the leaf the host
plugs into, or as a REMOTE row on the other leaves with that
leaf's loopback as remoteVtepIp. A MAC that changes location
during a window is one of:

  - Real host move (rare, expected for VM migrations)
  - Mac flap from a STP topology change (real bug)
  - ESI-LAG inconsistency where the two leaves disagree about
    which one is the DF for an MH host (real bug)
  - Spoofing or duplicate MAC (real bug)

The drift harness's _diff_anycast_macs check is anycast-MAC
specific - it only catches the gateway VMAC missing somewhere.
This query is the broader "any MAC moved" check.

## What gets counted

For each (vlan, macaddr) in the window:
  locations = distinct (hostname, oif, remoteVtepIp) tuples

If locations >= 2, it's a move event.

## Output

One row per moved MAC:
  vlan, macaddr, distinct_locations, hosts (comma-separated),
  oifs (comma-separated), remoteVtepIps (comma-separated)

Plus summary:
  macs_moved   - count of MACs with locations >= 2
  macs_seen    - total distinct (vlan, macaddr) in the window
"""
from typing import TYPE_CHECKING

import pandas as pd

from ..reader import WindowedTable

if TYPE_CHECKING:
    from . import TimeseriesResult


_MAC_KEYS = ["vlan", "macaddr"]
_LOCATION_COLS = ["hostname", "oif", "remoteVtepIp"]


def mac_mobility(wt: WindowedTable) -> "TimeseriesResult":
    """Find MACs that appeared on more than one distinct location."""
    from . import TimeseriesResult

    if wt.is_empty:
        return TimeseriesResult(
            name="mac_mobility", table="macs", window=wt.window,
            rows=pd.DataFrame(),
            summary={"macs_moved": 0, "macs_seen": 0},
        )

    missing = [c for c in _MAC_KEYS + _LOCATION_COLS if c not in wt.rows.columns]
    if missing:
        return TimeseriesResult(
            name="mac_mobility", table="macs", window=wt.window,
            rows=pd.DataFrame(),
            summary={
                "macs_moved": 0,
                "macs_seen": 0,
                "warning": f"missing macs columns: {missing}",
            },
        )

    # Distinct (vlan, macaddr, hostname, oif, remoteVtepIp) rows.
    # Multiple snapshots of the same MAC at the same location
    # collapse to one row before counting.
    distinct_locations = (
        wt.rows[_MAC_KEYS + _LOCATION_COLS]
        .drop_duplicates()
        .reset_index(drop=True)
    )

    # Group by (vlan, macaddr) and roll the location columns
    # into deduped, sorted, comma-separated strings for the JSON
    # output. Drop None / empty so the strings stay tidy.
    move_records = []
    macs_seen = 0
    for (vlan, mac), group in distinct_locations.groupby(_MAC_KEYS, dropna=False):
        macs_seen += 1
        n_locations = len(group)
        if n_locations < 2:
            continue
        hosts = sorted({h for h in group["hostname"].tolist() if h})
        oifs = sorted({o for o in group["oif"].tolist() if o})
        vteps = sorted({v for v in group["remoteVtepIp"].tolist() if v})
        move_records.append({
            "vlan":               vlan,
            "macaddr":            mac,
            "distinct_locations": n_locations,
            "hosts":              ",".join(hosts),
            "oifs":               ",".join(oifs),
            "remoteVtepIps":      ",".join(vteps),
        })

    rows_out = pd.DataFrame(move_records)

    return TimeseriesResult(
        name="mac_mobility", table="macs", window=wt.window,
        rows=rows_out,
        summary={
            "macs_moved": len(rows_out),
            "macs_seen":  macs_seen,
        },
    )
