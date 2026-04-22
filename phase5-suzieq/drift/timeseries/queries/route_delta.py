"""Route churn query.

Reports per-VRF route activity over the window:

  prefixes_touched     distinct (vrf, prefix) tuples that received
                       any update during the window
  churned_prefixes     subset of those that received MORE THAN ONE
                       update (a single update is a stable observation,
                       multiple updates is real churn)
  total_changes        sum of update counts across churned prefixes

## Why "touched" and "churned" instead of an absolute delta

The natural question is "how did the route count change between
the start and end of the window?". Answering it cleanly requires
knowing the route table state at exactly the window's start AND
end, which we don't have - the parquet store has snapshots at
poll cadence (1 minute), and a query window starts at an arbitrary
time that almost never aligns with a snapshot.

The "touched/churned" decomposition sidesteps this. It answers
the related question "what happened to the route table during
the window?" using only the snapshots actually in the window,
which is well-defined regardless of where the window boundaries
fall. Operators looking for "did anything go wrong with route
propagation in the last hour?" get a more useful answer this way
than from a raw count delta - a delta of zero is consistent with
"nothing happened" AND with "100 routes flapped through the same
state and ended up identical", whereas the churn count distinguishes
the two.

## What gets counted

Per (hostname, vrf, prefix), all rows in the window are one
"snapshot history" for that prefix. The number of rows is the
update count.

  - update_count == 1  -> stable, just one observation
  - update_count >= 2  -> churned, prefix changed value at least
                          once (different oifs, different nexthops,
                          state flip, etc.)

The output rolls these up to (hostname, vrf):
  prefixes_touched  = COUNT distinct prefix
  churned_prefixes  = COUNT distinct prefix with update_count >= 2
  total_changes     = SUM of update_count for churned prefixes
                      (a "5-update prefix" contributes 5 here)

## Empty input

Returns a TimeseriesResult with empty rows and zero summary
counters. Never raises - the CLI surfaces empty results as
"0 events" not as an error.
"""
from typing import TYPE_CHECKING

import pandas as pd

from ..reader import WindowedTable

if TYPE_CHECKING:
    from . import TimeseriesResult


_ROUTE_KEYS = ["hostname", "vrf", "prefix"]


def route_churn(wt: WindowedTable) -> "TimeseriesResult":
    """Per-VRF route activity for the window."""
    from . import TimeseriesResult

    if wt.is_empty:
        return TimeseriesResult(
            name="route_churn", table="routes", window=wt.window,
            rows=pd.DataFrame(),
            summary={
                "total_prefixes_touched": 0,
                "total_churned_prefixes": 0,
                "total_changes": 0,
                "vrfs_seen": 0,
            },
        )

    missing = [c for c in _ROUTE_KEYS if c not in wt.rows.columns]
    if missing:
        return TimeseriesResult(
            name="route_churn", table="routes", window=wt.window,
            rows=pd.DataFrame(),
            summary={
                "total_prefixes_touched": 0,
                "total_churned_prefixes": 0,
                "total_changes": 0,
                "vrfs_seen": 0,
                "warning": f"missing routes columns: {missing}",
            },
        )

    # Per-prefix update counts (one row per prefix per host per vrf)
    per_prefix = (
        wt.rows.groupby(_ROUTE_KEYS, dropna=False)
        .size()
        .reset_index(name="update_count")
    )

    # Roll up to (hostname, vrf)
    per_vrf_records = []
    for (host, vrf), group in per_prefix.groupby(["hostname", "vrf"], dropna=False):
        prefixes_touched = len(group)
        churned = group[group["update_count"] >= 2]
        churned_prefixes = len(churned)
        total_changes = int(churned["update_count"].sum())
        per_vrf_records.append({
            "hostname":          host,
            "vrf":               vrf,
            "prefixes_touched":  prefixes_touched,
            "churned_prefixes":  churned_prefixes,
            "total_changes":     total_changes,
        })

    rows_out = pd.DataFrame(per_vrf_records)
    if rows_out.empty:
        summary = {
            "total_prefixes_touched": 0,
            "total_churned_prefixes": 0,
            "total_changes": 0,
            "vrfs_seen": 0,
        }
    else:
        summary = {
            "total_prefixes_touched": int(rows_out["prefixes_touched"].sum()),
            "total_churned_prefixes": int(rows_out["churned_prefixes"].sum()),
            "total_changes":          int(rows_out["total_changes"].sum()),
            "vrfs_seen":              len(rows_out),
        }

    return TimeseriesResult(
        name="route_churn", table="routes", window=wt.window,
        rows=rows_out,
        summary=summary,
    )
