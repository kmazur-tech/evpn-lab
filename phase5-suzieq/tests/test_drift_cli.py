"""Unit tests for drift/cli.py - the I/O orchestration layer.

cli.py wires intent.collect() -> state.collect() -> diff.compare()
-> output. We test the wiring (exit codes, JSON shape, env-var
fallbacks) by monkeypatching the three collaborators with stubs
that return hand-built data. The collaborator modules are
exhaustively tested in their own test files.
"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift import cli  # noqa: E402
from drift.diff import Drift, SEVERITY_ERROR  # noqa: E402
from drift.intent import FabricIntent  # noqa: E402
from drift.state import FabricState  # noqa: E402


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_default_format_is_json(self):
        args = cli.parse_args([])
        assert args.format == "json"

    def test_human_format_flag(self):
        args = cli.parse_args(["--human"])
        assert args.format == "human"

    def test_namespace_default_is_dc1(self, monkeypatch):
        monkeypatch.delenv("DRIFT_NAMESPACE", raising=False)
        args = cli.parse_args([])
        assert args.namespace == "dc1"

    def test_namespace_from_env(self, monkeypatch):
        monkeypatch.setenv("DRIFT_NAMESPACE", "dc2")
        args = cli.parse_args([])
        assert args.namespace == "dc2"

    def test_namespace_cli_beats_env(self, monkeypatch):
        monkeypatch.setenv("DRIFT_NAMESPACE", "dc2")
        args = cli.parse_args(["--namespace", "dc3"])
        assert args.namespace == "dc3"

    def test_netbox_creds_from_env(self, monkeypatch):
        monkeypatch.setenv("NETBOX_URL", "http://test:8000")
        monkeypatch.setenv("NETBOX_TOKEN", "abc123")
        args = cli.parse_args([])
        assert args.netbox_url == "http://test:8000"
        assert args.netbox_token == "abc123"


# ---------------------------------------------------------------------------
# run() - end-to-end with stubbed collaborators
# ---------------------------------------------------------------------------

def _stub_intent(devices=None):
    return FabricIntent(namespace="dc1", devices=devices or [])


def _stub_state(devices_df=None):
    return FabricState(
        namespace="dc1",
        devices=devices_df if devices_df is not None else pd.DataFrame(),
    )


def _args(format="json", url="http://test:8000", token="abc"):
    return cli.parse_args([
        f"--{format}",
        "--netbox-url", url,
        "--netbox-token", token,
    ])


class TestRun:
    def test_no_drift_returns_exit_0(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "pynetbox",
                            type("M", (), {"api": lambda *a, **k: object()}))
        monkeypatch.setattr(cli, "collect_intent", lambda nb, ns: _stub_intent())
        monkeypatch.setattr(cli, "collect_state",
                            lambda ns, pd_dir: _stub_state())
        rc = cli.run(_args())
        assert rc == cli.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["drift_count"] == 0

    def test_drift_found_returns_exit_1(self, monkeypatch, capsys):
        from drift.intent import DeviceIntent
        modeled = [DeviceIntent(name="dc1-leaf1", status="active",
                                site_slug="dc1", role_slug="leaf")]
        monkeypatch.setattr(cli, "pynetbox",
                            type("M", (), {"api": lambda *a, **k: object()}))
        monkeypatch.setattr(cli, "collect_intent",
                            lambda nb, ns: _stub_intent(devices=modeled))
        # State has zero devices -> drift on the modeled one
        monkeypatch.setattr(cli, "collect_state",
                            lambda ns, pd_dir: _stub_state(pd.DataFrame()))
        rc = cli.run(_args())
        assert rc == cli.EXIT_DRIFT_FOUND
        out = json.loads(capsys.readouterr().out)
        assert out["drift_count"] >= 1
        assert out["error_count"] >= 1

    def test_warning_only_does_not_set_exit_1(self, monkeypatch, capsys):
        """Harness contract: only error-severity drifts return exit 1.
        Warnings still appear in output but exit 0. Phase 6 deploy
        workflow hard-fails on exit 1; warnings never trigger rollback."""
        # Empty intent + non-empty state => warning-only drift
        # (polled-but-not-modeled)
        df = pd.DataFrame([{"hostname": "stale-device"}])
        monkeypatch.setattr(cli, "pynetbox",
                            type("M", (), {"api": lambda *a, **k: object()}))
        monkeypatch.setattr(cli, "collect_intent", lambda nb, ns: _stub_intent())
        monkeypatch.setattr(cli, "collect_state",
                            lambda ns, pd_dir: _stub_state(df))
        rc = cli.run(_args())
        assert rc == cli.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["warning_count"] == 1
        assert out["error_count"] == 0

    def test_missing_netbox_creds_returns_exit_2(self, capsys):
        args = cli.parse_args(["--namespace", "dc1"])
        args.netbox_url = None
        args.netbox_token = None
        rc = cli.run(args)
        assert rc == cli.EXIT_TOOLING_ERROR
        err = capsys.readouterr().err
        assert "NETBOX_URL" in err

    def test_intent_collection_failure_returns_exit_2(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "pynetbox",
                            type("M", (), {"api": lambda *a, **k: object()}))

        def boom(*a, **k):
            raise RuntimeError("netbox 500")
        monkeypatch.setattr(cli, "collect_intent", boom)
        rc = cli.run(_args())
        assert rc == cli.EXIT_TOOLING_ERROR
        assert "NetBox intent collection failed" in capsys.readouterr().err

    def test_state_read_failure_returns_exit_2(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "pynetbox",
                            type("M", (), {"api": lambda *a, **k: object()}))
        monkeypatch.setattr(cli, "collect_intent", lambda nb, ns: _stub_intent())

        def boom(*a, **k):
            raise OSError("parquet store missing")
        monkeypatch.setattr(cli, "collect_state", boom)
        rc = cli.run(_args())
        assert rc == cli.EXIT_TOOLING_ERROR
        assert "SuzieQ state read failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

class TestEmit:
    def test_json_output_has_required_top_level_keys(self, capsys):
        cli._emit_json([], "dc1")
        out = json.loads(capsys.readouterr().out)
        assert set(out.keys()) >= {
            "namespace", "timestamp", "drift_count",
            "error_count", "warning_count", "drifts",
        }

    def test_json_output_drift_serialization(self, capsys):
        d = Drift(
            dimension="device_presence",
            severity=SEVERITY_ERROR,
            category="inventory",
            subject="dc1-leaf1",
            detail="missing",
            intent={"name": "dc1-leaf1"},
            state=None,
        )
        cli._emit_json([d], "dc1")
        out = json.loads(capsys.readouterr().out)
        assert out["drift_count"] == 1
        assert out["drifts"][0]["dimension"] == "device_presence"
        assert out["drifts"][0]["severity"] == "error"
        assert out["drifts"][0]["category"] == "inventory"
        assert out["drifts"][0]["intent"]["name"] == "dc1-leaf1"

    def test_human_output_no_drift(self, capsys):
        cli._emit_human([], "dc1")
        out = capsys.readouterr().out
        assert "no drift" in out

    def test_human_output_with_drift(self, capsys):
        d = Drift(
            dimension="device_presence",
            severity=SEVERITY_ERROR,
            category="inventory",
            subject="dc1-leaf1",
            detail="modeled but not seen",
        )
        cli._emit_human([d], "dc1")
        out = capsys.readouterr().out
        assert "ERR" in out
        assert "dc1-leaf1" in out
        assert "device_presence" in out
