"""Tests for the --format json output mode of validate.py.

Pin the JSON contract that Phase 6's PR-comment bot will consume:
top-level dict with result/total/passed/failed/checks fields, where
each check is a dict mirroring CheckResult exactly. Mirrors the
human-readable text report's data so the two formats can never
disagree.
"""
import json

import pytest

from questions import CheckResult
from validate import render_json_report


def test_json_all_passed():
    results = [
        CheckResult("a", True, "ok"),
        CheckResult("b", True, "also ok"),
    ]
    out = render_json_report(results)
    payload = json.loads(out)
    assert payload["result"] == "PASS"
    assert payload["total"] == 2
    assert payload["passed"] == 2
    assert payload["failed"] == 0
    assert len(payload["checks"]) == 2
    assert payload["checks"][0] == {
        "name": "a", "passed": True, "summary": "ok", "detail": "",
    }


def test_json_one_failed():
    results = [
        CheckResult("a", True, "ok"),
        CheckResult("b", False, "broke", detail="line1\nline2"),
    ]
    out = render_json_report(results)
    payload = json.loads(out)
    assert payload["result"] == "FAIL"
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["checks"][1]["passed"] is False
    assert payload["checks"][1]["detail"] == "line1\nline2"


def test_json_all_failed():
    results = [CheckResult("a", False, "no")]
    payload = json.loads(render_json_report(results))
    assert payload["result"] == "FAIL"
    assert payload["passed"] == 0
    assert payload["failed"] == 1


def test_json_empty_results():
    """Edge case: zero checks should still produce a valid JSON
    payload that the PR-comment bot can render."""
    payload = json.loads(render_json_report([]))
    assert payload["result"] == "PASS"  # vacuously
    assert payload["total"] == 0
    assert payload["passed"] == 0
    assert payload["failed"] == 0
    assert payload["checks"] == []


def test_json_is_valid_and_indented():
    """Phase 6 will paste the JSON into a PR comment markdown block.
    Pretty-printed (indent=2) is required for human readability in
    that context."""
    results = [CheckResult("a", True, "ok")]
    out = render_json_report(results)
    assert "\n" in out
    payload = json.loads(out)
    assert "result" in payload


def test_json_check_keys_match_dataclass():
    """Pin the field set so a CheckResult schema change is caught
    here, not in production by the PR-comment bot blowing up on a
    missing key."""
    results = [CheckResult("a", True, "ok", detail="some detail")]
    payload = json.loads(render_json_report(results))
    assert set(payload["checks"][0].keys()) == {"name", "passed", "summary", "detail"}
