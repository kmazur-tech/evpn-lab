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
from datetime import datetime, timezone
from typing import Any, Dict, IO, Iterable, List

import pandas as pd

from .reader import TimeWindow, WindowedTable
from .queries import TimeseriesResult


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
) -> Dict[str, Any]:
    """Build the Part D JSON envelope from a sequence of query
    results plus per-table file-read counts.

    Args:
      namespace            SuzieQ namespace
      window               the requested TimeWindow
      results              iterable of TimeseriesResult, in display order
      files_read_by_table  map from SuzieQ table name to count of parquet
                           files actually opened (from WindowedTable.files_read)
    """
    return {
        "namespace":    namespace,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window":       _window_to_dict(window),
        "queries": [
            {
                "name":       r.name,
                "table":      r.table,
                "files_read": int(files_read_by_table.get(r.table, 0)),
                "summary":    r.summary,
                "rows":       _df_to_records(r.rows),
            }
            for r in results
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
