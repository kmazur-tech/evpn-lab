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

# Tables we read for Part B (min + full). Order matches intent.py
# and diff.py dimension order.
TABLES = ("device", "interfaces", "lldp", "bgp",
          "evpnVni", "routes", "macs", "arpnd")


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
        bgp=read_table("bgp", namespace, parquet_dir,
                       pk=("namespace", "hostname", "vrf", "peer")),
        evpn_vnis=read_table("evpnVni", namespace, parquet_dir,
                             pk=("namespace", "hostname", "vni")),
        routes=read_table("routes", namespace, parquet_dir,
                          pk=("namespace", "hostname", "vrf", "prefix")),
        macs=read_table("macs", namespace, parquet_dir,
                        pk=("namespace", "hostname", "vlan", "macaddr")),
        arpnd=read_table("arpnd", namespace, parquet_dir,
                         pk=("namespace", "hostname", "ipAddress")),
    )


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

    # view='latest' equivalent: keep the most recent row per PK
    if pk and "timestamp" in df.columns:
        df = (
            df.sort_values("timestamp")
              .drop_duplicates(subset=list(pk), keep="last")
              .reset_index(drop=True)
        )

    return df
