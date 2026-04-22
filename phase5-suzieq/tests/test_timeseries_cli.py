"""Unit tests for the --mode timeseries branch of drift/cli.py.

The other modes are covered by test_drift_cli.py and
test_assertions_runall_and_cli.py - this file targets only the
Part D additions:

  - resolve_window: relative (--window), absolute (--from/--to),
    error cases
  - --mode timeseries dispatch
  - run_timeseries with mocked window_read so we exercise the
    orchestration without touching parquet
  - exit code semantics: timeseries returns 0 even when results
    are non-empty (observations, not pass/fail)
  - JSON output is the timeseries envelope shape, NOT the drift
    envelope shape (regression guard)
  - --window mutually exclusive with --from/--to
  - timeseries mode does NOT call collect_state, collect_intent,
    or run_all_assertions
"""
import argparse
import json
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift import cli  # noqa: E402
from drift.timeseries.queries import TimeseriesResult  # noqa: E402
from drift.timeseries.reader import TimeWindow, WindowedTable  # noqa: E402


# ---------------------------------------------------------------------------
# parse_args additions
# ---------------------------------------------------------------------------

class TestParseArgsTimeseries:
    def test_mode_timeseries_accepted(self):
        args = cli.parse_args(["--mode", "timeseries", "--window", "1h"])
        assert args.mode == "timeseries"
        assert args.window == "1h"

    def test_window_default_is_none(self):
        args = cli.parse_args([])
        assert args.window is None
        assert args.from_epoch is None
        assert args.to_epoch is None

    def test_from_to_parsed_as_int(self):
        args = cli.parse_args(["--mode", "timeseries", "--from", "100", "--to", "200"])
        assert args.from_epoch == 100
        assert args.to_epoch == 200


# ---------------------------------------------------------------------------
# resolve_window
# ---------------------------------------------------------------------------

class TestResolveWindow:
    def _args(self, window=None, from_epoch=None, to_epoch=None):
        return argparse.Namespace(
            window=window,
            from_epoch=from_epoch,
            to_epoch=to_epoch,
        )

    def test_relative_window(self):
        # now=10000, window=1h -> [6400, 10000)
        out = cli.resolve_window(self._args(window="1h"), now=10000)
        assert out == TimeWindow(10000 - 3600, 10000)

    def test_relative_window_minute(self):
        out = cli.resolve_window(self._args(window="5m"), now=10000)
        assert out == TimeWindow(10000 - 300, 10000)

    def test_absolute_window(self):
        out = cli.resolve_window(self._args(from_epoch=100, to_epoch=200))
        assert out == TimeWindow(100, 200)

    def test_relative_and_absolute_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            cli.resolve_window(self._args(window="1h", from_epoch=100, to_epoch=200))

    def test_from_without_to_rejected(self):
        with pytest.raises(ValueError, match="--from and --to"):
            cli.resolve_window(self._args(from_epoch=100))

    def test_to_without_from_rejected(self):
        with pytest.raises(ValueError, match="--from and --to"):
            cli.resolve_window(self._args(to_epoch=200))

    def test_inverted_absolute_window_rejected(self):
        with pytest.raises(ValueError, match="strictly less than"):
            cli.resolve_window(self._args(from_epoch=200, to_epoch=100))

    def test_equal_from_and_to_rejected(self):
        with pytest.raises(ValueError, match="strictly less than"):
            cli.resolve_window(self._args(from_epoch=100, to_epoch=100))

    def test_no_args_at_all_rejected(self):
        with pytest.raises(ValueError, match="requires either --window"):
            cli.resolve_window(self._args())

    def test_invalid_duration_propagates(self):
        with pytest.raises(ValueError):
            cli.resolve_window(self._args(window="bogus"))


# ---------------------------------------------------------------------------
# run_timeseries dispatch
# ---------------------------------------------------------------------------

def _empty_wt(table):
    return WindowedTable(
        table=table, namespace="dc1",
        window=TimeWindow(0, 100),
        rows=pd.DataFrame(),
        files_read=0,
    )


def _populated_bgp_wt():
    return WindowedTable(
        table="bgp", namespace="dc1",
        window=TimeWindow(0, 100),
        rows=pd.DataFrame([
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.0.0.2",
             "afi": "ipv4", "safi": "unicast", "state": "Established", "timestamp": 50000},
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.0.0.2",
             "afi": "ipv4", "safi": "unicast", "state": "Idle", "timestamp": 60000},
            {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.0.0.2",
             "afi": "ipv4", "safi": "unicast", "state": "Established", "timestamp": 70000},
        ]),
        files_read=2,
    )


@pytest.fixture
def mock_window_read(monkeypatch):
    """Replace window_read with a stub that returns a fixed map of
    table -> WindowedTable. The CLI never touches pyarrow with this
    fixture installed."""
    table_map = {
        "bgp": _populated_bgp_wt(),
        "routes": _empty_wt("routes"),
        "macs": _empty_wt("macs"),
    }

    def fake_window_read(table, namespace, start_epoch, end_epoch, parquet_dir):
        wt = table_map.get(table, _empty_wt(table))
        return wt

    monkeypatch.setattr(cli, "window_read", fake_window_read)
    return table_map


class TestRunTimeseries:
    def test_returns_zero_with_clean_run(self, mock_window_read, capsys):
        args = argparse.Namespace(
            namespace="dc1", parquet_dir="/tmp/fake",
            window="1h", from_epoch=None, to_epoch=None,
            format="json",
        )
        rc = cli.run_timeseries(args)
        assert rc == cli.EXIT_OK

    def test_returns_zero_even_when_flaps_detected(self, mock_window_read, capsys):
        # Critical contract: timeseries observations are NEVER
        # pass/fail. Even with bgp flaps in the result the exit
        # code stays 0.
        args = argparse.Namespace(
            namespace="dc1", parquet_dir="/tmp/fake",
            window="1h", from_epoch=None, to_epoch=None,
            format="json",
        )
        rc = cli.run_timeseries(args)
        out = json.loads(capsys.readouterr().out)
        # Confirm flaps were actually detected
        bgp_q = next(q for q in out["queries"] if q["name"] == "bgp_flaps")
        assert bgp_q["summary"]["total_flaps"] >= 1
        # And the exit code is still OK
        assert rc == cli.EXIT_OK

    def test_returns_tooling_error_on_bad_window(self, capsys):
        args = argparse.Namespace(
            namespace="dc1", parquet_dir="/tmp/fake",
            window="bogus", from_epoch=None, to_epoch=None,
            format="json",
        )
        rc = cli.run_timeseries(args)
        assert rc == cli.EXIT_TOOLING_ERROR
        err = capsys.readouterr().err
        assert "ERROR" in err

    def test_json_envelope_has_timeseries_shape_not_drift_shape(self, mock_window_read, capsys):
        # Regression guard: the timeseries envelope must NOT carry
        # the drift envelope's {drift_count, drifts, error_count}
        # fields, and MUST carry the timeseries-specific
        # {window, queries} fields. If a future refactor merges
        # the two output paths this test fails loudly.
        args = argparse.Namespace(
            namespace="dc1", parquet_dir="/tmp/fake",
            window="1h", from_epoch=None, to_epoch=None,
            format="json",
        )
        cli.run_timeseries(args)
        out = json.loads(capsys.readouterr().out)
        # timeseries fields
        assert "window" in out
        assert "queries" in out
        assert "namespace" in out
        # drift fields MUST be absent
        assert "drift_count" not in out
        assert "drifts" not in out
        assert "error_count" not in out

    def test_files_read_propagated_to_envelope(self, mock_window_read, capsys):
        args = argparse.Namespace(
            namespace="dc1", parquet_dir="/tmp/fake",
            window="1h", from_epoch=None, to_epoch=None,
            format="json",
        )
        cli.run_timeseries(args)
        out = json.loads(capsys.readouterr().out)
        # populated bgp wt has files_read=2
        bgp_q = next(q for q in out["queries"] if q["name"] == "bgp_flaps")
        assert bgp_q["files_read"] == 2

    def test_human_format_writes_human_envelope(self, mock_window_read, capsys):
        args = argparse.Namespace(
            namespace="dc1", parquet_dir="/tmp/fake",
            window="1h", from_epoch=None, to_epoch=None,
            format="human",
        )
        rc = cli.run_timeseries(args)
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "namespace=dc1" in out
        assert "bgp_flaps" in out

    def test_parquet_read_failure_returns_tooling_error(self, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise OSError("disk on fire")
        monkeypatch.setattr(cli, "window_read", boom)
        args = argparse.Namespace(
            namespace="dc1", parquet_dir="/tmp/fake",
            window="1h", from_epoch=None, to_epoch=None,
            format="json",
        )
        rc = cli.run_timeseries(args)
        assert rc == cli.EXIT_TOOLING_ERROR


# ---------------------------------------------------------------------------
# run() dispatch - timeseries mode skips state/intent/assertion paths
# ---------------------------------------------------------------------------

class TestRunDispatchTimeseries:
    def test_timeseries_mode_does_not_call_collect_state(self, mock_window_read, monkeypatch):
        # The pre-Part-D code path always called collect_state
        # before dispatching on mode. Part D's whole point is that
        # the latest-snapshot read is unnecessary - we want the
        # window history. Pin that contract.
        called = {"collect_state": False, "collect_intent": False, "run_all": False}

        def fake_collect_state(*a, **kw):
            called["collect_state"] = True
            raise AssertionError("collect_state should NOT be called in timeseries mode")

        def fake_collect_intent(*a, **kw):
            called["collect_intent"] = True
            raise AssertionError("collect_intent should NOT be called in timeseries mode")

        def fake_run_all(*a, **kw):
            called["run_all"] = True
            raise AssertionError("run_all_assertions should NOT be called in timeseries mode")

        monkeypatch.setattr(cli, "collect_state", fake_collect_state)
        monkeypatch.setattr(cli, "collect_intent", fake_collect_intent)
        monkeypatch.setattr(cli, "run_all_assertions", fake_run_all)

        args = cli.parse_args([
            "--mode", "timeseries", "--window", "1h",
            "--namespace", "dc1", "--parquet-dir", "/tmp/fake",
            "--json",
        ])
        rc = cli.run(args)
        assert rc == cli.EXIT_OK
        assert called["collect_state"] is False
        assert called["collect_intent"] is False
        assert called["run_all"] is False
