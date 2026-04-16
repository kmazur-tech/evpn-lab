"""Unit tests for gen-inventory.py.

Two surfaces under test:

  1. map_devtype()  - the substring lookup with the documented
     junos-mx override. Critical regression guard for the multi-RE
     JSON shape pothole; future contributors WILL be tempted to
     "fix" the EX9214 -> junos-mx mapping. The first test exists to
     stop them.

  2. generate()     - the (namespace, devtype) grouping logic that
     produces the SuzieQ native inventory. Becomes load-bearing in
     Phase 10 when DC2 + cEOS arrives.

What is intentionally NOT tested:
  - fetch_devices() - 8 lines of stdlib urllib; mocking it would
    test the mock, not the script.
  - Verbatim YAML output - we round-trip through yaml.safe_load and
    assert on the structured dict, not the formatting.
  - Anything that needs a running NetBox or poller - that's the
    Part A live verification gate, run on netdevops-srv.
"""
import pytest
import yaml

from helpers import gen_inventory, fake_nb_device

generate = gen_inventory.generate
map_devtype = gen_inventory.map_devtype


# ---------------------------------------------------------------------------
# map_devtype() - the JSON-shape override
# ---------------------------------------------------------------------------

class TestDevtypeMap:
    def test_ex9214_maps_to_junos_mx_NOT_junos_ex(self):
        """REGRESSION GUARD. Do not change to junos-ex without first
        verifying that vJunos-switch returns a multi-routing-engine
        wrapper in `show system uptime | display json`. It does not
        on the pinned vrnetlab image. See README "Junos devtype
        override" and gen-inventory.py DEVTYPE_OVERRIDES comment."""
        assert map_devtype("EX9214") == "junos-mx"

    def test_qfx_family_also_maps_to_junos_mx(self):
        """vQFX containers (Phase 10 dc2-arista may use real Arista
        cEOS, but if anyone reintroduces vQFX they need this)."""
        assert map_devtype("QFX5120-32C") == "junos-mx"
        assert map_devtype("qfx10002-60c") == "junos-mx"

    def test_real_mx_maps_to_junos_mx(self):
        assert map_devtype("MX204") == "junos-mx"

    def test_srx_maps_to_junos_mx(self):
        assert map_devtype("SRX345") == "junos-mx"

    def test_unknown_model_returns_none(self):
        """Caller is responsible for skipping with a WARNING line."""
        assert map_devtype("Catalyst 9300") is None

    def test_empty_or_none_model_returns_none(self):
        assert map_devtype(None) is None
        assert map_devtype("") is None

    def test_match_is_case_insensitive(self):
        assert map_devtype("ex9214") == map_devtype("EX9214")
        assert map_devtype("Ex9214") == map_devtype("EX9214")


# ---------------------------------------------------------------------------
# generate() - inventory shape and grouping
# ---------------------------------------------------------------------------

class TestGenerateLabShape:
    """The exact 4-device shape Phase 5 Part A actually deploys."""

    @pytest.fixture
    def lab_devices(self):
        return [
            fake_nb_device("dc1-spine1", "EX9214", "172.16.18.160", "dc1"),
            fake_nb_device("dc1-spine2", "EX9214", "172.16.18.161", "dc1"),
            fake_nb_device("dc1-leaf1",  "EX9214", "172.16.18.162", "dc1"),
            fake_nb_device("dc1-leaf2",  "EX9214", "172.16.18.163", "dc1"),
        ]

    @pytest.fixture
    def lab_inv(self, lab_devices):
        return yaml.safe_load(generate(lab_devices))

    def test_yaml_is_valid(self, lab_inv):
        assert isinstance(lab_inv, dict)

    def test_top_level_keys_are_what_suzieq_expects(self, lab_inv):
        assert set(lab_inv.keys()) == {
            "sources", "devices", "auths", "namespaces"
        }

    def test_single_source_for_single_namespace_devtype_combo(self, lab_inv):
        assert len(lab_inv["sources"]) == 1
        assert lab_inv["sources"][0]["name"] == "dc1-junos-mx"

    def test_all_four_devices_present_in_source(self, lab_inv):
        hosts = lab_inv["sources"][0]["hosts"]
        assert len(hosts) == 4
        addrs = sorted(h["url"] for h in hosts)
        assert addrs == [
            "ssh://172.16.18.160",
            "ssh://172.16.18.161",
            "ssh://172.16.18.162",
            "ssh://172.16.18.163",
        ]

    def test_devices_block_uses_junos_mx_devtype(self, lab_inv):
        """The override regression guard, but at the inventory
        level rather than the function level."""
        assert len(lab_inv["devices"]) == 1
        assert lab_inv["devices"][0]["devtype"] == "junos-mx"

    def test_devices_block_has_ignore_known_hosts_true(self, lab_inv):
        """vJunos containers regenerate keys on every cold boot.
        Without this, only the first device polls successfully and
        the other three fail with `Host key is not trusted`."""
        assert lab_inv["devices"][0]["ignore-known-hosts"] is True

    def test_namespace_name_pulled_from_netbox_site_slug(self, lab_inv):
        assert lab_inv["namespaces"][0]["name"] == "dc1"

    def test_creds_use_env_resolver_not_literal(self, lab_inv):
        """Project rule (feedback_no_secrets): never write a real
        password to disk in the repo or generated artifacts."""
        auth = lab_inv["auths"][0]
        assert auth["username"] == "env:JUNOS_SSH_USER"
        assert auth["password"] == "env:JUNOS_SSH_PASSWORD"


class TestGenerateGroupingFanout:
    """The Phase 10 multi-DC / multi-vendor case the lab does not
    yet exercise but the script must support."""

    def test_two_sites_same_devtype_make_two_namespaces(self):
        devices = [
            fake_nb_device("dc1-leaf1", "EX9214", "10.1.0.1", "dc1"),
            fake_nb_device("dc2-leaf1", "EX9214", "10.2.0.1", "dc2"),
        ]
        inv = yaml.safe_load(generate(devices))
        ns_names = sorted(n["name"] for n in inv["namespaces"])
        assert ns_names == ["dc1", "dc2"]
        # Each namespace gets its own source even when devtype is shared
        assert len(inv["sources"]) == 2

    def test_one_site_two_devtypes_makes_two_sources(self):
        """Hypothetical: dc1 with both vJunos and a real MX."""
        devices = [
            fake_nb_device("dc1-leaf1", "EX9214", "10.1.0.1", "dc1"),
            fake_nb_device("dc1-mx-edge", "MX204", "10.1.0.99", "dc1"),
        ]
        inv = yaml.safe_load(generate(devices))
        # Both map to junos-mx, so they actually collapse to one
        # source - which is correct behavior. Verify that.
        assert len(inv["sources"]) == 1
        assert len(inv["sources"][0]["hosts"]) == 2

    def test_site_slug_is_lowercased(self):
        devices = [
            fake_nb_device("d", "EX9214", "1.1.1.1", "DC1-UPPER"),
        ]
        inv = yaml.safe_load(generate(devices))
        assert inv["namespaces"][0]["name"] == "dc1-upper"


class TestGenerateSkipping:
    """Devices the script must filter out (with WARNING)."""

    def test_device_without_oob_ip_is_skipped(self, capsys):
        devices = [
            fake_nb_device("orphan",   "EX9214", None,         "dc1"),
            fake_nb_device("dc1-leaf1", "EX9214", "10.1.0.1", "dc1"),
        ]
        inv = yaml.safe_load(generate(devices))
        # orphan filtered out, the other one survives
        assert len(inv["sources"][0]["hosts"]) == 1
        err = capsys.readouterr().err
        assert "orphan" in err and "no oob_ip" in err

    def test_device_with_unsupported_model_is_skipped(self, capsys):
        devices = [
            fake_nb_device("oddball", "Catalyst9300", "1.2.3.4", "dc1"),
            fake_nb_device("dc1-leaf1", "EX9214",     "10.1.0.1", "dc1"),
        ]
        inv = yaml.safe_load(generate(devices))
        assert len(inv["sources"][0]["hosts"]) == 1
        err = capsys.readouterr().err
        assert "oddball" in err
        assert "Catalyst9300" in err

    def test_empty_device_list_raises(self):
        with pytest.raises(ValueError, match="no devices"):
            generate([])

    def test_all_devices_filtered_raises(self):
        devices = [
            fake_nb_device("orphan", "EX9214", None, "dc1"),
        ]
        with pytest.raises(ValueError, match="no usable devices"):
            generate(devices)
