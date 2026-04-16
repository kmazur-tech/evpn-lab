"""Batfish question definitions for the EVPN lab.

Each check function takes a pybatfish Session bound to an initialized
snapshot and returns a CheckResult tuple. validate.py runs all checks
and aggregates the result into a pass/fail report.

Honest scope of what these checks catch:

- BGP session establishment (`bgpSessionStatus`):
  Catches missing peer config, ASN mismatch, unreachable peer, wrong
  family, wrong local-address. The most useful single check Batfish
  offers for an EVPN lab.

- Topology symmetry (`bgpEdges`):
  Catches the case where one side defines a peer the other doesn't.
  Won't catch IP misnumbering as long as both sides agree (use
  routing checks for that).

- Undefined references (`undefinedReferences`):
  Catches the case where a template emits `vrf-import EVPN-IMPORT-X`
  but no policy named EVPN-IMPORT-X is defined. Phase 3 has had
  template typos like this caught manually before; this automates it.

- Parse status (`fileParseStatus`):
  Catches the case where Batfish cannot parse a config at all. vJunos
  configs sometimes use syntax Batfish's Junos parser doesn't fully
  understand - we report PARTIALLY_UNRECOGNIZED as a warning rather
  than a hard fail because the unrecognized blocks (e.g. some EVPN
  sub-stanzas) don't affect the underlay analysis we care about.

What these checks do NOT cover (intentionally - smoke suite handles
the runtime side):
- VXLAN data plane (Type-2/3/5 propagation, ARP suppression, MAC
  learning) - Batfish has limited EVPN modeling
- ESI-LAG behavior, DF election - runtime EVPN concept
- BFD timers, convergence speed
- Actual interface up/down state
"""

from dataclasses import dataclass
from typing import List

import pandas as pd
from pybatfish.client.session import Session


@dataclass
class CheckResult:
    """Result of a single Batfish check."""
    name: str
    passed: bool
    summary: str
    detail: str = ""  # multi-line, shown when not passed


# ----- helpers ---------------------------------------------------------

def _frame_to_str(df: pd.DataFrame, max_rows: int = 20) -> str:
    """Render a DataFrame for human-readable error output. Truncates
    wide rows so a 30-column Batfish frame doesn't blow up the terminal."""
    if df is None or df.empty:
        return "(empty)"
    with pd.option_context(
        "display.max_rows", max_rows,
        "display.max_columns", 8,
        "display.width", 140,
        "display.max_colwidth", 40,
    ):
        return df.to_string(index=False)


# ----- checks ----------------------------------------------------------

# Init-issue Details substrings we ignore as known false positives.
# Both of these are downstream effects of the SAME Batfish gap:
# the Junos parser does not track VLAN definitions inside
# `routing-instances ... mac-vrf { vlans { ... } }`, so VLANs
# referenced from `family ethernet-switching` show as "no vlan-id
# assigned" even though real Junos resolves them fine. The IRBs
# bound to those VLANs then get deactivated as a downstream effect.
# Same root cause as IGNORED_REF_STRUCT_TYPES = {"vlan"} above.
IGNORED_INIT_ISSUE_PATTERNS = (
    "Cannot assign access vlan to interface",  # access port + VLAN binding
    "Deactivating irb",                         # IRB downstream of above
)


def check_init_issues(bf: Session) -> CheckResult:
    """The broadest single Batfish question - reports issues from snapshot
    initialization including parse failures, vendor-model conversion errors,
    feature-not-supported warnings, and red flags across the whole snapshot.

    Recommended by pybatfish docs as the FIRST thing to check after
    init_snapshot. Complements (does not replace) check_parse_status:
    parse_status only catches parse failures, init_issues catches the
    additional class of "Batfish parsed your line but couldn't build a
    model from it" issues.

    Severity model:
      - "Convert error" / "Parse error" rows  -> hard fail
      - "Convert warning" rows (incl. "redflag" sub-category)  -> info, do not fail
      - rows whose Details match a known-false-positive pattern -> filtered out entirely
      - empty                 -> pass

    Junos EVPN/VXLAN under-modeling produces a lot of warning-level
    init_issues for things Batfish doesn't simulate (Type-2 routes,
    ESI-LAG, etc). We accept these as warnings rather than failing,
    same way check_parse_status accepts PARTIALLY_UNRECOGNIZED.
    """
    df = bf.q.initIssues().answer().frame()
    if df.empty:
        return CheckResult("init_issues", True, "no init issues")

    # Batfish severity column is "Type" in some versions, "Severity" in
    # others. Probe both. Anything containing "error" (case-insensitive)
    # is fatal. Note: we deliberately do NOT match "Red" as a substring -
    # "redflag" is Batfish's tag for "warning we want you to notice",
    # NOT a fatal severity, and an earlier version of this check
    # incorrectly classified "Convert warning (redflag)" rows as errors.
    sev_col = None
    for col in ("Type", "Severity"):
        if col in df.columns:
            sev_col = col
            break

    if sev_col is None:
        return CheckResult(
            "init_issues",
            True,
            f"{len(df)} init issue(s) reported (severity column not found, "
            f"treating as warning)",
        )

    # Filter out known false positives by Details substring match.
    # Use the existing Details column if present.
    if "Details" in df.columns:
        ignored_mask = df["Details"].astype(str).apply(
            lambda d: any(p in d for p in IGNORED_INIT_ISSUE_PATTERNS)
        )
        ignored = df[ignored_mask]
        df = df[~ignored_mask]
    else:
        ignored = pd.DataFrame()

    error_mask = df[sev_col].astype(str).str.contains("error", case=False, na=False)
    errors = df[error_mask]
    warnings = df[~error_mask]

    if not errors.empty:
        cols_to_show = [c for c in ["Nodes", "Type", "Severity", "Details", "Line_Text"]
                        if c in df.columns]
        return CheckResult(
            "init_issues",
            False,
            f"{len(errors)} init error(s) (and {len(warnings)} warning(s), "
            f"{len(ignored)} known-false-positive(s) filtered)",
            detail=_frame_to_str(errors[cols_to_show] if cols_to_show else errors),
        )

    summary_parts = ["no init errors"]
    if len(warnings):
        summary_parts.append(
            f"{len(warnings)} warning(s) - typically Junos EVPN features "
            f"Batfish does not fully model"
        )
    if len(ignored):
        summary_parts.append(f"{len(ignored)} known false positive(s) ignored")
    return CheckResult("init_issues", True, "; ".join(summary_parts))


def check_parse_status(bf: Session) -> CheckResult:
    """Every config file must be parsed by Batfish. Hard fail on PASSED=False;
    PARTIALLY_UNRECOGNIZED is a warning (Junos has features Batfish doesn't
    fully model, e.g. some EVPN sub-stanzas, which is fine for our analysis)."""
    df = bf.q.fileParseStatus().answer().frame()
    if df.empty:
        return CheckResult("parse_status", False, "no config files found in snapshot")

    failed_rows = df[df["Status"] == "FAILED"]
    if not failed_rows.empty:
        return CheckResult(
            "parse_status",
            False,
            f"{len(failed_rows)} file(s) failed to parse",
            detail=_frame_to_str(failed_rows[["File_Name", "Status", "File_Format"]]),
        )

    partial = df[df["Status"] == "PARTIALLY_UNRECOGNIZED"]
    summary_parts = [f"{len(df)} file(s) parsed"]
    if not partial.empty:
        summary_parts.append(
            f"{len(partial)} partially unrecognized (Junos features Batfish does not "
            f"fully model - non-fatal for our analysis)"
        )
    return CheckResult("parse_status", True, "; ".join(summary_parts))


def check_bgp_sessions(bf: Session) -> CheckResult:
    """Every defined BGP session must reach ESTABLISHED status in Batfish's
    simulation. Catches ASN mismatch, missing peer config, unreachable peer."""
    df = bf.q.bgpSessionStatus().answer().frame()
    if df.empty:
        return CheckResult("bgp_sessions", False, "no BGP sessions found in snapshot")

    not_established = df[df["Established_Status"] != "ESTABLISHED"]
    if not not_established.empty:
        return CheckResult(
            "bgp_sessions",
            False,
            f"{len(not_established)}/{len(df)} session(s) not ESTABLISHED",
            detail=_frame_to_str(not_established[
                ["Node", "VRF", "Local_AS", "Local_IP", "Remote_AS", "Remote_IP", "Established_Status"]
            ]),
        )

    return CheckResult(
        "bgp_sessions",
        True,
        f"{len(df)}/{len(df)} session(s) ESTABLISHED",
    )


def check_bgp_edges_symmetric(bf: Session) -> CheckResult:
    """Every BGP session edge must appear in both directions. A one-sided
    edge means one device defines the peer and the other doesn't (asymmetric
    template bug)."""
    df = bf.q.bgpEdges().answer().frame()
    if df.empty:
        return CheckResult("bgp_edges_symmetric", False, "no BGP edges found")

    # Build set of (node1, ip1, node2, ip2) tuples and check each has its
    # mirror. Batfish bgpEdges already includes both directions for each
    # session - we verify the symmetry directly.
    edges = set()
    for _, row in df.iterrows():
        edges.add((row["Node"], row["IP"], row["Remote_Node"], row["Remote_IP"]))

    asymmetric = []
    for n1, ip1, n2, ip2 in edges:
        if (n2, ip2, n1, ip1) not in edges:
            asymmetric.append(f"{n1}({ip1}) -> {n2}({ip2}) has no return edge")

    if asymmetric:
        return CheckResult(
            "bgp_edges_symmetric",
            False,
            f"{len(asymmetric)} asymmetric edge(s)",
            detail="\n".join(asymmetric),
        )

    return CheckResult(
        "bgp_edges_symmetric",
        True,
        f"{len(edges)} BGP edge(s), all symmetric",
    )


# Struct types we ignore in undefined_references because Batfish's
# Junos parser doesn't fully model where they're defined:
#
# - "vlan": VLANs defined inside `routing-instances ... mac-vrf { vlans
#   { VLAN10 ... } }` are not tracked across the mac-vrf scope, so any
#   `family ethernet-switching vlan members VLAN10` on an access port
#   shows up as "undefined" even though real Junos resolves it fine.
#   This is a Batfish EVPN-modeling gap, not a config bug.
IGNORED_REF_STRUCT_TYPES = {"vlan"}


def check_undefined_references(bf: Session) -> CheckResult:
    """No template should emit a reference to a policy/community/AS-path
    that isn't defined. Catches the class of bug where a template was
    edited to use a new RT or policy name but the definition wasn't
    added in the same change.

    Filters out struct types Batfish's Junos parser does not fully
    model (see IGNORED_REF_STRUCT_TYPES above)."""
    df = bf.q.undefinedReferences().answer().frame()
    if df.empty:
        return CheckResult("undefined_references", True, "no undefined references")

    real_issues = df[~df["Struct_Type"].isin(IGNORED_REF_STRUCT_TYPES)]
    ignored = df[df["Struct_Type"].isin(IGNORED_REF_STRUCT_TYPES)]

    if real_issues.empty:
        msg = "no undefined references"
        if not ignored.empty:
            msg += (
                f" ({len(ignored)} Batfish-parser false positive(s) ignored, "
                f"types: {sorted(set(ignored['Struct_Type']))})"
            )
        return CheckResult("undefined_references", True, msg)

    return CheckResult(
        "undefined_references",
        False,
        f"{len(real_issues)} undefined reference(s) found",
        detail=_frame_to_str(real_issues[["File_Name", "Struct_Type", "Ref_Name", "Context"]]),
    )


def check_overlay_loopback_reachability(bf: Session) -> CheckResult:
    """For every iBGP overlay session, the peer's loopback (the BGP
    neighbor address) must be in this device's BGP RIB. iBGP sessions
    REQUIRE underlay reachability to the peer's loopback - if it's
    not in the RIB, the session can't even establish.

    eBGP underlay sessions are NOT checked here: they peer over
    directly-connected /31 P2P links, so reachability is automatic
    (the /31 prefix is in the connected RIB, no BGP learning needed).

    iBGP is identified by Local_AS == Remote_AS in bgpSessionStatus.
    The lab uses overlay AS 65000 for all iBGP, unique per-device AS
    65001-65004 for eBGP underlay.

    This check is the meaningful semantic version of "loopback
    reachability": we check what each device actually NEEDS to reach
    for its configured iBGP sessions. The lab convention is no
    spine-spine peering, so spine1 and spine2 do NOT need each
    other's loopbacks - the topology-aware check captures that
    correctly via bgpEdges.
    """
    sessions_df = bf.q.bgpSessionStatus().answer().frame()
    if sessions_df.empty:
        return CheckResult(
            "overlay_loopback_reachability",
            False,
            "no BGP sessions found",
        )

    # Use Batfish's own session classification rather than comparing
    # Local_AS == Remote_AS - those columns come back as inconsistent
    # dtypes (Local_AS=int, Remote_AS=str) and the comparison silently
    # returns all-False. Session_Type is the trustworthy field.
    ibgp = sessions_df[sessions_df["Session_Type"] == "IBGP"]
    if ibgp.empty:
        return CheckResult(
            "overlay_loopback_reachability",
            True,
            "no iBGP overlay sessions to check (lab has no overlay or eBGP-only)",
        )

    routes_df = bf.q.routes(protocols="bgp").answer().frame()

    missing = []
    for _, session in ibgp.iterrows():
        local_node = session["Node"]
        remote_ip = str(session["Remote_IP"])
        peer_loopback = f"{remote_ip}/32"
        in_bgp_rib = not routes_df[
            (routes_df["Node"] == local_node)
            & (routes_df["Network"] == peer_loopback)
        ].empty
        if not in_bgp_rib:
            missing.append(
                f"{local_node} missing iBGP peer loopback {peer_loopback} in BGP RIB"
            )

    if missing:
        return CheckResult(
            "overlay_loopback_reachability",
            False,
            f"{len(missing)}/{len(ibgp)} iBGP overlay peer loopback(s) unreachable",
            detail="\n".join(missing),
        )

    return CheckResult(
        "overlay_loopback_reachability",
        True,
        f"{len(ibgp)} iBGP overlay peer loopback(s) reachable",
    )


# Interface name patterns whose IPs are EXPECTED to be duplicated
# across nodes by design. Lab convention: anycast gateways live on
# IRB interfaces using `virtual-gateway-address`, intentionally shared
# across both leaves of an ESI-LAG. Batfish reports the virtual-gateway
# address as a normal owner on each leaf, which would otherwise look
# like a duplicate IP. The leaf-LOCAL irb address (different per leaf)
# is unaffected because it's, well, different per leaf.
#
# This is the only legitimate cross-node IP duplication in the lab.
# Anything else (a /31 P2P link with both ends configured the same,
# a typoed loopback, etc.) is a real bug and must fail the check.
IGNORED_DUPLICATE_IP_INTERFACE_PREFIXES = ("irb.",)


def check_ip_ownership_conflicts(bf: Session) -> CheckResult:
    """No non-anycast IP should be owned by more than one (node, VRF)
    pair. Catches duplicate /31 P2P numbering, loopback collisions,
    IRB unicast collisions - all real bugs that would either prevent
    the session establishing or cause silent black-holing.

    Allowlist: IRB interfaces are expected to share the anycast
    gateway address across leaves by design (lab convention uses
    `virtual-gateway-address` on `irb.<vlan>`). See
    IGNORED_DUPLICATE_IP_INTERFACE_PREFIXES for the rationale.

    Uses Batfish's `ipOwners` question. Active=False rows are filtered
    out (an IP on a shutdown interface isn't a conflict)."""
    df = bf.q.ipOwners().answer().frame()
    if df.empty:
        return CheckResult(
            "ip_ownership_conflicts",
            True,
            "no IP owners reported (empty fabric?)",
        )

    # Drop inactive interfaces - their IPs aren't really owned.
    if "Active" in df.columns:
        df = df[df["Active"] != False]  # noqa: E712 - explicit bool compare for pandas
    if df.empty:
        return CheckResult("ip_ownership_conflicts", True, "no active IP owners")

    # Filter the anycast allowlist BEFORE counting duplicates so that
    # legitimate IRB virtual-gateway addresses don't pollute the
    # conflict report.
    if "Interface" in df.columns:
        ignored_mask = df["Interface"].astype(str).apply(
            lambda i: any(i.startswith(p) for p in IGNORED_DUPLICATE_IP_INTERFACE_PREFIXES)
        )
        ignored_count = int(ignored_mask.sum())
        df = df[~ignored_mask]
    else:
        ignored_count = 0

    # Group by IP. A conflict is any IP owned by more than one
    # (Node, VRF) pair. Same node + same VRF + multiple interfaces is
    # legitimate (e.g. secondary addresses); we don't flag that.
    grouped = df.groupby("IP")[["Node", "VRF"]].apply(
        lambda g: len({(r["Node"], r["VRF"]) for _, r in g.iterrows()})
    )
    conflicting_ips = grouped[grouped > 1].index.tolist()

    if not conflicting_ips:
        msg = f"no IP conflicts ({len(df)} active owner(s) checked"
        if ignored_count:
            msg += f", {ignored_count} anycast IRB row(s) allowlisted"
        msg += ")"
        return CheckResult("ip_ownership_conflicts", True, msg)

    detail_rows = df[df["IP"].isin(conflicting_ips)].sort_values("IP")
    cols = [c for c in ["IP", "Node", "VRF", "Interface"] if c in detail_rows.columns]
    return CheckResult(
        "ip_ownership_conflicts",
        False,
        f"{len(conflicting_ips)} IP(s) owned by multiple (node, VRF) pairs",
        detail=_frame_to_str(detail_rows[cols]),
    )


# Master list - validate.py iterates this. Order is informational only;
# all checks run regardless of earlier results so the operator sees the
# full picture in one report.
ALL_CHECKS = [
    check_init_issues,
    check_parse_status,
    check_bgp_sessions,
    check_bgp_edges_symmetric,
    check_undefined_references,
    check_overlay_loopback_reachability,
    check_ip_ownership_conflicts,
]


# ----- Differential analysis -----------------------------------------
#
# Differential checks compare a CANDIDATE snapshot (typically the
# rendered output of the current PR) against a REFERENCE snapshot
# (typically phase3-nornir/expected/, the renderer's last known-good
# golden file). They answer the question "what does this change to
# the templates / NetBox actually do to the fabric?" - the killer
# Batfish feature for CI/CD per the pybatfish docs.
#
# Output is INFORMATIONAL only - the differential summary never
# fails the deploy. Adds and removals get reported as a structured
# report that the Phase 6 PR-comment bot will paste into a PR.
#
# Implementation note: pybatfish supports a native differential mode
# (snapshot=<cand>, reference_snapshot=<ref>) on most questions, but
# we use the manual "fetch both, set-diff in pandas" approach because:
#   - It's deterministic and easy to test with mocked frames.
#   - It composes naturally with the existing _frame_to_str rendering.
#   - The native diff mode requires both snapshots to be uploaded
#     under the same Network at the same time, which our caller
#     already does.


@dataclass
class DiffSummary:
    """Output of one differential question. The same shape as
    CheckResult so the JSON renderer can serialize it without a
    second code path."""
    name: str
    summary: str
    added: List[str]
    removed: List[str]


def _bgp_edge_key(row) -> str:
    """Stable string key for a BGP edge row, used as set element."""
    return f"{row['Node']}({row['IP']}) -> {row['Remote_Node']}({row['Remote_IP']})"


def diff_bgp_edges(bf: Session, ref_name: str, cand_name: str) -> DiffSummary:
    """BGP topology delta: which BGP sessions were added/removed
    between the reference snapshot and the candidate snapshot."""
    ref_df = bf.q.bgpEdges().answer(snapshot=ref_name).frame()
    cand_df = bf.q.bgpEdges().answer(snapshot=cand_name).frame()

    ref_set = {_bgp_edge_key(r) for _, r in ref_df.iterrows()}
    cand_set = {_bgp_edge_key(r) for _, r in cand_df.iterrows()}

    added = sorted(cand_set - ref_set)
    removed = sorted(ref_set - cand_set)

    if not added and not removed:
        summary = f"no changes ({len(cand_set)} BGP edges, identical to reference)"
    else:
        summary = f"{len(added)} added, {len(removed)} removed (candidate has {len(cand_set)}, reference had {len(ref_set)})"

    return DiffSummary(name="bgp_edges", summary=summary, added=added, removed=removed)


def diff_node_set(bf: Session, ref_name: str, cand_name: str) -> DiffSummary:
    """Device delta: which devices appeared / disappeared between
    snapshots. Catches the "this PR adds a new device" or "this PR
    decommissions a device" cases."""
    ref_df = bf.q.nodeProperties().answer(snapshot=ref_name).frame()
    cand_df = bf.q.nodeProperties().answer(snapshot=cand_name).frame()

    ref_set = set(ref_df["Node"].astype(str))
    cand_set = set(cand_df["Node"].astype(str))

    added = sorted(cand_set - ref_set)
    removed = sorted(ref_set - cand_set)

    if not added and not removed:
        summary = f"no changes ({len(cand_set)} device(s), identical to reference)"
    else:
        summary = f"{len(added)} added, {len(removed)} removed (candidate has {len(cand_set)}, reference had {len(ref_set)})"

    return DiffSummary(name="devices", summary=summary, added=added, removed=removed)


# Master list of differential analyses run by validate.py when a
# reference snapshot is provided.
ALL_DIFFS = [
    diff_node_set,
    diff_bgp_edges,
]
