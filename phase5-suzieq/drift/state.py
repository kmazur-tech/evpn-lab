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
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pandas as pd
import pyarrow.dataset as ds


# Default location of the suzieq parquet store inside the drift
# container. The drift container's docker-compose.yml mounts the
# suzieq_parquet docker volume read-only at this path.
DEFAULT_PARQUET_DIR = "/suzieq/parquet"

# Tables we read. Order matches intent.py, diff.py and
# assertions/ usage. sqPoller is the meta-health table - its rows
# describe the poller itself, not the fabric.
TABLES = ("device", "interfaces", "lldp", "bgp",
          "evpnVni", "routes", "macs", "arpnd",
          "sqPoller")


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


def collect(namespace: str, parquet_dir: str = DEFAULT_PARQUET_DIR) -> FabricState:
    """Read the eight state tables for one namespace. Returns empty
    DataFrames for any table that has not been polled yet (rather
    than raising) so first-cycle drift runs do not crash before any
    data has been collected."""
    return FabricState(
        namespace=namespace,
        devices=read_table("device", namespace, parquet_dir,
                           pk=("namespace", "hostname")),
        interfaces=read_table("interfaces", namespace, parquet_dir,
                              pk=("namespace", "hostname", "ifname")),
        lldp=read_table("lldp", namespace, parquet_dir,
                        pk=("namespace", "hostname", "ifname")),
        # PK matches SuzieQ's bgp schema (config/schema/bgp.avsc):
        # (namespace, hostname, vrf, peer, afi, safi). A single peer
        # has one row PER AFI/SAFI - e.g. an overlay peer has
        # l2vpn/evpn AND the underlay peer has ipv4/unicast. The
        # earlier 4-field PK was collapsing distinct AFI/SAFI rows
        # silently and was independently wrong regardless of the
        # phantom-row issue.
        bgp=read_table("bgp", namespace, parquet_dir,
                       pk=("namespace", "hostname", "vrf",
                           "peer", "afi", "safi")),
        evpn_vnis=read_table("evpnVni", namespace, parquet_dir,
                             pk=("namespace", "hostname", "vni")),
        routes=read_table("routes", namespace, parquet_dir,
                          pk=("namespace", "hostname", "vrf", "prefix")),
        macs=read_table("macs", namespace, parquet_dir,
                        pk=("namespace", "hostname", "vlan", "macaddr")),
        arpnd=read_table("arpnd", namespace, parquet_dir,
                         pk=("namespace", "hostname", "ipAddress")),
        sq_poller=read_table("sqPoller", namespace, parquet_dir,
                             pk=("namespace", "hostname", "service")),
    )


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
    (namespace, hostname, vrf, peer, afi, safi). The state.py
    read_table() call now uses exactly that PK (fixed in the
    same commit that added this docstring). But a correct PK
    only helps when rows collide - the phantom rows have
    DISTINCT (empty) afi/safi and therefore distinct PK values,
    so they survive drop_duplicates regardless. We have to drop
    them explicitly.

    ## What we drop

    Any row where vrf, afi, OR safi is empty / NaN / None.
    An empty-AFI or empty-vrf BGP row is semantically
    meaningless (a BGP session is always in a routing instance
    and always negotiates a specific AFI/SAFI), so dropping
    these rows is correct - not a workaround.

    ## History

    First attempt (the commit before this one) dropped only
    empty-vrf rows and explained the pairing as "Junos emits
    each peer twice." The user correctly pushed back that
    Junos does NOT in fact emit each peer twice - the rows are
    a SuzieQ pipeline transient, not a Junos duplicate. This
    cleanup and the accompanying test comments are now
    consistent with the verified mechanism.
    """
    if df.empty:
        return df
    # Treat pandas NaN, None, and "" all as phantom for any of
    # the three structural fields. If a row is missing any one
    # of vrf/afi/safi it is a partial-view row and not usable.
    mask = pd.Series(True, index=df.index)
    for col in ("vrf", "afi", "safi"):
        if col not in df.columns:
            continue
        mask &= df[col].notna() & (df[col].astype(str) != "")
    return df[mask].reset_index(drop=True)


# Per-table cleanup hooks. Called after concat+namespace-filter but
# BEFORE drop_duplicates so cleaned-up tables dedup correctly.
_TABLE_CLEANUP = {
    "bgp": _cleanup_bgp_phantom_rows,
}


def read_table(
    table: str,
    namespace: str,
    parquet_dir: str = DEFAULT_PARQUET_DIR,
    pk: Optional[tuple] = None,
) -> pd.DataFrame:
    """Read one suzieq table, return latest-row-per-PK as a DataFrame.

    Reads BOTH the coalesced/<table>/ directory (where the coalescer
    parks compacted history) AND the raw <table>/ directory (where
    the poller writes incoming data between coalescer runs). The
    .drop_duplicates() de-dupes across both sources.

    Returns an empty DataFrame (NOT raises) when the table directory
    does not yet exist - this is the expected first-cycle state and
    drift.py knows how to handle empty inputs.
    """
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
    cleanup = _TABLE_CLEANUP.get(table)
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
