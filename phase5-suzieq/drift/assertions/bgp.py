"""BGP state-only assertions.

Two assertions:

  assert_bgp_all_established    - every session in the SuzieQ bgp
                                  table has state == Established.
                                  Catches any session that has
                                  dropped state since the drift
                                  harness's intent check ran.

  assert_bgp_pfx_rx_positive    - every Established session has
                                  pfxRx > 0 per (afi, safi). An
                                  Established session with zero
                                  received prefixes is a real bug:
                                  either the peer flapped and is
                                  converging, or policy filtering
                                  killed every route, or the peer
                                  is not advertising anything.
                                  Drift's _diff_bgp only checks
                                  state=Established, not whether
                                  routes are actually flowing, so
                                  this is a non-overlap check.

Both operate on state.bgp. Neither reads NetBox intent. Neither
has an opinion about WHICH sessions should exist - that's drift's
job. These assertions only care that sessions that DO exist are
healthy.
"""
from dataclasses import asdict
from typing import List

from ..diff import Drift, SEVERITY_ERROR
from ..state import FabricState


def assert_bgp_all_established(state: FabricState) -> List[Drift]:
    """Every row in the bgp state table must have state==Established."""
    out: List[Drift] = []
    if state.bgp.empty:
        return out
    for _, row in state.bgp.iterrows():
        s = str(row.get("state", "")).lower()
        if s == "established":
            continue
        out.append(Drift(
            dimension="assert_bgp_established",
            severity=SEVERITY_ERROR,
            subject=f"{row.get('hostname')}:{row.get('vrf')}:{row.get('peer')}",
            detail=(
                f"BGP session {row.get('peer')} on {row.get('hostname')} "
                f"({row.get('vrf')}) is not Established: state={row.get('state')!r}"
            ),
            intent=None,
            state={
                "hostname": row.get("hostname"),
                "vrf": row.get("vrf"),
                "peer": row.get("peer"),
                "state": row.get("state"),
                "afi": row.get("afi"),
                "safi": row.get("safi"),
            },
        ))
    return out


def assert_bgp_pfx_rx_positive(state: FabricState) -> List[Drift]:
    """Every Established session must have pfxRx > 0 per (afi, safi).

    A session that is Established but has zero received prefixes is
    a real problem:
      - peer just flapped and is mid-converge (transient, but worth
        catching in a continuous check loop)
      - policy filtered every route (operator bug)
      - peer is announcing nothing (configuration bug on the peer)

    Drift's _diff_bgp only checks state==Established. This is the
    non-overlap angle: same session, different property, same dataset.
    """
    out: List[Drift] = []
    if state.bgp.empty:
        return out
    for _, row in state.bgp.iterrows():
        s = str(row.get("state", "")).lower()
        if s != "established":
            # Not established - the other assertion covers that.
            # Don't double-report.
            continue
        pfx = row.get("pfxRx", 0)
        try:
            pfx_int = int(pfx) if pfx is not None else 0
        except (TypeError, ValueError):
            pfx_int = 0
        if pfx_int > 0:
            continue
        out.append(Drift(
            dimension="assert_bgp_pfx_rx",
            severity=SEVERITY_ERROR,
            subject=(
                f"{row.get('hostname')}:{row.get('peer')}"
                f":{row.get('afi')}/{row.get('safi')}"
            ),
            detail=(
                f"BGP session {row.get('peer')} on {row.get('hostname')} "
                f"is Established but pfxRx=0 for "
                f"{row.get('afi')}/{row.get('safi')}"
            ),
            intent=None,
            state={
                "hostname": row.get("hostname"),
                "peer": row.get("peer"),
                "afi": row.get("afi"),
                "safi": row.get("safi"),
                "pfxRx": pfx_int,
                "state": row.get("state"),
            },
        ))
    return out
