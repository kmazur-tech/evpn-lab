"""BGP flap query.

Counts BGP session state transitions in the window per
(hostname, vrf, peer, afi, safi). A "flap" here is any
adjacent-snapshot transition where the row's `state` column
changes value.

## Why count transitions instead of using numChanges

The bgp parquet has a `numChanges` column that the device increments
on each session state change. Two reasons we don't use it directly:

1. The counter resets when the daemon restarts. A reset between
   two snapshots produces a NEGATIVE delta and the naive
   max-min approach silently undercounts.

2. The counter counts EVERY state change including transient ones
   the device sees but the poller does not snapshot. We want
   "what state transitions did the harness OBSERVE?" not "what
   state transitions did the device experience?". The first is
   what an operator can correlate against assertions output and
   logs; the second is a device internal we have no visibility
   into between polls.

Counting row-to-row state transitions in the polled snapshots
gives us the operator-observable answer. It is conservative -
fast flaps that complete inside one poll cycle are missed - but
that's correct: if the harness can't see it, an alert on it
would be unfounded.

## What gets counted as a transition

For each (hostname, vrf, peer, afi, safi) group sorted by
timestamp, count adjacent pairs where state[i].lower() !=
state[i-1].lower(). The lowercase comparison absorbs the
"Established" vs "established" Junos vs Arista cosmetic
difference.

## Output

Per session that had at least one transition:
  hostname, vrf, peer, afi, safi, flap_count, snapshots,
  first_state, last_state

Plus a summary header:
  total_flaps         sum of flap_count across all sessions
  sessions_with_flaps number of sessions that had at least one
  sessions_seen       total distinct sessions in the window
"""
from typing import TYPE_CHECKING

import pandas as pd

from ..reader import WindowedTable

if TYPE_CHECKING:
    from . import TimeseriesResult


_BGP_KEYS = ["hostname", "vrf", "peer", "afi", "safi"]


def bgp_flap_count(wt: WindowedTable) -> "TimeseriesResult":
    """Count BGP session state transitions per session in the window."""
    # Local import to avoid the circular import between this module
    # and queries/__init__.py (which imports this function).
    from . import TimeseriesResult

    if wt.is_empty:
        return TimeseriesResult(
            name="bgp_flaps", table="bgp", window=wt.window,
            rows=pd.DataFrame(),
            summary={
                "total_flaps": 0,
                "sessions_with_flaps": 0,
                "sessions_seen": 0,
            },
        )

    # Defensive column check - if we somehow get a windowed table
    # without the expected bgp shape, return empty rather than
    # crashing the whole timeseries run. Real bgp data always has
    # these columns; an empty result here means upstream broke,
    # not the user.
    missing = [c for c in _BGP_KEYS + ["state", "timestamp"] if c not in wt.rows.columns]
    if missing:
        return TimeseriesResult(
            name="bgp_flaps", table="bgp", window=wt.window,
            rows=pd.DataFrame(),
            summary={
                "total_flaps": 0,
                "sessions_with_flaps": 0,
                "sessions_seen": 0,
                "warning": f"missing bgp columns: {missing}",
            },
        )

    df = wt.rows.sort_values(_BGP_KEYS + ["timestamp"])

    flap_records = []
    sessions_seen = 0
    for key, group in df.groupby(_BGP_KEYS, dropna=False):
        sessions_seen += 1
        states = [str(s).lower() for s in group["state"].tolist()]
        # Count adjacent transitions
        transitions = sum(
            1 for i in range(1, len(states)) if states[i] != states[i - 1]
        )
        if transitions == 0:
            continue
        # The groupby key is a tuple in the same order as _BGP_KEYS
        host, vrf, peer, afi, safi = key
        flap_records.append({
            "hostname":    host,
            "vrf":         vrf,
            "peer":        peer,
            "afi":         afi,
            "safi":        safi,
            "flap_count":  transitions,
            "snapshots":   len(states),
            "first_state": states[0],
            "last_state":  states[-1],
        })

    rows_out = pd.DataFrame(flap_records)
    total_flaps = int(rows_out["flap_count"].sum()) if not rows_out.empty else 0

    return TimeseriesResult(
        name="bgp_flaps", table="bgp", window=wt.window,
        rows=rows_out,
        summary={
            "total_flaps":         total_flaps,
            "sessions_with_flaps": len(rows_out),
            "sessions_seen":       sessions_seen,
        },
    )
