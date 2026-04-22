"""Phase 5 Part D: time-window queries over the SuzieQ parquet history.

Where Part B/C answer "what is the current state right now?", Part D
answers "what happened over a window?". Same parquet store, different
read pattern: we read multiple snapshots per (host, key) instead of
collapsing to view='latest'.

## Module boundary rule

This package follows the same boundary discipline as drift/ proper:

  partition.py     - filesystem + filename parsing only (pure)
  reader.py        - the ONLY pyarrow import in this package
  queries/*.py     - pure pandas computation, take DataFrames as input
  envelope.py      - JSON shape (no pandas, no pyarrow)
  cli wiring       - drift/cli.py --mode timeseries dispatches here

So tests for queries/ never need pyarrow installed and never touch
the filesystem - same pattern that keeps Phase 5's main suite under
2 seconds.

## What it does NOT do

- Live polling. Part D reads PARQUET HISTORY only. The poller is the
  SuzieQ stack, not this package.
- Per-row writes. Read-only.
- View='latest' collapse. The whole point is to see multiple snapshots
  per (host, key) over a window. Use drift/state.py if you want the
  collapsed-to-latest view.
"""
