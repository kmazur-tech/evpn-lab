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
    def test_ex9214_maps_to_vjunos_switch(self):
        """REGRESSION GUARD. EX9214 in this lab is vJunos-switch
        emulation, NOT a real EX. The project-owned junos-vjunos-switch
        devtype is added by the build-time patcher in
        suzieq-image/add-junos-vjunos-switch.py and combines junos-mx's
        device service (single-RE JSON) with junos-qfx's lldp
        service (detail view, has port id). Do not change to
        junos-mx (would lose the LLDP detail view) or junos-ex
        (would lose the single-RE device parsing). See
        README 'junos-vjunos-switch devtype' for the full background."""
        assert map_devtype("EX9214") == "junos-vjunos-switch"

    def test_qfx_family_maps_to_vjunos_switch(self):
        """vQFX containers (if anyone reintroduces them) share
        vJunos-switch's behavior - same vrnetlab origin, same
        single-RE device JSON, same need for the project-owned
        junos-vjunos-switch devtype rather than the upstream junos-qfx."""
        assert map_devtype("QFX5120-32C") == "junos-vjunos-switch"
        assert map_devtype("qfx10002-60c") == "junos-vjunos-switch"

    def test_real_mx_maps_to_junos_mx(self):
        """Real Juniper MX hardware (not vJunos) uses the upstream
        junos-mx devtype unmodified - the patcher leaves it alone
        precisely so a future Phase 10+ MX is not affected by the
        junos-vjunos-switch overlay."""
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
        assert lab_inv["sources"][0]["name"] == "dc1-junos-vjunos-switch"

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

    def test_devices_block_uses_vjunos_switch_devtype(self, lab_inv):
        """The override regression guard, but at the inventory
        level rather than the function level. EX9214 -> junos-vjunos-switch
        is the project's documented mapping; the patched suzieq image
        is what makes junos-vjunos-switch a valid devtype at the SuzieQ
        side."""
        assert len(lab_inv["devices"]) == 1
        assert lab_inv["devices"][0]["devtype"] == "junos-vjunos-switch"

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
        """Hypothetical: dc1 with both a vJunos lab device AND a
        real Juniper MX. vJunos-switch maps to junos-vjunos-switch
        (project-owned devtype) while MX204 maps to junos-mx
        (upstream devtype). Two distinct devtypes -> two sources."""
        devices = [
            fake_nb_device("dc1-leaf1", "EX9214", "10.1.0.1", "dc1"),
            fake_nb_device("dc1-mx-edge", "MX204", "10.1.0.99", "dc1"),
        ]
        inv = yaml.safe_load(generate(devices))
        assert len(inv["sources"]) == 2
        source_names = sorted(s["name"] for s in inv["sources"])
        assert source_names == ["dc1-junos-mx", "dc1-junos-vjunos-switch"]

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


# ---------------------------------------------------------------------------
# SUZIEQ_STRICT_HOST_KEYS env var (Phase 5.1 security hygiene)
# ---------------------------------------------------------------------------
#
# The default is still ignore-known-hosts: true because lab vJunos
# containers regenerate SSH host keys on every containerlab
# destroy/deploy cycle and wiping known_hosts on every cold boot is
# operationally painful. Production deployments are expected to set
# SUZIEQ_STRICT_HOST_KEYS=1 in the environment and provision
# known_hosts via configuration management.
#
# These tests pin the env var contract and the default behavior so
# a future contributor cannot silently flip either direction.

class TestStrictHostKeysEnvVar:
    _STRICT_DEVICES = [
        fake_nb_device("dc1-leaf1", "EX9214", "10.1.0.1", "dc1"),
    ]

    def test_default_is_permissive_lab_mode(self, monkeypatch):
        """With no env var set, the lab default stays permissive so
        existing lab deploys keep working after the env var lands."""
        monkeypatch.delenv("SUZIEQ_STRICT_HOST_KEYS", raising=False)
        inv = yaml.safe_load(generate(self._STRICT_DEVICES))
        assert inv["devices"][0]["ignore-known-hosts"] is True

    def test_strict_keys_1_flips_to_strict(self, monkeypatch):
        monkeypatch.setenv("SUZIEQ_STRICT_HOST_KEYS", "1")
        inv = yaml.safe_load(generate(self._STRICT_DEVICES))
        assert inv["devices"][0]["ignore-known-hosts"] is False

    def test_strict_keys_true_flips_to_strict(self, monkeypatch):
        monkeypatch.setenv("SUZIEQ_STRICT_HOST_KEYS", "true")
        inv = yaml.safe_load(generate(self._STRICT_DEVICES))
        assert inv["devices"][0]["ignore-known-hosts"] is False

    def test_strict_keys_case_insensitive(self, monkeypatch):
        for value in ("TRUE", "Yes", "ON", "1"):
            monkeypatch.setenv("SUZIEQ_STRICT_HOST_KEYS", value)
            inv = yaml.safe_load(generate(self._STRICT_DEVICES))
            assert inv["devices"][0]["ignore-known-hosts"] is False, (
                f"{value!r} should enable strict host keys"
            )

    def test_strict_keys_falsy_values_stay_permissive(self, monkeypatch):
        for value in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("SUZIEQ_STRICT_HOST_KEYS", value)
            inv = yaml.safe_load(generate(self._STRICT_DEVICES))
            assert inv["devices"][0]["ignore-known-hosts"] is True, (
                f"{value!r} should stay permissive"
            )

    def test_strict_keys_kwarg_overrides_env(self, monkeypatch):
        """Tests pass strict_host_keys= explicitly to avoid touching
        process env. The kwarg must win over the env var so the
        tests themselves are reproducible."""
        monkeypatch.setenv("SUZIEQ_STRICT_HOST_KEYS", "")
        inv = yaml.safe_load(
            generate(self._STRICT_DEVICES, strict_host_keys=True)
        )
        assert inv["devices"][0]["ignore-known-hosts"] is False
        inv = yaml.safe_load(
            generate(self._STRICT_DEVICES, strict_host_keys=False)
        )
        assert inv["devices"][0]["ignore-known-hosts"] is True

    def test_all_device_groups_get_same_setting(self, monkeypatch):
        """If strict mode is on, EVERY devices block entry must be
        strict. Not just the first one."""
        monkeypatch.setenv("SUZIEQ_STRICT_HOST_KEYS", "1")
        devices = [
            fake_nb_device("dc1-leaf1", "EX9214", "10.1.0.1", "dc1"),
            fake_nb_device("dc1-mx-edge", "MX204", "10.1.0.99", "dc1"),
        ]
        inv = yaml.safe_load(generate(devices))
        assert len(inv["devices"]) == 2
        assert all(d["ignore-known-hosts"] is False for d in inv["devices"])
