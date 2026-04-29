"""Tests for pure helper functions in tasks.enrich.

These functions take primitive inputs and return primitives - no
NetBox, no Nornir context. Easy to test, regress easily on refactor.
"""

import pytest

from tasks.enrich import (
    _lo0_unit_from_iface_name,
    _loopback_description,
    derive_login_hash,
)


# ----- _lo0_unit_from_iface_name -----

@pytest.mark.parametrize("name,expected", [
    ("lo0.0", 0),
    ("lo0.1", 1),
    ("lo0.2", 2),
    ("lo0.42", 42),
])
def test_lo0_unit_extracts(name, expected):
    assert _lo0_unit_from_iface_name(name) == expected


@pytest.mark.parametrize("name", [
    "ge-0/0/0",
    "lo0",            # missing unit
    "lo1.0",          # wrong loopback
    "irb.10",
    "ae0",
    "",
    "lo0.x",          # non-numeric unit
])
def test_lo0_unit_rejects_non_lo0(name):
    assert _lo0_unit_from_iface_name(name) is None


# ----- _loopback_description -----

def test_lo0_unit1_leaf_is_router_id_vtep():
    """Leaves act as VTEP. Their lo0.1 description differs from spines."""
    assert _loopback_description(1, "leaf", None) == "Router-ID / VTEP"


def test_lo0_unit1_spine_is_router_id():
    """Spines do not act as VTEP."""
    assert _loopback_description(1, "spine", None) == "Router-ID"


def test_lo0_unit2_in_vrf_uses_vrf_name():
    """lo0.2 lives in a tenant VRF; description matches the routing
    instance name (Junos shows it as `VRF <name>`)."""
    assert _loopback_description(2, "leaf", "TENANT-1") == "VRF TENANT-1"


def test_lo0_unit_no_vrf_no_router_id_falls_back():
    """A higher-numbered unit with no VRF -> generic fallback."""
    assert _loopback_description(5, "leaf", None) == "lo0.5"


def test_lo0_unit1_ignores_vrf_name():
    """Unit 1 is router-id; even if it had a VRF, the description
    rule for unit 1 wins."""
    assert _loopback_description(1, "leaf", "TENANT-1") == "Router-ID / VTEP"


# ----- derive_login_hash -----

def test_derive_login_hash_deterministic(monkeypatch):
    """Same plaintext + same salt -> same hash. The whole point."""
    monkeypatch.setenv("JUNOS_LOGIN_PASSWORD", "TestLabPass1")
    monkeypatch.setenv("JUNOS_LOGIN_SALT", "$6$evpnlab1$")
    h1 = derive_login_hash()
    h2 = derive_login_hash()
    assert h1 == h2


def test_derive_login_hash_format(monkeypatch):
    """Result must be a real SHA-512 crypt: $6$<salt>$<86chars>."""
    monkeypatch.setenv("JUNOS_LOGIN_PASSWORD", "TestLabPass1")
    monkeypatch.setenv("JUNOS_LOGIN_SALT", "$6$evpnlab1$")
    h = derive_login_hash()
    assert h.startswith("$6$evpnlab1$")
    # The hash digest portion is exactly 86 chars for SHA-512 crypt
    digest = h.split("$", 3)[3]
    assert len(digest) == 86


def test_derive_login_hash_changes_with_plaintext(monkeypatch):
    """Different plaintext -> different hash (with same salt)."""
    monkeypatch.setenv("JUNOS_LOGIN_SALT", "$6$evpnlab1$")
    monkeypatch.setenv("JUNOS_LOGIN_PASSWORD", "TestLabPass1")
    h1 = derive_login_hash()
    monkeypatch.setenv("JUNOS_LOGIN_PASSWORD", "different")
    h2 = derive_login_hash()
    assert h1 != h2


def test_derive_login_hash_changes_with_salt(monkeypatch):
    """Different salt -> different hash (with same plaintext)."""
    monkeypatch.setenv("JUNOS_LOGIN_PASSWORD", "TestLabPass1")
    monkeypatch.setenv("JUNOS_LOGIN_SALT", "$6$saltA$")
    h1 = derive_login_hash()
    monkeypatch.setenv("JUNOS_LOGIN_SALT", "$6$saltB$")
    h2 = derive_login_hash()
    assert h1 != h2


def test_derive_login_hash_missing_password_raises(monkeypatch):
    """No fallback to placeholder. Hard fail. This is the postmortem fix."""
    monkeypatch.delenv("JUNOS_LOGIN_PASSWORD", raising=False)
    monkeypatch.setenv("JUNOS_LOGIN_SALT", "$6$evpnlab1$")
    with pytest.raises(RuntimeError, match="JUNOS_LOGIN_PASSWORD"):
        derive_login_hash()


def test_derive_login_hash_missing_salt_raises(monkeypatch):
    monkeypatch.setenv("JUNOS_LOGIN_PASSWORD", "TestLabPass1")
    monkeypatch.delenv("JUNOS_LOGIN_SALT", raising=False)
    with pytest.raises(RuntimeError, match="JUNOS_LOGIN_SALT"):
        derive_login_hash()


def test_derive_login_hash_empty_password_raises(monkeypatch):
    """Empty string is treated the same as missing - we never want to
    silently render an empty hash."""
    monkeypatch.setenv("JUNOS_LOGIN_PASSWORD", "")
    monkeypatch.setenv("JUNOS_LOGIN_SALT", "$6$evpnlab1$")
    with pytest.raises(RuntimeError, match="JUNOS_LOGIN_PASSWORD"):
        derive_login_hash()
