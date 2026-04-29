"""Unit tests for questions.py - offline, no real Batfish container.

Each test constructs a fake pybatfish Session with stubbed query
methods that return canned pandas DataFrames. The check functions
should produce predictable CheckResult tuples for known input.

These tests pin behaviors that are easy to break on refactor:
- iBGP filter uses Session_Type, NOT Local_AS == Remote_AS (the
  original bug where pandas type coercion silently dropped all rows)
- VLAN struct_type is filtered out of undefined_references
- All checks fail closed when the relevant frame is empty
"""
from dataclasses import dataclass

import pandas as pd
import pytest

from questions import (
    ALL_CHECKS,
    IGNORED_REF_STRUCT_TYPES,
    check_bgp_edges_symmetric,
    check_bgp_sessions,
    check_init_issues,
    check_ip_ownership_conflicts,
    check_overlay_loopback_reachability,
    check_parse_status,
    check_undefined_references,
)


# ----- Fakes ---------------------------------------------------------

@dataclass
class _FakeAnswer:
    df: pd.DataFrame
    def frame(self): return self.df


@dataclass
class _FakeQ:
    """Returned by fake_session.q.<question_name>()"""
    df: pd.DataFrame
    def answer(self): return _FakeAnswer(self.df)


class _FakeQNamespace:
    """Stand-in for bf.q.* - every question name returns a _FakeQ
    constructor that yields the canned df for that question."""
    def __init__(self, frames: dict):
        self._frames = frames

    def __getattr__(self, name: str):
        if name not in self._frames:
            raise AttributeError(f"no canned frame for question {name}")
        df = self._frames[name]
        return lambda *_a, **_kw: _FakeQ(df)


class FakeSession:
    """Drop-in replacement for pybatfish.client.session.Session for unit
    tests. Construct with a dict mapping question name -> DataFrame."""
    def __init__(self, **frames):
        self.q = _FakeQNamespace(frames)


# ----- init_issues ---------------------------------------------------

def test_init_issues_empty_passes():
    """No init issues at all -> clean pass."""
    bf = FakeSession(initIssues=pd.DataFrame())
    r = check_init_issues(bf)
    assert r.passed
    assert "no init issues" in r.summary


def test_init_issues_warnings_only_passes():
    """Junos EVPN warnings (Batfish doesn't fully model the feature)
    must NOT cause a failure - they're informational."""
    df = pd.DataFrame([
        {"Nodes": ["dc1-leaf1"], "Type": "Convert warning",
         "Details": "EVPN Type-2 not modeled", "Line_Text": ""},
        {"Nodes": ["dc1-leaf2"], "Type": "Convert warning",
         "Details": "ESI-LAG not modeled", "Line_Text": ""},
    ])
    r = check_init_issues(FakeSession(initIssues=df))
    assert r.passed
    assert "no init errors" in r.summary
    assert "2 warning" in r.summary


def test_init_issues_with_real_error_fails():
    """An actual error row must hard-fail the check, with the warning
    count and false-positive count called out separately."""
    df = pd.DataFrame([
        {"Nodes": ["dc1-leaf1"], "Type": "Convert error",
         "Details": "syntax error in protocol stanza", "Line_Text": "set bogus"},
        {"Nodes": ["dc1-leaf2"], "Type": "Convert warning",
         "Details": "EVPN Type-2 not modeled", "Line_Text": ""},
    ])
    r = check_init_issues(FakeSession(initIssues=df))
    assert not r.passed
    assert "1 init error" in r.summary
    assert "1 warning" in r.summary
    assert "syntax error" in r.detail


def test_init_issues_redflag_warning_does_not_fail():
    """Regression guard: an earlier version of this check matched
    'Red' as a substring of the severity field. That accidentally
    flagged 'Convert warning (redflag)' rows as errors - they're
    warnings (Batfish's tag for "warning we want you to notice"),
    not fatal. Match 'error' only, not 'Red'."""
    df = pd.DataFrame([
        {"Nodes": ["x"], "Type": "Convert warning (redflag)",
         "Details": "feature partially modeled", "Line_Text": ""},
    ])
    r = check_init_issues(FakeSession(initIssues=df))
    assert r.passed
    assert "no init errors" in r.summary
    assert "1 warning" in r.summary


def test_init_issues_known_false_positive_filtered():
    """The same Junos mac-vrf VLAN scope gap that causes the 'vlan'
    undefined_references false positives also produces 'Cannot assign
    access vlan to interface ...' and 'Deactivating irb...' init
    warnings. Both are downstream of the same Batfish parser
    limitation. They must be filtered, not reported."""
    df = pd.DataFrame([
        {"Nodes": ["dc1-leaf1"], "Type": "Convert warning (redflag)",
         "Details": "Cannot assign access vlan to interface ge-0/0/2.0: no vlan-id is assigned to vlan VLAN10",
         "Line_Text": ""},
        {"Nodes": ["dc1-leaf1"], "Type": "Convert warning (redflag)",
         "Details": "Deactivating irb.10 because it has no assigned vlan",
         "Line_Text": ""},
    ])
    r = check_init_issues(FakeSession(initIssues=df))
    assert r.passed
    assert "2 known false positive(s) ignored" in r.summary


def test_init_issues_real_error_not_filtered():
    """An error that doesn't match a false-positive pattern still fails."""
    df = pd.DataFrame([
        {"Nodes": ["x"], "Type": "Convert error",
         "Details": "syntax error in protocol stanza", "Line_Text": "set bogus"},
    ])
    r = check_init_issues(FakeSession(initIssues=df))
    assert not r.passed
    assert "1 init error" in r.summary


def test_init_issues_unknown_severity_column_treated_as_warning():
    """If neither Type nor Severity column exists (older Batfish?),
    we report row count as a warning rather than failing - we'd
    rather not block the deploy on a column-name change."""
    df = pd.DataFrame([
        {"Nodes": ["x"], "Details": "something", "Line_Text": ""},
    ])
    r = check_init_issues(FakeSession(initIssues=df))
    assert r.passed
    assert "1 init issue" in r.summary
    assert "severity column not found" in r.summary


# ----- parse_status --------------------------------------------------

def test_parse_status_all_passed():
    df = pd.DataFrame([
        {"File_Name": "configs/dc1-spine1.cfg", "Status": "PASSED", "File_Format": "JUNIPER"},
        {"File_Name": "configs/dc1-leaf1.cfg",  "Status": "PASSED", "File_Format": "JUNIPER"},
    ])
    bf = FakeSession(fileParseStatus=df)
    r = check_parse_status(bf)
    assert r.passed
    assert "2 file(s) parsed" in r.summary


def test_parse_status_partial_is_warning_not_fail():
    df = pd.DataFrame([
        {"File_Name": "configs/dc1-spine1.cfg", "Status": "PARTIALLY_UNRECOGNIZED", "File_Format": "JUNIPER"},
    ])
    bf = FakeSession(fileParseStatus=df)
    r = check_parse_status(bf)
    assert r.passed  # partial = warning, not fail
    assert "partially unrecognized" in r.summary


def test_parse_status_failed_is_hard_fail():
    df = pd.DataFrame([
        {"File_Name": "configs/dc1-spine1.cfg", "Status": "FAILED", "File_Format": "JUNIPER"},
        {"File_Name": "configs/dc1-leaf1.cfg",  "Status": "PASSED", "File_Format": "JUNIPER"},
    ])
    bf = FakeSession(fileParseStatus=df)
    r = check_parse_status(bf)
    assert not r.passed
    assert "1 file(s) failed to parse" in r.summary


def test_parse_status_empty_is_hard_fail():
    bf = FakeSession(fileParseStatus=pd.DataFrame())
    r = check_parse_status(bf)
    assert not r.passed


# ----- bgp_sessions --------------------------------------------------

def _bgp_session_row(node, local_as, remote_node, remote_as, status, session_type="EBGP_SINGLEHOP"):
    return {
        "Node": node, "VRF": "default",
        "Local_AS": local_as, "Local_IP": "1.1.1.1", "Local_Interface": None,
        "Remote_Node": remote_node, "Remote_AS": remote_as, "Remote_IP": "2.2.2.2",
        "Remote_Interface": None,
        "Address_Families": ["IPV4_UNICAST"],
        "Session_Type": session_type, "Established_Status": status,
    }


def test_bgp_sessions_all_established():
    df = pd.DataFrame([
        _bgp_session_row("dc1-spine1", 65001, "dc1-leaf1", 65003, "ESTABLISHED"),
        _bgp_session_row("dc1-spine1", 65001, "dc1-leaf2", 65004, "ESTABLISHED"),
    ])
    r = check_bgp_sessions(FakeSession(bgpSessionStatus=df))
    assert r.passed
    assert "2/2" in r.summary


def test_bgp_sessions_one_not_established():
    df = pd.DataFrame([
        _bgp_session_row("dc1-spine1", 65001, "dc1-leaf1", 65003, "ESTABLISHED"),
        _bgp_session_row("dc1-spine1", 65001, "dc1-leaf2", 65004, "NOT_ESTABLISHED"),
    ])
    r = check_bgp_sessions(FakeSession(bgpSessionStatus=df))
    assert not r.passed
    assert "1/2" in r.summary


def test_bgp_sessions_empty():
    r = check_bgp_sessions(FakeSession(bgpSessionStatus=pd.DataFrame()))
    assert not r.passed


# ----- bgp_edges_symmetric -------------------------------------------

def test_bgp_edges_symmetric_pass():
    df = pd.DataFrame([
        {"Node": "A", "IP": "10.0.0.1", "Remote_Node": "B", "Remote_IP": "10.0.0.2"},
        {"Node": "B", "IP": "10.0.0.2", "Remote_Node": "A", "Remote_IP": "10.0.0.1"},
    ])
    r = check_bgp_edges_symmetric(FakeSession(bgpEdges=df))
    assert r.passed


def test_bgp_edges_one_sided_fails():
    df = pd.DataFrame([
        {"Node": "A", "IP": "10.0.0.1", "Remote_Node": "B", "Remote_IP": "10.0.0.2"},
        # B doesn't define A
    ])
    r = check_bgp_edges_symmetric(FakeSession(bgpEdges=df))
    assert not r.passed
    assert "asymmetric" in r.summary


# ----- undefined_references ------------------------------------------

def test_undefined_references_clean():
    df = pd.DataFrame()
    r = check_undefined_references(FakeSession(undefinedReferences=df))
    assert r.passed
    assert "no undefined" in r.summary


def test_undefined_references_real_issue_fails():
    df = pd.DataFrame([
        {"File_Name": "configs/dc1-leaf1.cfg", "Struct_Type": "policy-statement",
         "Ref_Name": "EVPN-IMPORT-MISSING", "Context": "vrf-import"},
    ])
    r = check_undefined_references(FakeSession(undefinedReferences=df))
    assert not r.passed
    assert "1 undefined reference" in r.summary


@pytest.mark.parametrize("ignored", IGNORED_REF_STRUCT_TYPES)
def test_undefined_references_ignored_types_dont_fail(ignored):
    """Every struct type in IGNORED_REF_STRUCT_TYPES (currently just
    'vlan') must NOT cause a failure - it's a Batfish parser limitation,
    not a real config bug. Pinned so the ignore list is testable."""
    df = pd.DataFrame([
        {"File_Name": "configs/dc1-leaf1.cfg", "Struct_Type": ignored,
         "Ref_Name": "VLAN10", "Context": "interface vlan"},
    ])
    r = check_undefined_references(FakeSession(undefinedReferences=df))
    assert r.passed
    assert "false positive" in r.summary


def test_undefined_references_mixed_real_and_ignored():
    df = pd.DataFrame([
        {"File_Name": "configs/dc1-leaf1.cfg", "Struct_Type": "vlan",
         "Ref_Name": "VLAN10", "Context": "interface vlan"},
        {"File_Name": "configs/dc1-leaf1.cfg", "Struct_Type": "policy-statement",
         "Ref_Name": "MISSING-POLICY", "Context": "vrf-export"},
    ])
    r = check_undefined_references(FakeSession(undefinedReferences=df))
    assert not r.passed  # the real one fails the check
    assert "1 undefined reference" in r.summary


# ----- overlay_loopback_reachability ---------------------------------

def test_overlay_loopback_uses_session_type_not_as_comparison():
    """Regression guard for the original bug: Local_AS comes back as
    int from Batfish while Remote_AS comes back as str. The earlier
    filter `Local_AS == Remote_AS` always returned False due to type
    mismatch, hiding all iBGP sessions. The fix uses Session_Type
    which is a clean string column. This test pins the fix by
    deliberately mixing int and str AS values."""
    sessions_df = pd.DataFrame([
        # iBGP overlay session - note int Local_AS, str Remote_AS
        {"Node": "dc1-spine1", "VRF": "default", "Local_AS": 65000,
         "Local_IP": "10.1.0.1", "Local_Interface": None,
         "Remote_Node": "dc1-leaf1", "Remote_AS": "65000",
         "Remote_IP": "10.1.0.3", "Remote_Interface": None,
         "Address_Families": ["IPV4_UNICAST"],
         "Session_Type": "IBGP", "Established_Status": "ESTABLISHED"},
    ])
    routes_df = pd.DataFrame([
        {"Node": "dc1-spine1", "Network": "10.1.0.3/32"},
    ])
    bf = FakeSession(bgpSessionStatus=sessions_df, routes=routes_df)
    r = check_overlay_loopback_reachability(bf)
    assert r.passed, (
        f"check returned not-passed: {r.summary}\n"
        f"This is the iBGP filter regression: Session_Type='IBGP' must "
        f"identify the row even when Local_AS/Remote_AS dtypes mismatch."
    )
    assert "1 iBGP overlay peer loopback(s) reachable" in r.summary


def test_overlay_loopback_missing_loopback_fails():
    sessions_df = pd.DataFrame([
        {"Node": "dc1-spine1", "VRF": "default", "Local_AS": 65000,
         "Local_IP": "10.1.0.1", "Local_Interface": None,
         "Remote_Node": "dc1-leaf1", "Remote_AS": "65000",
         "Remote_IP": "10.1.0.3", "Remote_Interface": None,
         "Address_Families": ["IPV4_UNICAST"],
         "Session_Type": "IBGP", "Established_Status": "ESTABLISHED"},
    ])
    # spine1 has no routes at all - 10.1.0.3/32 missing
    routes_df = pd.DataFrame(columns=["Node", "Network"])
    bf = FakeSession(bgpSessionStatus=sessions_df, routes=routes_df)
    r = check_overlay_loopback_reachability(bf)
    assert not r.passed
    assert "10.1.0.3/32" in r.detail


def test_overlay_loopback_no_ibgp_is_pass():
    """A topology with only eBGP underlay (no overlay) is a valid
    state - the check should report 0 sessions and pass, not fail."""
    sessions_df = pd.DataFrame([
        {"Node": "dc1-spine1", "VRF": "default", "Local_AS": 65001,
         "Local_IP": "10.1.4.0", "Local_Interface": None,
         "Remote_Node": "dc1-leaf1", "Remote_AS": "65003",
         "Remote_IP": "10.1.4.1", "Remote_Interface": None,
         "Address_Families": ["IPV4_UNICAST"],
         "Session_Type": "EBGP_SINGLEHOP", "Established_Status": "ESTABLISHED"},
    ])
    routes_df = pd.DataFrame()
    bf = FakeSession(bgpSessionStatus=sessions_df, routes=routes_df)
    r = check_overlay_loopback_reachability(bf)
    assert r.passed
    assert "no iBGP overlay sessions" in r.summary


def test_overlay_loopback_empty_sessions_fails():
    bf = FakeSession(bgpSessionStatus=pd.DataFrame(), routes=pd.DataFrame())
    r = check_overlay_loopback_reachability(bf)
    assert not r.passed


# ----- ip_ownership_conflicts ----------------------------------------

def _ip_owner_row(ip, node, vrf="default", interface="lo0.0", active=True):
    return {"IP": ip, "Node": node, "VRF": vrf, "Interface": interface,
            "Mask": 32, "Active": active}


def test_ip_ownership_no_duplicates_passes():
    df = pd.DataFrame([
        _ip_owner_row("10.1.0.1", "dc1-spine1"),
        _ip_owner_row("10.1.0.2", "dc1-spine2"),
        _ip_owner_row("10.1.0.3", "dc1-leaf1"),
    ])
    r = check_ip_ownership_conflicts(FakeSession(ipOwners=df))
    assert r.passed
    assert "no IP conflicts" in r.summary


def test_ip_ownership_duplicate_loopback_fails():
    """Two devices configured with the same loopback - the bug class
    that breaks the EVPN overlay silently."""
    df = pd.DataFrame([
        _ip_owner_row("10.1.0.3", "dc1-leaf1"),
        _ip_owner_row("10.1.0.3", "dc1-leaf2"),  # collision
    ])
    r = check_ip_ownership_conflicts(FakeSession(ipOwners=df))
    assert not r.passed
    assert "1 IP(s) owned by multiple" in r.summary
    assert "10.1.0.3" in r.detail


def test_ip_ownership_duplicate_p2p_fails():
    """Both ends of a /31 P2P link configured with the same address."""
    df = pd.DataFrame([
        _ip_owner_row("10.1.4.0", "dc1-spine1", interface="ge-0/0/0.0"),
        _ip_owner_row("10.1.4.0", "dc1-leaf1", interface="ge-0/0/0.0"),
    ])
    r = check_ip_ownership_conflicts(FakeSession(ipOwners=df))
    assert not r.passed


def test_ip_ownership_anycast_irb_allowlisted():
    """The lab's anycast gateway lives on irb.<vlan> and is shared
    across both leaves by design (virtual-gateway-address). Must NOT
    be flagged as a conflict."""
    df = pd.DataFrame([
        _ip_owner_row("10.10.10.1", "dc1-leaf1", interface="irb.10"),
        _ip_owner_row("10.10.10.1", "dc1-leaf2", interface="irb.10"),
    ])
    r = check_ip_ownership_conflicts(FakeSession(ipOwners=df))
    assert r.passed
    assert "anycast IRB row(s) allowlisted" in r.summary


def test_ip_ownership_inactive_interface_ignored():
    """An IP on a shutdown interface isn't really owned and shouldn't
    create a conflict report."""
    df = pd.DataFrame([
        _ip_owner_row("10.1.0.3", "dc1-leaf1", active=True),
        _ip_owner_row("10.1.0.3", "dc1-leaf2", active=False),  # shutdown
    ])
    r = check_ip_ownership_conflicts(FakeSession(ipOwners=df))
    assert r.passed


def test_ip_ownership_same_node_same_vrf_not_a_conflict():
    """Multiple addresses on the same (node, VRF) - e.g. a primary
    plus secondary on one interface - is legitimate, not a conflict."""
    df = pd.DataFrame([
        _ip_owner_row("10.1.0.3", "dc1-leaf1", interface="lo0.0"),
        _ip_owner_row("10.1.0.3", "dc1-leaf1", interface="lo0.1"),
    ])
    r = check_ip_ownership_conflicts(FakeSession(ipOwners=df))
    assert r.passed


def test_ip_ownership_empty_passes():
    r = check_ip_ownership_conflicts(FakeSession(ipOwners=pd.DataFrame()))
    assert r.passed


# ----- ALL_CHECKS sanity ---------------------------------------------

def test_all_checks_have_unique_names():
    """Each check returns a CheckResult with a name; names must be
    unique so the report is unambiguous."""
    bf = FakeSession(
        fileParseStatus=pd.DataFrame([
            {"File_Name": "x", "Status": "PASSED", "File_Format": "JUNIPER"}
        ]),
        bgpSessionStatus=pd.DataFrame(),
        bgpEdges=pd.DataFrame(),
        undefinedReferences=pd.DataFrame(),
        routes=pd.DataFrame(),
        ipOwners=pd.DataFrame(),
    )
    names = []
    for check in ALL_CHECKS:
        try:
            r = check(bf)
            names.append(r.name)
        except Exception:
            pass
    assert len(names) == len(set(names)), f"duplicate check names: {names}"


def test_all_checks_callable():
    """Smoke test: every entry in ALL_CHECKS is a callable taking a
    Session and returning a CheckResult-shaped object."""
    for check in ALL_CHECKS:
        assert callable(check)
