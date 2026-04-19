"""Phase 5 Part C: strict state-only assertions.

Assertions check invariants that hold over the running fabric state,
independent of NetBox intent. They complement (never duplicate) the
drift harness in Part B:

  Drift (drift/diff.py):   "does NetBox intent match live state?"
  Assertions (this pkg):   "does live state satisfy these invariants?"

Each assertion takes a drift.state.FabricState and returns a list of
drift.diff.Drift records using the same shape so Phase 6 CI consumes
both uniformly. Different `dimension` values let the consumer
distinguish drift drifts from assertion violations.

## Non-overlap rule with Phase 2 smoke (non-negotiable)

Every assertion in this package MUST answer a question Phase 2 smoke
CANNOT answer. Phase 2 smoke runs once at deploy; assertions run
continuously via a systemd timer. The specific angles that make an
assertion "not a smoke duplicate" are:

  1. Continuous state - "is this still true now, between deploys?"
     (smoke is point-in-time; assertions loop)
  2. Property drift - "has a measurable property left its valid
     range since we last checked?" (smoke does absolute checks;
     assertions can catch flapping / silent withdrawal)
  3. Self-health - "is the harness itself keeping up?"
     (smoke does not introspect Suzieq's own state; assertions can)

If a proposed assertion is just a re-encoding of a smoke check with
none of the three angles above, it does not belong here. The PR
review gate: if the assertion text would read identically in a
smoke-check docstring, reject it.

## Current assertions (Part C initial set)

  assert_bgp_all_established      bgp.py  - every session Established
  assert_bgp_pfx_rx_positive      bgp.py  - every established session pfxRx>0
  assert_vtep_remote_count        vtep.py - L2 VNIs see at least one remote VTEP
  assert_poll_health              meta.py - pollExcdPeriodCount == 0 everywhere

## Adding a new assertion

1. Write a new function in the appropriate module (or a new module
   for a new concern). Signature: `(state: FabricState) -> List[Drift]`.
2. Import and call it from `run_all(state)` in this __init__.
3. Add tests to tests/test_assertions_*.py using inline DataFrame
   fixtures (no docker, no network).
4. Pick a unique `dimension` value - convention is
   `assert_<thing>` so the CI consumer can match on the prefix.
5. Document in the dimension table above.
"""
from typing import List

from ..diff import Drift
from ..state import FabricState

from .bgp import assert_bgp_all_established, assert_bgp_pfx_rx_positive
from .vtep import assert_vtep_remote_count
from .meta import assert_poll_health


def run_all(state: FabricState) -> List[Drift]:
    """Run every assertion in this package against the given state,
    return a flat list of Drift records sorted by dimension+subject
    so output is stable across runs (golden-file-friendly for
    Phase 6 CI)."""
    out: List[Drift] = []
    out.extend(assert_bgp_all_established(state))
    out.extend(assert_bgp_pfx_rx_positive(state))
    out.extend(assert_vtep_remote_count(state))
    out.extend(assert_poll_health(state))
    return sorted(out, key=lambda d: (d.dimension, d.subject))


__all__ = [
    "run_all",
    "assert_bgp_all_established",
    "assert_bgp_pfx_rx_positive",
    "assert_vtep_remote_count",
    "assert_poll_health",
]
