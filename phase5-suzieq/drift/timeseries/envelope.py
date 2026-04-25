"""JSON envelope for Part D timeseries results.

The drift / assertions modes both emit a single
`{result, total, passed, failed, checks: [...]}` shape because they
fit a pass/fail model. Time-series queries don't - "BGP flapped
4 times in the last hour" isn't pass or fail, it's an observation
the operator interprets in context. So Part D has its own envelope
shape.

## Envelope shape (the contract)

```
{
  "namespace": "dc1",
  "generated_at": "2026-04-11T12:34:56+00:00",
  "window": {
    "start_epoch": 1775904896,
    "end_epoch":   1775908496,
    "start_iso":   "2026-04-11T11:34:56+00:00",
    "end_iso":     "2026-04-11T12:34:56+00:00",
    "duration_seconds": 3600
  },
  "queries": [
    {
      "name": "bgp_flaps",
      "table": "bgp",
      "files_read": 3,
      "summary": { ...query-specific aggregate metrics... },
      "rows":    [ ...one dict per record... ]
    },
    ...
  ]
}
```

## Why a list of queries instead of a dict keyed by name

Stable iteration order. Phase 6 CI / log-tail watchers want
deterministic field ordering. A dict-keyed envelope leaves the
output order to whatever Python dict insertion order does at
the time, which is fine right now but not contractually stable.
A list with a `name` field is.

## What it does NOT include

- Per-row drift severity. The whole point of Part D is to
  surface neutral observations, not pass/fail records. The
  CLI exit code is also always 0 in --mode timeseries unless
  the harness itself broke - that's the EXIT_TOOLING_ERROR
  branch reusing drift's existing exit code contract.
- A "result" / "passed" field. Same reason.
"""
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, IO, Iterable, List, Mapping, Optional, Tuple

import pandas as pd

from .reader import TimeWindow, WindowedTable
from .queries import TimeseriesResult


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------
#
# The self-check inspects the harness's own output for signs that
# something upstream is silently broken - for example, the hourly
# systemd timer running for weeks against an empty parquet store
# because the poller died and nobody noticed because the drift
# results are always-zero exit code "neutral observations". The
# self-check surfaces that class of bug as a top-level
# `status: "degraded"` + `warnings: [...]` in the JSON envelope,
# WITHOUT changing the process exit code (which stays 0 per the
# "timeseries observations are not pass/fail" rule in ADR-11).
#
# ## Why sqPoller is the heartbeat, not the query tables
#
# An earlier version of this self-check flagged `files_read == 0`
# on every query table and checked row-level freshness per table.
# Live test surfaced the false positive: sparse tables like `bgp`,
# `macs`, `routes` only write new rows when state CHANGES. A
# stable 4-device fabric can legitimately have zero bgp rows in
# the last hour, and the coalesced files can have their "latest
# row" timestamp 8 hours ago. Both are normal. Flagging them as
# degraded would noise the signal.
#
# The SuzieQ `sqPoller` table is different: it writes a row per
# (hostname, service) on every poll cycle regardless of fabric
# state. It is the poller's own heartbeat. If sqPoller's latest
# row is >_STALE_THRESHOLD_SEC old at the window end, the poller
# is stuck or dead - that's the bug the reviewers wanted us to
# catch. If sqPoller files_read is 0 (on a live window), either
# the poller never started or we pointed at the wrong parquet
# dir.
#
# The sparse query tables still contribute via rule 1 (shape
# warnings from the query pipeline like missing columns).

# Freshness threshold: if the query window ends within this many
# seconds of "now", the window is "live" and sqPoller's latest
# row must be within this many seconds of the window end. The
# default is 2 x the typical SuzieQ service poll cadence (~60 s),
# so one missed poll cycle is fine but two in a row is flagged.
# Historical windows (query end in the deep past) skip freshness
# because old data is expected to look old.
_STALE_THRESHOLD_SEC = 120

# The single "heartbeat" table. See module doc block above.
HEARTBEAT_TABLE = "sqPoller"


def self_check(
    results: Iterable[TimeseriesResult],
    files_read_by_table: Mapping[str, int],
    windowed_tables: Optional[Mapping[str, WindowedTable]] = None,
    now: Optional[int] = None,
) -> Tuple[str, List[str]]:
    """Inspect query results + windowed tables for degradation
    signals. Returns ('ok', []) or ('degraded', [...]).

    Rules:

      1. Propagate any per-query `warning` field (set when a
         query detects missing columns or some other shape issue
         upstream).
      2. Poller heartbeat via the sqPoller table. Only for LIVE
         windows (window.end_epoch within _STALE_THRESHOLD_SEC
         of now). Flag degraded when:
           - sqPoller was not read at all (windowed_tables has
             no entry) AND the caller did not explicitly opt
             out by passing windowed_tables=None
           - sqPoller files_read == 0 (poller never wrote in
             the window)
           - sqPoller has files but no rows in the window (the
             files exist but every row got filtered by the
             time-window pass)
           - sqPoller's latest row is older than
             _STALE_THRESHOLD_SEC at the window end (poller
             stuck between poll cycles for >2 x poll cadence)
         Historical queries skip this check entirely because old
         data is expected to look old.

    The `now` arg is injectable so tests can pin freshness math
    without monkey-patching time.time.

    `files_read_by_table` is accepted for back-compat but the
    self-check does NOT use it as a degradation signal on sparse
    query tables any more - see the module doc block above.
    """
    warnings: List[str] = []
    results_list = list(results)

    # Rule 1: propagate per-query warnings
    for r in results_list:
        if isinstance(r.summary, dict) and "warning" in r.summary:
            warnings.append(f"{r.name}: {r.summary['warning']}")

    # Rule 2: sqPoller heartbeat, only for live windows.
    # Skipping this check requires windowed_tables to be None
    # (the caller opting out explicitly).
    if results_list and windowed_tables is not None:
        window = results_list[0].window
        if now is None:
            now = int(time.time())
        window_is_live = (window.end_epoch >= now - _STALE_THRESHOLD_SEC)
        if window_is_live:
            warnings.extend(
                _check_heartbeat(windowed_tables, window)
            )

    status = "degraded" if warnings else "ok"
    return status, warnings


def _check_heartbeat(
    windowed_tables: Mapping[str, WindowedTable],
    window: TimeWindow,
) -> List[str]:
    """Evaluate the sqPoller heartbeat for a live window. Returns
    a list of 0 or more warning strings."""
    out: List[str] = []
    poller_wt = windowed_tables.get(HEARTBEAT_TABLE)
    if poller_wt is None:
        # Caller passed windowed_tables without sqPoller. That's
        # fine (some test or some future consumer only cares
        # about a subset of signals); we just can't verify the
        # heartbeat, which itself is worth a warning so the
        # caller knows the check was degraded.
        out.append(
            f"{HEARTBEAT_TABLE} heartbeat not provided: self-check "
            f"cannot verify poller liveness"
        )
        return out
    if poller_wt.files_read == 0:
        out.append(
            f"{HEARTBEAT_TABLE} heartbeat: 0 files read in window; "
            f"poller may be dead or parquet dir wrong"
        )
        return out
    if poller_wt.is_empty:
        out.append(
            f"{HEARTBEAT_TABLE} heartbeat: files present but no rows "
            f"in window; poller may be writing but rows out of range"
        )
        return out
    if "timestamp" not in poller_wt.rows.columns:
        # Schema drift - surface loudly, don't silently pass
        out.append(
            f"{HEARTBEAT_TABLE} heartbeat: no timestamp column in "
            f"rows; schema drift upstream?"
        )
        return out
    try:
        latest_ms = int(poller_wt.rows["timestamp"].max())
    except (TypeError, ValueError):
        out.append(
            f"{HEARTBEAT_TABLE} heartbeat: timestamp column unreadable"
        )
        return out
    latest_sec = latest_ms // 1000
    age = window.end_epoch - latest_sec
    if age > _STALE_THRESHOLD_SEC:
        out.append(
            f"{HEARTBEAT_TABLE} heartbeat: latest row is {age}s old "
            f"at window end, poller may be stuck "
            f"(threshold {_STALE_THRESHOLD_SEC}s)"
        )
    return out


def _df_to_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Convert a DataFrame to a list of plain-Python dicts safe to
    json.dump. Coerces numpy/pandas scalar types via .item() the
    same way drift/cli.py:_json_default does."""
    if df.empty:
        return []
    records = df.to_dict(orient="records")
    out: List[Dict[str, Any]] = []
    for r in records:
        clean: Dict[str, Any] = {}
        for k, v in r.items():
            clean[k] = _coerce_scalar(v)
        out.append(clean)
    return out


def _coerce_scalar(v: Any) -> Any:
    """Coerce a numpy/pandas scalar to a json-friendly Python type.
    Returns None for NaN. Falls through to str() for anything we
    don't recognize so json.dump never raises."""
    if v is None:
        return None
    # numpy scalars expose .item()
    if hasattr(v, "item"):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            pass
    # pandas NA / numpy NaN check
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _window_to_dict(window: TimeWindow) -> Dict[str, Any]:
    return {
        "start_epoch":      window.start_epoch,
        "end_epoch":        window.end_epoch,
        "start_iso":        datetime.fromtimestamp(
                                window.start_epoch, tz=timezone.utc
                            ).isoformat(),
        "end_iso":          datetime.fromtimestamp(
                                window.end_epoch, tz=timezone.utc
                            ).isoformat(),
        "duration_seconds": window.duration_seconds,
    }


def build_envelope(
    namespace: str,
    window: TimeWindow,
    results: Iterable[TimeseriesResult],
    files_read_by_table: Dict[str, int],
    windowed_tables: Optional[Mapping[str, WindowedTable]] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the Part D JSON envelope from a sequence of query
    results plus per-table file-read counts.

    Args:
      namespace            SuzieQ namespace
      window               the requested TimeWindow
      results              iterable of TimeseriesResult, in display order
      files_read_by_table  map from SuzieQ table name to count of parquet
                           files actually opened (from WindowedTable.files_read)
      windowed_tables      optional map from table name to WindowedTable;
                           used by the self-check to inspect row freshness.
                           When None, the freshness check is skipped and
                           only the files_read / per-query warning checks
                           run.
      now                  optional unix epoch seconds for freshness math.
                           Defaults to time.time() when None. Injectable
                           for tests.

    Output shape adds top-level `status` ("ok"/"degraded") and
    `warnings` (list of human strings) as of the review-finding fix.
    The exit code contract is unchanged - `status: "degraded"` does
    NOT mean a non-zero exit; it's a purely informational signal
    the Phase 6 consumer can alert on.
    """
    results_list = list(results)
    status, warnings = self_check(
        results_list,
        files_read_by_table,
        windowed_tables=windowed_tables,
        now=now,
    )
    return {
        "namespace":    namespace,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status":       status,
        "warnings":     warnings,
        "window":       _window_to_dict(window),
        "queries": [
            {
                "name":       r.name,
                "table":      r.table,
                "files_read": int(files_read_by_table.get(r.table, 0)),
                "summary":    r.summary,
                "rows":       _df_to_records(r.rows),
            }
            for r in results_list
        ],
    }


def emit_json(envelope: Dict[str, Any], stream: IO[str] = None) -> None:
    """Write the envelope to a stream as pretty-printed JSON. The
    default stream is stdout so the CLI can call this directly."""
    if stream is None:
        stream = sys.stdout
    json.dump(envelope, stream, indent=2, default=str)
    stream.write("\n")


def emit_human(envelope: Dict[str, Any], stream: IO[str] = None) -> None:
    """Write the envelope as a compact human-readable summary. Used
    when the operator runs the CLI by hand on netdevops-srv. Only
    summary lines and counts - the per-row tables would be too
    noisy for a terminal at lab scale."""
    if stream is None:
        stream = sys.stdout
    ns = envelope["namespace"]
    win = envelope["window"]
    print(
        f"namespace={ns}  window=[{win['start_iso']} -> {win['end_iso']}]"
        f"  ({win['duration_seconds']}s)",
        file=stream,
    )
    status = envelope.get("status", "ok")
    warnings = envelope.get("warnings", [])
    if status != "ok" or warnings:
        print(f"status={status}", file=stream)
        for w in warnings:
            print(f"  ! {w}", file=stream)
    print("-" * 80, file=stream)
    for q in envelope["queries"]:
        summary_pairs = [f"{k}={v}" for k, v in q["summary"].items()]
        line = f"  {q['name']:14s} table={q['table']:7s} files={q['files_read']:>3d}"
        if summary_pairs:
            line += "  " + " ".join(summary_pairs)
        print(line, file=stream)
        # Show first few rows inline if any
        for row in q["rows"][:5]:
            kv = "  ".join(f"{k}={v}" for k, v in row.items())
            print(f"      - {kv}", file=stream)
        if len(q["rows"]) > 5:
            print(f"      ... ({len(q['rows']) - 5} more)", file=stream)
