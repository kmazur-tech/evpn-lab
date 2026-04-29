"""Unit tests for drift/timeseries/envelope.py.

The envelope is the JSON contract Phase 6 will consume. Tests pin
the shape so a future refactor doesn't silently rename fields.

Coverage:
  - build_envelope: structural shape, namespace propagation,
    window encoding (epoch + ISO), per-query records, files_read
    propagation, empty queries list
  - emit_json: parses back, no NaN/numpy leakage
  - emit_human: writes a header + per-query summary line
  - scalar coercion: NaN -> None, numpy types -> python
"""
import io
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.timeseries.envelope import (  # noqa: E402
    build_envelope,
    emit_human,
    emit_json,
    self_check,
    HEARTBEAT_TABLE,
    _coerce_scalar,
    _df_to_records,
    _STALE_THRESHOLD_SEC,
)
from drift.timeseries.queries import TimeseriesResult  # noqa: E402
from drift.timeseries.reader import TimeWindow, WindowedTable  # noqa: E402


# ---------------------------------------------------------------------------
# _coerce_scalar
# ---------------------------------------------------------------------------

class TestCoerceScalar:
    def test_python_native_types_pass_through(self):
        assert _coerce_scalar("hello") == "hello"
        assert _coerce_scalar(42) == 42
        assert _coerce_scalar(3.14) == 3.14
        assert _coerce_scalar(True) is True
        assert _coerce_scalar(None) is None

    def test_numpy_int_coerced_to_python_int(self):
        out = _coerce_scalar(np.int64(7))
        assert isinstance(out, int)
        assert out == 7

    def test_numpy_float_coerced_to_python_float(self):
        out = _coerce_scalar(np.float64(2.5))
        assert isinstance(out, float)
        assert out == 2.5

    def test_nan_coerced_to_none(self):
        assert _coerce_scalar(float("nan")) is None
        assert _coerce_scalar(np.nan) is None

    def test_pandas_na_coerced_to_none(self):
        assert _coerce_scalar(pd.NA) is None


# ---------------------------------------------------------------------------
# _df_to_records
# ---------------------------------------------------------------------------

class TestDfToRecords:
    def test_empty_dataframe_returns_empty_list(self):
        assert _df_to_records(pd.DataFrame()) == []

    def test_simple_dataframe(self):
        df = pd.DataFrame([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
        out = _df_to_records(df)
        assert out == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]

    def test_numpy_scalars_become_python(self):
        df = pd.DataFrame([{"count": np.int64(5), "ratio": np.float64(0.5)}])
        out = _df_to_records(df)
        assert isinstance(out[0]["count"], int)
        assert isinstance(out[0]["ratio"], float)

    def test_nan_becomes_none(self):
        df = pd.DataFrame([{"a": 1, "b": float("nan")}])
        out = _df_to_records(df)
        assert out[0]["b"] is None


# ---------------------------------------------------------------------------
# build_envelope
# ---------------------------------------------------------------------------

class TestBuildEnvelope:
    def test_minimal_empty_envelope(self):
        env = build_envelope(
            namespace="dc1",
            window=TimeWindow(0, 3600),
            results=[],
            files_read_by_table={},
        )
        assert env["namespace"] == "dc1"
        assert env["window"]["start_epoch"] == 0
        assert env["window"]["end_epoch"] == 3600
        assert env["window"]["duration_seconds"] == 3600
        assert env["queries"] == []
        assert "generated_at" in env

    def test_window_iso_strings_present(self):
        env = build_envelope(
            namespace="dc1",
            window=TimeWindow(1775904896, 1775908496),
            results=[],
            files_read_by_table={},
        )
        assert "start_iso" in env["window"]
        assert "end_iso" in env["window"]
        assert env["window"]["start_iso"].startswith("2026-04-")
        assert env["window"]["end_iso"].startswith("2026-04-")

    def test_one_query_in_envelope(self):
        result = TimeseriesResult(
            name="bgp_flaps", table="bgp",
            window=TimeWindow(0, 3600),
            rows=pd.DataFrame([
                {"hostname": "dc1-leaf1", "peer": "10.0.0.2", "flap_count": 2},
            ]),
            summary={"total_flaps": 2, "sessions_with_flaps": 1, "sessions_seen": 4},
        )
        env = build_envelope(
            namespace="dc1",
            window=TimeWindow(0, 3600),
            results=[result],
            files_read_by_table={"bgp": 1},
        )
        assert len(env["queries"]) == 1
        q = env["queries"][0]
        assert q["name"] == "bgp_flaps"
        assert q["table"] == "bgp"
        assert q["files_read"] == 1
        assert q["summary"]["total_flaps"] == 2
        assert len(q["rows"]) == 1
        assert q["rows"][0]["peer"] == "10.0.0.2"

    def test_files_read_defaults_to_zero_when_table_missing(self):
        result = TimeseriesResult(
            name="bgp_flaps", table="bgp",
            window=TimeWindow(0, 3600),
            rows=pd.DataFrame(),
            summary={},
        )
        env = build_envelope(
            namespace="dc1",
            window=TimeWindow(0, 3600),
            results=[result],
            files_read_by_table={},  # nothing for bgp
        )
        assert env["queries"][0]["files_read"] == 0

    def test_query_order_preserved(self):
        # Stable iteration is the contract.
        results = [
            TimeseriesResult("bgp_flaps", "bgp", TimeWindow(0, 1), pd.DataFrame(), {}),
            TimeseriesResult("route_churn", "routes", TimeWindow(0, 1), pd.DataFrame(), {}),
            TimeseriesResult("mac_mobility", "macs", TimeWindow(0, 1), pd.DataFrame(), {}),
        ]
        env = build_envelope("dc1", TimeWindow(0, 1), results, {})
        names = [q["name"] for q in env["queries"]]
        assert names == ["bgp_flaps", "route_churn", "mac_mobility"]


# ---------------------------------------------------------------------------
# emit_json
# ---------------------------------------------------------------------------

class TestEmitJson:
    def test_round_trip_through_json_loads(self):
        result = TimeseriesResult(
            name="bgp_flaps", table="bgp",
            window=TimeWindow(0, 3600),
            rows=pd.DataFrame([
                {"hostname": "dc1-leaf1", "flap_count": np.int64(3)},
            ]),
            summary={"total_flaps": 3},
        )
        env = build_envelope("dc1", TimeWindow(0, 3600), [result], {"bgp": 1})

        buf = io.StringIO()
        emit_json(env, stream=buf)
        parsed = json.loads(buf.getvalue())
        assert parsed["namespace"] == "dc1"
        assert parsed["queries"][0]["rows"][0]["flap_count"] == 3

    def test_no_nan_in_output(self):
        # Ensures NaN coercion happened before serialization
        df = pd.DataFrame([{"a": 1.0, "b": float("nan")}])
        result = TimeseriesResult(
            name="x", table="bgp", window=TimeWindow(0, 1),
            rows=df, summary={},
        )
        env = build_envelope("dc1", TimeWindow(0, 1), [result], {})
        buf = io.StringIO()
        emit_json(env, stream=buf)
        text = buf.getvalue()
        assert "NaN" not in text
        # Confirm the field is null in the parsed output
        parsed = json.loads(text)
        assert parsed["queries"][0]["rows"][0]["b"] is None


# ---------------------------------------------------------------------------
# emit_human
# ---------------------------------------------------------------------------

class TestEmitHuman:
    def test_writes_header_and_query_lines(self):
        result = TimeseriesResult(
            name="bgp_flaps", table="bgp",
            window=TimeWindow(0, 3600),
            rows=pd.DataFrame([
                {"hostname": "dc1-leaf1", "peer": "10.0.0.2", "flap_count": 2},
            ]),
            summary={"total_flaps": 2, "sessions_with_flaps": 1},
        )
        env = build_envelope("dc1", TimeWindow(0, 3600), [result], {"bgp": 1})
        buf = io.StringIO()
        emit_human(env, stream=buf)
        text = buf.getvalue()
        assert "namespace=dc1" in text
        assert "bgp_flaps" in text
        assert "total_flaps=2" in text
        assert "files=1" in text or "files=  1" in text
        assert "10.0.0.2" in text

    def test_truncates_long_row_lists(self):
        rows = pd.DataFrame([
            {"hostname": f"dc1-leaf{i}"} for i in range(10)
        ])
        result = TimeseriesResult(
            name="bgp_flaps", table="bgp",
            window=TimeWindow(0, 3600),
            rows=rows,
            summary={"total_flaps": 10},
        )
        env = build_envelope("dc1", TimeWindow(0, 3600), [result], {"bgp": 1})
        buf = io.StringIO()
        emit_human(env, stream=buf)
        text = buf.getvalue()
        # First 5 rows shown, then "... (5 more)"
        assert "(5 more)" in text

    def test_empty_envelope_still_emits_header(self):
        env = build_envelope("dc1", TimeWindow(0, 60), [], {})
        buf = io.StringIO()
        emit_human(env, stream=buf)
        text = buf.getvalue()
        assert "namespace=dc1" in text


# ---------------------------------------------------------------------------
# self_check / status / warnings (review-finding fix)
# ---------------------------------------------------------------------------
#
# The self-check uses sqPoller as a heartbeat. An earlier version
# flagged every query table's files_read + row freshness and
# false-positived on stable fabrics: sparse tables like bgp/macs/
# routes legitimately go hours between writes. sqPoller is the one
# table SuzieQ writes unconditionally every poll cycle, so it is
# the right heartbeat source.
#
# Rules covered by these tests:
#   1. Per-query `warning` field (missing columns) -> propagated
#      regardless of live/historical window.
#   2. Live window + sqPoller heartbeat shows the poller is alive.
#      Historical windows skip the heartbeat check entirely.
#   3. Exit code stays 0 - per ADR-11, timeseries observations are
#      never pass/fail. Status is purely informational.


def _live_window(now):
    """A window whose end is at `now` - counts as live."""
    return TimeWindow(now - 3600, now)


def _historical_window():
    """A window that ended long ago - NOT live."""
    return TimeWindow(1000, 4600)


def _bgp_result(window, summary=None):
    return TimeseriesResult(
        name="bgp_flaps", table="bgp", window=window,
        rows=pd.DataFrame(),
        summary=summary or {"total_flaps": 0},
    )


def _healthy_heartbeat(window, now):
    """sqPoller WindowedTable with a fresh row at (now - 30s)."""
    return WindowedTable(
        table="sqPoller", namespace="dc1", window=window,
        rows=pd.DataFrame([
            {"hostname": "dc1-leaf1", "service": "device",
             "timestamp": (now - 30) * 1000},
        ]),
        files_read=1,
    )


class TestSelfCheckOk:
    def test_clean_run_with_heartbeat_returns_ok(self):
        now = 10000
        window = _live_window(now)
        results = [_bgp_result(window)]
        files = {"bgp": 3, "sqPoller": 1}
        wt = {"sqPoller": _healthy_heartbeat(window, now)}
        status, warnings = self_check(results, files, windowed_tables=wt, now=now)
        assert status == "ok"
        assert warnings == []

    def test_ok_on_historical_window_heartbeat_skipped(self):
        """Historical query windows skip the heartbeat check
        entirely because old data is expected to look old."""
        results = [_bgp_result(_historical_window())]
        status, warnings = self_check(
            results, {"bgp": 3, "sqPoller": 0},
            windowed_tables={"sqPoller": WindowedTable(
                table="sqPoller", namespace="dc1",
                window=_historical_window(),
                rows=pd.DataFrame(), files_read=0,
            )},
            now=99999,
        )
        assert status == "ok"
        assert warnings == []

    def test_ok_when_windowed_tables_is_none(self):
        """Passing None explicitly opts out of the heartbeat
        check. Used by tests / callers that don't care about
        liveness."""
        results = [_bgp_result(_live_window(10000))]
        status, warnings = self_check(
            results, {"bgp": 3}, windowed_tables=None, now=10000,
        )
        assert status == "ok"
        assert warnings == []

    def test_sparse_table_old_rows_do_not_trigger_false_positive(self):
        """Regression guard for the live-test false positive:
        bgp/macs/routes can legitimately have rows 8 hours old
        when the fabric is stable. The heartbeat model does NOT
        inspect query-table row timestamps, so this MUST NOT
        produce a warning."""
        now = 10000
        window = _live_window(now)
        # Stale bgp row, but sqPoller heartbeat is fresh
        bgp_wt = WindowedTable(
            table="bgp", namespace="dc1", window=window,
            rows=pd.DataFrame([{"timestamp": 100 * 1000}]),  # ~8h old
            files_read=2,
        )
        wt = {
            "bgp": bgp_wt,
            "sqPoller": _healthy_heartbeat(window, now),
        }
        status, warnings = self_check(
            [_bgp_result(window)], {"bgp": 2, "sqPoller": 1},
            windowed_tables=wt, now=now,
        )
        assert status == "ok", f"false positive on sparse table: {warnings}"


class TestSelfCheckDegraded:
    def test_per_query_warning_propagated(self):
        """Shape warnings (missing columns) surface regardless
        of live/historical."""
        now = 10000
        window = _live_window(now)
        results = [_bgp_result(
            window,
            summary={"total_flaps": 0, "warning": "missing bgp columns"},
        )]
        wt = {"sqPoller": _healthy_heartbeat(window, now)}
        status, warnings = self_check(
            results, {"bgp": 3, "sqPoller": 1},
            windowed_tables=wt, now=now,
        )
        assert status == "degraded"
        assert any("missing bgp columns" in w for w in warnings)

    def test_heartbeat_missing_is_degraded(self):
        """Live window, windowed_tables is not None, but has no
        sqPoller entry. The self-check cannot verify liveness
        and must say so."""
        now = 10000
        window = _live_window(now)
        results = [_bgp_result(window)]
        # windowed_tables without the sqPoller heartbeat
        wt = {"bgp": WindowedTable(
            table="bgp", namespace="dc1", window=window,
            rows=pd.DataFrame(), files_read=1,
        )}
        status, warnings = self_check(
            results, {"bgp": 1}, windowed_tables=wt, now=now,
        )
        assert status == "degraded"
        assert any("heartbeat not provided" in w for w in warnings)

    def test_heartbeat_zero_files_is_degraded(self):
        """sqPoller table exists but files_read == 0. Means the
        poller never wrote a heartbeat in the window."""
        now = 10000
        window = _live_window(now)
        wt = {"sqPoller": WindowedTable(
            table="sqPoller", namespace="dc1", window=window,
            rows=pd.DataFrame(), files_read=0,
        )}
        status, warnings = self_check(
            [_bgp_result(window)], {"sqPoller": 0},
            windowed_tables=wt, now=now,
        )
        assert status == "degraded"
        assert any("0 files read" in w and "sqPoller" in w for w in warnings)

    def test_heartbeat_empty_rows_is_degraded(self):
        """Files present but every row filtered out by the
        time-window pass - poller might be writing to a window
        we're not looking at."""
        now = 10000
        window = _live_window(now)
        wt = {"sqPoller": WindowedTable(
            table="sqPoller", namespace="dc1", window=window,
            rows=pd.DataFrame(), files_read=2,
        )}
        status, warnings = self_check(
            [_bgp_result(window)], {"sqPoller": 2},
            windowed_tables=wt, now=now,
        )
        assert status == "degraded"
        assert any("files present but no rows" in w for w in warnings)

    def test_heartbeat_stale_row_is_degraded(self):
        """sqPoller row exists but latest is > 120 s old at
        window end. Poller is stuck."""
        now = 10000
        window = _live_window(now)
        wt = {"sqPoller": WindowedTable(
            table="sqPoller", namespace="dc1", window=window,
            # latest row at (now - 500s), exceeds 120s threshold
            rows=pd.DataFrame([
                {"hostname": "dc1-leaf1", "service": "device",
                 "timestamp": (now - 500) * 1000},
            ]),
            files_read=1,
        )}
        status, warnings = self_check(
            [_bgp_result(window)], {"sqPoller": 1},
            windowed_tables=wt, now=now,
        )
        assert status == "degraded"
        assert any(
            "latest row" in w and "sqPoller" in w and "500s old" in w
            for w in warnings
        )

    def test_heartbeat_fresh_row_just_under_threshold_is_ok(self):
        """119 s of age is under the 120 s threshold. Just barely
        ok. This is the strict boundary test."""
        now = 10000
        window = _live_window(now)
        wt = {"sqPoller": WindowedTable(
            table="sqPoller", namespace="dc1", window=window,
            rows=pd.DataFrame([
                {"hostname": "dc1-leaf1", "service": "device",
                 "timestamp": (now - 119) * 1000},
            ]),
            files_read=1,
        )}
        status, warnings = self_check(
            [_bgp_result(window)], {"sqPoller": 1},
            windowed_tables=wt, now=now,
        )
        assert status == "ok"
        assert warnings == []


class TestBuildEnvelopeSelfCheck:
    def test_envelope_has_status_and_warnings_fields(self):
        window = TimeWindow(0, 3600)
        env = build_envelope("dc1", window, [], {})
        assert "status" in env
        assert "warnings" in env
        assert env["status"] == "ok"  # empty results is a clean run
        assert env["warnings"] == []

    def test_envelope_status_degraded_propagates(self):
        """End-to-end: a query with a shape warning must surface
        as a degraded envelope."""
        now = 10000
        window = _live_window(now)
        result = _bgp_result(
            window, summary={"warning": "missing"},
        )
        # Heartbeat healthy so only the shape warning contributes
        wt = {"sqPoller": _healthy_heartbeat(window, now)}
        env = build_envelope(
            "dc1", window, [result],
            files_read_by_table={"bgp": 1, "sqPoller": 1},
            windowed_tables=wt,
            now=now,
        )
        assert env["status"] == "degraded"
        assert len(env["warnings"]) >= 1
        assert any("missing" in w for w in env["warnings"])

    def test_envelope_key_shape_regression_guard(self):
        """Pin the full top-level key set so a future refactor
        can't silently drop or rename status/warnings."""
        env = build_envelope("dc1", TimeWindow(0, 60), [], {})
        assert set(env.keys()) == {
            "namespace", "generated_at", "status", "warnings",
            "window", "queries",
        }


class TestEmitHumanStatus:
    def test_ok_status_does_not_print_banner(self):
        env = build_envelope("dc1", TimeWindow(0, 60), [], {})
        buf = io.StringIO()
        emit_human(env, stream=buf)
        text = buf.getvalue()
        # Clean run -> no status line
        assert "status=" not in text

    def test_degraded_status_prints_banner_and_warnings(self):
        now = 10000
        window = _live_window(now)
        # A query with a shape warning triggers degraded
        env = build_envelope(
            "dc1", window,
            [_bgp_result(window, summary={"warning": "missing columns"})],
            files_read_by_table={"bgp": 1, "sqPoller": 1},
            windowed_tables={"sqPoller": _healthy_heartbeat(window, now)},
            now=now,
        )
        buf = io.StringIO()
        emit_human(env, stream=buf)
        text = buf.getvalue()
        assert "status=degraded" in text
        assert "!" in text  # the per-warning marker
        assert "missing columns" in text


class TestStaleThresholdConstant:
    def test_threshold_is_two_poll_cadences(self):
        """The stale threshold is documented as 2 x poll cadence
        (60 s) = 120 s. Pin the constant so a future tweak has to
        update both the code and the docstring / README."""
        assert _STALE_THRESHOLD_SEC == 120

    def test_heartbeat_table_constant(self):
        """Pin the heartbeat table name. A future SuzieQ that
        renames sqPoller would break this on the first test run."""
        assert HEARTBEAT_TABLE == "sqPoller"
