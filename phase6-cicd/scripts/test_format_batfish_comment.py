"""Tests for format_batfish_comment.py.

Pinned-shape tests: the markdown contract is what the GitHub Actions
workflow consumes (find-or-update via the COMMENT_MARKER). The shape
is therefore part of the public contract; these tests fail loudly if
the formatter ever drifts away from it.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "format_batfish_comment.py"


def _payload(passed_count=7, failed_count=0, diffs=None):
    """Build a minimal validate.py-shaped JSON payload."""
    checks = [
        {"name": f"check_{i}", "passed": True, "summary": "OK", "detail": ""}
        for i in range(passed_count)
    ] + [
        {"name": f"fail_{i}", "passed": False, "summary": "boom", "detail": "details"}
        for i in range(failed_count)
    ]
    payload = {
        "result": "PASS" if failed_count == 0 else "FAIL",
        "total": len(checks),
        "passed": passed_count,
        "failed": failed_count,
        "checks": checks,
    }
    if diffs is not None:
        payload["diffs"] = diffs
    return payload


def render(payload):
    """Import-free invocation: run the script as a subprocess and capture
    stdout. Mirrors how the workflow consumes the formatter."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_marker_present():
    """The hidden HTML marker is the find-or-update key the workflow
    relies on. Removing it would cause the workflow to spam new
    comments on every PR re-run."""
    out = render(_payload())
    assert "<!-- batfish-diff-bot -->" in out


def test_pass_summary():
    out = render(_payload(passed_count=7, failed_count=0))
    assert "**Result: PASS**" in out
    assert "(7/7 passed)" in out


def test_fail_summary():
    out = render(_payload(passed_count=5, failed_count=2))
    assert "**Result: FAIL**" in out
    assert "(5/7 passed)" in out


def test_check_table_renders_each_row():
    out = render(_payload(passed_count=2, failed_count=1))
    assert "| `check_0` | PASS | OK |" in out
    assert "| `check_1` | PASS | OK |" in out
    assert "| `fail_0` | FAIL | boom |" in out


def test_pipe_in_summary_is_escaped():
    """Markdown table cells break on bare pipes. The formatter must
    escape any pipe in a check summary."""
    payload = {
        "result": "PASS",
        "total": 1,
        "passed": 1,
        "failed": 0,
        "checks": [{"name": "x", "passed": True,
                    "summary": "a | b | c", "detail": ""}],
    }
    out = render(payload)
    assert r"a \| b \| c" in out


def test_no_diffs_section_when_diffs_omitted():
    """validate.py omits the diffs key when run without --reference-snapshot.
    The formatter must surface that clearly."""
    out = render(_payload(diffs=None))
    assert "### Differential vs `main` baseline" in out
    assert "_No reference snapshot was provided; skipped._" in out


def test_no_semantic_changes_when_all_diffs_empty():
    """All diff entries with empty added+removed -> single sentence,
    no table noise. This is the common PR case (PR didn't change
    phase3-nornir/* so the rendered configs are byte-identical to main)."""
    diffs = [
        {"name": "bgp_session_topology", "summary": "no diff",
         "added": [], "removed": []},
        {"name": "ip_owners", "summary": "no diff",
         "added": [], "removed": []},
    ]
    out = render(_payload(diffs=diffs))
    assert "**No semantic changes vs `main`.**" in out
    # The empty-diff sentence is mutually exclusive with the diff table:
    # if either is present, the other should not be.
    assert "| Question | Added | Removed |" not in out


def test_diffs_table_renders_when_changes_present():
    diffs = [
        {"name": "bgp_session_topology", "summary": "2 added, 0 removed",
         "added": ["dc1-spine1->dc1-leaf3", "dc1-spine2->dc1-leaf3"],
         "removed": []},
        {"name": "ip_owners", "summary": "no diff",
         "added": [], "removed": []},
    ]
    out = render(_payload(diffs=diffs))
    assert "| Question | Added | Removed | Summary |" in out
    assert "| `bgp_session_topology` | 2 | 0 | 2 added, 0 removed |" in out
    assert "| `ip_owners` | 0 | 0 | no diff |" in out


def test_diff_details_collapsible_only_for_nonempty_diffs():
    """The collapsible <details> block only appears for diffs that
    actually have added or removed entries; pure-zero diffs would
    just produce an empty disclosure."""
    diffs = [
        {"name": "bgp_session_topology", "summary": "1 added",
         "added": ["dc1-spine1->dc1-leaf3"], "removed": []},
        {"name": "ip_owners", "summary": "no diff",
         "added": [], "removed": []},
    ]
    out = render(_payload(diffs=diffs))
    assert "<details><summary><code>bgp_session_topology</code> entries</summary>" in out
    assert "<code>ip_owners</code> entries" not in out
    assert "dc1-spine1->dc1-leaf3" in out


def test_added_and_removed_blocks_separately():
    diffs = [{
        "name": "routes",
        "summary": "1 added, 1 removed",
        "added": ["10.1.0.0/16 via 10.0.0.1"],
        "removed": ["10.2.0.0/16 via 10.0.0.2"],
    }]
    out = render(_payload(diffs=diffs))
    assert "**Added:**" in out
    assert "10.1.0.0/16 via 10.0.0.1" in out
    assert "**Removed:**" in out
    assert "10.2.0.0/16 via 10.0.0.2" in out


def test_invalid_input_raises():
    """Non-JSON stdin should make the script exit nonzero. Catches
    accidental empty-file runs in CI."""
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="not json",
            capture_output=True,
            text=True,
            check=True,
        )
