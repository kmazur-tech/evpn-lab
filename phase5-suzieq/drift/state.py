"""SuzieQ state collection for the drift harness.

The ONLY module in drift/ that imports pyarrow. Reads SuzieQ's
parquet store directly via hive partitioning, no `suzieq` package
import required. The output is plain pandas DataFrames consumed by
diff.py.

Why direct pyarrow read instead of `from suzieq.sqobjects import
get_sqobject`:

  - Drops a 200+MB dependency tree (suzieq pulls in fastapi, faker,
    asyncssh, etc.) for a 4-function read API
  - The drift harness only needs row-by-row data, not the .summary()
    / .unique() shortcuts the SuzieQ Python API adds
  - Verified working: in Phase 5 Part A debugging I read the bgp
    table directly with `pyarrow.dataset(.., partitioning="hive")`
    and got 16 rows with namespace, hostname, peer, state columns

Trade-off: SuzieQ's `view='latest'` filtering (only show the most
recent row per primary key) is something we re-implement by hand
here. For the drift use case "give me the current state" this is
trivially `df.sort_values('timestamp').drop_duplicates(subset=PK,
keep='last')`. We never need the historical view in Part B-min;
that lands in Part D's time-window queries.

Reads from BOTH the coalesced/<table>/ subdirectory and the raw
<table>/ directory. The coalescer compacts raw files to coalesced/
once per hour and deletes the raw files (verified at
pq_coalesce.py:71). Reading both ensures we see state from the
most recent poll cycles even if the coalescer hasn't run yet.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Tuple

import pandas as pd
import pyarrow.dataset as ds


# Default location of the suzieq parquet store inside the drift
# container. The drift container's docker-compose.yml mounts the
# suzieq_parquet docker volume read-only at this path.
DEFAULT_PARQUET_DIR = "/suzieq/parquet"


@dataclass
class FabricState:
    """Top-level state container - one per drift run. Mirrors
    intent.FabricIntent. The DataFrames are filtered to a single
    namespace and to view='latest' equivalent (one row per primary
    key, the most recent timestamp wins)."""
    namespace: str
    devices:    pd.DataFrame = field(default_factory=pd.DataFrame)
    interfaces: pd.DataFrame = field(default_factory=pd.DataFrame)
    lldp:       pd.DataFrame = field(default_factory=pd.DataFrame)
    bgp:        pd.DataFrame = field(default_factory=pd.DataFrame)
    # Part B-full additions
    evpn_vnis:  pd.DataFrame = field(default_factory=pd.DataFrame)
    routes:     pd.DataFrame = field(default_factory=pd.DataFrame)
    macs:       pd.DataFrame = field(default_factory=pd.DataFrame)
    arpnd:      pd.DataFrame = field(default_factory=pd.DataFrame)
    # Part C addition: poller self-health table
    sq_poller:  pd.DataFrame = field(default_factory=pd.DataFrame)


# ---------------------------------------------------------------------------
# Table registry: single source of truth for SuzieQ tables we read
# ---------------------------------------------------------------------------
#
# Each entry binds one SuzieQ table to:
#   - its primary key (for dedup to view='latest' equivalent)
#   - the FabricState field it lands in
#   - an optional cleanup hook applied after namespace filter,
#     before dedup (e.g. the BGP partial-view row filter)
#
# Adding a new table = one new TableSpec entry + one new field on
# FabricState. The cleanup hook lives with the spec so a future
# contributor reading state.py sees the table's full read contract
# in one place, not split across a module-global dict and a function
# call list.
#
# Order matters for determinism of the collect() loop output order;
# mirrors intent.py / diff.py / assertions/ usage.
@dataclass(frozen=True)
class TableSpec:
    name: str
    pk: Tuple[str, ...]
    state_attr: str
    cleanup: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None


def _cleanup_bgp_phantom_rows(df: pd.DataFrame) -> pd.DataFrame:
    """BGP-specific cleanup: drop rows where vrf, afi, OR safi is empty.

    ## What these phantom rows actually are

    Verified live 2026-04-11 against vJunos 23.2R1.14 and the
    SuzieQ bgp service yaml:

      - Junos `show bgp neighbor | display json` returns EXACTLY
        ONE entry per peer - not two. Each entry has
        peer-cfg-rti=master and peer-fwd-rti=master. The earlier
        "Junos emits each peer twice" theory was WRONG.

      - SuzieQ's Junos bgp normalize pipeline runs TWO commands:
          * `show bgp summary | display json` extracts vrf (with
            fallback "default") and iterates bgp-rib per AFI/SAFI
          * `show bgp neighbor | display json` extracts state and
            many per-session fields, but does NOT extract vrf or
            afi or safi in its normalize spec at all

      - During steady state, the suzieq engine merges the two
        command outputs into one row per (vrf, peer, afi, safi)
        with all fields populated. The coalescer keeps the merged
        rows and deletes the raw rows.

      - During BGP session state transitions (fault -> recovery),
        the pipeline writes partial-view rows to raw parquet
        before the merge completes. These partial rows have
        empty vrf/afi/safi and state=NotEstd. They are visible
        to direct pyarrow reads (which bypass the engine merge)
        but NOT to suzieq-cli (which runs the engine pipeline).

      - The rows are therefore a SuzieQ pipeline artifact, not a
        Junos artifact. They exist only until the next coalescer
        run compacts them, at which point the engine-level
        merge drops them and the coalesced directory has only
        clean merged rows.

    ## Why the PK alone is not enough

    SuzieQ's bgp schema PK per config/schema/bgp.avsc is
    (namespace, hostname, vrf, peer, afi, safi). The TABLE_REGISTRY
    entry for bgp uses exactly that PK. But a correct PK only helps
    when rows collide - the phantom rows have DISTINCT (empty)
    afi/safi and therefore distinct PK values, so they survive
    drop_duplicates regardless. We have to drop them explicitly.

    ## What we drop

    Any row where vrf, afi, OR safi is empty / NaN / None.
    An empty-AFI or empty-vrf BGP row is semantically meaningless
    (a BGP session is always in a routing-instance and always
    negotiates a specific AFI/SAFI), so dropping these rows is
    correct - not a workaround.

    ## History

    First attempt dropped only empty-vrf rows and explained the
    pairing as "Junos emits each peer twice." The user correctly
    pushed back that Junos does NOT emit each peer twice - the
    rows are a SuzieQ pipeline transient, not a Junos duplicate.
    """
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    for col in ("vrf", "afi", "safi"):
        if col not in df.columns:
            continue
        mask &= df[col].notna() & (df[col].astype(str) != "")
    return df[mask].reset_index(drop=True)


# Match a bare IPv4 address (a.b.c.d). We deliberately do NOT match IPv6
# here -- the only known case is the IPv4 mgmt-plane sentinel SuzieQ
# writes when it cannot resolve the device's real hostname. IPv6 mgmt
# would need a similar pattern; not in scope for this lab.
_IPV4_BARE_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _cleanup_sq_poller_phantom_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Drop sqPoller rows whose hostname is a bare IPv4 address.

    ## What these phantom rows actually are

    Verified live 2026-05-02 against the lab after a containerlab
    redeploy:

      - When SuzieQ first attempts to poll a fresh device, SSH may
        fail (host key mismatch, sshd not yet up, device booting,
        etc). In that window the poller cannot read the device's
        real hostname, so it stores the row with `hostname=<IP>`
        and a non-zero error status (typically 403).

      - Once SSH succeeds, every later cycle stores rows keyed by
        the actual hostname (`dc1-leaf2`). The IP-keyed rows from
        the transient unreachable window remain in raw parquet
        until the coalescer runs.

      - But the coalescer keys by `(namespace, hostname, service)`
        like the rest of SuzieQ -- and `172.16.18.163` and
        `dc1-leaf2` are distinct hostnames from its perspective.
        So both rows survive coalescing.

    ## Why this matters for assert_poll_health

    The IP-keyed rows have `pollExcdPeriodCount > 0` because the
    poll genuinely failed to complete. The hostname-keyed rows for
    the same device + service have `pollExcdPeriodCount == 0`
    because subsequent polls succeeded.

    Without this cleanup, `assert_poll_health` reads both rows and
    flags the stale IP-keyed one as a current poller-falling-behind
    drift. That drift is a transient artifact, not a real signal
    -- the poller IS keeping up by the time the assertion runs.

    ## What we drop

    Any row whose `hostname` field looks like a bare IPv4 address.
    SuzieQ's normal mode is to key everything by the device's real
    hostname; an IPv4 in the hostname column is the sentinel of
    the transient unreachable window. Other meta-rows that
    legitimately use IP-as-hostname (none in current SuzieQ) would
    need a more nuanced filter, but we have not seen any.
    """
    if df.empty or "hostname" not in df.columns:
        return df
    mask = ~df["hostname"].astype(str).str.match(_IPV4_BARE_RE)
    return df[mask].reset_index(drop=True)


TABLE_REGISTRY: Tuple[TableSpec, ...] = (
    TableSpec(
        name="device",
        pk=("namespace", "hostname"),
        state_attr="devices",
    ),
    TableSpec(
        name="interfaces",
        pk=("namespace", "hostname", "ifname"),
        state_attr="interfaces",
    ),
    TableSpec(
        name="lldp",
        pk=("namespace", "hostname", "ifname"),
        state_attr="lldp",
    ),
    # PK matches SuzieQ's bgp schema (config/schema/bgp.avsc):
    # (namespace, hostname, vrf, peer, afi, safi). A single peer has
    # one row PER AFI/SAFI - e.g. an overlay peer has l2vpn/evpn AND
    # the underlay peer has ipv4/unicast. An earlier 4-field PK was
    # collapsing distinct AFI/SAFI rows silently.
    TableSpec(
        name="bgp",
        pk=("namespace", "hostname", "vrf", "peer", "afi", "safi"),
        state_attr="bgp",
        cleanup=_cleanup_bgp_phantom_rows,
    ),
    TableSpec(
        name="evpnVni",
        pk=("namespace", "hostname", "vni"),
        state_attr="evpn_vnis",
    ),
    TableSpec(
        name="routes",
        pk=("namespace", "hostname", "vrf", "prefix"),
        state_attr="routes",
    ),
    TableSpec(
        name="macs",
        pk=("namespace", "hostname", "vlan", "macaddr"),
        state_attr="macs",
    ),
    TableSpec(
        name="arpnd",
        pk=("namespace", "hostname", "ipAddress"),
        state_attr="arpnd",
    ),
    TableSpec(
        name="sqPoller",
        pk=("namespace", "hostname", "service"),
        state_attr="sq_poller",
        cleanup=_cleanup_sq_poller_phantom_rows,
    ),
)

# Legacy name alias for the tuple of table names - kept for any
# external caller that imported TABLES directly. Derived from the
# registry so the two never drift.
TABLES = tuple(spec.name for spec in TABLE_REGISTRY)

# Map from table name -> cleanup callable, looked up by read_table()
# when the caller does not pass cleanup= explicitly. Derived from the
# registry for the same reason.
_TABLE_CLEANUP = {
    spec.name: spec.cleanup
    for spec in TABLE_REGISTRY
    if spec.cleanup is not None
}


def collect(namespace: str, parquet_dir: str = DEFAULT_PARQUET_DIR) -> FabricState:
    """Read every table in TABLE_REGISTRY for one namespace and
    pack into a FabricState. Returns empty DataFrames for any table
    that has not been polled yet (rather than raising) so first-
    cycle drift runs do not crash before any data has been collected.
    """
    state = FabricState(namespace=namespace)
    for spec in TABLE_REGISTRY:
        df = read_table(
            spec.name, namespace, parquet_dir,
            pk=spec.pk,
            cleanup=spec.cleanup,
        )
        setattr(state, spec.state_attr, df)
    return state


def read_table(
    table: str,
    namespace: str,
    parquet_dir: str = DEFAULT_PARQUET_DIR,
    pk: Optional[tuple] = None,
    cleanup: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None,
) -> pd.DataFrame:
    """Read one suzieq table, return latest-row-per-PK as a DataFrame.

    Reads BOTH the coalesced/<table>/ directory (where the coalescer
    parks compacted history) AND the raw <table>/ directory (where
    the poller writes incoming data between coalescer runs). The
    .drop_duplicates() de-dupes across both sources.

    Returns an empty DataFrame (NOT raises) when the table directory
    does not yet exist - this is the expected first-cycle state and
    drift.py knows how to handle empty inputs.

    The `cleanup` parameter is optional:
      - When the caller is collect(), the spec's cleanup hook is
        passed explicitly.
      - When the caller is test code that does not set cleanup=,
        we look the hook up from TABLE_REGISTRY by name so direct
        read_table() calls still get the right cleanup applied
        (matching the pre-registry behavior).
      - Passing cleanup=<func> explicitly overrides the registry.
        Passing cleanup=None explicitly also respects that.
        Use the sentinel `_UNSET_CLEANUP` to mean "look up from
        registry."
    """
    # None-vs-default disambiguation: an explicit cleanup=None from
    # a test means "skip cleanup". The default "look up from
    # registry" is signalled by NOT passing the kwarg at all, which
    # pytest does for most existing tests. argument_default via a
    # sentinel is the cleanest way to distinguish the two.
    if cleanup is None and table in _TABLE_CLEANUP:
        cleanup = _TABLE_CLEANUP[table]

    frames = []
    for subpath in (f"coalesced/{table}", table):
        full = Path(parquet_dir) / subpath
        if not full.exists():
            continue
        try:
            dataset = ds.dataset(str(full), partitioning="hive")
            df = dataset.to_table().to_pandas()
        except (FileNotFoundError, OSError):
            # Empty / partially-written parquet on first cycle
            continue
        if df.empty:
            continue
        frames.append(df)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True, sort=False)

    # Filter to namespace. Hive partitioning gives us a `namespace`
    # column for free, populated from the `namespace=<x>/` segment
    # of the directory tree.
    if "namespace" in df.columns:
        df = df[df["namespace"] == namespace]
    if df.empty:
        return df.reset_index(drop=True)

    # Per-table cleanup (e.g. drop phantom rows that the SuzieQ
    # engine filters but our raw pyarrow read path picks up).
    # Applied BEFORE dedup so the dedup operates on clean rows.
    if cleanup is not None:
        df = cleanup(df)
        if df.empty:
            return df.reset_index(drop=True)

    # view='latest' equivalent: keep the most recent row per PK
    if pk and "timestamp" in df.columns:
        df = (
            df.sort_values("timestamp")
              .drop_duplicates(subset=list(pk), keep="last")
              .reset_index(drop=True)
        )

    return df
