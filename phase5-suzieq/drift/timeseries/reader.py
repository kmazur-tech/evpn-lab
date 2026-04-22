"""Window read of the SuzieQ parquet store.

The ONLY module in drift/timeseries/ that imports pyarrow. Mirrors
the boundary role of drift/state.py for Parts B/C: queries and the
envelope builder import only the WindowedTable dataclass and pandas,
never pyarrow itself.

## What window_read() does

1. Asks partition.filter_files_in_window() for the candidate files
   in the window (coalesced files pre-filtered by filename, raw
   files included unconditionally).
2. Reads each parquet file with pyarrow.
3. Manually injects `namespace` and `hostname` columns from the
   partition path when the file does not already carry them
   inside (raw poller files only have `hostname` via the
   directory hierarchy, not inside the parquet itself).
4. Concatenates everything into one DataFrame.
5. Drops rows whose `timestamp` (ms epoch) falls outside the
   requested [start_epoch, end_epoch) window. This is necessary
   for two reasons:
     - Coalesced files cover an entire hour; if the caller asks
       for a 5-minute sub-window, most rows in the matching file
       are out of range.
     - Raw files are included unconditionally and may contain
       rows from polls before the window started.
6. Adds a `ts_sec` column = (timestamp // 1000) as int64 so
   downstream queries can bucket by second without re-deriving
   it on every group-by.
7. Wraps the DataFrame in a WindowedTable with the window
   metadata so query functions can take a single object instead
   of (df, start, end, table) arg soup.

## Why a typed object instead of a bare DataFrame

Same pattern as Part B/C's FabricState / FabricIntent: the queries
in queries/*.py take WindowedTable as input, the envelope builder
in envelope.py takes WindowedTable to build the JSON header, and
neither needs to know how to find files or where the window
boundaries came from. Single source of truth, fewer arguments
to keep in sync.

## Defensive shape

If the window is empty (no files match, or all rows filtered out),
window_read() returns a WindowedTable with an empty DataFrame and
the same metadata as a populated one. Queries handle empty input
by returning empty results - they never crash on it. This is the
"first cycle has no data yet" case that drift/state.py also
handles defensively.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

import pandas as pd
import pyarrow.parquet as pq

from .partition import filter_files_in_window


# Default location of the suzieq parquet store inside the drift
# container - same path as drift/state.py uses, mounted read-only
# from the suzieq_parquet docker volume.
DEFAULT_PARQUET_DIR = "/suzieq/parquet"


@dataclass(frozen=True)
class TimeWindow:
    """A half-open [start_epoch, end_epoch) window in seconds since
    the unix epoch. Frozen so it can hash and so callers can rely
    on it being a constant once a WindowedTable is built."""
    start_epoch: int
    end_epoch: int

    @property
    def duration_seconds(self) -> int:
        return max(0, self.end_epoch - self.start_epoch)


@dataclass
class WindowedTable:
    """Output of window_read(). The metadata-carrying wrapper around
    a DataFrame of parquet rows that fall in a given time window
    for one (table, namespace).

    Fields:
      table       - SuzieQ table name (e.g. 'bgp', 'macs')
      namespace   - SuzieQ namespace (== NetBox site slug, e.g. 'dc1')
      window      - the requested TimeWindow
      rows        - merged DataFrame across all matching files,
                    with rows outside the window already filtered
                    and a `ts_sec` column added. May be empty.
      files_read  - number of parquet files actually opened. Useful
                    for the envelope's "what did we look at?" header
                    so a `0 files / 0 rows` result is distinguishable
                    from a `12 files / 0 rows` result (the second is
                    "the window had data but everything was filtered",
                    the first is "no data at all").
    """
    table: str
    namespace: str
    window: TimeWindow
    rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    files_read: int = 0

    @property
    def is_empty(self) -> bool:
        return self.rows.empty


# Pulls 'hostname=dc1-leaf1' out of a raw parquet file's path so
# we can manually add the column when the inner parquet does not
# carry it. Tolerates both forward and backslash separators so
# the regex works on Windows test paths and Linux container paths.
_HOSTNAME_PARTITION_RE = re.compile(r"hostname=([^/\\]+)")


def _read_one_file(path: Path, namespace: str) -> pd.DataFrame:
    """Read a single parquet file with pyarrow, return as a
    DataFrame, and inject `namespace` and `hostname` columns from
    the partition path if the file does not already carry them.

    Coalesced files written by the suzieq coalescer have `hostname`
    as a real column inside the parquet (the coalescer merges per-
    host raw files into one per-namespace file and writes hostname
    as data). Raw poller files have `hostname` ONLY in the
    directory tree (`hostname=<x>/`), so we have to add it back
    manually after reading."""
    df = pq.read_table(str(path)).to_pandas()
    if "namespace" not in df.columns:
        df["namespace"] = namespace
    if "hostname" not in df.columns:
        m = _HOSTNAME_PARTITION_RE.search(str(path))
        if m:
            df["hostname"] = m.group(1)
    return df


def window_read(
    table: str,
    namespace: str,
    start_epoch: int,
    end_epoch: int,
    parquet_dir: Union[str, Path] = DEFAULT_PARQUET_DIR,
) -> WindowedTable:
    """Read all rows of `table` in `namespace` with row-level
    timestamps in [start_epoch * 1000, end_epoch * 1000) ms.

    Returns a WindowedTable. Empty input is a valid result, NOT
    an error: queries and the envelope handle empty rows
    defensively and the CLI reports it as "0 events".

    Args:
      table         SuzieQ table name (bgp, macs, evpnVni, ...)
      namespace     SuzieQ namespace (dc1, dc2, ...)
      start_epoch   inclusive lower bound, seconds since epoch
      end_epoch     exclusive upper bound, seconds since epoch
      parquet_dir   suzieq parquet root, default /suzieq/parquet

    The two epoch arguments are SECONDS, not milliseconds. The
    parquet `timestamp` column is in milliseconds; this function
    handles the *1000 conversion internally so callers can think
    in plain unix-epoch seconds. The CLI's --window flag uses
    parse_duration() in partition.py which also returns seconds.
    """
    window = TimeWindow(start_epoch=start_epoch, end_epoch=end_epoch)
    files = filter_files_in_window(
        parquet_dir, table, namespace, start_epoch, end_epoch
    )

    if not files:
        return WindowedTable(
            table=table, namespace=namespace, window=window,
            rows=pd.DataFrame(), files_read=0,
        )

    frames = []
    files_read = 0
    for f in files:
        try:
            frames.append(_read_one_file(f, namespace))
            files_read += 1
        except (FileNotFoundError, OSError):
            # Race with the poller writing a fresh file - skip
            # silently. The next call will pick it up. drift/
            # state.py handles the same case the same way.
            continue

    if not frames:
        return WindowedTable(
            table=table, namespace=namespace, window=window,
            rows=pd.DataFrame(), files_read=0,
        )

    df = pd.concat(frames, ignore_index=True, sort=False)

    # Row-level window filter. SuzieQ stores `timestamp` in
    # milliseconds since epoch.
    if "timestamp" in df.columns and not df.empty:
        df = df[
            (df["timestamp"] >= start_epoch * 1000)
            & (df["timestamp"] < end_epoch * 1000)
        ].reset_index(drop=True)

    # Add a second-precision integer view for downstream queries.
    # Computed once here so every group-by/diff doesn't redivide.
    if "timestamp" in df.columns and not df.empty:
        df["ts_sec"] = (df["timestamp"] // 1000).astype("int64")

    return WindowedTable(
        table=table, namespace=namespace, window=window,
        rows=df, files_read=files_read,
    )
