"""Poller self-health assertion.

assert_poll_health: the SuzieQ poller must be keeping up with its
service periods. `pollExcdPeriodCount` in the sqPoller table is
the number of times a service's gather took longer than its
configured poll period. On a healthy 4-device lab this should be
zero for every (hostname, service) row.

Non-zero means one of:
  - Poller CPU-starved (netdevops-srv is overloaded)
  - A device is slow to respond (SSH auth latency, vendor-side
    command latency, or the device itself is under load)
  - A specific service is misconfigured with too short a period
    for the amount of data it has to pull
  - NetBox API slow (rare, but would show up on services that
    re-query NetBox state)

None of these are fabric bugs - they are harness bugs. Catching
them continuously is the only way to know the harness's own
output is trustworthy. Drift cannot do this check because drift
has no opinion about the harness itself.

Catches the class of problem where the drift and other assertion
outputs are *silently* stale because the poller is falling behind
its own schedule.
"""
from typing import List

from ..diff import Drift, SEVERITY_ERROR, SEVERITY_WARNING, CATEGORY_META
from ..state import FabricState


def assert_poll_health(state: FabricState) -> List[Drift]:
    """Every row in the sqPoller table must have pollExcdPeriodCount == 0.

    Severity is ERROR when the count is non-zero because a falling-
    behind poller produces stale data that every other check would
    read without realizing it. Loud failure is the correct response.

    The sqPoller table is empty on a brand-new stack before the
    first poll cycle completes. Empty is treated as "no signal, no
    drift" - the drift harness handles the first-cycle case
    the same way in other dimensions.
    """
    out: List[Drift] = []
    if state.sq_poller.empty:
        return out
    if "pollExcdPeriodCount" not in state.sq_poller.columns:
        # Schema drift - the column we rely on is gone. Emit one
        # warning so the operator knows the assertion is disabled
        # rather than silently passing.
        out.append(Drift(
            dimension="assert_poll_health",
            severity=SEVERITY_WARNING,
            category=CATEGORY_META,
            subject="sqPoller.schema",
            detail=(
                "sqPoller table has no pollExcdPeriodCount column - "
                "upstream suzieq schema changed? Assertion disabled "
                "until the column is available again."
            ),
            intent=None,
            state=None,
        ))
        return out

    for _, row in state.sq_poller.iterrows():
        raw = row.get("pollExcdPeriodCount", 0)
        # The column is sometimes a numpy-scalar, sometimes a list
        # (if the row had per-period breakdowns), sometimes a
        # plain int. Normalize to a single max value.
        try:
            if hasattr(raw, "__iter__") and not isinstance(raw, (str, bytes)):
                vals = [int(x) for x in raw]
                count = max(vals) if vals else 0
            else:
                count = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            count = 0
        if count == 0:
            continue
        out.append(Drift(
            dimension="assert_poll_health",
            severity=SEVERITY_ERROR,
            category=CATEGORY_META,
            subject=f"{row.get('hostname')}:{row.get('service')}",
            detail=(
                f"Poller is falling behind: pollExcdPeriodCount={count} "
                f"for service {row.get('service')!r} on "
                f"{row.get('hostname')}. Drift and assertion output from "
                f"this namespace may be stale."
            ),
            intent=None,
            state={
                "hostname": row.get("hostname"),
                "service": row.get("service"),
                "pollExcdPeriodCount": count,
            },
        ))
    return out
