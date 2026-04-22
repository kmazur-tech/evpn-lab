"""Unit tests for drift/timeseries/partition.py.

partition.py is pure: filename parsing + directory walking + duration
parsing. No pyarrow, no pandas, no network. Tests use tmp_path fixtures
and touch only os/pathlib, so they run in a few milliseconds.

What is tested:

  - parse_coalesced_filename: valid sqc-* filenames, raw filenames,
    sidecars, junk, edge cases (period_hours != 1, shard != 0)
  - windows_overlap: half-open semantics including the touching-edge
    boundary that drove the [start, end) decision
  - filter_files_in_window: empty store, only coalesced, only raw,
    both, multiple sqvers dirs, namespace filtering, the file-level
    pre-filter actually drops out-of-window files
  - parse_duration: each supported unit + invalid input
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.timeseries.partition import (  # noqa: E402
    CoalescedFile,
    COALESCED_FILENAME_RE,
    filter_files_in_window,
    parse_coalesced_filename,
    parse_duration,
    windows_overlap,
)


# ---------------------------------------------------------------------------
# parse_coalesced_filename
# ---------------------------------------------------------------------------

class TestParseCoalescedFilename:
    def test_canonical_lab_filename(self, tmp_path):
        # The exact pattern observed on netdevops-srv: sqc-h1-0-<start>-<end>.parquet
        f = tmp_path / "sqc-h1-0-1775584800-1775588400.parquet"
        f.touch()
        cf = parse_coalesced_filename(f)
        assert cf is not None
        assert cf.period_hours == 1
        assert cf.shard == 0
        assert cf.start_epoch == 1775584800
        assert cf.end_epoch == 1775588400
        assert cf.path == f

    def test_window_is_one_hour_in_canonical_pattern(self):
        # 1775588400 - 1775584800 == 3600 seconds == 1 hour. Sanity
        # check that our reference data really is hourly so the rest
        # of the partition logic can rely on it.
        cf = parse_coalesced_filename("sqc-h1-0-1775584800-1775588400.parquet")
        assert cf is not None
        assert cf.end_epoch - cf.start_epoch == 3600

    def test_accepts_period_hours_not_one(self):
        # We don't lock the regex to h1 - if a future suzieq tunes
        # the coalescer to h6 we should still parse the result.
        cf = parse_coalesced_filename("sqc-h6-0-1000-22600.parquet")
        assert cf is not None
        assert cf.period_hours == 6
        assert cf.shard == 0

    def test_accepts_shard_not_zero(self):
        cf = parse_coalesced_filename("sqc-h1-3-1000-4600.parquet")
        assert cf is not None
        assert cf.shard == 3

    def test_returns_none_for_raw_data_file(self):
        # Raw poller filenames don't match - they have UUID-ish names
        # like "ed3f6...parquet" with no sqc- prefix.
        assert parse_coalesced_filename("data-0.parquet") is None
        assert parse_coalesced_filename("ed3f6c2a-91be-4f70-9c12-data.parquet") is None

    def test_returns_none_for_crc_sidecar(self):
        assert parse_coalesced_filename(".sqc-h1-0-100-3700.parquet.crc") is None

    def test_returns_none_for_garbage(self):
        assert parse_coalesced_filename("README.md") is None
        assert parse_coalesced_filename("sqc-h1-0-100.parquet") is None  # missing field
        assert parse_coalesced_filename("sqc-h1-0-100-200-300.parquet") is None  # extra field
        assert parse_coalesced_filename("sqc-hX-0-100-200.parquet") is None  # non-int period

    def test_accepts_string_or_path(self):
        assert parse_coalesced_filename("sqc-h1-0-1-2.parquet") is not None
        assert parse_coalesced_filename(Path("sqc-h1-0-1-2.parquet")) is not None

    def test_returns_path_object_in_dataclass(self):
        cf = parse_coalesced_filename("sqc-h1-0-100-3700.parquet")
        assert isinstance(cf.path, Path)


# ---------------------------------------------------------------------------
# windows_overlap
# ---------------------------------------------------------------------------

class TestWindowsOverlap:
    def test_disjoint_left(self):
        assert windows_overlap(0, 10, 20, 30) is False

    def test_disjoint_right(self):
        assert windows_overlap(20, 30, 0, 10) is False

    def test_touching_does_not_overlap(self):
        # [0, 10) and [10, 20) share NO time. Critical: a query
        # window that exactly matches a coalesced hour [start, end)
        # must read EXACTLY that file, not the adjacent one.
        assert windows_overlap(0, 10, 10, 20) is False
        assert windows_overlap(10, 20, 0, 10) is False

    def test_one_second_overlap(self):
        assert windows_overlap(0, 11, 10, 20) is True

    def test_a_contains_b(self):
        assert windows_overlap(0, 100, 25, 75) is True

    def test_b_contains_a(self):
        assert windows_overlap(25, 75, 0, 100) is True

    def test_identical_windows_overlap(self):
        assert windows_overlap(100, 200, 100, 200) is True

    def test_zero_width_window_does_not_overlap(self):
        # Zero-width windows are degenerate but we handle them
        # consistently with the half-open rule: nothing overlaps
        # an empty interval.
        assert windows_overlap(50, 50, 0, 100) is False
        assert windows_overlap(0, 100, 50, 50) is False


# ---------------------------------------------------------------------------
# filter_files_in_window
# ---------------------------------------------------------------------------

def _touch_coalesced(parquet_dir, table, sqvers, namespace, start, end):
    """Create a coalesced parquet file at the canonical hive path
    with the canonical sqc-* filename. Returns the file path."""
    d = (
        Path(parquet_dir) / "coalesced" / table
        / f"sqvers={sqvers}" / f"namespace={namespace}"
    )
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"sqc-h1-0-{start}-{end}.parquet"
    f.touch()
    return f


def _touch_raw(parquet_dir, table, sqvers, namespace, hostname, name="data-0.parquet"):
    """Create a raw parquet file at the canonical hive path
    (with hostname dim). Returns the file path."""
    d = (
        Path(parquet_dir) / table
        / f"sqvers={sqvers}" / f"namespace={namespace}"
        / f"hostname={hostname}"
    )
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.touch()
    return f


class TestFilterFilesInWindow:
    def test_empty_store_returns_empty_list(self, tmp_path):
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 1_000_000_000)
        assert out == []

    def test_picks_coalesced_files_overlapping_window(self, tmp_path):
        # Three hourly files at 0..3600, 3600..7200, 7200..10800
        f1 = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 0, 3600)
        f2 = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 3600, 7200)
        f3 = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 7200, 10800)

        # Window covering only the middle hour
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 3600, 7200)
        assert out == [f2]

        # Window spanning all three
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 10800)
        assert out == [f1, f2, f3]

        # Window touching but NOT overlapping the first file
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 3600, 4000)
        assert out == [f2]

    def test_filters_out_files_strictly_before_window(self, tmp_path):
        _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 0, 3600)
        f2 = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 7200, 10800)
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 5000, 100_000)
        assert out == [f2]

    def test_filters_out_files_strictly_after_window(self, tmp_path):
        f1 = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 0, 3600)
        _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 7200, 10800)
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 5000)
        assert out == [f1]

    def test_namespace_filter_excludes_other_namespaces(self, tmp_path):
        _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 0, 3600)
        _touch_coalesced(tmp_path, "bgp", "3.0", "dc2", 0, 3600)
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 3600)
        assert len(out) == 1
        assert "namespace=dc1" in str(out[0])

    def test_walks_multiple_sqvers_directories(self, tmp_path):
        # SuzieQ does versioned schemas. We should pick up files
        # from any sqvers=* dir under the table, not just one.
        f1 = _touch_coalesced(tmp_path, "bgp", "2.0", "dc1", 0, 3600)
        f2 = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 3600, 7200)
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 7200)
        assert set(out) == {f1, f2}

    def test_includes_raw_files_unconditionally(self, tmp_path):
        # Raw files cannot be pre-filtered by name - they have no
        # window encoded. We must include them always so the
        # row-level filter in reader.py can do its job.
        f = _touch_raw(tmp_path, "bgp", "3.0", "dc1", "dc1-leaf1")
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 100)
        assert f in out

    def test_includes_raw_for_all_hostnames(self, tmp_path):
        # Raw is per-host. A four-device fabric produces four
        # parallel hostname=*/ subdirs. We should walk all of them.
        files = [
            _touch_raw(tmp_path, "bgp", "3.0", "dc1", h)
            for h in ("dc1-spine1", "dc1-spine2", "dc1-leaf1", "dc1-leaf2")
        ]
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 100)
        for f in files:
            assert f in out

    def test_combines_coalesced_and_raw(self, tmp_path):
        coalesced = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 0, 3600)
        raw = _touch_raw(tmp_path, "bgp", "3.0", "dc1", "dc1-leaf1")
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 3600)
        assert coalesced in out
        assert raw in out

    def test_coalesced_listed_before_raw_for_deterministic_order(self, tmp_path):
        # Convention: coalesced first, then raw. Reader.py concats
        # in the returned order so reproducibility matters.
        coalesced = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 0, 3600)
        raw = _touch_raw(tmp_path, "bgp", "3.0", "dc1", "dc1-leaf1")
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 3600)
        assert out.index(coalesced) < out.index(raw)

    def test_raw_files_with_non_parquet_suffix_ignored(self, tmp_path):
        # Hidden _SUCCESS markers, .crc sidecars, etc. should not
        # leak into the file list.
        d = (
            Path(tmp_path) / "bgp" / "sqvers=3.0"
            / "namespace=dc1" / "hostname=dc1-leaf1"
        )
        d.mkdir(parents=True)
        (d / "_SUCCESS").touch()
        (d / ".data-0.parquet.crc").touch()
        good = d / "data-0.parquet"
        good.touch()
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 100)
        assert out == [good]

    def test_ignores_nonparquet_files_in_coalesced_dir(self, tmp_path):
        # The coalescer can park archive tarballs alongside parquet
        # files (in /suzieq/archive). The walker should not crash
        # on them - just skip anything that isn't sqc-*.parquet.
        good = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 0, 3600)
        d = (
            Path(tmp_path) / "coalesced" / "bgp"
            / "sqvers=3.0" / "namespace=dc1"
        )
        (d / "_archive-2026-04-11.tar.bz2").touch()
        (d / "README.md").touch()
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 3600)
        assert out == [good]

    def test_only_coalesced_when_raw_dir_missing(self, tmp_path):
        # Coalescer just ran - raw is empty. We should still see
        # the coalesced file.
        f = _touch_coalesced(tmp_path, "bgp", "3.0", "dc1", 0, 3600)
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 3600)
        assert out == [f]

    def test_only_raw_when_coalesced_dir_missing(self, tmp_path):
        # First-cycle state: poller has written rows but coalescer
        # hasn't run yet, so coalesced/ does not exist.
        f = _touch_raw(tmp_path, "bgp", "3.0", "dc1", "dc1-leaf1")
        out = filter_files_in_window(tmp_path, "bgp", "dc1", 0, 100)
        assert out == [f]

    def test_rejects_inverted_window(self, tmp_path):
        with pytest.raises(ValueError, match="start_epoch.*end_epoch"):
            filter_files_in_window(tmp_path, "bgp", "dc1", 100, 50)


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------

class TestParseDuration:
    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("1s", 1),
            ("30s", 30),
            ("1m", 60),
            ("5m", 300),
            ("1h", 3600),
            ("24h", 86400),
            ("1d", 86400),
            ("7d", 604800),
        ],
    )
    def test_valid_durations(self, spec, expected):
        assert parse_duration(spec) == expected

    def test_strips_whitespace(self):
        assert parse_duration("  1h  ") == 3600

    def test_case_insensitive(self):
        assert parse_duration("1H") == 3600
        assert parse_duration("5M") == 300

    @pytest.mark.parametrize(
        "spec",
        [
            "",
            "1",       # missing unit
            "h",       # missing number
            "1.5h",    # no fractions
            "1h30m",   # no compounds
            "1 h",     # no internal whitespace
            "-1h",     # no negatives
            "1y",      # year not supported
            "1ms",     # milliseconds not supported
            "abc",
        ],
    )
    def test_invalid_durations_raise_value_error(self, spec):
        with pytest.raises(ValueError):
            parse_duration(spec)

    def test_non_string_raises_value_error(self):
        with pytest.raises(ValueError, match="must be a string"):
            parse_duration(3600)
        with pytest.raises(ValueError, match="must be a string"):
            parse_duration(None)


# ---------------------------------------------------------------------------
# COALESCED_FILENAME_RE - regression guard
# ---------------------------------------------------------------------------

def test_filename_regex_matches_actual_lab_files():
    """The exact filenames captured from netdevops-srv on 2026-04-11
    must parse cleanly. If a future suzieq changes the naming
    convention, this regression guard fails loudly."""
    samples = [
        "sqc-h1-0-1775584800-1775588400.parquet",  # bgp coalesced (Apr 7 18:00)
        "sqc-h1-0-1775890800-1775894400.parquet",  # bgp coalesced (Apr 11 07:00)
        "sqc-h1-0-1775905200-1775908800.parquet",  # bgp coalesced (Apr 11 11:00)
        "sqc-h1-0-1775588400-1775592000.parquet",  # sqPoller coalesced
        "sqc-h1-0-1775592000-1775595600.parquet",  # evpnVni coalesced
    ]
    for s in samples:
        assert COALESCED_FILENAME_RE.match(s), f"failed to parse {s}"
