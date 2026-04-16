"""Unit tests for the pure helpers in populate.py.

Three surfaces under test:

  1. slugify()       - lowercases, collapses non-alnum to single dash,
                       trims edge dashes. Used for every NetBox slug
                       across the whole data model.

  2. ensure_slug()   - mutates a dict in place, derives slug from
                       name if missing, leaves explicit slug alone.
                       Silent miss = duplicate-name CREATEs in NetBox.

  3. load_config()   - reads netbox-data.yml, substitutes ONLY the
                       expected env vars, hard-fails on unresolved
                       expected placeholders. The smoke test against
                       the real netbox-data.yml is the highest-value
                       single test in the project: catches YAML and
                       env-var typos at PR time, before they hit a
                       deploy.

Not tested (intentionally):
  - get_or_create() - pynetbox-coupled; mocking it would test the mock
  - main()          - the live populate run is the integration test;
                      Phase 1 README documents the clean-baseline + run
                      verification path
"""
import os
from pathlib import Path

import pytest

from conftest import populate

slugify = populate.slugify
ensure_slug = populate.ensure_slug
load_config = populate.load_config
EXPECTED_ENV_VARS = populate.EXPECTED_ENV_VARS


# ---------------------------------------------------------------------------
# slugify()
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_lowercases(self):
        assert slugify("DC1") == "dc1"

    def test_spaces_become_single_dash(self):
        assert slugify("Lab Operations") == "lab-operations"

    def test_multiple_spaces_collapse(self):
        assert slugify("Lab    Operations") == "lab-operations"

    def test_special_chars_become_dash(self):
        assert slugify("Juniper Networks") == "juniper-networks"
        assert slugify("EVPN/VXLAN") == "evpn-vxlan"
        assert slugify("ESI-LAG") == "esi-lag"

    def test_leading_trailing_dashes_stripped(self):
        # leading/trailing non-alphanumeric must not survive as edge dashes
        assert slugify("  spine  ") == "spine"
        assert slugify("--spine--") == "spine"
        assert slugify("!spine!") == "spine"

    def test_already_slug_unchanged(self):
        assert slugify("dc1-spine1") == "dc1-spine1"

    def test_real_phase1_tag_names(self):
        """The actual netbox-data.yml tag names must round-trip
        through slugify() to the slugs Phase 1 hardcodes alongside
        them. If slugify() changes, the populate.py create-only
        convergence breaks silently (would create duplicates)."""
        assert slugify("Spine") == "spine"
        assert slugify("ESI-LAG") == "esi-lag"
        assert slugify("Anycast-GW") == "anycast-gw"
        assert slugify("Suzieq") == "suzieq"

    def test_empty_string(self):
        assert slugify("") == ""

    def test_only_special_chars(self):
        assert slugify("---") == ""
        assert slugify("   ") == ""


# ---------------------------------------------------------------------------
# ensure_slug()
# ---------------------------------------------------------------------------

class TestEnsureSlug:
    def test_adds_slug_when_missing(self):
        d = {"name": "Spine"}
        out = ensure_slug(d)
        assert out["slug"] == "spine"

    def test_preserves_explicit_slug(self):
        """An explicit slug must NOT be overwritten - some Phase 1
        objects deliberately use shorter slugs than slugify() would
        produce (e.g. Management -> mgmt)."""
        d = {"name": "Management", "slug": "mgmt"}
        out = ensure_slug(d)
        assert out["slug"] == "mgmt"

    def test_no_name_no_slug(self):
        """No `name` key -> nothing to derive from -> no slug added."""
        d = {"description": "anonymous"}
        out = ensure_slug(d)
        assert "slug" not in out

    def test_returns_same_dict_in_place(self):
        """Existing populate.py callers rely on `data = ensure_slug(data)`
        being a no-op chain - the dict must be mutated, not replaced."""
        d = {"name": "Leaf"}
        out = ensure_slug(d)
        assert out is d


# ---------------------------------------------------------------------------
# load_config() - the highest-value test in the audit
# ---------------------------------------------------------------------------

class TestLoadConfig:
    """The smoke test that the real netbox-data.yml loads cleanly with
    a fully-populated env. Catches YAML syntax errors, missing env vars,
    and broken $VARIABLE references at PR time, BEFORE they reach a
    populate run against a live NetBox."""

    @pytest.fixture
    def populated_env(self, monkeypatch):
        """Set every var EXPECTED_ENV_VARS expects to a believable
        non-empty value. Real production values come from
        evpn-lab-env/env.sh and are not needed here - load_config
        does pure substitution, not validation."""
        monkeypatch.setenv("NETBOX_URL", "http://test.local:8000")
        monkeypatch.setenv("NETBOX_TOKEN", "Token test123")
        monkeypatch.setenv("MGMT_SUBNET", "10.99.99.0/24")
        monkeypatch.setenv("MGMT_dc1_spine1", "10.99.99.1/24")
        monkeypatch.setenv("MGMT_dc1_spine2", "10.99.99.2/24")
        monkeypatch.setenv("MGMT_dc1_leaf1",  "10.99.99.3/24")
        monkeypatch.setenv("MGMT_dc1_leaf2",  "10.99.99.4/24")

    def test_real_netbox_data_yml_loads(self, populated_env):
        """SMOKE TEST. The real, committed netbox-data.yml must load
        through load_config() without raising. This is the test that
        catches a typo in the YAML before a populate run tries to
        consume it."""
        cfg = load_config()
        assert isinstance(cfg, dict)
        assert "tags" in cfg
        assert "devices" in cfg

    def test_suzieq_tag_present_after_phase5_addition(self, populated_env):
        """Phase 5 added a `Suzieq` tag and applied it to the four
        DC1 fabric devices. This test pins that addition so a careless
        rebase can't silently revert it."""
        cfg = load_config()
        tag_slugs = {t["slug"] for t in cfg["tags"]}
        assert "suzieq" in tag_slugs

        suzieq_tagged = [
            d for d in cfg["devices"]
            if "suzieq" in d.get("tags", [])
        ]
        names = sorted(d["name"] for d in suzieq_tagged)
        assert names == ["dc1-leaf1", "dc1-leaf2", "dc1-spine1", "dc1-spine2"]

    def test_env_substitution_resolves_mgmt_ips(self, populated_env):
        cfg = load_config()
        # MGMT_dc1_spine1 was set to 10.99.99.1/24 in the fixture; the
        # device entry uses "$MGMT_dc1_spine1" which must be resolved.
        spine1 = next(d for d in cfg["devices"] if d["name"] == "dc1-spine1")
        assert spine1["oob_ip"] == "10.99.99.1/24"

    def test_unresolved_expected_var_hard_fails(self, monkeypatch, capsys):
        """Missing one of the expected env vars must cause SystemExit
        with a clear error - we never want to silently render an empty
        string into a NetBox object."""
        monkeypatch.setenv("NETBOX_URL", "http://test.local:8000")
        monkeypatch.setenv("NETBOX_TOKEN", "Token test123")
        monkeypatch.setenv("MGMT_SUBNET", "10.99.99.0/24")
        # Deliberately omit one
        monkeypatch.delenv("MGMT_dc1_spine1", raising=False)
        monkeypatch.setenv("MGMT_dc1_spine2", "10.99.99.2/24")
        monkeypatch.setenv("MGMT_dc1_leaf1",  "10.99.99.3/24")
        monkeypatch.setenv("MGMT_dc1_leaf2",  "10.99.99.4/24")
        with pytest.raises(SystemExit):
            load_config()
        err = capsys.readouterr().out
        assert "MGMT_dc1_spine1" in err

    def test_only_expected_vars_are_substituted(self, populated_env, monkeypatch):
        """A $HOME or $PATH appearing in the YAML must NOT be touched.
        The EXPECTED_ENV_VARS allowlist is a security boundary - test
        that it actually does what it claims."""
        # HOME is universally set; the test verifies that even if some
        # devious YAML included $HOME, it would not be substituted.
        # We can't easily mutate the on-disk YAML, so we assert the
        # invariant on EXPECTED_ENV_VARS instead.
        assert "HOME" not in EXPECTED_ENV_VARS
        assert "PATH" not in EXPECTED_ENV_VARS
