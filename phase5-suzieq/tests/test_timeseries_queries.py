"""Unit tests for drift/timeseries/queries/.

The query layer is pure: each query takes a WindowedTable, returns
a TimeseriesResult. No pyarrow, no parquet, no filesystem. Tests
build inline DataFrame fixtures and assert the result shape.

Why this file is so cheap to run: same reason drift/diff.py tests
are cheap. The boundary modules (reader.py for parquet, intent.py
for pynetbox) handle the heavy I/O once; the queries downstream
consume the result via plain DataFrames and never need the heavy
deps installed.

Coverage:
  - bgp_flap_count: empty, no flaps, single flap, multiple flaps,
    multiple sessions, missing column defensive shape, summary
    counters
  - route_churn: empty, all stable (no churn), some churned, summary
  - mac_mobility: empty, no moves, one move, VTEP-only move, summary
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.timeseries.queries import (  # noqa: E402
    QUERIES,
    QueryEntry,
    TimeseriesResult,
    bgp_flap_count,
    mac_mobility,
    route_churn,
)
from drift.timeseries.reader import TimeWindow, WindowedTable  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bgp_wt(rows):
    """Wrap a list of dicts as a WindowedTable for the bgp table."""
    return WindowedTable(
        table="bgp", namespace="dc1",
        window=TimeWindow(0, 10000),
        rows=pd.DataFrame(rows),
        files_read=1,
    )


def _routes_wt(rows):
    return WindowedTable(
        table="routes", namespace="dc1",
        window=TimeWindow(0, 10000),
        rows=pd.DataFrame(rows),
        files_read=1,
    )


def _macs_wt(rows):
    return WindowedTable(
        table="macs", namespace="dc1",
        window=TimeWindow(0, 10000),
        rows=pd.DataFrame(rows),
        files_read=1,
    )


def _bgp_row(host, peer, ts, state="Established", vrf="default",
             afi="ipv4", safi="unicast"):
    return {
        "hostname": host, "peer": peer, "vrf": vrf,
        "afi": afi, "safi": safi,
        "state": state, "timestamp": ts,
    }


# ---------------------------------------------------------------------------
# bgp_flap_count
# ---------------------------------------------------------------------------

class TestBgpFlapCount:
    def test_empty_window_returns_empty_result(self):
        wt = WindowedTable(
            table="bgp", namespace="dc1",
            window=TimeWindow(0, 10),
            rows=pd.DataFrame(),
        )
        out = bgp_flap_count(wt)
        assert out.name == "bgp_flaps"
        assert out.table == "bgp"
        assert out.rows.empty
        assert out.summary == {
            "total_flaps": 0,
            "sessions_with_flaps": 0,
            "sessions_seen": 0,
        }

    def test_no_state_changes_is_no_flap(self):
        wt = _bgp_wt([
            _bgp_row("dc1-leaf1", "10.0.0.2", 1000),
            _bgp_row("dc1-leaf1", "10.0.0.2", 2000),
            _bgp_row("dc1-leaf1", "10.0.0.2", 3000),
        ])
        out = bgp_flap_count(wt)
        assert out.rows.empty
        assert out.summary["total_flaps"] == 0
        assert out.summary["sessions_with_flaps"] == 0
        assert out.summary["sessions_seen"] == 1

    def test_single_flap_caught(self):
        wt = _bgp_wt([
            _bgp_row("dc1-leaf1", "10.0.0.2", 1000, state="Established"),
            _bgp_row("dc1-leaf1", "10.0.0.2", 2000, state="Idle"),
            _bgp_row("dc1-leaf1", "10.0.0.2", 3000, state="Established"),
        ])
        out = bgp_flap_count(wt)
        assert len(out.rows) == 1
        row = out.rows.iloc[0]
        assert row["hostname"] == "dc1-leaf1"
        assert row["peer"] == "10.0.0.2"
        assert row["flap_count"] == 2  # Established->Idle and Idle->Established
        assert row["snapshots"] == 3
        assert row["first_state"] == "established"
        assert row["last_state"] == "established"
        assert out.summary["total_flaps"] == 2
        assert out.summary["sessions_with_flaps"] == 1
        assert out.summary["sessions_seen"] == 1

    def test_case_insensitive_state_comparison(self):
        # Established vs established must NOT count as a flap.
        # Different vendors capitalize differently and the SuzieQ
        # normalize layer doesn't always lowercase.
        wt = _bgp_wt([
            _bgp_row("dc1-leaf1", "10.0.0.2", 1000, state="Established"),
            _bgp_row("dc1-leaf1", "10.0.0.2", 2000, state="established"),
            _bgp_row("dc1-leaf1", "10.0.0.2", 3000, state="ESTABLISHED"),
        ])
        out = bgp_flap_count(wt)
        assert out.rows.empty
        assert out.summary["total_flaps"] == 0

    def test_multiple_sessions_each_independent(self):
        wt = _bgp_wt([
            _bgp_row("dc1-leaf1", "10.0.0.2", 1000, state="Established"),
            _bgp_row("dc1-leaf1", "10.0.0.2", 2000, state="Idle"),
            _bgp_row("dc1-leaf1", "10.0.0.3", 1000, state="Established"),
            _bgp_row("dc1-leaf1", "10.0.0.3", 2000, state="Established"),
        ])
        out = bgp_flap_count(wt)
        # Only the first session flapped
        assert len(out.rows) == 1
        assert out.rows.iloc[0]["peer"] == "10.0.0.2"
        assert out.summary["sessions_seen"] == 2
        assert out.summary["sessions_with_flaps"] == 1

    def test_distinct_afi_safi_are_distinct_sessions(self):
        # Same peer, different AFI/SAFI = different bgp rows in
        # SuzieQ's PK model. They flap independently.
        wt = _bgp_wt([
            _bgp_row("dc1-leaf1", "10.0.0.2", 1000, afi="ipv4", safi="unicast",
                     state="Established"),
            _bgp_row("dc1-leaf1", "10.0.0.2", 2000, afi="ipv4", safi="unicast",
                     state="Idle"),
            _bgp_row("dc1-leaf1", "10.0.0.2", 1000, afi="l2vpn", safi="evpn",
                     state="Established"),
            _bgp_row("dc1-leaf1", "10.0.0.2", 2000, afi="l2vpn", safi="evpn",
                     state="Established"),
        ])
        out = bgp_flap_count(wt)
        assert out.summary["sessions_seen"] == 2
        assert out.summary["sessions_with_flaps"] == 1

    def test_missing_columns_returns_warning_summary(self):
        # Defensive: a malformed bgp DataFrame must not crash the
        # whole timeseries run. Returns empty rows + a warning in
        # the summary the envelope can surface.
        wt = WindowedTable(
            table="bgp", namespace="dc1",
            window=TimeWindow(0, 10),
            rows=pd.DataFrame([{"foo": 1}]),
            files_read=1,
        )
        out = bgp_flap_count(wt)
        assert out.rows.empty
        assert "warning" in out.summary
        assert "missing bgp columns" in out.summary["warning"]


# ---------------------------------------------------------------------------
# route_churn
# ---------------------------------------------------------------------------

class TestRouteChurn:
    def test_empty_window(self):
        wt = WindowedTable(
            table="routes", namespace="dc1",
            window=TimeWindow(0, 10),
            rows=pd.DataFrame(),
        )
        out = route_churn(wt)
        assert out.name == "route_churn"
        assert out.rows.empty
        assert out.summary["total_prefixes_touched"] == 0
        assert out.summary["total_churned_prefixes"] == 0
        assert out.summary["total_changes"] == 0
        assert out.summary["vrfs_seen"] == 0

    def test_all_stable_one_update_per_prefix(self):
        # Each prefix has exactly one row in the window. That's a
        # stable observation, not churn.
        wt = _routes_wt([
            {"hostname": "dc1-leaf1", "vrf": "TENANT-1", "prefix": "10.10.10.0/24"},
            {"hostname": "dc1-leaf1", "vrf": "TENANT-1", "prefix": "10.10.20.0/24"},
            {"hostname": "dc1-leaf2", "vrf": "TENANT-1", "prefix": "10.10.10.0/24"},
        ])
        out = route_churn(wt)
        assert out.summary["total_prefixes_touched"] == 3
        assert out.summary["total_churned_prefixes"] == 0
        assert out.summary["total_changes"] == 0
        assert out.summary["vrfs_seen"] == 2  # two (host,vrf) groups

    def test_one_churned_prefix(self):
        wt = _routes_wt([
            {"hostname": "dc1-leaf1", "vrf": "TENANT-1", "prefix": "10.10.10.0/24"},
            {"hostname": "dc1-leaf1", "vrf": "TENANT-1", "prefix": "10.10.10.0/24"},
            {"hostname": "dc1-leaf1", "vrf": "TENANT-1", "prefix": "10.10.10.0/24"},
            {"hostname": "dc1-leaf1", "vrf": "TENANT-1", "prefix": "10.10.20.0/24"},
        ])
        out = route_churn(wt)
        assert out.summary["total_prefixes_touched"] == 2
        assert out.summary["total_churned_prefixes"] == 1
        assert out.summary["total_changes"] == 3  # the 3 update rows
        assert len(out.rows) == 1
        row = out.rows.iloc[0]
        assert row["hostname"] == "dc1-leaf1"
        assert row["churned_prefixes"] == 1
        assert row["prefixes_touched"] == 2

    def test_multiple_vrfs_separate_rollup(self):
        wt = _routes_wt([
            {"hostname": "dc1-leaf1", "vrf": "TENANT-1", "prefix": "10.10.10.0/24"},
            {"hostname": "dc1-leaf1", "vrf": "TENANT-2", "prefix": "10.20.10.0/24"},
        ])
        out = route_churn(wt)
        assert out.summary["vrfs_seen"] == 2
        assert len(out.rows) == 2

    def test_missing_columns_warning(self):
        wt = WindowedTable(
            table="routes", namespace="dc1",
            window=TimeWindow(0, 10),
            rows=pd.DataFrame([{"foo": 1}]),
            files_read=1,
        )
        out = route_churn(wt)
        assert out.rows.empty
        assert "warning" in out.summary


# ---------------------------------------------------------------------------
# mac_mobility
# ---------------------------------------------------------------------------

class TestMacMobility:
    def test_empty_window(self):
        wt = WindowedTable(
            table="macs", namespace="dc1",
            window=TimeWindow(0, 10),
            rows=pd.DataFrame(),
        )
        out = mac_mobility(wt)
        assert out.name == "mac_mobility"
        assert out.rows.empty
        assert out.summary == {"macs_moved": 0, "macs_seen": 0}

    def test_no_moves_one_location_per_mac(self):
        wt = _macs_wt([
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/2", "remoteVtepIp": ""},
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:02",
             "hostname": "dc1-leaf2", "oif": "ge-0/0/2", "remoteVtepIp": ""},
        ])
        out = mac_mobility(wt)
        assert out.rows.empty
        assert out.summary["macs_moved"] == 0
        assert out.summary["macs_seen"] == 2

    def test_same_mac_seen_locally_and_via_vtep_is_not_a_move(self):
        # In a healthy EVPN fabric, a MAC appears LOCAL on its
        # owning leaf AND REMOTE (via the owner's VTEP) on every
        # other leaf. That's TWO different (host, oif, vtep)
        # tuples - we DO count this as a move at the moment.
        # The current contract is "more than one distinct
        # location during the window means a move". Refining to
        # "ignore the local-vs-vtep duality" is a Phase D2 polish.
        # Test pinned here so we notice if we change behavior.
        wt = _macs_wt([
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/2", "remoteVtepIp": ""},
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf2", "oif": "vtep.32769", "remoteVtepIp": "10.1.0.3"},
        ])
        out = mac_mobility(wt)
        # 2 distinct locations seen
        assert len(out.rows) == 1
        assert out.rows.iloc[0]["distinct_locations"] == 2

    def test_real_move_same_host_different_oif(self):
        wt = _macs_wt([
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/2", "remoteVtepIp": ""},
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/3", "remoteVtepIp": ""},
        ])
        out = mac_mobility(wt)
        assert len(out.rows) == 1
        row = out.rows.iloc[0]
        assert row["distinct_locations"] == 2
        assert "ge-0/0/2" in row["oifs"]
        assert "ge-0/0/3" in row["oifs"]

    def test_distinct_macs_independent(self):
        wt = _macs_wt([
            # MAC 1 - moved
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/2", "remoteVtepIp": ""},
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/3", "remoteVtepIp": ""},
            # MAC 2 - stable
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:02",
             "hostname": "dc1-leaf2", "oif": "ge-0/0/2", "remoteVtepIp": ""},
        ])
        out = mac_mobility(wt)
        assert out.summary["macs_moved"] == 1
        assert out.summary["macs_seen"] == 2

    def test_dedup_within_one_location(self):
        # Multiple snapshots of the SAME MAC at the SAME location
        # collapse to one location, NOT counted as a move.
        wt = _macs_wt([
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/2", "remoteVtepIp": ""},
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/2", "remoteVtepIp": ""},
            {"vlan": 10, "macaddr": "aa:bb:cc:00:00:01",
             "hostname": "dc1-leaf1", "oif": "ge-0/0/2", "remoteVtepIp": ""},
        ])
        out = mac_mobility(wt)
        assert out.rows.empty
        assert out.summary["macs_moved"] == 0

    def test_missing_columns_warning(self):
        wt = WindowedTable(
            table="macs", namespace="dc1",
            window=TimeWindow(0, 10),
            rows=pd.DataFrame([{"foo": 1}]),
            files_read=1,
        )
        out = mac_mobility(wt)
        assert out.rows.empty
        assert "warning" in out.summary


# ---------------------------------------------------------------------------
# QUERIES registry
# ---------------------------------------------------------------------------

class TestQueriesRegistry:
    def test_three_canonical_queries_registered(self):
        assert set(QUERIES.keys()) == {"bgp_flaps", "route_churn", "mac_mobility"}

    def test_each_entry_has_table_and_callable(self):
        for name, entry in QUERIES.items():
            assert isinstance(entry, QueryEntry)
            assert entry.name == name
            assert entry.table in {"bgp", "routes", "macs"}
            assert callable(entry.fn)

    def test_each_query_handles_empty_input_without_crashing(self):
        # Smoke test the whole registry against an empty WindowedTable.
        for name, entry in QUERIES.items():
            wt = WindowedTable(
                table=entry.table, namespace="dc1",
                window=TimeWindow(0, 10),
                rows=pd.DataFrame(),
            )
            out = entry.fn(wt)
            assert isinstance(out, TimeseriesResult)
            assert out.name == name
            assert out.table == entry.table
            assert out.rows.empty
