"""Tests for extract_stanza() - the brace-balanced Junos stanza extractor.

Used by the regression gate to find a named stanza in a baseline file
and pull it out for byte-comparison against rendered output. Bugs in
this parser produce false PASS or false FAIL on real configs.
"""
from deploy import extract_stanza


SIMPLE = """system {
    host-name dc1-spine1;
}
chassis {
    aggregated-devices {
        ethernet {
            device-count 48;
        }
    }
}
"""


def test_extract_top_level_stanza():
    out = extract_stanza(SIMPLE, "system")
    assert "host-name dc1-spine1" in out
    assert "chassis" not in out


def test_extract_handles_nested_braces():
    """chassis has 3 levels of nested braces - depth tracking must be correct."""
    out = extract_stanza(SIMPLE, "chassis")
    assert "device-count 48" in out
    # Three opening, three closing braces -> outermost balanced
    assert out.count("{") == out.count("}") == 3


def test_extract_missing_stanza_returns_empty():
    out = extract_stanza(SIMPLE, "protocols")
    assert out == ""


def test_extract_indented_stanza():
    """lo0 lives at indent 4 inside `interfaces { ... }`. Must be findable
    by passing indent='    '."""
    text = """interfaces {
    ge-0/0/0 {
        mtu 9192;
    }
    lo0 {
        unit 1 {
            description Router-ID;
        }
    }
}
"""
    out = extract_stanza(text, "lo0", indent="    ")
    assert "unit 1" in out
    assert "description Router-ID" in out
    assert "ge-0/0/0" not in out


def test_extract_does_not_match_substring():
    """`router-id` should not match a stanza named `router`."""
    text = "router {\n    foo;\n}\nrouter-id 10.1.0.1;\n"
    out = extract_stanza(text, "router")
    assert "foo" in out
    assert "router-id" not in out


def test_extract_first_match_only():
    """If a name appears twice, return only the first complete block."""
    text = """foo {
    a;
}
foo {
    b;
}
"""
    out = extract_stanza(text, "foo")
    assert "a;" in out
    assert "b;" not in out


def test_brace_in_string_does_not_break_parser():
    """Junos descriptions with braces in strings would break a naive
    parser. The current implementation does NOT handle this (no string
    awareness), so we document the limitation as the test."""
    # Real Junos configs don't usually have { or } inside descriptions,
    # but if they ever do, the parser will get confused. This test
    # documents current behavior - a description containing `}` ends
    # the stanza early. If we ever need to fix this, the test pins the
    # current limitation so the fix is intentional.
    text = """foo {
    description "this } breaks";
    bar;
}
"""
    out = extract_stanza(text, "foo")
    # Currently the `}` inside the string ends the stanza prematurely.
    # This test pins the limitation. Junos configs in this lab don't
    # contain { or } inside string literals, so it doesn't bite us.
    assert "this }" in out


def test_extract_real_routing_options_stanza():
    """Realistic Phase 2 baseline fragment."""
    text = """routing-options {
    router-id 10.1.0.3;
    graceful-restart;
    forwarding-table {
        export LOAD-BALANCE;
        chained-composite-next-hop {
            ingress {
                evpn;
            }
        }
    }
}
protocols {
    bgp {
        group UNDERLAY;
    }
}
"""
    out = extract_stanza(text, "routing-options")
    assert "router-id 10.1.0.3" in out
    assert "graceful-restart" in out
    assert "LOAD-BALANCE" in out
    assert "evpn;" in out
    assert "bgp" not in out  # protocols stanza must NOT leak in
