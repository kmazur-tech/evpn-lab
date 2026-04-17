"""Unit tests for suzieq-image/add-junos-vjunos-switch.py.

The patcher script runs once at `docker compose build` time inside
the suzieq child image, mutating upstream service yamls in place
to add a `junos-vjunos-switch:` devtype block. We test it by writing
small fixture yamls into a tmp_path and asserting the post-patch
content is correct.

The script is hyphenated and lives in suzieq-image/ (not in a
python package), so we load it via importlib - same pattern as
the gen-inventory.py loader in tests/helpers.py.

What this test guards:
  - The patcher actually adds the junos-vjunos-switch entry
  - lldp.yml gets the explicit detail-view block (not the simple
    copy: junos-mx shim) - this is the load-bearing fix for the
    drift harness
  - All other services get `junos-vjunos-switch: {copy: junos-mx}`
  - The patcher is IDEMPOTENT (re-running on an already-patched
    file is a no-op, not a duplicate append)
  - Files without a `junos-mx:` block are skipped, not corrupted
  - The original upstream content survives (we never mutate
    junos-mx, junos-qfx, etc. - junos-vjunos-switch is purely additive)
"""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


PHASE5 = Path(__file__).resolve().parent.parent


def _load_patcher():
    """Load suzieq-image/add-junos-vjunos-switch.py via importlib.

    The script is hyphenated (operator-friendly bare filename) so
    a regular `import` does not work. Same trick as the
    gen-inventory.py loader in helpers.py."""
    spec = importlib.util.spec_from_file_location(
        "add_vjunos_switch",
        PHASE5 / "suzieq-image" / "add-vjunos-switch.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


patcher = _load_patcher()


# ---------------------------------------------------------------------------
# Fixture content - tiny believable upstream yaml shapes
# ---------------------------------------------------------------------------

UPSTREAM_BGP_YML = """\
service: bgp
keys:
  - peer
apply:
  cumulus:
    version: all
    command: net show bgp summary json
  eos:
    version: all
    command: show ip bgp summary
  junos-qfx:
    version: all
    command: show bgp summary | display json
  junos-ex:
    copy: junos-qfx
  junos-mx:
    copy: junos-qfx
  junos-es:
    copy: junos-mx
"""

# Real-world shape from upstream devconfig.yml: junos-mx is itself
# a copy: junos-qfx (because devconfig is the same command across
# all Junos variants).
UPSTREAM_DEVCONFIG_YML = """\
service: devconfig
apply:
  junos-qfx:
    version: all
    command: show configuration | except SECRET-DATA
    textfsm:
  junos-mx:
    copy: junos-qfx
  junos-es:
    copy: junos-qfx
"""

# device.yml is the lone service where junos-mx has its OWN real
# definition that vJunos needs (the single-RE uptime parser).
# This is the only service in SERVICE_BASE_OVERRIDES.
UPSTREAM_DEVICE_YML = """\
service: device
apply:
  junos-qfx:
    version: all
    command:
      - command: "show system uptime | display json"
        normalize: 'multi-routing-engine-results/[0]/multi-routing-engine-item/[0]/system-uptime-information/*/[
        "data: bootupTimestamp?|"
        ]'
  junos-ex:
    copy: junos-qfx
  junos-mx:
    version: all
    command:
      - command: "show system uptime | display json"
        normalize: 'system-uptime-information/*/[
        "data: bootupTimestamp?|"
        ]'
  junos-es:
    copy: junos-mx
"""

# evpnVni.yml shape: junos-mx is COMPLETELY ABSENT upstream. The
# patcher must still produce a valid junos-vjunos-switch entry by
# falling back to junos-qfx (the new default base, which is real).
UPSTREAM_EVPNVNI_YML = """\
service: evpnVni
apply:
  cumulus:
    version: all
    command: vtysh -c "show evpn vni detail json"
  junos-qfx:
    version: all
    command: show evpn instance extensive | display json
  junos-ex:
    copy: junos-qfx
  junos-es:
    copy: junos-qfx
  junos-qfx10k:
    copy: junos-qfx
  junos-evo:
    copy: junos-qfx
"""

UPSTREAM_LLDP_YML = """\
service: lldp
keys:
  - ifname
apply:
  cumulus:
    version: all
    command: net show lldp json
  junos-qfx:
    - version: all
      command: show lldp neighbors detail | display json | no-more
      normalize: 'lldp-neighbors-information/[0]/lldp-neighbor-information/*/[
      "lldp-local-port-id/[0]/data: ifname?|",
      "lldp-remote-port-id/[0]/data: peerIfname?|",
      ]'
  junos-mx:
    version: all
    command: show lldp neighbors | display json | no-more
    normalize: 'lldp-neighbors-information/[0]/lldp-neighbor-information/*/[
    "lldp-local-port-id/[0]/data: ifname?|",
    "lldp-remote-system-name/[0]/data: peerHostname",
    ]'
"""

UPSTREAM_NO_JUNOS_YML = """\
service: opensauce
apply:
  cumulus:
    version: all
    command: cat /proc/net/dev
"""


# ---------------------------------------------------------------------------
# Simple-copy services (the 12 of 13)
# ---------------------------------------------------------------------------

class TestResolveBaseDevtype:
    def test_real_definition_resolves_to_self(self):
        """A devtype with command/normalize is its own base."""
        yml = """\
apply:
  junos-mx:
    version: all
    command: show bgp
    normalize: x
"""
        assert patcher.resolve_base_devtype(yml, start="junos-mx") == "junos-mx"

    def test_one_level_chain(self):
        """junos-mx -> junos-qfx (the devconfig.yml shape)"""
        yml = """\
apply:
  junos-qfx:
    version: all
    command: show config
    textfsm:
  junos-mx:
    copy: junos-qfx
"""
        assert patcher.resolve_base_devtype(yml, start="junos-mx") == "junos-qfx"

    def test_two_level_chain(self):
        """Hypothetical deeper chain - the walker handles arbitrary
        depth, not just one level. Defends against future upstream
        edits that introduce a longer chain."""
        yml = """\
apply:
  base:
    command: real
    normalize: x
  junos-qfx:
    copy: base
  junos-mx:
    copy: junos-qfx
"""
        assert patcher.resolve_base_devtype(yml, start="junos-mx") == "base"

    def test_missing_devtype_returns_start(self):
        """If junos-mx isn't in this file at all, return the start
        (caller is responsible for skipping the file)."""
        yml = """\
apply:
  cumulus:
    command: x
    normalize: y
"""
        assert patcher.resolve_base_devtype(yml, start="junos-mx") == "junos-mx"

    def test_cycle_detection(self):
        """Pathological - a copy cycle would be a SuzieQ bug
        upstream, but our walker must not infinite-loop on it."""
        yml = """\
apply:
  a:
    copy: b
  b:
    copy: a
"""
        # Should return something reasonable (the start) without hanging
        result = patcher.resolve_base_devtype(yml, start="a")
        assert result in ("a", "b")  # whichever the walker stops at


class TestSimpleCopyPatch:
    def test_default_base_is_junos_qfx(self, tmp_path):
        """Default base for the simple-copy patch is junos-qfx
        (the canonical Junos base in upstream - REAL in every
        Junos service yaml). The earlier patcher version defaulted
        to junos-mx with a chain resolver, but the user pushed back
        pointing out that junos-qfx is semantically closer for
        vJunos-switch (which IS architecturally a switch). The
        live verification confirmed: routes table went from 26
        connected-only rows to 86 rows including bgp/evpn/vpn
        protocols once routes was switched to junos-qfx."""
        bgp = tmp_path / "bgp.yml"
        bgp.write_text(UPSTREAM_BGP_YML)
        patcher.patch_simple_copy_yaml(bgp)

        result = yaml.safe_load(bgp.read_text())
        assert "junos-vjunos-switch" in result["apply"]
        assert result["apply"]["junos-vjunos-switch"] == {"copy": "junos-qfx"}

    def test_device_yml_overrides_to_junos_mx(self, tmp_path):
        """REGRESSION GUARD for the lone SERVICE_BASE_OVERRIDES
        entry. device.yml is the only service where junos-mx is
        the right base for vJunos-switch (single-RE uptime
        parser). Don't change this without first verifying that
        the upstream junos-qfx device template no longer requires
        the multi-RE wrapper AND that vJunos has changed its
        JSON shape - both are extremely unlikely."""
        device = tmp_path / "device.yml"
        device.write_text(UPSTREAM_DEVICE_YML)
        patcher.patch_simple_copy_yaml(device)

        result = yaml.safe_load(device.read_text())
        assert result["apply"]["junos-vjunos-switch"] == {"copy": "junos-mx"}

    def test_evpnvni_with_no_junos_mx_block_still_patches(self, tmp_path):
        """REGRESSION GUARD. evpnVni.yml has NO junos-mx block at
        all upstream - only junos-qfx (and copies of it). With
        the earlier patcher (default=junos-mx) the file got
        SKIPped because there was no junos-mx text to copy from,
        leaving suzieq with no evpnVni collector for vJunos-switch
        and the table empty. With default=junos-qfx the patch
        produces a valid junos-vjunos-switch entry from the only
        base that exists in the file. Verified live: after the
        fix, vJunos collects 6 evpnVni rows."""
        evpnvni = tmp_path / "evpnVni.yml"
        evpnvni.write_text(UPSTREAM_EVPNVNI_YML)
        patcher.patch_simple_copy_yaml(evpnvni)

        result = yaml.safe_load(evpnvni.read_text())
        assert "junos-vjunos-switch" in result["apply"]
        assert result["apply"]["junos-vjunos-switch"] == {"copy": "junos-qfx"}

    def test_idempotent_on_already_patched_file(self, tmp_path):
        bgp = tmp_path / "bgp.yml"
        bgp.write_text(UPSTREAM_BGP_YML)
        patcher.patch_simple_copy_yaml(bgp)
        first = bgp.read_text()

        # Re-running must be a no-op (no duplicate append)
        patcher.patch_simple_copy_yaml(bgp)
        second = bgp.read_text()
        assert first == second

    def test_skips_yaml_without_any_junos_block(self, tmp_path):
        """Services like mlag.yml / time.yml / topcpu.yml have no
        Junos block at all - skipped silently as a normal outcome."""
        weird = tmp_path / "weird.yml"
        weird.write_text(UPSTREAM_NO_JUNOS_YML)
        patcher.patch_simple_copy_yaml(weird)
        # File unchanged
        assert weird.read_text() == UPSTREAM_NO_JUNOS_YML

    def test_preserves_upstream_devtype_blocks(self, tmp_path):
        """REGRESSION GUARD. The patcher MUST NOT mutate junos-mx,
        junos-qfx, junos-ex, etc. - junos-vjunos-switch is purely
        additive. Pollution of the built-in devtype namespace is
        the bug class this whole approach exists to avoid."""
        bgp = tmp_path / "bgp.yml"
        original = yaml.safe_load(UPSTREAM_BGP_YML)
        bgp.write_text(UPSTREAM_BGP_YML)
        patcher.patch_simple_copy_yaml(bgp)

        result = yaml.safe_load(bgp.read_text())
        # Every stock devtype block must be byte-identical to
        # upstream. Iterate the original, assert each survived.
        for devtype in original["apply"]:
            assert result["apply"][devtype] == original["apply"][devtype], \
                f"upstream {devtype!r} block was mutated by the patcher"
        # And junos-vjunos-switch is the only NEW key
        added = set(result["apply"]) - set(original["apply"])
        assert added == {"junos-vjunos-switch"}


# ---------------------------------------------------------------------------
# lldp.yml special-case (the load-bearing patch)
# ---------------------------------------------------------------------------

class TestLldpPatch:
    def test_appends_explicit_detail_view_block(self, tmp_path):
        lldp = tmp_path / "lldp.yml"
        lldp.write_text(UPSTREAM_LLDP_YML)
        patcher.patch_lldp_yaml(lldp)

        result = yaml.safe_load(lldp.read_text())
        vjunos = result["apply"]["junos-vjunos-switch"]
        # Must use the detail view (the whole point of the patch)
        assert "detail" in vjunos["command"]
        # Must NOT be a copy: shim
        assert "copy" not in vjunos
        # Must extract peerIfname from lldp-remote-port-id
        assert "lldp-remote-port-id" in vjunos["normalize"]
        assert "peerIfname" in vjunos["normalize"]

    def test_lldp_patch_does_not_mutate_junos_mx_block(self, tmp_path):
        """The lldp patch is what we are here for. CRITICAL that
        upstream junos-mx stays exactly as it was - we add a NEW
        block, never edit the existing one. This is the difference
        between approach B (junos-vjunos-switch overlay) and approach A
        (the rejected in-place mutation of junos-mx)."""
        lldp = tmp_path / "lldp.yml"
        lldp.write_text(UPSTREAM_LLDP_YML)
        patcher.patch_lldp_yaml(lldp)

        result = yaml.safe_load(lldp.read_text())
        upstream_junos_mx = result["apply"]["junos-mx"]
        # The summary view command is unchanged - we did not touch it
        assert "detail" not in upstream_junos_mx["command"]
        assert upstream_junos_mx["command"] == "show lldp neighbors | display json | no-more"

    def test_lldp_patch_idempotent(self, tmp_path):
        lldp = tmp_path / "lldp.yml"
        lldp.write_text(UPSTREAM_LLDP_YML)
        patcher.patch_lldp_yaml(lldp)
        first = lldp.read_text()
        patcher.patch_lldp_yaml(lldp)
        assert lldp.read_text() == first

    def test_lldp_patch_extracts_port_description(self, tmp_path):
        """Bonus: the patch also extracts lldp-remote-port-description
        so the description field carries the operator-set port label
        (e.g. 'to dc1-spine1') instead of only the system description.
        Same shape upstream junos-qfx already uses."""
        lldp = tmp_path / "lldp.yml"
        lldp.write_text(UPSTREAM_LLDP_YML)
        patcher.patch_lldp_yaml(lldp)
        text = lldp.read_text()
        assert "lldp-remote-port-description" in text


# ---------------------------------------------------------------------------
# main() - the orchestrator
# ---------------------------------------------------------------------------

class TestPatchNodeMultiReList:
    """The third validation gate: poller/worker/nodes/node.py has
    a hardcoded list of devtypes that return single-routing-engine
    JSON shape from `show system uptime | display json`. Devtypes
    NOT in the list have a `multi-routing-engine-results` wrapper
    extraction applied. vJunos returns single-RE shape, so we must
    add junos-vjunos-switch to the list or the JunosNode parser
    crashes on every poll cycle.

    Discovered during Phase 5 Part B vjunos-switch bring-up: the
    yaml patches and known_devtypes() patch made the inventory
    load and devtype validation pass, but every node still failed
    at the bootupTimestamp parse step because of THIS hardcoded
    Python list."""

    UPSTREAM_NODE_PY = '''\
class JunosNode(Node):
    async def _parse_init_dev_data_devtype(self, output, cb_token) -> None:
        """Parse the uptime command output"""
        if output[0]["status"] == 0:
            data = output[0]["data"]
            try:
                jdata = json.loads(data.replace('\\n', '').strip())
                if self.devtype not in ["junos-mx", "junos-qfx10k",
                                        "junos-evo"]:
                    jdata = (jdata['multi-routing-engine-results'][0]
                             ['multi-routing-engine-item'][0])
'''

    def test_adds_junos_vjunos_switch_to_list(self, tmp_path):
        node = tmp_path / "node.py"
        node.write_text(self.UPSTREAM_NODE_PY)
        patcher.patch_node_multi_re_list(node)
        text = node.read_text()
        assert "'junos-vjunos-switch'" in text or '"junos-vjunos-switch"' in text
        # Original entries must still be there
        assert '"junos-mx"' in text
        assert '"junos-qfx10k"' in text
        assert '"junos-evo"' in text

    def test_idempotent_when_already_patched(self, tmp_path):
        node = tmp_path / "node.py"
        node.write_text(self.UPSTREAM_NODE_PY)
        patcher.patch_node_multi_re_list(node)
        first = node.read_text()
        patcher.patch_node_multi_re_list(node)
        assert node.read_text() == first

    def test_fails_loudly_when_marker_missing(self, tmp_path):
        """If upstream restructures the multi-RE check, the build
        must FAIL LOUDLY rather than silently produce a broken
        image where every Junos device fails to parse uptime."""
        node = tmp_path / "node.py"
        node.write_text("class JunosNode: pass\n")
        with pytest.raises(SystemExit, match="could not find"):
            patcher.patch_node_multi_re_list(node)

    def test_fails_loudly_when_node_py_missing(self, tmp_path):
        with pytest.raises(SystemExit, match="not found"):
            patcher.patch_node_multi_re_list(tmp_path / "nonexistent.py")


class TestPatchKnownDevtypes:
    """The second validation gate: SuzieQ has a hardcoded
    known_devtypes() allowlist in shared/utils.py. Without
    patching it, every node init fails with `An unknown devtype
    junos-vjunos-switch is being added` even though the service yamls
    are correct. This was discovered during Phase 5 Part B
    bring-up - it's the second of two validation points.

    The patcher does a literal-string substitution against the
    closing `'panos'])` of the return value. Tests verify the
    substitution lands correctly, is idempotent, and fails
    loudly if upstream restructures the function."""

    UPSTREAM_UTILS_PY = """\
def known_devtypes() -> list:
    \"\"\"Returns the list of known dev types\"\"\"
    return (['cumulus', 'eos', 'iosxe', 'iosxr', 'ios', 'junos-mx',
             'junos-qfx', 'junos-qfx10k', 'junos-ex', 'junos-es', 'junos-evo',
             'linux', 'nxos', 'sonic', 'panos'])
"""

    def test_adds_vjunos_switch_to_list(self, tmp_path):
        utils = tmp_path / "utils.py"
        utils.write_text(self.UPSTREAM_UTILS_PY)
        patcher.patch_known_devtypes(utils)

        # The patched file must list junos-vjunos-switch as a known type
        ns = {}
        exec(utils.read_text(), ns)
        result = ns["known_devtypes"]()
        assert "junos-vjunos-switch" in result
        # And ALL the original devtypes must still be there
        for original in ["cumulus", "eos", "junos-mx", "junos-qfx",
                         "junos-ex", "linux", "nxos", "panos"]:
            assert original in result

    def test_idempotent_when_already_patched(self, tmp_path):
        utils = tmp_path / "utils.py"
        utils.write_text(self.UPSTREAM_UTILS_PY)
        patcher.patch_known_devtypes(utils)
        first = utils.read_text()
        patcher.patch_known_devtypes(utils)
        assert utils.read_text() == first

    def test_fails_loudly_when_panos_marker_missing(self, tmp_path):
        """If upstream restructures known_devtypes() in a way that
        removes our `'panos'])` substitution target, the build
        must FAIL LOUDLY rather than silently produce an
        unpatched image."""
        utils = tmp_path / "utils.py"
        utils.write_text("def known_devtypes(): return ['linux']\n")
        with pytest.raises(SystemExit, match="could not find"):
            patcher.patch_known_devtypes(utils)

    def test_fails_loudly_when_utils_py_missing(self, tmp_path):
        with pytest.raises(SystemExit, match="not found"):
            patcher.patch_known_devtypes(tmp_path / "nonexistent.py")


class TestMainOrchestrator:
    def test_patches_lldp_and_simple_services_in_one_pass(self, tmp_path):
        (tmp_path / "lldp.yml").write_text(UPSTREAM_LLDP_YML)
        (tmp_path / "bgp.yml").write_text(UPSTREAM_BGP_YML)
        (tmp_path / "device.yml").write_text(UPSTREAM_BGP_YML.replace("bgp", "device"))

        patcher.main(tmp_path)

        # All three files have a junos-vjunos-switch entry
        for fname in ("lldp.yml", "bgp.yml", "device.yml"):
            result = yaml.safe_load((tmp_path / fname).read_text())
            assert "junos-vjunos-switch" in result["apply"], f"{fname} missing junos-vjunos-switch"

        # And the lldp one specifically uses the detail view
        lldp_result = yaml.safe_load((tmp_path / "lldp.yml").read_text())
        assert "detail" in lldp_result["apply"]["junos-vjunos-switch"]["command"]

    def test_main_fails_loudly_when_lldp_yml_missing(self, tmp_path):
        """The lldp patch is the load-bearing one. If the upstream
        image ever restructures its config layout in a way that
        removes lldp.yml, we want a HARD BUILD FAILURE rather than
        a silently broken image. The script's `sys.exit` enforces
        this."""
        # Note: bgp.yml exists but lldp.yml does not
        (tmp_path / "bgp.yml").write_text(UPSTREAM_BGP_YML)
        with pytest.raises(SystemExit, match="lldp.yml"):
            patcher.main(tmp_path)

    def test_main_fails_loudly_when_config_dir_missing(self, tmp_path):
        with pytest.raises(SystemExit, match="does not exist"):
            patcher.main(tmp_path / "nonexistent")
