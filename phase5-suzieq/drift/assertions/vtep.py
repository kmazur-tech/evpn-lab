"""VTEP discovery assertion.

assert_vtep_remote_count: every L2 VNI on a leaf must see at least
one remote VTEP. L3 VNIs are routing-only and intentionally have
no remote VTEP list (verified in the live lab), so they are
excluded from the check.

Catches:
  - EVPN Type-3 (inclusive multicast) routes not propagating
    between leaves (leaves know about their own VNI but don't see
    the peer's IMET)
  - Peer leaf's VTEP IP not in the source leaf's VTEP list (either
    BGP underlay broken for that one peer, or Type-3 import policy
    filtering)
  - Leaf newly added to NetBox + Phase 3 rendered config but not
    yet fully converged (would produce drift too, but this is a
    fast self-check)

Non-overlap with drift `evpn_vni` dimension: drift checks whether
the modeled VNI is present and state==up. This assertion checks a
different property of the same row: whether remote VTEP discovery
has actually happened for L2 VNIs.

## A subtle thing about the column name

SuzieQ's Python API (`suzieq-cli evpnVni show`) exposes a
`remoteVtepCnt` column. That column is **computed at query time**
by the suzieq engine as `len(remoteVtepList)`. It does NOT exist
in the raw parquet file. The `drift/state.py` read path uses
direct pyarrow reads that bypass the engine, so the assertion
must compute the count itself from the `remoteVtepList` column
(which IS stored in parquet as a list-typed column).

Discovered live on the lab 2026-04-11 when the first assertions
run fired 4 false positives because the assertion was reading
the non-existent `remoteVtepCnt` column and defaulting it to 0.
suzieq-cli showed the expected `remoteVtepCnt=1`; direct parquet
read showed no such column. Root cause: engine-computed vs raw.
"""
from typing import List

from ..diff import Drift, SEVERITY_ERROR, CATEGORY_OVERLAY
from ..state import FabricState


def _count_remote_vteps(row) -> int:
    """Compute the number of remote VTEPs from the raw parquet
    `remoteVtepList` column. Handles every shape pyarrow produces:

      - numpy array: normal case, has len()
      - Python list: also has len()
      - None: no column value (L3 VNI or bad row), returns 0
      - pandas NaN: same as None, returns 0
      - anything else: best-effort, returns 0
    """
    raw = row.get("remoteVtepList")
    if raw is None:
        return 0
    # pandas NaN check - pandas NaN is not None but `== None` is False
    try:
        import pandas as pd
        if pd.isna(raw):
            return 0
    except (TypeError, ValueError):
        # pd.isna on array-like raises; that's fine, fall through
        pass
    try:
        return len(raw)
    except TypeError:
        return 0


def assert_vtep_remote_count(state: FabricState) -> List[Drift]:
    """Every L2 VNI row must have len(remoteVtepList) >= 1."""
    out: List[Drift] = []
    if state.evpn_vnis.empty:
        return out
    for _, row in state.evpn_vnis.iterrows():
        vni_type = str(row.get("type", "")).upper()
        if vni_type != "L2":
            # L3 VNIs are routing-only and have no remote VTEP
            # list by design - skip.
            continue
        remote_cnt = _count_remote_vteps(row)
        if remote_cnt >= 1:
            continue
        out.append(Drift(
            dimension="assert_vtep_remote_count",
            severity=SEVERITY_ERROR,
            category=CATEGORY_OVERLAY,
            subject=f"{row.get('hostname')}:vni{row.get('vni')}",
            detail=(
                f"L2 VNI {row.get('vni')} on {row.get('hostname')} "
                f"sees 0 remote VTEPs (EVPN Type-3 discovery not "
                f"converged, or underlay broken to peer)"
            ),
            intent=None,
            state={
                "hostname": row.get("hostname"),
                "vni": int(row.get("vni")) if row.get("vni") is not None else None,
                "type": vni_type,
                "state": row.get("state"),
                "remoteVtepCnt": remote_cnt,
            },
        ))
    return out
