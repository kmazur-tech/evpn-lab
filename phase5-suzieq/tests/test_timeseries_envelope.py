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
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.timeseries.envelope import (  # noqa: E402
    build_envelope,
    emit_human,
    emit_json,
    _coerce_scalar,
    _df_to_records,
)
from drift.timeseries.queries import TimeseriesResult  # noqa: E402
from drift.timeseries.reader import TimeWindow  # noqa: E402


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
