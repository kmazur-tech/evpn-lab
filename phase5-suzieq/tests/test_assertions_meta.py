"""Unit tests for drift/assertions/meta.py (poller self-health)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drift.assertions.meta import assert_poll_health  # noqa: E402
from drift.diff import SEVERITY_ERROR, SEVERITY_WARNING  # noqa: E402
from drift.state import FabricState  # noqa: E402


def _state(poller_rows):
    return FabricState(
        namespace="dc1",
        sq_poller=pd.DataFrame(poller_rows),
    )


class TestPollHealth:
    def test_all_zero_no_drift(self):
        """Healthy state: every service keeping up."""
        state = _state([
            {"hostname": "dc1-leaf1", "service": "bgp", "pollExcdPeriodCount": 0},
            {"hostname": "dc1-leaf1", "service": "lldp", "pollExcdPeriodCount": 0},
            {"hostname": "dc1-spine1", "service": "bgp", "pollExcdPeriodCount": 0},
        ])
        out = assert_poll_health(state)
        assert out == []

    def test_nonzero_count_is_error(self):
        """The headline: poller is falling behind. Assertion
        MUST be ERROR severity (not warning) because stale poll
        data corrupts every other check silently."""
        state = _state([
            {"hostname": "dc1-leaf1", "service": "bgp", "pollExcdPeriodCount": 3},
        ])
        out = assert_poll_health(state)
        assert len(out) == 1
        assert out[0].severity == SEVERITY_ERROR
        assert out[0].dimension == "assert_poll_health"
        assert "dc1-leaf1:bgp" in out[0].subject
        assert "pollExcdPeriodCount=3" in out[0].detail

    def test_empty_table_no_drift(self):
        """First-cycle case: poller hasn't written sqPoller data
        yet. Treated as no-signal, no-assertion-failure."""
        state = _state([])
        out = assert_poll_health(state)
        assert out == []

    def test_missing_column_emits_warning(self):
        """SuzieQ schema drift: if the column we rely on is gone,
        warn loudly and disable the assertion. Silent pass would
        hide a real harness bug."""
        state = _state([
            {"hostname": "dc1-leaf1", "service": "bgp"},  # no pollExcdPeriodCount column
        ])
        out = assert_poll_health(state)
        assert len(out) == 1
        assert out[0].severity == SEVERITY_WARNING
        assert "pollExcdPeriodCount column" in out[0].detail

    def test_list_valued_count_takes_max(self):
        """SuzieQ's sqPoller sometimes exposes a rolling-window
        breakdown as a list of per-period values. We take the max."""
        state = _state([
            {"hostname": "dc1-leaf1", "service": "bgp",
             "pollExcdPeriodCount": [0, 2, 0]},
        ])
        out = assert_poll_health(state)
        assert len(out) == 1
        assert "pollExcdPeriodCount=2" in out[0].detail

    def test_mixed_services_only_bad_one_reported(self):
        state = _state([
            {"hostname": "dc1-leaf1", "service": "bgp", "pollExcdPeriodCount": 0},
            {"hostname": "dc1-leaf1", "service": "lldp", "pollExcdPeriodCount": 5},
            {"hostname": "dc1-spine1", "service": "routes", "pollExcdPeriodCount": 0},
        ])
        out = assert_poll_health(state)
        assert len(out) == 1
        assert "dc1-leaf1:lldp" in out[0].subject

    def test_category_is_meta(self):
        """Regression guard: the poller self-health check belongs to
        the 'meta' category so Phase 6 filters can separate harness-
        health signals from fabric-health signals."""
        state = _state([
            {"hostname": "dc1-leaf1", "service": "lldp", "pollExcdPeriodCount": 3},
        ])
        out = assert_poll_health(state)
        assert len(out) == 1
        assert out[0].category == "meta"
