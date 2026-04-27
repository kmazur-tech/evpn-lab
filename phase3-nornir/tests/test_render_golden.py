"""Golden-file render tests.

Render main.j2 with canned fixture data for each device and assert
normalized byte-equality against the checked-in golden files in
phase3-nornir/expected/.  Catches template regressions without
needing NetBox or devices.
"""

import json
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from deploy import normalize

PHASE3 = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "render"
EXPECTED_DIR = PHASE3 / "expected"
TEMPLATE_DIR = PHASE3 / "templates" / "junos"
DEFAULTS_FILE = PHASE3 / "vars" / "junos_defaults.yml"

# Fixed hash matching the golden files.  normalize() masks all
# encrypted-password values to "<HASH>" so the exact content does
# not affect the comparison -- but using the real value lets us
# also verify the raw (pre-normalization) output if needed.
TEST_HASH = (
    "$6$evpnlab1$x/0MmAitK3rDmZWPb.mNqW4YglzhbN5D0g0aGR"
    "toWAaSUUMM1Om/FGfcPT3nmCP26uu2srtayTb46F1Id6Z/x."
)

DEVICES = ["dc1-spine1", "dc1-spine2", "dc1-leaf1", "dc1-leaf2"]


@pytest.fixture(scope="module")
def defaults():
    return yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jinja_env():
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )


def _load_fixture(device_name):
    path = FIXTURE_DIR / f"{device_name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _render(device_name, defaults, jinja_env):
    host = _load_fixture(device_name)
    template = jinja_env.get_template("main.j2")
    return template.render(
        host=host,
        defaults=defaults,
        junos_root_hash=TEST_HASH,
        junos_admin_hash=TEST_HASH,
    )


# ---- full-config golden-file tests ----

@pytest.mark.parametrize("device", DEVICES)
def test_full_render_matches_golden(device, defaults, jinja_env):
    """Rendered main.j2 must be byte-equal to expected/<device>.conf
    after normalization (hash masking, version/timestamp stripping)."""
    rendered = _render(device, defaults, jinja_env)
    expected = (EXPECTED_DIR / f"{device}.conf").read_text(encoding="utf-8")

    rendered_norm = normalize(rendered).strip()
    expected_norm = normalize(expected).strip()

    assert rendered_norm == expected_norm, (
        f"Rendered output for {device} does not match golden file.\n"
        f"Run: diff <(echo \"$rendered\") expected/{device}.conf"
    )


# ---- structural sanity checks (role-driven) ----

@pytest.mark.parametrize("device", ["dc1-spine1", "dc1-spine2"])
def test_spine_has_no_forwarding_options(device, defaults, jinja_env):
    rendered = _render(device, defaults, jinja_env)
    assert "forwarding-options" not in rendered


@pytest.mark.parametrize("device", ["dc1-leaf1", "dc1-leaf2"])
def test_leaf_has_forwarding_options(device, defaults, jinja_env):
    rendered = _render(device, defaults, jinja_env)
    assert "forwarding-options" in rendered


@pytest.mark.parametrize("device", ["dc1-spine1", "dc1-spine2"])
def test_spine_has_cluster_rr(device, defaults, jinja_env):
    rendered = _render(device, defaults, jinja_env)
    host = _load_fixture(device)
    assert f"cluster {host['router_id']};" in rendered


@pytest.mark.parametrize("device", ["dc1-leaf1", "dc1-leaf2"])
def test_leaf_has_no_cluster_rr(device, defaults, jinja_env):
    rendered = _render(device, defaults, jinja_env)
    assert "cluster " not in rendered


@pytest.mark.parametrize("device", ["dc1-leaf1", "dc1-leaf2"])
def test_leaf_has_network_isolation(device, defaults, jinja_env):
    rendered = _render(device, defaults, jinja_env)
    assert "network-isolation" in rendered


@pytest.mark.parametrize("device", ["dc1-spine1", "dc1-spine2"])
def test_spine_has_no_network_isolation(device, defaults, jinja_env):
    rendered = _render(device, defaults, jinja_env)
    assert "network-isolation" not in rendered


@pytest.mark.parametrize("device", ["dc1-leaf1", "dc1-leaf2"])
def test_leaf_has_evpn_vxlan_instance(device, defaults, jinja_env):
    rendered = _render(device, defaults, jinja_env)
    assert "instance-type mac-vrf;" in rendered
    assert "EVPN-VXLAN" in rendered


@pytest.mark.parametrize("device", ["dc1-spine1", "dc1-spine2"])
def test_spine_has_no_evpn_vxlan_instance(device, defaults, jinja_env):
    rendered = _render(device, defaults, jinja_env)
    assert "instance-type mac-vrf;" not in rendered


@pytest.mark.parametrize("device", DEVICES)
def test_no_deploy_sentinels(device, defaults, jinja_env):
    """Rendered output must not contain any deploy-guard sentinels."""
    rendered = _render(device, defaults, jinja_env)
    for sentinel in ["PLACEHOLDER", "TODO", "REPLACE_ME", "<HASH>"]:
        assert sentinel not in rendered, f"{device}: sentinel '{sentinel}' found"
