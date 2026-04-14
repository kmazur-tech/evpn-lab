"""Integration tests against a real Batfish container.

These tests REQUIRE the Batfish container to be running and reachable
at $BATFISH_HOST:9996. They are skipped by default (the pytest.ini
default `addopts` filters them out via `-m "not integration"`).

Run with:
  source ../../evpn-lab-env/env.sh
  pytest -m integration

Run everything (unit + integration):
  pytest -m "integration or not integration"

Per Said van de Klundert's pytest+Batfish pattern: session-scoped
fixture establishes the Session and inits the snapshot ONCE for all
tests in the file, so the ~30s init cost is amortized across the
whole integration run rather than paid per-test.
"""
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from questions import (
    ALL_CHECKS,
    ALL_DIFFS,
    check_bgp_sessions,
    check_init_issues,
    check_overlay_loopback_reachability,
    check_parse_status,
    check_undefined_references,
)
from validate import (
    BATFISH_COORDINATOR_PORT,
    check_reachable,
    render_json_report,
    run_checks,
    run_diffs,
    stage_snapshot,
)


REPO = Path(__file__).resolve().parents[2]
BUILD_DIR = REPO / "phase3-nornir" / "build"
EXPECTED_DIR = REPO / "phase3-nornir" / "expected"


def _batfish_host():
    """Resolve $BATFISH_HOST or skip the test cleanly if not set."""
    host = os.environ.get("BATFISH_HOST")
    if not host:
        pytest.skip("BATFISH_HOST not set; source ../../evpn-lab-env/env.sh")
    return host


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bf_session():
    """Per Said van de Klundert's pattern: ONE Session + ONE snapshot
    init for the whole module. The 30-60s init cost is paid once."""
    from pybatfish.client.session import Session

    host = _batfish_host()
    try:
        check_reachable(host)
    except RuntimeError as e:
        pytest.skip(f"Batfish unreachable: {e}")

    if not BUILD_DIR.exists() or not list(BUILD_DIR.glob("*.conf")):
        pytest.skip(
            f"{BUILD_DIR} has no rendered configs - run `python "
            f"phase3-nornir/deploy.py --full` first"
        )

    bf = Session(host=host)
    bf.set_network("evpn-lab-test")

    with tempfile.TemporaryDirectory(prefix="bf-int-") as staged:
        cand_root = stage_snapshot(BUILD_DIR, Path(staged) / "candidate")
        bf.init_snapshot(str(cand_root), name="integration-cand", overwrite=True)
        yield bf


@pytest.fixture(scope="module")
def bf_session_with_reference():
    """Same as bf_session but ALSO inits the reference snapshot
    from phase3-nornir/expected/. Used by the differential tests."""
    from pybatfish.client.session import Session

    host = _batfish_host()
    try:
        check_reachable(host)
    except RuntimeError as e:
        pytest.skip(f"Batfish unreachable: {e}")
    if not BUILD_DIR.exists() or not list(BUILD_DIR.glob("*.conf")):
        pytest.skip(f"{BUILD_DIR} has no rendered configs")
    if not EXPECTED_DIR.exists() or not list(EXPECTED_DIR.glob("*.conf")):
        pytest.skip(f"{EXPECTED_DIR} has no expected configs")

    bf = Session(host=host)
    bf.set_network("evpn-lab-test-diff")

    with tempfile.TemporaryDirectory(prefix="bf-int-diff-") as staged:
        cand_root = stage_snapshot(BUILD_DIR, Path(staged) / "candidate")
        ref_root = stage_snapshot(EXPECTED_DIR, Path(staged) / "reference")
        bf.init_snapshot(str(cand_root), name="diff-cand", overwrite=True)
        bf.init_snapshot(str(ref_root), name="diff-ref", overwrite=True)
        bf.set_snapshot("diff-cand")
        yield bf


# ----- All checks against the real fabric ----------------------------

def test_all_checks_pass_against_real_render(bf_session):
    """The full check suite must pass against the current Phase 3
    rendered configs. This is the end-to-end gate the manual
    `python validate.py` invocation runs - automated here so CI
    catches it without manual operator action."""
    results = run_checks(bf_session)
    failures = [r for r in results if not r.passed]
    assert not failures, "\n".join(
        f"{r.name}: {r.summary}\n{r.detail}" for r in failures
    )
    # Sanity: we should have run every check, not silently skipped any
    assert len(results) == len(ALL_CHECKS)


def test_init_issues_no_real_errors(bf_session):
    """Specifically: there should be NO init errors after filtering
    the known Junos EVPN false positives."""
    r = check_init_issues(bf_session)
    assert r.passed, f"init_issues real error: {r.summary}\n{r.detail}"


def test_bgp_sessions_all_established(bf_session):
    r = check_bgp_sessions(bf_session)
    assert r.passed
    # 16 = 4 underlay + 8 overlay sessions in the lab
    assert "16/16" in r.summary


def test_overlay_loopback_reachability(bf_session):
    """Regression guard for the int-vs-str dtype bug we found
    earlier. Pins that Session_Type='IBGP' filtering still works
    against a real Batfish answer."""
    r = check_overlay_loopback_reachability(bf_session)
    assert r.passed
    assert "8 iBGP overlay peer loopback(s) reachable" in r.summary


def test_undefined_references_clean_after_filter(bf_session):
    r = check_undefined_references(bf_session)
    assert r.passed
    # The vlan struct_type filter must be doing its job
    assert "false positive" in r.summary or "no undefined references" == r.summary.split(" (")[0]


# ----- Differential analysis -----------------------------------------

def test_differential_self_compare_is_empty(bf_session_with_reference):
    """When candidate == reference (running build/ against build/...
    actually expected/, but they're identical when Phase 3 is in a
    clean state), the diff should report zero changes. This is the
    most important guarantee for CI: a no-op PR produces a no-op
    diff report."""
    diffs = run_diffs(bf_session_with_reference, ref_name="diff-ref", cand_name="diff-cand")
    assert len(diffs) == len(ALL_DIFFS)
    for d in diffs:
        assert not d.added, f"{d.name}: unexpected additions {d.added}"
        assert not d.removed, f"{d.name}: unexpected removals {d.removed}"
        assert "no changes" in d.summary


# ----- JSON output end-to-end ----------------------------------------

def test_json_output_against_real_session(bf_session):
    """End-to-end JSON renderer against real Batfish output - pins
    that the dataclass fields actually serialize without surprise."""
    import json
    results = run_checks(bf_session)
    out = render_json_report(results)
    payload = json.loads(out)
    assert payload["result"] in ("PASS", "FAIL")
    assert payload["total"] == len(ALL_CHECKS)
    assert isinstance(payload["checks"], list)
    for check in payload["checks"]:
        assert {"name", "passed", "summary", "detail"} <= set(check.keys())
