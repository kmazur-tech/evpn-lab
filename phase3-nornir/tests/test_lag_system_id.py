"""Tests for the deterministic ESI-LAG system-id derivation.

The formula `f"00:00:00:00:{(ae_index + 3):02x}:00"` must produce a
valid 6-octet MAC address for any ae_index from 0 up to 252 (the
last value where ae_index+3 fits in one byte).

History: an earlier formula `f"00:00:00:00:0{ae_index + 3}:00"`
produced `00:00:00:00:010:00` for ae7 - invalid 3-char octet.
This test pins the fix and prevents regression.
"""
import re

import pytest

# We test _build_lag indirectly via a minimal stub that mimics the
# pynetbox interface object shape. The function only reads `.name`
# and `.untagged_vlan` so a small stub is enough.
from tasks.enrich.interfaces import _build_lag


class IfaceStub:
    def __init__(self, name, untagged_vlan_name=None):
        self.name = name
        if untagged_vlan_name is None:
            self.untagged_vlan = None
        else:
            self.untagged_vlan = type("V", (), {"name": untagged_vlan_name})()


# Valid Junos LACP system-id: 6 colon-separated 2-char hex octets
VALID_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


@pytest.mark.parametrize("name,expected", [
    # Phase 2 baseline values - the formula MUST produce these for ae0/ae1
    ("ae0",  "00:00:00:00:03:00"),
    ("ae1",  "00:00:00:00:04:00"),
    # ae6 is the last single-digit case; ae7+ was broken in the old formula
    ("ae6",  "00:00:00:00:09:00"),
    ("ae7",  "00:00:00:00:0a:00"),
    ("ae12", "00:00:00:00:0f:00"),
    ("ae13", "00:00:00:00:10:00"),
    ("ae252", "00:00:00:00:ff:00"),
])
def test_lag_system_id_formula(name, expected):
    lag = _build_lag(IfaceStub(name, untagged_vlan_name="VLAN20"))
    assert lag.system_id == expected
    assert VALID_MAC_RE.match(lag.system_id), \
        f"system_id {lag.system_id!r} is not a valid 6-octet MAC"


@pytest.mark.parametrize("name,expected_admin_key", [
    ("ae0", 1),
    ("ae1", 2),
    ("ae42", 43),
])
def test_lag_admin_key_formula(name, expected_admin_key):
    """admin-key = ae_index + 1. Pins the convention so a refactor
    can't silently change it under templates that depend on it."""
    lag = _build_lag(IfaceStub(name, untagged_vlan_name="VLAN20"))
    assert lag.admin_key == expected_admin_key


def test_lag_vlan_name_preserved():
    lag = _build_lag(IfaceStub("ae0", untagged_vlan_name="VLAN20"))
    assert lag.vlan_name == "VLAN20"


def test_lag_no_vlan():
    """Trunk LAG with no untagged_vlan -> vlan_name stays None."""
    lag = _build_lag(IfaceStub("ae0"))
    assert lag.vlan_name is None


def test_lag_phase2_baseline_values_unchanged():
    """Phase 2 expected/ baselines were generated with the old formula
    for ae0 and ae1. The hex-format fix MUST produce identical bytes
    for these two interfaces - else expected/ would need regenerating
    AND a re-commit on the live fabric. This test guards against that."""
    lag0 = _build_lag(IfaceStub("ae0", untagged_vlan_name="VLAN20"))
    lag1 = _build_lag(IfaceStub("ae1", untagged_vlan_name="VLAN20"))
    assert lag0.system_id == "00:00:00:00:03:00"
    assert lag1.system_id == "00:00:00:00:04:00"
