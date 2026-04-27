"""Integration tests for enrich_from_netbox() using vcrpy cassettes.

Each test replays a recorded NetBox HTTP session for one device and
asserts that the enriched HostData matches the canned fixture in
tests/fixtures/render/.  This catches NetBox schema drift, pynetbox
query bugs, and collector regressions without needing a running
NetBox instance.

Cassettes are recorded once against a known-good NetBox snapshot
using phase6-cicd/scripts/refresh-netbox-cassettes.py, then checked
into the repo.  Tests skip gracefully when cassettes are absent.
"""

import json
from pathlib import Path

import pytest

vcr = pytest.importorskip("vcr", reason="vcrpy not installed")

from tasks.enrich.main import enrich_from_netbox

PHASE3 = Path(__file__).resolve().parent.parent
CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "render"

DEVICES = ["dc1-spine1", "dc1-spine2", "dc1-leaf1", "dc1-leaf2"]

# Fields written by enrich_from_netbox() via HostData.model_dump().
# 'name' is not part of HostData -- it comes from the Nornir inventory.
ENRICH_FIELDS = [
    "role_slug", "router_id", "asn",
    "fabric_links", "access_ports", "lag_members", "lags", "irbs",
    "loopbacks", "tenants",
    "mgmt_gw_v4", "mgmt_gw_v6",
    "mac_vrf_interfaces", "vlans_in_mac_vrf", "extended_vni_list",
    "underlay_neighbors", "overlay_neighbors",
]


# --- lightweight mock of the Nornir Task/Host interface ---

class _MockHost(dict):
    """Dict that also exposes .name as an attribute."""
    def __init__(self, name):
        super().__init__()
        self._name = name

    @property
    def name(self):
        return self._name


class _MockTask:
    """Minimal Nornir Task stub for enrich_from_netbox()."""
    def __init__(self, host):
        self.host = host


# --- vcrpy configuration ---

def _make_vcr():
    """VCR instance with token sanitization."""
    return vcr.VCR(
        cassette_library_dir=str(CASSETTE_DIR),
        record_mode="none",
        decode_compressed_response=True,
        before_record_request=_sanitize_request,
    )


def _sanitize_request(request):
    """Strip Authorization header from recorded cassettes."""
    if "Authorization" in request.headers:
        request.headers["Authorization"] = "Token sanitized"
    return request


# --- fixtures ---

@pytest.fixture()
def mock_netbox_env(monkeypatch):
    """Set the env vars enrich_from_netbox() reads.

    The URL must match whatever was used when the cassette was
    recorded.  The token value doesn't matter in replay mode
    (record_mode='none') but must be present.
    """
    monkeypatch.setenv("NETBOX_URL", "http://netbox.lab.local:8000")
    monkeypatch.setenv("NETBOX_TOKEN", "cassette-replay-token")


def _load_expected(device_name):
    path = FIXTURE_DIR / f"{device_name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --- tests ---

@pytest.mark.parametrize("device", DEVICES)
def test_enrich_produces_expected_host_data(device, mock_netbox_env):
    """Replay a recorded NetBox session and verify enriched data
    matches the canned fixture for this device."""
    cassette_path = CASSETTE_DIR / f"{device}.yaml"
    if not cassette_path.exists():
        pytest.skip(f"Cassette {cassette_path.name} not recorded yet -- "
                    "run phase6-cicd/scripts/refresh-netbox-cassettes.py")

    host = _MockHost(device)
    task = _MockTask(host)

    with _make_vcr().use_cassette(str(cassette_path)):
        result = enrich_from_netbox(task)

    assert not result.failed, f"enrich failed: {result.result}"

    expected = _load_expected(device)
    for field in ENRICH_FIELDS:
        assert host[field] == expected[field], (
            f"{device}.{field} mismatch:\n"
            f"  got:      {host[field]!r}\n"
            f"  expected: {expected[field]!r}"
        )


@pytest.mark.parametrize("device", DEVICES)
def test_enrich_result_summary_contains_key_facts(device, mock_netbox_env):
    """The result string should mention router_id, asn, and counts."""
    cassette_path = CASSETTE_DIR / f"{device}.yaml"
    if not cassette_path.exists():
        pytest.skip(f"Cassette {cassette_path.name} not recorded yet")

    host = _MockHost(device)
    task = _MockTask(host)

    with _make_vcr().use_cassette(str(cassette_path)):
        result = enrich_from_netbox(task)

    assert "router_id=" in result.result
    assert "asn=" in result.result
    assert "fabric=" in result.result
