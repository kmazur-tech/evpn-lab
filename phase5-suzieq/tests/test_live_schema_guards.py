"""Phase 5.1 live-only schema regression guards.

These tests run only when the `live` pytest marker is enabled and
point at a real SuzieQ parquet store. Their job is to catch schema
drift between our production code and upstream SuzieQ - the class
of bug that offline unit tests cannot see because the unit tests
build their own parquet fixtures.

## Why this file exists

Phase 5 Part C's first live run surfaced a silent bug in
assert_vtep_remote_count: the production code was reading the
`remoteVtepCnt` column, which turned out to be computed by the
SuzieQ pandas engine at query time, NOT stored in the raw parquet
file. Direct pyarrow reads saw `None` for the column and the
assertion defaulted the count to 0, producing 4 false-positive
drifts on a clean fabric.

Unit tests never caught it because the fixtures hand-build parquet
files that include whichever columns the test wants to assert on.
The live store has a DIFFERENT column set. The gap is a schema
divergence only a live test can close.

This file is that live test. It runs only when `live` marker is
enabled and `SUZIEQ_LIVE_PARQUET_DIR` points at a real SuzieQ
parquet store. Each test reads one sample from a real table and
asserts that every column production code depends on is actually
present in the parquet schema.

## How to run

Default suite skips this file via `pytest.ini addopts = -m "not live"`:

    cd phase5-suzieq
    python -m pytest                              # skips this file

To run the live guards:

    # On netdevops-srv, against the docker-volume parquet store
    SUZIEQ_LIVE_PARQUET_DIR=/var/lib/docker/volumes/suzieq_parquet/_data \
        python -m pytest -m live tests/test_live_schema_guards.py -v

    # From inside the drift container (parquet is mounted at /suzieq/parquet)
    docker compose run --rm drift \
        pytest -m live /drift/tests/test_live_schema_guards.py -v
        # (needs pytest installed in the drift image; currently it is not,
        # so the netdevops-srv host path is the canonical test target)

If `SUZIEQ_LIVE_PARQUET_DIR` is unset the fixture skips every test
in the module with a clear message.

## What it catches that unit tests cannot

1. **New engine-computed columns.** When SuzieQ adds a column
   `suzieq-cli` exposes at query time but does NOT store in raw
   parquet, direct pyarrow reads silently default to None/0.
   `assert_vtep_remote_count` hit this with `remoteVtepCnt`. A
   CI run of this file on a fresh image bump would catch the
   next one before it fires in production.

2. **Schema renames.** If upstream SuzieQ renames a column the
   production code would read None for the old name silently.
   This test fails loudly when the expected column name
   disappears.

3. **Schema version bumps.** `sqvers=2.0` → `sqvers=3.0` under
   the hood sometimes drops deprecated columns. A unit fixture
   still uses the old shape; the live store moves on. This
   test bridges the gap.
"""
import os
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow.dataset as ds
import pytest


# Per-table set of columns that production code DEPENDS ON. Every
# entry in this set MUST exist in the live parquet schema - if it
# does not, production code will silently read None/NaN for it
# and either crash with an AttributeError or, worse, produce a
# wrong answer without crashing.
#
# Source of each entry (grepping for the column name in the
# drift/ package should hit at least one of these files):
#
#   drift/state.py        - TABLE_REGISTRY PKs + dedup timestamp
#   drift/diff.py         - per-dimension _diff_* functions
#   drift/assertions/*.py - per-assertion reads
#   drift/timeseries/queries/*.py - per-query reads
#   drift/timeseries/envelope.py  - self_check heartbeat check
#
# Notes:
#   - `namespace` is a hive-partition column for every table; it
#     appears in the DataFrame only when read via ds.dataset(
#     partitioning="hive"), which is what state.py uses and what
#     this test uses below. Do NOT try to read it from the raw
#     parquet file directly.
#   - `hostname` is a real column inside coalesced parquet files
#     (the coalescer merges per-host raw files and writes
#     hostname as data) AND a hive-partition column for raw
#     files. Either way, production code sees it.
#   - `remoteVtepCnt` is deliberately NOT listed. It is
#     engine-computed per ADR-7. assert_vtep_remote_count uses
#     `remoteVtepList` instead and computes the count itself.
#     A separate test below pins this specifically.
REQUIRED_COLUMNS = {
    "device": {
        "namespace", "hostname", "timestamp",
    },
    "interfaces": {
        "namespace", "hostname", "ifname",
        "adminState", "state", "timestamp",
    },
    "lldp": {
        "namespace", "hostname", "ifname",
        "peerHostname", "peerIfname", "timestamp",
    },
    "bgp": {
        "namespace", "hostname", "vrf", "peer", "afi", "safi",
        "state", "pfxRx", "timestamp",
    },
    "evpnVni": {
        "namespace", "hostname", "vni", "state", "type",
        # remoteVtepList (list-typed) NOT remoteVtepCnt
        # (engine-computed). See ADR-7.
        "remoteVtepList", "timestamp",
    },
    "routes": {
        "namespace", "hostname", "vrf", "prefix", "timestamp",
    },
    "macs": {
        "namespace", "hostname", "vlan", "macaddr",
        "oif", "remoteVtepIp", "timestamp",
    },
    "arpnd": {
        "namespace", "hostname", "ipAddress", "timestamp",
    },
    "sqPoller": {
        "namespace", "hostname", "service",
        "pollExcdPeriodCount", "timestamp",
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def parquet_dir():
    """Resolve the live parquet dir from env, skip the module if unset."""
    path = os.environ.get("SUZIEQ_LIVE_PARQUET_DIR")
    if not path:
        pytest.skip(
            "SUZIEQ_LIVE_PARQUET_DIR not set - set to the SuzieQ "
            "parquet root (e.g. /var/lib/docker/volumes/"
            "suzieq_parquet/_data) to run these live schema guards"
        )
    p = Path(path)
    if not p.is_dir():
        pytest.skip(f"parquet dir {path} does not exist on this host")
    return p


def _read_coalesced_sample(parquet_dir: Path, table: str) -> Optional[pd.DataFrame]:
    """Read the coalesced parquet subtree for `table` via
    `ds.dataset(partitioning='hive')` - same path state.py uses -
    and return a DataFrame with hive-partition columns present.
    Returns None if the subtree does not exist or is empty (table
    never polled, pre-first-coalesce, etc.)."""
    subtree = parquet_dir / "coalesced" / table
    if not subtree.exists():
        return None
    try:
        dataset = ds.dataset(str(subtree), partitioning="hive")
        df = dataset.to_table().to_pandas()
    except (FileNotFoundError, OSError):
        return None
    if df.empty:
        return None
    return df


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.live
class TestLiveRequiredColumns:
    """Per-table regression guard. Each test reads one sample
    from the live coalesced parquet subtree and asserts that
    every column in REQUIRED_COLUMNS[table] is present.

    Failures fail with a clear diagnostic naming the missing
    columns, so the fix path is "add a unit test + update
    production code" rather than "trace the silent None-return
    back through five files"."""

    @pytest.mark.parametrize(
        "table",
        sorted(REQUIRED_COLUMNS.keys()),
    )
    def test_required_columns_present(self, parquet_dir, table):
        df = _read_coalesced_sample(parquet_dir, table)
        if df is None:
            pytest.skip(
                f"no coalesced parquet file found for table {table!r} "
                f"under {parquet_dir} - table may not have been "
                f"polled yet, or the lab is pre-first-coalesce"
            )
        required = REQUIRED_COLUMNS[table]
        actual = set(df.columns)
        missing = required - actual
        assert not missing, (
            f"table {table!r}: production code depends on columns "
            f"{sorted(missing)} but they are NOT in the live parquet "
            f"schema. This usually means SuzieQ made them "
            f"engine-computed (like remoteVtepCnt in Part C) or "
            f"renamed them. Update production code + "
            f"REQUIRED_COLUMNS to match the new schema.\n"
            f"Actual columns: {sorted(actual)}"
        )


@pytest.mark.live
class TestEngineComputedColumnDrift:
    """Detect columns that are NEW engine-computed-only (like the
    original remoteVtepCnt incident) by pinning the current
    understanding of known-engine-computed columns. Failures here
    mean either (a) SuzieQ started persisting a previously
    engine-computed column (informational, update the pin), or
    (b) SuzieQ made a NEW column engine-computed that production
    code is now silently defaulting (real bug, fix production
    code to compute the value itself)."""

    def test_remote_vtep_cnt_still_engine_computed(self, parquet_dir):
        """ADR-7 pin: evpnVni.remoteVtepCnt is engine-computed,
        NOT stored in parquet. assert_vtep_remote_count computes
        the count from remoteVtepList itself. If this assumption
        ever changes, the assertion could be simplified to read
        the raw column directly - fail here so the simplification
        gets noticed."""
        df = _read_coalesced_sample(parquet_dir, "evpnVni")
        if df is None:
            pytest.skip("no evpnVni parquet yet")
        if "remoteVtepCnt" in df.columns:
            pytest.fail(
                "evpnVni: remoteVtepCnt now exists as a raw parquet "
                "column. Previously this was engine-computed per "
                "ADR-7 and production code used remoteVtepList + "
                "len() instead. Either SuzieQ started persisting "
                "remoteVtepCnt, or the lab is running a different "
                "SuzieQ release than Phase 5 was developed against. "
                "If the former: update drift/assertions/vtep.py to "
                "read remoteVtepCnt directly and add it to "
                "REQUIRED_COLUMNS['evpnVni']. If the latter: "
                "investigate why the lab image differs from the "
                "pinned digest in suzieq-image/Dockerfile."
            )

    def test_bgp_pk_fields_present(self, parquet_dir):
        """ADR-8 pin: bgp has a 6-field PK
        (namespace, hostname, vrf, peer, afi, safi). The earlier
        4-field PK collapsed distinct AFI/SAFI rows silently. If
        any of those 6 columns disappears from the live schema,
        state.py's dedup path would break the same way."""
        df = _read_coalesced_sample(parquet_dir, "bgp")
        if df is None:
            pytest.skip("no bgp parquet yet")
        pk = {"namespace", "hostname", "vrf", "peer", "afi", "safi"}
        actual = set(df.columns)
        missing_pk = pk - actual
        assert not missing_pk, (
            f"bgp PK fields missing from parquet: {sorted(missing_pk)}. "
            f"drift/state.py dedups on the 6-field PK; if any field "
            f"is gone, either upstream schema changed or the lab "
            f"parquet is stale. Actual columns: {sorted(actual)}"
        )

    def test_sq_poller_heartbeat_column_present(self, parquet_dir):
        """ADR-15 pin: the envelope self-check uses sqPoller
        timestamp as a heartbeat signal. If the column is gone,
        self_check.heartbeat silently passes when it should not.
        Surface the gap loudly."""
        df = _read_coalesced_sample(parquet_dir, "sqPoller")
        if df is None:
            pytest.skip("no sqPoller parquet yet")
        assert "timestamp" in df.columns, (
            "sqPoller: no timestamp column in parquet. The envelope "
            "self_check heartbeat rule uses this column to detect "
            "stuck pollers (drift/timeseries/envelope.py). Without "
            "it the heartbeat rule would silently pass and a real "
            "poller hang would look healthy. Investigate."
        )
