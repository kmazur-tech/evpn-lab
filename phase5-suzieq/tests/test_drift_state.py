"""Unit tests for drift/state.py.

state.py reads the SuzieQ parquet store via pyarrow hive partitioning.
We test it by writing tiny real parquet files into a tmp_path
fixture, then asserting the read returns the right shape. This is
a real integration with pyarrow (not a mock) but it's hermetic -
no SuzieQ container, no network, ~50 ms total.

What is intentionally NOT tested:
  - Reading the actual /suzieq/parquet on the live container -
    that's the live integration smoke run at the end of Part B-min,
    not a unit test.
"""
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.state import collect, read_table  # noqa: E402
from drift.state import _cleanup_sq_poller_phantom_rows  # noqa: E402


def _write_table(parquet_dir, table, namespace, hostname, df):
    """Write a DataFrame as the hive-partitioned parquet shape
    SuzieQ uses: <table>/sqvers=N/namespace=<ns>/hostname=<h>/*.parquet

    The hive partition keys (`namespace`, `hostname`) become columns
    automatically when read - so the input df must NOT carry those
    same column names or pyarrow refuses to merge data files with
    conflicting types for the duplicated column. Drop them here so
    the test helper output matches the real SuzieQ on-disk shape."""
    df = df.drop(columns=[c for c in ("namespace", "hostname") if c in df.columns])
    partition = (
        Path(parquet_dir) / table / "sqvers=1.0"
        / f"namespace={namespace}" / f"hostname={hostname}"
    )
    partition.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df), str(partition / "data-0.parquet"))


@pytest.fixture
def populated_parquet(tmp_path):
    """A tiny believable parquet store with two devices and one
    BGP session row each."""
    # device table
    _write_table(tmp_path, "device", "dc1", "dc1-spine1", pd.DataFrame([
        {"hostname": "dc1-spine1", "model": "ex9214", "version": "23.2R1.14",
         "vendor": "Juniper", "status": "alive", "address": "172.16.18.160",
         "timestamp": 1700000000000},
    ]))
    _write_table(tmp_path, "device", "dc1", "dc1-leaf1", pd.DataFrame([
        {"hostname": "dc1-leaf1", "model": "ex9214", "version": "23.2R1.14",
         "vendor": "Juniper", "status": "alive", "address": "172.16.18.162",
         "timestamp": 1700000000000},
    ]))
    # bgp table - fixture must include afi/safi because the real
    # SuzieQ schema PK is (ns, host, vrf, peer, afi, safi).
    _write_table(tmp_path, "bgp", "dc1", "dc1-spine1", pd.DataFrame([
        {"hostname": "dc1-spine1", "vrf": "default", "peer": "10.1.4.1",
         "afi": "ipv4", "safi": "unicast",
         "state": "Established", "timestamp": 1700000000000},
    ]))
    return tmp_path


# ---------------------------------------------------------------------------
# read_table()
# ---------------------------------------------------------------------------

class TestReadTable:
    def test_reads_existing_table(self, populated_parquet):
        df = read_table("device", "dc1", str(populated_parquet),
                        pk=("namespace", "hostname"))
        assert len(df) == 2
        assert set(df["hostname"]) == {"dc1-spine1", "dc1-leaf1"}

    def test_filters_to_namespace(self, populated_parquet):
        # Add a dc2 device that should NOT appear in dc1 read
        _write_table(populated_parquet, "device", "dc2", "dc2-spine1", pd.DataFrame([
            {"hostname": "dc2-spine1", "model": "x", "version": "y",
             "vendor": "z", "status": "alive", "address": "1.1.1.1",
             "timestamp": 1700000000000},
        ]))
        df = read_table("device", "dc1", str(populated_parquet),
                        pk=("namespace", "hostname"))
        assert "dc2-spine1" not in set(df["hostname"])
        assert len(df) == 2

    def test_missing_table_returns_empty_df(self, populated_parquet):
        """First-cycle case: a table not yet polled returns an
        empty DataFrame, not an exception. drift.py needs this
        for graceful handling on a fresh stack."""
        df = read_table("evpnVni", "dc1", str(populated_parquet),
                        pk=("namespace", "hostname"))
        assert df.empty

    def test_latest_row_per_pk(self, populated_parquet):
        """Two timestamped rows for the same full SuzieQ PK
        (ns, host, vrf, peer, afi, safi) - the later one wins.
        This is the SuzieQ view='latest' equivalent we re-
        implement in state.py."""
        _write_table(populated_parquet, "bgp", "dc1", "dc1-spine1", pd.DataFrame([
            {"hostname": "dc1-spine1", "vrf": "default", "peer": "10.1.4.1",
             "afi": "ipv4", "safi": "unicast",
             "state": "NotEstd",       "timestamp": 1700000000000},
            {"hostname": "dc1-spine1", "vrf": "default", "peer": "10.1.4.1",
             "afi": "ipv4", "safi": "unicast",
             "state": "Established",   "timestamp": 1700000999999},
        ]))
        df = read_table("bgp", "dc1", str(populated_parquet),
                        pk=("namespace", "hostname", "vrf",
                            "peer", "afi", "safi"))
        # Only one row should remain after dedup, and it's the
        # later (Established) one
        assert len(df) == 1
        assert df.iloc[0]["state"] == "Established"

    def test_empty_parquet_dir_returns_empty(self, tmp_path):
        """Brand-new install with no data yet - must not raise."""
        df = read_table("device", "dc1", str(tmp_path),
                        pk=("namespace", "hostname"))
        assert df.empty


# ---------------------------------------------------------------------------
# collect() - top-level
# ---------------------------------------------------------------------------

class TestCollect:
    def test_returns_fabric_state_with_all_eight_tables(self, populated_parquet):
        state = collect("dc1", str(populated_parquet), max_age_seconds=None)
        # device + bgp populated by fixture, the other 6 are empty
        assert len(state.devices) == 2
        assert len(state.bgp) == 1
        assert state.interfaces.empty
        assert state.lldp.empty
        # Part B-full additions
        assert state.evpn_vnis.empty
        assert state.routes.empty
        assert state.macs.empty
        assert state.arpnd.empty
        assert state.namespace == "dc1"

    def test_reads_part_b_full_tables_when_present(self, populated_parquet):
        """End-to-end smoke for the new tables: write each one
        with one row, verify state.collect() picks them up."""
        _write_table(populated_parquet, "evpnVni", "dc1", "dc1-leaf1",
                     pd.DataFrame([{"vni": 10010, "type": "L2",
                                    "vlan": 10, "state": "up",
                                    "timestamp": 1700000000000}]))
        _write_table(populated_parquet, "routes", "dc1", "dc1-leaf1",
                     pd.DataFrame([{"vrf": "default",
                                    "prefix": "10.1.0.1/32",
                                    "protocol": "bgp",
                                    "timestamp": 1700000000000}]))
        _write_table(populated_parquet, "macs", "dc1", "dc1-leaf1",
                     pd.DataFrame([{"vlan": 10,
                                    "macaddr": "00:00:5e:00:01:01",
                                    "oif": "esi", "flags": "remote",
                                    "timestamp": 1700000000000}]))
        _write_table(populated_parquet, "arpnd", "dc1", "dc1-leaf1",
                     pd.DataFrame([{"ipAddress": "10.10.10.4",
                                    "macaddr": "2c:6b:f5:41:e8:f0",
                                    "state": "reachable",
                                    "timestamp": 1700000000000}]))

        state = collect("dc1", str(populated_parquet), max_age_seconds=None)
        assert len(state.evpn_vnis) == 1
        assert len(state.routes) == 1
        assert len(state.macs) == 1
        assert len(state.arpnd) == 1

    def test_empty_store_yields_all_empty_dataframes(self, tmp_path):
        state = collect("dc1", str(tmp_path))
        assert state.devices.empty
        assert state.interfaces.empty
        assert state.lldp.empty
        assert state.bgp.empty

    def test_bgp_partial_view_rows_dropped(self, tmp_path):
        """REGRESSION GUARD for the partial-view bug discovered
        live 2026-04-11 during Part C assertion verification, with
        the explanation subsequently corrected after the user
        pointed out the earlier analysis was wrong.

        ## What actually happens

        Verified against vJunos 23.2R1.14 and the SuzieQ bgp
        service yaml directly:

          1. Junos `show bgp neighbor | display json` returns
             EXACTLY one entry per peer (not two). The earlier
             "Junos emits each peer twice" theory was wrong.

          2. SuzieQ's Junos bgp normalize pipeline runs TWO
             commands: `show bgp summary` (extracts vrf +
             per-AFI pfx counts) and `show bgp neighbor`
             (extracts state + per-session fields). Neither
             command's normalize spec extracts both vrf AND
             state in one row.

          3. In steady state the suzieq engine merges the two
             command outputs into one row per (vrf, peer, afi,
             safi). Coalescer keeps the merged rows.

          4. During BGP session state TRANSITIONS (fault ->
             recovery), the pipeline writes partial-view rows
             to raw parquet before the merge completes. The
             partial rows have empty vrf, empty afi, empty safi,
             and state=NotEstd. They are visible to direct
             pyarrow reads but NOT to suzieq-cli.

        ## The fix

        The cleanup hook drops rows where vrf, afi, OR safi is
        empty. An empty-AFI or empty-vrf BGP row is structurally
        meaningless (a session is always in a routing-instance
        and always negotiates a specific AFI/SAFI), so dropping
        them is correct. The bgp read also uses the full schema
        PK (ns, host, vrf, peer, afi, safi) so dedup happens on
        the same key suzieq itself uses.
        """
        _write_table(tmp_path, "bgp", "dc1", "dc1-leaf1", pd.DataFrame([
            # Real merged row
            {"vrf": "default", "peer": "10.1.0.1",
             "afi": "l2vpn", "safi": "evpn",
             "state": "Established", "timestamp": 1700000000000},
            # Partial-view row from the bgp-neighbor command
            # only - state but no vrf/afi/safi
            {"vrf": "", "peer": "10.1.0.1",
             "afi": "", "safi": "",
             "state": "NotEstd", "timestamp": 1700000000000},
        ]))
        df = read_table("bgp", "dc1", str(tmp_path),
                        pk=("namespace", "hostname", "vrf",
                            "peer", "afi", "safi"))
        # Only the real merged row should survive
        assert len(df) == 1
        assert df.iloc[0]["state"] == "Established"
        assert df.iloc[0]["vrf"] == "default"

    def test_bgp_partial_view_any_empty_structural_field(self, tmp_path):
        """Any of (vrf, afi, safi) empty is enough to drop the
        row. Covers the case where the Junos pipeline writes a
        row with vrf='default' but empty afi/safi (which does
        happen during transitions - the vrf comes from the
        summary command's fallback while afi/safi come from the
        rib iteration that hasn't completed yet)."""
        _write_table(tmp_path, "bgp", "dc1", "dc1-leaf1", pd.DataFrame([
            # vrf set but afi/safi missing - still partial
            {"vrf": "default", "peer": "10.1.0.1",
             "afi": "", "safi": "",
             "state": "Established", "timestamp": 1700000000000},
            # The real merged row for the same peer
            {"vrf": "default", "peer": "10.1.0.1",
             "afi": "l2vpn", "safi": "evpn",
             "state": "Established", "timestamp": 1700000000000},
        ]))
        df = read_table("bgp", "dc1", str(tmp_path),
                        pk=("namespace", "hostname", "vrf",
                            "peer", "afi", "safi"))
        assert len(df) == 1
        assert df.iloc[0]["afi"] == "l2vpn"

    def test_bgp_cleanup_handles_all_partial(self, tmp_path):
        """Edge case: every row is partial (unlikely but possible
        during poller first-cycle before any summary command has
        completed). The cleanup must leave the empty DataFrame
        intact rather than crashing."""
        _write_table(tmp_path, "bgp", "dc1", "dc1-leaf1", pd.DataFrame([
            {"vrf": "", "peer": "10.1.0.1",
             "afi": "", "safi": "",
             "state": "Idle", "timestamp": 1700000000000},
        ]))
        df = read_table("bgp", "dc1", str(tmp_path),
                        pk=("namespace", "hostname", "vrf",
                            "peer", "afi", "safi"))
        assert df.empty

    def test_bgp_cleanup_preserves_non_empty_vrfs(self, tmp_path):
        """Multi-VRF routing: a leaf with peers in vrf='default'
        AND vrf='TENANT-1' should keep BOTH rows. The cleanup
        only drops rows where a structural field is empty, not
        non-default vrfs."""
        _write_table(tmp_path, "bgp", "dc1", "dc1-leaf1", pd.DataFrame([
            {"vrf": "default", "peer": "10.1.0.1",
             "afi": "l2vpn", "safi": "evpn",
             "state": "Established", "timestamp": 1700000000000},
            {"vrf": "TENANT-1", "peer": "10.10.10.1",
             "afi": "ipv4", "safi": "unicast",
             "state": "Established", "timestamp": 1700000000000},
        ]))
        df = read_table("bgp", "dc1", str(tmp_path),
                        pk=("namespace", "hostname", "vrf",
                            "peer", "afi", "safi"))
        assert len(df) == 2
        assert set(df["vrf"]) == {"default", "TENANT-1"}

    def test_bgp_pk_distinguishes_same_peer_different_afi(self, tmp_path):
        """REGRESSION GUARD for the independent PK bug. SuzieQ's
        bgp schema PK is (ns, host, vrf, peer, afi, safi). A
        single peer IP has MULTIPLE legitimate rows - one per
        AFI/SAFI combination. In this lab the overlay iBGP peer
        has one l2vpn/evpn row AND the underlay eBGP peer has
        one ipv4/unicast row at the SAME peer IP. The earlier
        4-field PK (ns, host, vrf, peer) was collapsing distinct
        AFI/SAFI rows silently and dropping one arbitrarily
        based on sort stability. The full 6-field PK keeps
        them."""
        _write_table(tmp_path, "bgp", "dc1", "dc1-leaf1", pd.DataFrame([
            # Overlay peer, l2vpn/evpn
            {"vrf": "default", "peer": "10.1.0.1",
             "afi": "l2vpn", "safi": "evpn",
             "state": "Established", "timestamp": 1700000000000},
            # Hypothetical second session on the same peer IP
            # with a different AFI/SAFI. In real Junos this
            # would be the same underlying session negotiating
            # two families, but SuzieQ's schema PK treats them
            # as distinct rows.
            {"vrf": "default", "peer": "10.1.0.1",
             "afi": "ipv4", "safi": "unicast",
             "state": "Established", "timestamp": 1700000000000},
        ]))
        df = read_table("bgp", "dc1", str(tmp_path),
                        pk=("namespace", "hostname", "vrf",
                            "peer", "afi", "safi"))
        assert len(df) == 2
        assert set(zip(df["afi"], df["safi"])) == {
            ("l2vpn", "evpn"), ("ipv4", "unicast"),
        }

    def test_reads_from_both_coalesced_and_raw_dirs(self, tmp_path):
        """The poller writes to <table>/ and the coalescer compacts
        to coalesced/<table>/ then deletes raw. Right after a
        coalescer run, recent rows live in coalesced/ only; right
        before the next run, recent rows live in <table>/ only.
        state.read_table must read both."""
        # Coalesced (older, compacted) row
        _write_table(tmp_path, "coalesced/device", "dc1", "dc1-spine1", pd.DataFrame([
            {"hostname": "dc1-spine1", "model": "x", "version": "y",
             "vendor": "z", "status": "alive", "address": "1.1.1.1",
             "timestamp": 1700000000000},
        ]))
        # Raw (newer, uncoalesced) row for a DIFFERENT device
        _write_table(tmp_path, "device", "dc1", "dc1-leaf1", pd.DataFrame([
            {"hostname": "dc1-leaf1", "model": "x", "version": "y",
             "vendor": "z", "status": "alive", "address": "2.2.2.2",
             "timestamp": 1700000999999},
        ]))
        df = read_table("device", "dc1", str(tmp_path),
                        pk=("namespace", "hostname"))
        assert set(df["hostname"]) == {"dc1-spine1", "dc1-leaf1"}


# ---------------------------------------------------------------------------
# TABLE_REGISTRY contract
# ---------------------------------------------------------------------------
#
# The table registry is the single source of truth for which SuzieQ
# tables we read, their primary keys, the FabricState field they
# land in, and any per-table cleanup hook. These tests pin the
# registry's shape and the invariants a future contributor must
# preserve when adding a new table.

from drift.state import FabricState, TABLE_REGISTRY, TABLES, TableSpec  # noqa: E402


class TestTableRegistry:
    def test_registry_is_non_empty(self):
        assert len(TABLE_REGISTRY) >= 9

    def test_every_spec_has_required_fields(self):
        for spec in TABLE_REGISTRY:
            assert isinstance(spec, TableSpec)
            assert spec.name
            assert isinstance(spec.pk, tuple) and len(spec.pk) >= 2
            assert spec.state_attr
            # cleanup may be None - that's fine

    def test_tables_alias_derived_from_registry(self):
        """TABLES tuple must be derived from TABLE_REGISTRY so
        they can't drift. A manual edit to TABLES would fail this."""
        assert TABLES == tuple(spec.name for spec in TABLE_REGISTRY)

    def test_every_state_attr_maps_to_a_fabricstate_field(self):
        """Every registry entry's state_attr must correspond to an
        actual field on FabricState. Catches the class of bug where
        a contributor adds a new table to the registry but forgets
        to add the matching field to the dataclass - silent failure
        otherwise, because setattr on a non-existent attribute
        just adds it as an instance attr and diff.py never finds it."""
        state = FabricState(namespace="dc1")
        for spec in TABLE_REGISTRY:
            assert hasattr(state, spec.state_attr), (
                f"FabricState has no field {spec.state_attr!r} "
                f"(table {spec.name!r})"
            )
            # Default must be an empty DataFrame so first-cycle
            # runs don't crash.
            assert isinstance(getattr(state, spec.state_attr), pd.DataFrame)

    def test_bgp_spec_has_six_field_pk(self):
        """Regression guard for the schema-correct BGP PK. SuzieQ's
        bgp.avsc declares (namespace, hostname, vrf, peer, afi,
        safi) - an earlier 4-field PK was silently collapsing
        distinct AFI/SAFI rows."""
        bgp_spec = next(s for s in TABLE_REGISTRY if s.name == "bgp")
        assert bgp_spec.pk == (
            "namespace", "hostname", "vrf", "peer", "afi", "safi"
        )

    def test_bgp_spec_has_cleanup_hook(self):
        """The BGP partial-view row filter must be in the registry,
        not inlined in collect(). Verifies the cleanup hook is
        attached to the right table."""
        bgp_spec = next(s for s in TABLE_REGISTRY if s.name == "bgp")
        assert bgp_spec.cleanup is not None
        # And the hook is actually a function the user expects
        assert bgp_spec.cleanup.__name__ == "_cleanup_bgp_phantom_rows"

    def test_sq_poller_spec_has_cleanup_hook(self):
        """sqPoller cleanup drops IP-keyed phantom rows that survive
        SuzieQ's transient-unreachable window post-redeploy. Pin the
        hook here so a future refactor cannot quietly re-introduce
        the false-positive class in assert_poll_health."""
        sp = next(s for s in TABLE_REGISTRY if s.name == "sqPoller")
        assert sp.cleanup is not None
        assert sp.cleanup.__name__ == "_cleanup_sq_poller_phantom_rows"

    def test_only_bgp_and_sq_poller_have_cleanup_in_current_registry(self):
        """Pinning the current shape. If a future contributor adds
        a cleanup to another table, they should update this test
        with intent, not drift past it by accident."""
        tables_with_cleanup = sorted(
            s.name for s in TABLE_REGISTRY if s.cleanup is not None
        )
        assert tables_with_cleanup == ["bgp", "sqPoller"]

    def test_collect_uses_the_registry(self, populated_parquet):
        """Smoke test: collect() reads every table in the registry
        and sets the right FabricState field on each. This is the
        end-to-end check that the refactor didn't lose any tables."""
        state = collect("dc1", str(populated_parquet), max_age_seconds=None)
        # Every spec's state_attr should be a non-None DataFrame
        # (may be empty for tables the fixture doesn't populate).
        for spec in TABLE_REGISTRY:
            attr = getattr(state, spec.state_attr)
            assert isinstance(attr, pd.DataFrame), (
                f"{spec.state_attr} is not a DataFrame after collect()"
            )


class TestReadTableCleanupDispatch:
    """The read_table() call path honors cleanup hooks from both
    the registry (implicit) and explicit kwargs."""

    def test_explicit_cleanup_overrides_registry(self, tmp_path):
        """Passing cleanup=<func> explicitly should override the
        registry. This is what collect() does with spec.cleanup."""
        called = {"count": 0}

        def marker_cleanup(df):
            called["count"] += 1
            return df.iloc[0:0]  # drop all rows

        _write_table(tmp_path, "device", "dc1", "dc1-x", pd.DataFrame([
            {"hostname": "dc1-x", "model": "a", "version": "b",
             "vendor": "c", "status": "alive", "address": "1.1.1.1",
             "timestamp": 1000},
        ]))
        df = read_table(
            "device", "dc1", str(tmp_path),
            pk=("namespace", "hostname"),
            cleanup=marker_cleanup,
        )
        assert called["count"] == 1
        assert df.empty  # the marker cleanup dropped everything


class TestCleanupSqPollerPhantomRows:
    """The sqPoller cleanup drops rows whose hostname is a bare IPv4
    address. SuzieQ writes those during the transient unreachable
    window after a containerlab redeploy: device booting, sshd not
    up yet, host key changed, etc. After the device is reachable,
    SuzieQ uses the real hostname for every subsequent row, so the
    IP-keyed rows are stale by the time assert_poll_health reads
    them. Without this cleanup, the assertion flags those stale
    rows as current poller-falling-behind drifts."""

    def test_drops_bare_ipv4_hostname(self):
        df = pd.DataFrame([
            {"namespace": "dc1", "hostname": "dc1-leaf2",
             "service": "evpnVni", "pollExcdPeriodCount": 0},
            {"namespace": "dc1", "hostname": "172.16.18.163",
             "service": "evpnVni", "pollExcdPeriodCount": 1},
        ])
        out = _cleanup_sq_poller_phantom_rows(df)
        assert len(out) == 1
        assert out["hostname"].iloc[0] == "dc1-leaf2"

    def test_keeps_real_hostname_with_dot(self):
        """Real hostnames may contain dots (FQDNs); only fully-numeric
        4-octet patterns get dropped."""
        df = pd.DataFrame([
            {"namespace": "dc1", "hostname": "leaf2.dc1.example.com",
             "service": "device", "pollExcdPeriodCount": 0},
        ])
        out = _cleanup_sq_poller_phantom_rows(df)
        assert len(out) == 1

    def test_drops_every_ipv4_octet_pattern(self):
        """Triple-digit and single-digit octets all match."""
        df = pd.DataFrame([
            {"namespace": "dc1", "hostname": "1.2.3.4",
             "service": "x", "pollExcdPeriodCount": 1},
            {"namespace": "dc1", "hostname": "192.168.1.1",
             "service": "y", "pollExcdPeriodCount": 1},
            {"namespace": "dc1", "hostname": "10.0.0.0",
             "service": "z", "pollExcdPeriodCount": 1},
        ])
        out = _cleanup_sq_poller_phantom_rows(df)
        assert len(out) == 0

    def test_does_not_drop_partial_ip_lookalikes(self):
        """Hostnames that look IPv4-ish but are not full 4-octet
        addresses (e.g. trailing dot, only 3 octets, embedded text)
        must survive."""
        df = pd.DataFrame([
            {"namespace": "dc1", "hostname": "10.0.0",
             "service": "x", "pollExcdPeriodCount": 0},
            {"namespace": "dc1", "hostname": "host-10.0.0.1",
             "service": "x", "pollExcdPeriodCount": 0},
            {"namespace": "dc1", "hostname": "1.2.3.4.lab",
             "service": "x", "pollExcdPeriodCount": 0},
        ])
        out = _cleanup_sq_poller_phantom_rows(df)
        assert len(out) == 3

    def test_empty_df_passthrough(self):
        out = _cleanup_sq_poller_phantom_rows(pd.DataFrame())
        assert out.empty

    def test_no_hostname_column_passthrough(self):
        """If the schema ever drops `hostname`, the cleanup must not
        crash -- it should just no-op so assert_poll_health surfaces
        the schema-drift warning instead."""
        df = pd.DataFrame([{"namespace": "dc1", "service": "x"}])
        out = _cleanup_sq_poller_phantom_rows(df)
        assert len(out) == 1

    def test_resets_index_after_filtering(self):
        """Drop+keep with a pandas mask leaves a non-contiguous index
        unless we reset_index. The next consumer (drift.diff) does
        positional iteration in places and expects a clean range index."""
        df = pd.DataFrame([
            {"namespace": "dc1", "hostname": "a", "service": "x"},
            {"namespace": "dc1", "hostname": "1.2.3.4", "service": "y"},
            {"namespace": "dc1", "hostname": "b", "service": "z"},
        ])
        out = _cleanup_sq_poller_phantom_rows(df)
        assert list(out.index) == [0, 1]
