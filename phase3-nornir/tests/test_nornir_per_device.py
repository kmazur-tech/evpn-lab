"""Per-device Nornir pipeline tests.

Exercises the Nornir task execution path (template_file -> deploy
guard) per device, using an in-memory inventory populated from the
golden-file fixtures.  Each device appears as a separate test row
in CI output.

Unlike test_render_golden.py (which calls Jinja2 directly), these
tests go through Nornir's task runner and template_file plugin,
validating the actual execution path deploy.py uses.
"""

import json
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from nornir.core import Nornir
from nornir.core.inventory import Defaults, Groups, Host, Hosts, Inventory
from nornir.core.plugins.runners import RunnersPluginRegister
from nornir.core.task import Result, Task
from nornir.plugins.runners import SerialRunner
from nornir_jinja2.plugins.tasks import template_file

from deploy import normalize, assert_safe_to_deploy

PHASE3 = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "render"
EXPECTED_DIR = PHASE3 / "expected"
TEMPLATE_DIR = PHASE3 / "templates" / "junos"
DEFAULTS_FILE = PHASE3 / "vars" / "junos_defaults.yml"

TEST_HASH = (
    "$6$evpnlab1$x/0MmAitK3rDmZWPb.mNqW4YglzhbN5D0g0aGR"
    "toWAaSUUMM1Om/FGfcPT3nmCP26uu2srtayTb46F1Id6Z/x."
)

DEVICES = ["dc1-spine1", "dc1-spine2", "dc1-leaf1", "dc1-leaf2"]


def _load_fixture(name):
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def defaults():
    return yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nornir_instance():
    """Nornir with in-memory inventory, one host per device."""
    inv_hosts = {}
    for device in DEVICES:
        fixture = _load_fixture(device)
        h = Host(name=device)
        for key, value in fixture.items():
            h[key] = value
        inv_hosts[device] = h

    inventory = Inventory(
        hosts=Hosts(inv_hosts),
        groups=Groups({}),
        defaults=Defaults(),
    )

    RunnersPluginRegister.register("serial", SerialRunner)
    return Nornir(inventory=inventory, runner=SerialRunner())


def _make_jinja_env():
    """Match deploy.py's Jinja2 environment settings."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )


def _render_task(task: Task, defaults, junos_root_hash, junos_admin_hash, jinja_env):
    """Nornir task that renders main.j2 for one host."""
    result = task.run(
        task=template_file,
        template="main.j2",
        path=str(TEMPLATE_DIR),
        jinja_env=jinja_env,
        defaults=defaults,
        junos_root_hash=junos_root_hash,
        junos_admin_hash=junos_admin_hash,
    )
    return Result(host=task.host, result=result[0].result)


@pytest.mark.parametrize("device", DEVICES)
def test_nornir_render_matches_golden(device, nornir_instance, defaults):
    """Run the render pipeline through Nornir and compare to golden file."""
    jinja_env = _make_jinja_env()
    nr = nornir_instance.filter(name=device)
    render_result = nr.run(
        task=_render_task,
        defaults=defaults,
        junos_root_hash=TEST_HASH,
        junos_admin_hash=TEST_HASH,
        jinja_env=jinja_env,
    )

    assert device in render_result
    host_result = render_result[device]
    assert not host_result.failed, f"Nornir task failed for {device}: {host_result.exception}"

    rendered = host_result[0].result
    expected = (EXPECTED_DIR / f"{device}.conf").read_text(encoding="utf-8")

    assert normalize(rendered).strip() == normalize(expected).strip(), (
        f"Nornir-rendered output for {device} does not match golden file"
    )


@pytest.mark.parametrize("device", DEVICES)
def test_nornir_render_passes_deploy_guard(device, nornir_instance, defaults):
    """Rendered output through Nornir must pass the deploy guard."""
    jinja_env = _make_jinja_env()
    nr = nornir_instance.filter(name=device)
    render_result = nr.run(
        task=_render_task,
        defaults=defaults,
        junos_root_hash=TEST_HASH,
        junos_admin_hash=TEST_HASH,
        jinja_env=jinja_env,
    )

    rendered = render_result[device][0].result
    # Should not raise
    assert_safe_to_deploy(rendered, device)
