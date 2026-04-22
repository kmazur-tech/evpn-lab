"""Parquet partition discovery for time-window queries.

This module is pure: it parses filenames and walks directories,
nothing more. It does NOT open parquet files - that's reader.py's
job (reader.py is the only module in drift/timeseries/ that imports
pyarrow). Keeping the two split lets the partition tests run with
just stdlib + pytest.

## SuzieQ parquet store shape (verified live 2026-04-11)

Two parallel trees under the suzieq parquet root:

  coalesced/<table>/sqvers=X.Y/namespace=<ns>/sqc-h1-0-<start>-<end>.parquet

      Where the coalescer parks compacted hourly windows. The
      filename encodes (start_epoch, end_epoch) of the exact 1-hour
      window the file covers. SPARSE: a file exists only for hours
      where rows actually changed. So an empty hour produces no
      file, and the absence of a file IS evidence of no change.

  <table>/sqvers=X.Y/namespace=<ns>/hostname=<host>/*.parquet

      Where the poller writes raw rows between coalescer runs. The
      coalescer drains this directory at the top of each hour
      (verified at suzieq pq_coalesce.py:71). The "current hour's
      data not yet compacted" lives here.

A complete time-window read must touch BOTH trees - coalesced for
history, raw for the most-recent uncompacted poll cycles.

## Windowing strategy

Coalesced filenames give us hour-aligned [start_epoch, end_epoch)
bounds for free, so the file-level pre-filter is cheap and exact:
no file open needed. Sub-hour windows still work - we just open
the matching coalesced file(s) and let reader.py filter rows by
the row-level `timestamp` column (millisecond epoch).

For raw files we cannot pre-filter by name, so we include all of
them. Raw is per-host and small (~one polling cadence's worth of
rows per host), so this is cheap.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union


# sqc-h<period_hours>-<shard>-<start_epoch>-<end_epoch>.parquet
#
# Lab data shows period_hours=1 always (hourly compaction is the
# coalescer default in suzieq.cfg) and shard=0 always (single
# shard for the 4-device fabric). The regex accepts arbitrary
# values for both so we don't break if a future suzieq tunes
# either knob.
COALESCED_FILENAME_RE = re.compile(
    r"^sqc-h(?P<period_hours>\d+)-(?P<shard>\d+)"
    r"-(?P<start>\d+)-(?P<end>\d+)\.parquet$"
)


@dataclass(frozen=True)
class CoalescedFile:
    """One parsed coalesced parquet filename. start/end are inclusive
    of the window the coalescer used to write it - the suzieq
    coalescer convention is [start, end), half-open."""
    path: Path
    start_epoch: int
    end_epoch: int
    period_hours: int
    shard: int


def parse_coalesced_filename(path: Union[str, Path]) -> Optional[CoalescedFile]:
    """Parse a coalesced parquet filename. Returns None if the name
    does not match the sqc-* pattern (raw files, .crc sidecars,
    anything else - all silently ignored, NOT errors)."""
    p = Path(path)
    m = COALESCED_FILENAME_RE.match(p.name)
    if not m:
        return None
    return CoalescedFile(
        path=p,
        start_epoch=int(m.group("start")),
        end_epoch=int(m.group("end")),
        period_hours=int(m.group("period_hours")),
        shard=int(m.group("shard")),
    )


def windows_overlap(
    a_start: int, a_end: int, b_start: int, b_end: int
) -> bool:
    """Half-open window overlap. [a_start, a_end) overlaps
    [b_start, b_end) iff a_start < b_end AND b_start < a_end.

    Touching windows do NOT overlap: [0, 10) and [10, 20) share
    no time and return False. This matches the coalescer's
    [start, end) semantics so a query window that exactly
    matches a coalesced hour reads exactly that one file.

    Zero-width or inverted windows (start >= end) are treated as
    empty and never overlap anything - the half-open formula
    alone would let `[50, 50)` overlap `[0, 100)` because point
    50 sits inside the second interval, but a degenerate
    empty interval has no points to overlap with."""
    if a_start >= a_end or b_start >= b_end:
        return False
    return a_start < b_end and b_start < a_end


def filter_files_in_window(
    parquet_dir: Union[str, Path],
    table: str,
    namespace: str,
    start_epoch: int,
    end_epoch: int,
) -> List[Path]:
    """List parquet files for one table+namespace whose data MIGHT
    fall in [start_epoch, end_epoch).

    Reads BOTH the coalesced/<table>/ and raw <table>/ trees:

      - Coalesced files: pre-filtered by filename epoch overlap.
        Cheap, no file opens.
      - Raw files: included unconditionally because the filename
        does not encode a window. reader.py filters them by the
        row-level timestamp column.

    Returns an empty list if neither tree has any matching file.
    The caller (reader.py) treats empty input as "no data in this
    window" - that's a valid result, not an error.

    The list is returned in deterministic order: coalesced first
    (sorted by start_epoch), then raw (sorted by hostname then
    filename) so downstream concatenation is reproducible.
    """
    if start_epoch > end_epoch:
        raise ValueError(
            f"start_epoch {start_epoch} > end_epoch {end_epoch}"
        )
    parquet_dir = Path(parquet_dir)
    out: List[Path] = []

    # 1. Coalesced tree - filename pre-filter.
    coalesced_root = parquet_dir / "coalesced" / table
    if coalesced_root.exists():
        coalesced_matches: List[CoalescedFile] = []
        for sqvers_dir in sorted(coalesced_root.glob("sqvers=*")):
            ns_dir = sqvers_dir / f"namespace={namespace}"
            if not ns_dir.exists():
                continue
            for f in sorted(ns_dir.iterdir()):
                cf = parse_coalesced_filename(f)
                if cf is None:
                    continue
                if windows_overlap(
                    cf.start_epoch, cf.end_epoch, start_epoch, end_epoch
                ):
                    coalesced_matches.append(cf)
        coalesced_matches.sort(key=lambda c: (c.start_epoch, c.shard))
        out.extend(c.path for c in coalesced_matches)

    # 2. Raw tree - include everything for table+namespace,
    #    regardless of name. Raw is by-host-partitioned and small.
    raw_root = parquet_dir / table
    if raw_root.exists():
        for sqvers_dir in sorted(raw_root.glob("sqvers=*")):
            ns_dir = sqvers_dir / f"namespace={namespace}"
            if not ns_dir.exists():
                continue
            for host_dir in sorted(ns_dir.glob("hostname=*")):
                if not host_dir.is_dir():
                    continue
                for f in sorted(host_dir.iterdir()):
                    if f.suffix == ".parquet":
                        out.append(f)

    return out


# Duration parsing for the CLI --window flag.
#
# The smallest practically-useful window is 1 minute - that's the
# poller cadence, so anything narrower returns at most one snapshot
# per (host, key) and the time-series view collapses to a point.
# This function does NOT enforce that floor; sub-minute durations
# are accepted but the README documents the resolution caveat.
_DURATION_RE = re.compile(r"^(?P<num>\d+)(?P<unit>[smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> int:
    """Parse '<int><unit>' into seconds. Units: s, m, h, d.

    Examples: '30s' -> 30, '5m' -> 300, '1h' -> 3600, '2d' -> 172800.

    Raises ValueError on any other shape (no spaces, no fractions,
    no compound expressions like '1h30m'). The CLI surface is small
    enough that the simple form is enough; if a future caller needs
    compound durations, build them in the caller and pass seconds."""
    if not isinstance(s, str):
        raise ValueError(
            f"duration must be a string, got {type(s).__name__}"
        )
    m = _DURATION_RE.match(s.strip().lower())
    if not m:
        raise ValueError(
            f"invalid duration {s!r}: expected '<int><unit>' "
            f"with unit in s/m/h/d (e.g. '5m', '1h', '2d')"
        )
    return int(m.group("num")) * _DURATION_UNITS[m.group("unit")]
