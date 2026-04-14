"""Unit tests for differential analysis (diff_bgp_edges, diff_node_set).

The differential layer compares two Batfish snapshots and reports
what changed between them. Phase 6's PR-comment bot will consume
these as the "what does this PR do to the fabric?" report.

Tests use a per-snapshot fake session - the FakeSession's question
methods accept the standard pybatfish kwarg `snapshot=<name>` and
return the canned frame for that snapshot.
"""
from dataclasses import dataclass

import pandas as pd
import pytest

from questions import (
    ALL_DIFFS,
    DiffSummary,
    diff_bgp_edges,
    diff_node_set,
)


# ----- Fakes ---------------------------------------------------------

@dataclass
class _FakeAnswer:
    df: pd.DataFrame
    def frame(self): return self.df


class _FakeSnapshotAwareQ:
    """Returned by fake_session.q.<question_name>(). Captures the
    snapshot kwarg passed to .answer() and returns the canned frame
    for that snapshot."""
    def __init__(self, frames_by_snapshot):
        self._frames = frames_by_snapshot

    def answer(self, snapshot=None, **_kw):
        if snapshot is None:
            # Single-snapshot mode (legacy callers)
            return _FakeAnswer(next(iter(self._frames.values())))
        return _FakeAnswer(self._frames[snapshot])


class _FakeQNamespace:
    def __init__(self, frames_by_question):
        self._frames = frames_by_question
    def __getattr__(self, name):
        if name not in self._frames:
            raise AttributeError(f"no canned frame for question {name}")
        frames = self._frames[name]
        return lambda *_a, **_kw: _FakeSnapshotAwareQ(frames)


class FakeDiffSession:
    """Drop-in pybatfish Session stand-in for differential tests.
    Construct with a dict mapping question_name -> {snapshot_name: df}."""
    def __init__(self, **frames):
        self.q = _FakeQNamespace(frames)


def _bgp_edges_df(rows):
    return pd.DataFrame([
        {"Node": n, "IP": ip, "Remote_Node": rn, "Remote_IP": rip}
        for n, ip, rn, rip in rows
    ])


def _node_props_df(nodes):
    return pd.DataFrame([{"Node": n} for n in nodes])


# ----- diff_bgp_edges ------------------------------------------------

def test_diff_bgp_edges_no_change():
    edges = [
        ("dc1-spine1", "10.1.4.0", "dc1-leaf1", "10.1.4.1"),
        ("dc1-leaf1", "10.1.4.1", "dc1-spine1", "10.1.4.0"),
    ]
    bf = FakeDiffSession(bgpEdges={"ref": _bgp_edges_df(edges), "cand": _bgp_edges_df(edges)})
    d = diff_bgp_edges(bf, "ref", "cand")
    assert d.added == []
    assert d.removed == []
    assert "no changes" in d.summary
    assert "2 BGP edges" in d.summary


def test_diff_bgp_edges_added():
    ref = [("A", "1.1.1.1", "B", "2.2.2.2")]
    cand = [
        ("A", "1.1.1.1", "B", "2.2.2.2"),
        ("A", "1.1.1.1", "C", "3.3.3.3"),  # new
    ]
    bf = FakeDiffSession(bgpEdges={"ref": _bgp_edges_df(ref), "cand": _bgp_edges_df(cand)})
    d = diff_bgp_edges(bf, "ref", "cand")
    assert d.removed == []
    assert d.added == ["A(1.1.1.1) -> C(3.3.3.3)"]
    assert "1 added" in d.summary


def test_diff_bgp_edges_removed():
    ref = [
        ("A", "1.1.1.1", "B", "2.2.2.2"),
        ("A", "1.1.1.1", "C", "3.3.3.3"),
    ]
    cand = [("A", "1.1.1.1", "B", "2.2.2.2")]
    bf = FakeDiffSession(bgpEdges={"ref": _bgp_edges_df(ref), "cand": _bgp_edges_df(cand)})
    d = diff_bgp_edges(bf, "ref", "cand")
    assert d.added == []
    assert d.removed == ["A(1.1.1.1) -> C(3.3.3.3)"]
    assert "1 removed" in d.summary


def test_diff_bgp_edges_added_and_removed():
    ref = [("A", "1.1.1.1", "B", "2.2.2.2")]
    cand = [("A", "1.1.1.1", "C", "3.3.3.3")]
    bf = FakeDiffSession(bgpEdges={"ref": _bgp_edges_df(ref), "cand": _bgp_edges_df(cand)})
    d = diff_bgp_edges(bf, "ref", "cand")
    assert d.added == ["A(1.1.1.1) -> C(3.3.3.3)"]
    assert d.removed == ["A(1.1.1.1) -> B(2.2.2.2)"]
    assert "1 added" in d.summary
    assert "1 removed" in d.summary


def test_diff_bgp_edges_empty_candidate():
    ref = [("A", "1.1.1.1", "B", "2.2.2.2")]
    bf = FakeDiffSession(bgpEdges={"ref": _bgp_edges_df(ref), "cand": _bgp_edges_df([])})
    d = diff_bgp_edges(bf, "ref", "cand")
    assert len(d.removed) == 1
    assert d.added == []


# ----- diff_node_set -------------------------------------------------

def test_diff_node_set_no_change():
    nodes = ["dc1-spine1", "dc1-spine2", "dc1-leaf1", "dc1-leaf2"]
    bf = FakeDiffSession(nodeProperties={"ref": _node_props_df(nodes), "cand": _node_props_df(nodes)})
    d = diff_node_set(bf, "ref", "cand")
    assert d.added == []
    assert d.removed == []
    assert "no changes" in d.summary
    assert "4 device(s)" in d.summary


def test_diff_node_set_device_added():
    ref = ["dc1-spine1", "dc1-leaf1"]
    cand = ["dc1-spine1", "dc1-leaf1", "dc1-leaf3"]
    bf = FakeDiffSession(nodeProperties={"ref": _node_props_df(ref), "cand": _node_props_df(cand)})
    d = diff_node_set(bf, "ref", "cand")
    assert d.added == ["dc1-leaf3"]
    assert d.removed == []


def test_diff_node_set_device_removed():
    ref = ["dc1-spine1", "dc1-leaf1", "dc1-leaf3"]
    cand = ["dc1-spine1", "dc1-leaf1"]
    bf = FakeDiffSession(nodeProperties={"ref": _node_props_df(ref), "cand": _node_props_df(cand)})
    d = diff_node_set(bf, "ref", "cand")
    assert d.added == []
    assert d.removed == ["dc1-leaf3"]


# ----- ALL_DIFFS contract --------------------------------------------

def test_all_diffs_callable_and_unique_names():
    nodes = ["x"]
    edges = [("A", "1.1.1.1", "B", "2.2.2.2")]
    bf = FakeDiffSession(
        nodeProperties={"r": _node_props_df(nodes), "c": _node_props_df(nodes)},
        bgpEdges={"r": _bgp_edges_df(edges), "c": _bgp_edges_df(edges)},
    )
    names = []
    for diff_fn in ALL_DIFFS:
        d = diff_fn(bf, "r", "c")
        assert isinstance(d, DiffSummary)
        names.append(d.name)
    assert len(names) == len(set(names)), f"duplicate diff names: {names}"


def test_diff_summary_dataclass_shape():
    """Pin the shape so the JSON serializer in render_json_report
    keeps working without surprise field changes."""
    d = DiffSummary(name="x", summary="s", added=["a"], removed=["b"])
    assert d.name == "x"
    assert d.added == ["a"]
    assert d.removed == ["b"]
