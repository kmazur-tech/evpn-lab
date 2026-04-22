"""Unit tests for drift/timeseries/reader.py.

reader.py is the only Part D module that imports pyarrow. We test
it the same way drift/state.py is tested: write tiny real parquet
files into a tmp_path fixture and assert window_read() returns the
right WindowedTable shape. Hermetic, no SuzieQ container, ~50 ms
per test.

What is tested:

  - Empty store returns an empty WindowedTable with the right
    metadata (table, namespace, window) - NOT an error.
  - WindowedTable / TimeWindow dataclass shape and properties.
  - Coalesced file is read and rows below the window are filtered
    out by the row-level timestamp check (the file covers a 1-hour
    window but the query asks for 5 minutes inside it).
  - Raw file with hostname-only-in-path has the hostname column
    backfilled by reader._read_one_file's partition regex.
  - Multiple files (coalesced + raw) merge cleanly.
  - ts_sec column is added and equals timestamp // 1000.
  - files_read counter distinguishes "no data" from "had files
    but everything was filtered" - the envelope uses this.
  - Inverted window propagates the partition.py ValueError.

What is NOT tested here:
  - Live read against the real /suzieq/parquet on netdevops-srv
    (that's the integration smoke run at the end of Part D).
  - Query layer behavior - that lives in test_timeseries_queries_*.
"""
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.timeseries.reader import (  # noqa: E402
    DEFAULT_PARQUET_DIR,
    TimeWindow,
    WindowedTable,
    window_read,
)


# ---------------------------------------------------------------------------
# helpers - write parquet files in the canonical SuzieQ shape
# ---------------------------------------------------------------------------

def _write_coalesced(parquet_dir, table, namespace, start, end, df, sqvers="3.0"):
    """Write a coalesced parquet file at the canonical hive path
    with the canonical sqc-h1-0-<start>-<end>.parquet filename.

    Coalesced files in SuzieQ carry `hostname` as a real column
    inside the parquet (the coalescer merges per-host raw files
    into one per-namespace file before writing). They do NOT have
    `namespace` inside - that comes from the path."""
    d = (
        Path(parquet_dir) / "coalesced" / table
        / f"sqvers={sqvers}" / f"namespace={namespace}"
    )
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"sqc-h1-0-{start}-{end}.parquet"
    pq.write_table(pa.Table.from_pandas(df), str(f))
    return f


def _write_raw(parquet_dir, table, namespace, hostname, df, sqvers="3.0",
               name="data-0.parquet"):
    """Write a raw parquet file at the per-host hive path. Raw
    files have hostname ONLY in the directory tree, not as a
    column in the parquet itself - the helper drops the column
    if the input df carries it (matching how the live poller
    writes them)."""
    df = df.drop(columns=[c for c in ("hostname",) if c in df.columns])
    d = (
        Path(parquet_dir) / table / f"sqvers={sqvers}"
        / f"namespace={namespace}" / f"hostname={hostname}"
    )
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    pq.write_table(pa.Table.from_pandas(df), str(f))
    return f


def _bgp_row(hostname, peer, ts_ms, state="Established", pfxRx=10, vrf="default",
             afi="ipv4", safi="unicast"):
    """One believable bgp row. The minimal column set the queries
    will use - hostname, peer, vrf, afi, safi, state, pfxRx,
    timestamp."""
    return {
        "hostname": hostname,
        "peer": peer,
        "vrf": vrf,
        "afi": afi,
        "safi": safi,
        "state": state,
        "pfxRx": pfxRx,
        "numChanges": 0,
        "timestamp": ts_ms,
        "active": True,
    }


# ---------------------------------------------------------------------------
# TimeWindow / WindowedTable dataclass shape
# ---------------------------------------------------------------------------

class TestTimeWindow:
    def test_duration_seconds(self):
        w = TimeWindow(start_epoch=100, end_epoch=160)
        assert w.duration_seconds == 60

    def test_duration_zero_when_start_equals_end(self):
        w = TimeWindow(start_epoch=100, end_epoch=100)
        assert w.duration_seconds == 0

    def test_duration_clamps_to_zero_when_inverted(self):
        # We don't want negative durations leaking into envelope JSON
        # if a caller somehow constructs an inverted window directly
        # (window_read itself rejects them via partition.py).
        w = TimeWindow(start_epoch=200, end_epoch=100)
        assert w.duration_seconds == 0

    def test_is_frozen(self):
        w = TimeWindow(start_epoch=0, end_epoch=10)
        with pytest.raises((AttributeError, TypeError)):
            w.start_epoch = 5  # type: ignore[misc]


class TestWindowedTable:
    def test_is_empty_true_for_empty_dataframe(self):
        wt = WindowedTable(
            table="bgp", namespace="dc1",
            window=TimeWindow(0, 100),
        )
        assert wt.is_empty is True
        assert wt.files_read == 0

    def test_is_empty_false_for_populated_dataframe(self):
        wt = WindowedTable(
            table="bgp", namespace="dc1",
            window=TimeWindow(0, 100),
            rows=pd.DataFrame([{"a": 1}]),
            files_read=1,
        )
        assert wt.is_empty is False


# ---------------------------------------------------------------------------
# window_read - empty store
# ---------------------------------------------------------------------------

class TestWindowReadEmpty:
    def test_empty_store_returns_empty_windowed_table(self, tmp_path):
        wt = window_read("bgp", "dc1", 0, 1000, parquet_dir=tmp_path)
        assert wt.is_empty
        assert wt.table == "bgp"
        assert wt.namespace == "dc1"
        assert wt.window == TimeWindow(0, 1000)
        assert wt.files_read == 0

    def test_table_dir_exists_but_empty(self, tmp_path):
        # Coalescer ran, table dir exists, but no files in the
        # current sqvers/namespace.
        (tmp_path / "coalesced" / "bgp" / "sqvers=3.0" / "namespace=dc1").mkdir(parents=True)
        wt = window_read("bgp", "dc1", 0, 1000, parquet_dir=tmp_path)
        assert wt.is_empty
        assert wt.files_read == 0


# ---------------------------------------------------------------------------
# window_read - coalesced files
# ---------------------------------------------------------------------------

class TestWindowReadCoalesced:
    def test_reads_one_coalesced_file(self, tmp_path):
        df = pd.DataFrame([
            _bgp_row("dc1-leaf1", "10.0.0.2", ts_ms=1500 * 1000),
            _bgp_row("dc1-leaf1", "10.0.0.3", ts_ms=1700 * 1000),
        ])
        _write_coalesced(tmp_path, "bgp", "dc1", 1000, 4600, df)

        wt = window_read("bgp", "dc1", 1000, 4600, parquet_dir=tmp_path)
        assert wt.files_read == 1
        assert len(wt.rows) == 2
        assert set(wt.rows["peer"]) == {"10.0.0.2", "10.0.0.3"}

    def test_filters_rows_below_window(self, tmp_path):
        # Coalesced file covers [1000, 4600). Query asks for
        # [3000, 4000) - rows at ts<3000 and ts>=4000 must be dropped.
        df = pd.DataFrame([
            _bgp_row("dc1-leaf1", "10.0.0.2", ts_ms=1500 * 1000),  # below
            _bgp_row("dc1-leaf1", "10.0.0.3", ts_ms=3500 * 1000),  # in
            _bgp_row("dc1-leaf1", "10.0.0.4", ts_ms=4500 * 1000),  # above
        ])
        _write_coalesced(tmp_path, "bgp", "dc1", 1000, 4600, df)

        wt = window_read("bgp", "dc1", 3000, 4000, parquet_dir=tmp_path)
        assert wt.files_read == 1  # file was opened
        assert len(wt.rows) == 1   # only one row survived
        assert wt.rows.iloc[0]["peer"] == "10.0.0.3"

    def test_files_read_distinguishes_no_data_from_filtered(self, tmp_path):
        # The envelope uses files_read > 0 with empty rows to mean
        # "the window had files but every row was filtered" -
        # different from "no files at all".
        df = pd.DataFrame([
            _bgp_row("dc1-leaf1", "10.0.0.2", ts_ms=1500 * 1000),
        ])
        _write_coalesced(tmp_path, "bgp", "dc1", 1000, 4600, df)

        # Window inside the coalesced file's range but row is outside.
        wt = window_read("bgp", "dc1", 2000, 3000, parquet_dir=tmp_path)
        assert wt.files_read == 1
        assert wt.is_empty

    def test_adds_ts_sec_column(self, tmp_path):
        df = pd.DataFrame([
            _bgp_row("dc1-leaf1", "10.0.0.2", ts_ms=1500_000),
            _bgp_row("dc1-leaf1", "10.0.0.3", ts_ms=1500_500),
        ])
        _write_coalesced(tmp_path, "bgp", "dc1", 1000, 4600, df)

        wt = window_read("bgp", "dc1", 1000, 4600, parquet_dir=tmp_path)
        assert "ts_sec" in wt.rows.columns
        # 1500_000 ms == 1500 sec, 1500_500 ms == 1500 sec (truncated)
        assert wt.rows["ts_sec"].tolist() == [1500, 1500]

    def test_multiple_coalesced_files_merge(self, tmp_path):
        df1 = pd.DataFrame([_bgp_row("dc1-leaf1", "10.0.0.2", 1500 * 1000)])
        df2 = pd.DataFrame([_bgp_row("dc1-leaf1", "10.0.0.3", 5500 * 1000)])
        _write_coalesced(tmp_path, "bgp", "dc1", 1000, 4600, df1)
        _write_coalesced(tmp_path, "bgp", "dc1", 4600, 8200, df2)

        wt = window_read("bgp", "dc1", 1000, 8200, parquet_dir=tmp_path)
        assert wt.files_read == 2
        assert len(wt.rows) == 2
        assert set(wt.rows["peer"]) == {"10.0.0.2", "10.0.0.3"}

    def test_namespace_filter_via_path_no_other_namespace_data_leaks(self, tmp_path):
        df1 = pd.DataFrame([_bgp_row("dc1-leaf1", "10.0.0.2", 1500 * 1000)])
        df2 = pd.DataFrame([_bgp_row("dc2-leaf1", "10.1.0.2", 1500 * 1000)])
        _write_coalesced(tmp_path, "bgp", "dc1", 1000, 4600, df1)
        _write_coalesced(tmp_path, "bgp", "dc2", 1000, 4600, df2)

        wt = window_read("bgp", "dc1", 1000, 4600, parquet_dir=tmp_path)
        assert len(wt.rows) == 1
        assert wt.rows.iloc[0]["peer"] == "10.0.0.2"


# ---------------------------------------------------------------------------
# window_read - raw files (hostname only in path)
# ---------------------------------------------------------------------------

class TestWindowReadRaw:
    def test_raw_file_hostname_backfilled_from_path(self, tmp_path):
        # Raw poller files don't carry `hostname` as a column inside
        # the parquet - it's only in the directory name. reader.py
        # has to inject it back so queries can group by host.
        df = pd.DataFrame([_bgp_row("ignored", "10.0.0.2", 1500 * 1000)])
        # Drop hostname from the dict before writing - the helper
        # also drops it but we double-down to be explicit.
        df = df.drop(columns=["hostname"])
        d = (
            tmp_path / "bgp" / "sqvers=3.0"
            / "namespace=dc1" / "hostname=dc1-leaf1"
        )
        d.mkdir(parents=True)
        pq.write_table(pa.Table.from_pandas(df), str(d / "data-0.parquet"))

        wt = window_read("bgp", "dc1", 1000, 4600, parquet_dir=tmp_path)
        assert len(wt.rows) == 1
        assert wt.rows.iloc[0]["hostname"] == "dc1-leaf1"

    def test_raw_files_for_multiple_hosts_kept_distinct(self, tmp_path):
        for host in ("dc1-leaf1", "dc1-leaf2", "dc1-spine1"):
            df = pd.DataFrame([_bgp_row("ignored", "10.0.0.2", 1500 * 1000)])
            _write_raw(tmp_path, "bgp", "dc1", host, df)

        wt = window_read("bgp", "dc1", 1000, 4600, parquet_dir=tmp_path)
        assert len(wt.rows) == 3
        assert set(wt.rows["hostname"]) == {"dc1-leaf1", "dc1-leaf2", "dc1-spine1"}


# ---------------------------------------------------------------------------
# window_read - mixed coalesced + raw
# ---------------------------------------------------------------------------

class TestWindowReadMixed:
    def test_coalesced_and_raw_merge(self, tmp_path):
        coalesced_df = pd.DataFrame([
            _bgp_row("dc1-leaf1", "10.0.0.2", 1500 * 1000),
        ])
        raw_df = pd.DataFrame([
            _bgp_row("ignored", "10.0.0.3", 2500 * 1000),
        ])
        _write_coalesced(tmp_path, "bgp", "dc1", 1000, 4600, coalesced_df)
        _write_raw(tmp_path, "bgp", "dc1", "dc1-leaf2", raw_df)

        wt = window_read("bgp", "dc1", 1000, 4600, parquet_dir=tmp_path)
        assert wt.files_read == 2
        assert len(wt.rows) == 2
        # Coalesced row keeps its baked-in hostname; raw row gets
        # the hostname injected from the partition path.
        peer_to_host = dict(zip(wt.rows["peer"], wt.rows["hostname"]))
        assert peer_to_host["10.0.0.2"] == "dc1-leaf1"
        assert peer_to_host["10.0.0.3"] == "dc1-leaf2"

    def test_window_filter_drops_raw_rows_outside_window(self, tmp_path):
        # Raw files are included unconditionally by partition.py;
        # the row-level filter in reader.py is what excludes their
        # out-of-window rows.
        raw_df = pd.DataFrame([
            _bgp_row("ignored", "10.0.0.2", 500 * 1000),   # below window
            _bgp_row("ignored", "10.0.0.3", 1500 * 1000),  # in window
            _bgp_row("ignored", "10.0.0.4", 9000 * 1000),  # above window
        ])
        _write_raw(tmp_path, "bgp", "dc1", "dc1-leaf1", raw_df)

        wt = window_read("bgp", "dc1", 1000, 4600, parquet_dir=tmp_path)
        assert len(wt.rows) == 1
        assert wt.rows.iloc[0]["peer"] == "10.0.0.3"


# ---------------------------------------------------------------------------
# defensive shape
# ---------------------------------------------------------------------------

class TestWindowReadDefensive:
    def test_inverted_window_raises(self, tmp_path):
        with pytest.raises(ValueError):
            window_read("bgp", "dc1", 100, 50, parquet_dir=tmp_path)

    def test_default_parquet_dir_constant_matches_state_module(self):
        # Coordination between drift/state.py and drift/timeseries/
        # reader.py - they read from the same docker volume mount.
        # If one drifts the harness ends up reading from two different
        # places.
        from drift.state import DEFAULT_PARQUET_DIR as STATE_DEFAULT
        assert DEFAULT_PARQUET_DIR == STATE_DEFAULT
