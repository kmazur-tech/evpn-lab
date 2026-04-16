"""Tests for the remaining pure-function surface in tasks/enrich/.

Already covered elsewhere in this directory:
  - derive_login_hash       in test_enrich_helpers.py
  - _build_lag              in test_lag_system_id.py
  - _lo0_unit_from_iface_name, _loopback_description
                            in test_enrich_helpers.py

This file fills two gaps the audit found before Phase 5 Part B:

  1. collect_underlay_neighbors(fabric_links) - bgp.py
     Pure list-in / list-out transform. Sorts neighbors by IP. The
     sorted output is what gets serialized into the BGP underlay
     stanza, so a sort-key change here is a deploy-time bug.

  2. _Strict pydantic models - models.py
     The `extra='forbid'` contract is the typo guard that protects
     every collector in tasks/enrich/ from silently dropping fields.
     If someone removes it (e.g. while "fixing" a strict model that
     rejected a new optional field), templates will start receiving
     missing keys and the failure mode is a malformed config on a
     real device. These tests pin the contract.
"""
import pytest

from tasks.enrich.bgp import collect_underlay_neighbors
from tasks.enrich.models import (
    BgpUnderlayNeighbor,
    FabricLink,
    HostData,
    Irb,
    LoopbackUnit,
    Tenant,
    VlanInMacVrf,
)


# ---------------------------------------------------------------------------
# collect_underlay_neighbors() - pure transform
# ---------------------------------------------------------------------------

def _link(addr, peer_ip, peer_asn, peer_name="peer"):
    """FabricLink factory with believable defaults so each test
    can specify only the fields it cares about."""
    return FabricLink(
        name=f"ge-0/0/{peer_asn % 10}",
        description=f"to {peer_name}",
        address=addr,
        peer_ip=peer_ip,
        peer_asn=peer_asn,
        peer_name=peer_name,
    )


class TestCollectUnderlayNeighbors:
    def test_empty_input_empty_output(self):
        assert collect_underlay_neighbors([]) == []

    def test_single_link_yields_single_neighbor(self):
        links = [_link("10.1.4.1/31", "10.1.4.0", 65001, "spine1")]
        out = collect_underlay_neighbors(links)
        assert len(out) == 1
        assert out[0].ip == "10.1.4.0"
        assert out[0].asn == 65001

    def test_multiple_links_produces_one_neighbor_per_link(self):
        """No deduplication - each fabric P2P is a distinct BGP
        session even if two links coincidentally point at the same
        peer IP. The dedup case does not exist in the lab today."""
        links = [
            _link("10.1.4.1/31", "10.1.4.0", 65001, "spine1"),
            _link("10.1.4.5/31", "10.1.4.4", 65002, "spine2"),
        ]
        out = collect_underlay_neighbors(links)
        assert len(out) == 2

    def test_sort_is_by_numeric_ip_not_lexicographic(self):
        """REGRESSION GUARD. Lexicographic sort would put 10.1.4.10
        before 10.1.4.2; numeric IP sort puts them in the right
        order. The BGP stanza in the rendered config depends on
        deterministic ordering or the byte-exact regression gate
        fires for cosmetic reasons."""
        links = [
            _link("10.1.4.1/31",  "10.1.4.10", 65010, "p10"),
            _link("10.1.4.3/31",  "10.1.4.2",  65002, "p2"),
            _link("10.1.4.5/31",  "10.1.4.1",  65001, "p1"),
        ]
        out = collect_underlay_neighbors(links)
        assert [n.ip for n in out] == ["10.1.4.1", "10.1.4.2", "10.1.4.10"]

    def test_sort_is_stable_across_input_order(self):
        """Same set of links in any input order -> same output.
        Catches accidental reliance on input order."""
        links_a = [
            _link("a/31", "10.1.4.0", 65001),
            _link("b/31", "10.1.4.4", 65002),
        ]
        links_b = list(reversed(links_a))
        out_a = collect_underlay_neighbors(links_a)
        out_b = collect_underlay_neighbors(links_b)
        assert [(n.ip, n.asn) for n in out_a] == [(n.ip, n.asn) for n in out_b]


# ---------------------------------------------------------------------------
# _Strict pydantic model contracts
# ---------------------------------------------------------------------------

class TestStrictExtraForbid:
    """The single most important contract in tasks/enrich/. Every
    model inherits from _Strict which sets `extra='forbid'`. If a
    collector misspells a field name, validation must raise. Without
    this guard the misspelled field is silently dropped, the template
    falls back to its default (or fails with UndefinedError on render),
    and we ship a broken config to a device.

    These tests pin the contract on the most-used models so a future
    "let's loosen this for flexibility" PR fails CI."""

    def test_loopback_unit_rejects_extra_field(self):
        with pytest.raises(Exception) as exc:
            LoopbackUnit(
                unit=1, address="10.1.0.1/32",
                description="r-id", typo_field="oops",
            )
        assert "extra" in str(exc.value).lower() or "forbid" in str(exc.value).lower()

    def test_fabric_link_rejects_extra_field(self):
        with pytest.raises(Exception):
            FabricLink(
                name="ge-0/0/0", description="x", address="10.1.4.0/31",
                peer_ip="10.1.4.1", peer_asn=65001, peer_name="leaf1",
                bandwidth=10000,  # not a real field
            )

    def test_bgp_underlay_neighbor_rejects_extra_field(self):
        with pytest.raises(Exception):
            BgpUnderlayNeighbor(ip="10.1.4.0", asn=65001, password="oops")

    def test_tenant_rejects_extra_field(self):
        with pytest.raises(Exception):
            Tenant(
                name="TENANT-1", tenant_id=1, l3vni=5000, rt="65000:5000",
                vrf_color="red",  # not a real field
            )

    def test_irb_rejects_extra_field(self):
        with pytest.raises(Exception):
            Irb(unit=10, leaf_ip="10.10.10.3/24", mtu=9000)


class TestStrictTypeValidation:
    """Schema drift catch: NetBox returning a string where pydantic
    expects an int (or vice versa) must surface as a clear validation
    error at enrich time, not as a Jinja error at render time."""

    def test_loopback_unit_int_required(self):
        with pytest.raises(Exception):
            LoopbackUnit(unit="one", address="10.1.0.1/32", description="r-id")

    def test_bgp_neighbor_asn_int_required(self):
        with pytest.raises(Exception):
            BgpUnderlayNeighbor(ip="10.1.4.0", asn="not-an-int")

    def test_tenant_l3vni_int_required(self):
        with pytest.raises(Exception):
            Tenant(name="T", tenant_id=1, l3vni="five-thousand", rt="65000:5000")

    def test_required_field_missing_raises(self):
        """Tenant.rt has no default; omitting it must raise."""
        with pytest.raises(Exception):
            Tenant(name="T", tenant_id=1, l3vni=5000)


class TestStrictDefaults:
    """Optional fields with sensible defaults must NOT raise when
    omitted. Catches an over-zealous tightening that would force
    every collector to spell out empty lists."""

    def test_host_data_all_optional_defaults(self):
        h = HostData()
        assert h.fabric_links == []
        assert h.tenants == []
        assert h.irbs == []
        assert h.loopbacks == []
        assert h.role_slug is None
        assert h.router_id is None

    def test_tenant_t5_prefixes_default_empty_list(self):
        t = Tenant(name="T", tenant_id=1, l3vni=5000, rt="65000:5000")
        assert t.t5_prefixes == []

    def test_irb_optional_fields_default_none(self):
        i = Irb(unit=10)
        assert i.leaf_ip is None
        assert i.gateway_ip is None
        assert i.anycast_mac is None

    def test_vlan_in_mac_vrf_vni_optional(self):
        v = VlanInMacVrf(name="VLAN10", vid=10, l3_interface="irb.10")
        assert v.vni is None
