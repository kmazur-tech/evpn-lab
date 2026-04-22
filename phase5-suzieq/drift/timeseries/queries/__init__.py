"""Phase 5 Part D query layer.

Each query is a pure function that takes a WindowedTable and returns
a TimeseriesResult. Queries import only `pandas` and the local
dataclasses - never pyarrow, never the filesystem. The reader.py
boundary already did the I/O before any query runs.

## Why a TimeseriesResult dataclass instead of bare dicts

Same reason as Part B's Drift dataclass: a typed result is the
contract between the queries and the envelope, so the envelope
builder doesn't need to know the shape of every query and the
queries don't need to know how the JSON envelope formats them.

## Query catalog

  bgp_flap_count   bgp_flaps.py    BGP session state transitions
                                   per (host, vrf, peer, afi, safi)
  route_churn      route_delta.py  Distinct prefixes touched in the
                                   window per (host, vrf), with a
                                   "high churn" subset for prefixes
                                   updated >1 time
  mac_mobility     mac_mobility.py MACs that appeared on >1 distinct
                                   (host, oif, remoteVtepIp) location
                                   during the window

## Adding a new query

1. New module under queries/. One function. Signature:
   `(wt: WindowedTable) -> TimeseriesResult`.
2. Import + add to QUERIES dict in this __init__.
3. Tests in tests/test_timeseries_queries_<name>.py using inline
   DataFrame fixtures (no pyarrow, no parquet, no docker).
4. Envelope automatically picks it up via the QUERIES dict.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict

import pandas as pd

from ..reader import TimeWindow, WindowedTable

from .bgp_flaps import bgp_flap_count
from .route_delta import route_churn
from .mac_mobility import mac_mobility


@dataclass
class TimeseriesResult:
    """The output of one Part D query.

    Fields:
      name      stable identifier the envelope uses as a JSON key
                ("bgp_flaps", "route_churn", "mac_mobility")
      table     SuzieQ source table the query reads from
      window    the TimeWindow this was computed over
      rows      pandas DataFrame of per-event records (may be empty)
      summary   small dict of aggregate metrics for the JSON header
    """
    name: str
    table: str
    window: TimeWindow
    rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    summary: Dict[str, Any] = field(default_factory=dict)


# Registry of query name -> (table, function). The CLI dispatches
# on this and the envelope iterates over it. Adding a new query
# means adding one entry here and one import above.
QUERIES: Dict[str, "QueryEntry"] = {}


@dataclass(frozen=True)
class QueryEntry:
    name: str
    table: str
    fn: Callable[[WindowedTable], TimeseriesResult]


QUERIES = {
    "bgp_flaps":    QueryEntry("bgp_flaps",    "bgp",    bgp_flap_count),
    "route_churn":  QueryEntry("route_churn",  "routes", route_churn),
    "mac_mobility": QueryEntry("mac_mobility", "macs",   mac_mobility),
}


__all__ = [
    "TimeseriesResult",
    "QueryEntry",
    "QUERIES",
    "bgp_flap_count",
    "route_churn",
    "mac_mobility",
]
