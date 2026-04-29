"""Tests for the assertions package __init__.run_all() orchestrator
and for the cli.py --mode flag wiring.

These are the integration points between the individual assertion
modules and the CLI. Unit tests for each assertion function live
in their own test_assertions_*.py files.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift import cli  # noqa: E402
from drift.assertions import run_all  # noqa: E402
from drift.diff import SEVERITY_ERROR  # noqa: E402
from drift.intent import FabricIntent  # noqa: E402
from drift.state import FabricState  # noqa: E402


# ---------------------------------------------------------------------------
# run_all() orchestrator
# ---------------------------------------------------------------------------

class TestRunAll:
    def test_clean_state_zero_drift(self):
        """Healthy fabric: all assertions pass."""
        state = FabricState(
            namespace="dc1",
            bgp=pd.DataFrame([
                {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
                 "state": "Established", "afi": "ipv4", "safi": "unicast", "pfxRx": 1},
            ]),
            evpn_vnis=pd.DataFrame([
                {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
                 "state": "up", "remoteVtepList": ["10.1.0.4"]},
                {"hostname": "dc1-leaf1", "vni": 5000, "type": "L3",
                 "state": "up"},
            ]),
            sq_poller=pd.DataFrame([
                {"hostname": "dc1-leaf1", "service": "bgp", "pollExcdPeriodCount": 0},
            ]),
        )
        out = run_all(state)
        assert out == []

    def test_multiple_assertion_failures_aggregated(self):
        """One of each kind of failure, all visible in the
        combined output."""
        state = FabricState(
            namespace="dc1",
            bgp=pd.DataFrame([
                # BGP not established
                {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.0",
                 "state": "Active", "afi": "ipv4", "safi": "unicast", "pfxRx": 0},
                # BGP established but pfxRx=0
                {"hostname": "dc1-leaf1", "vrf": "default", "peer": "10.1.4.2",
                 "state": "Established", "afi": "ipv4", "safi": "unicast", "pfxRx": 0},
            ]),
            evpn_vnis=pd.DataFrame([
                # L2 VNI with no remote VTEPs (empty list)
                {"hostname": "dc1-leaf1", "vni": 10010, "type": "L2",
                 "state": "up", "remoteVtepList": []},
            ]),
            sq_poller=pd.DataFrame([
                # Poller falling behind
                {"hostname": "dc1-leaf1", "service": "bgp", "pollExcdPeriodCount": 3},
            ]),
        )
        out = run_all(state)
        dims = [d.dimension for d in out]
        assert "assert_bgp_established" in dims
        assert "assert_bgp_pfx_rx" in dims
        assert "assert_vtep_remote_count" in dims
        assert "assert_poll_health" in dims

    def test_output_sorted_stable(self):
        """Same input -> identical output ordering, required for
        Phase 6 CI golden-file tests."""
        state = FabricState(
            namespace="dc1",
            bgp=pd.DataFrame([
                {"hostname": "z-leaf", "vrf": "default", "peer": "10.1.4.0",
                 "state": "Active", "afi": "ipv4", "safi": "unicast", "pfxRx": 0},
                {"hostname": "a-leaf", "vrf": "default", "peer": "10.1.4.2",
                 "state": "Active", "afi": "ipv4", "safi": "unicast", "pfxRx": 0},
            ]),
        )
        out1 = run_all(state)
        out2 = run_all(state)
        assert [d.subject for d in out1] == [d.subject for d in out2]
        # Within a dimension, subjects are sorted
        bgp_subjects = [d.subject for d in out1
                        if d.dimension == "assert_bgp_established"]
        assert bgp_subjects == sorted(bgp_subjects)


# ---------------------------------------------------------------------------
# CLI --mode flag
# ---------------------------------------------------------------------------

class TestCliMode:
    def test_default_mode_is_drift(self):
        args = cli.parse_args([])
        assert args.mode == "drift"

    def test_mode_assertions_flag(self):
        args = cli.parse_args(["--mode", "assertions"])
        assert args.mode == "assertions"

    def test_mode_all_flag(self):
        args = cli.parse_args(["--mode", "all"])
        assert args.mode == "all"

    def test_mode_env_var_default(self, monkeypatch):
        monkeypatch.setenv("DRIFT_MODE", "assertions")
        args = cli.parse_args([])
        assert args.mode == "assertions"

    def test_mode_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("DRIFT_MODE", "assertions")
        args = cli.parse_args(["--mode", "drift"])
        assert args.mode == "drift"

    def test_invalid_mode_rejected(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["--mode", "bogus"])


class TestCliModeRun:
    """End-to-end wiring of the --mode flag through run().
    Monkeypatches the collaborators so no network / no parquet
    are needed."""

    def test_assertions_mode_skips_netbox_entirely(self, monkeypatch, capsys):
        """The headline win of the assertions mode: it needs NO
        NetBox credentials, so the systemd timer can run it
        without having to manage the NetBox token."""
        called = {"intent": False, "state": False, "assertions": False}

        def fake_state(ns, pd_dir):
            called["state"] = True
            return FabricState(namespace=ns)

        def fake_intent(nb, ns):
            called["intent"] = True
            return FabricIntent(namespace=ns)

        def fake_assertions(state):
            called["assertions"] = True
            return []

        monkeypatch.setattr(cli, "collect_state", fake_state)
        monkeypatch.setattr(cli, "collect_intent", fake_intent)
        monkeypatch.setattr(cli, "run_all_assertions", fake_assertions)

        args = cli.parse_args(["--mode", "assertions"])
        # NO --netbox-url, NO --netbox-token
        args.netbox_url = None
        args.netbox_token = None
        rc = cli.run(args)

        assert rc == cli.EXIT_OK
        assert called["state"] is True
        assert called["assertions"] is True
        # CRITICAL: intent.collect() MUST NOT be called in
        # assertions mode. The whole point of the mode is zero
        # NetBox dependency.
        assert called["intent"] is False

    def test_drift_mode_still_requires_netbox(self, capsys):
        """Regression guard: the mode split must not break the
        existing drift-mode contract."""
        args = cli.parse_args(["--mode", "drift"])
        args.netbox_url = None
        args.netbox_token = None
        rc = cli.run(args)
        assert rc == cli.EXIT_TOOLING_ERROR
        assert "NETBOX_URL" in capsys.readouterr().err

    def test_all_mode_runs_both(self, monkeypatch):
        called = {"intent": False, "assertions": False}

        def fake_state(ns, pd_dir):
            return FabricState(namespace=ns)

        def fake_intent(nb, ns):
            called["intent"] = True
            return FabricIntent(namespace=ns)

        def fake_assertions(state):
            called["assertions"] = True
            return []

        monkeypatch.setattr(cli, "pynetbox",
                            type("M", (), {"api": lambda *a, **k: object()}))
        monkeypatch.setattr(cli, "collect_state", fake_state)
        monkeypatch.setattr(cli, "collect_intent", fake_intent)
        monkeypatch.setattr(cli, "run_all_assertions", fake_assertions)

        args = cli.parse_args(["--mode", "all",
                               "--netbox-url", "http://x",
                               "--netbox-token", "t"])
        rc = cli.run(args)
        assert rc == cli.EXIT_OK
        assert called["intent"] is True
        assert called["assertions"] is True

    def test_assertion_failure_sets_exit_1(self, monkeypatch):
        """The exit-code contract extends to assertions: an ERROR-
        severity assertion must produce exit 1 so the systemd
        timer can detect failures via `systemctl status`."""
        from drift.diff import Drift

        def fake_state(ns, pd_dir):
            return FabricState(namespace=ns)

        def fake_assertions(state):
            return [Drift(
                dimension="assert_bgp_established",
                severity=SEVERITY_ERROR,
                category="control_plane",
                subject="dc1-leaf1:default:10.1.4.0",
                detail="injected failure",
            )]

        monkeypatch.setattr(cli, "collect_state", fake_state)
        monkeypatch.setattr(cli, "run_all_assertions", fake_assertions)

        args = cli.parse_args(["--mode", "assertions"])
        rc = cli.run(args)
        assert rc == cli.EXIT_DRIFT_FOUND
