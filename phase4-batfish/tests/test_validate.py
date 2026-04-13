"""Unit tests for validate.py - the parts that don't need Batfish.

stage_snapshot() and check_reachable() are pure-function-ish (file IO
+ socket) and easy to test in isolation. The Batfish-driving parts of
main() need a real Batfish container and live in the integration test
that runs against the real fabric, not here.
"""
import socket
import threading
from pathlib import Path

import pytest

from validate import stage_snapshot, check_reachable, BATFISH_COORDINATOR_PORT


# ----- stage_snapshot ------------------------------------------------

def _write(path: Path, content: str = "system { host-name x; }\n") -> Path:
    path.write_text(content)
    return path


def test_stage_snapshot_copies_full_configs(tmp_path):
    src = tmp_path / "build"
    src.mkdir()
    _write(src / "dc1-spine1.conf")
    _write(src / "dc1-spine2.conf")
    _write(src / "dc1-leaf1.conf")
    _write(src / "dc1-leaf2.conf")

    staged = tmp_path / "staged"
    stage_snapshot(src, staged)

    cfgs = sorted(p.name for p in (staged / "configs").iterdir())
    assert cfgs == ["dc1-leaf1.cfg", "dc1-leaf2.cfg", "dc1-spine1.cfg", "dc1-spine2.cfg"]


def test_stage_snapshot_excludes_pre_commit_backups(tmp_path):
    """build/<host>.pre-commit.conf are pre-deploy backups from
    Phase 3, NOT something we want to validate. They have stems
    like 'dc1-spine1.pre-commit' which the dot-in-stem filter
    catches AND the .pre-commit.conf suffix check catches."""
    src = tmp_path / "build"
    src.mkdir()
    _write(src / "dc1-spine1.conf")
    _write(src / "dc1-spine1.pre-commit.conf", "previous running config\n")

    staged = tmp_path / "staged"
    stage_snapshot(src, staged)

    cfgs = sorted(p.name for p in (staged / "configs").iterdir())
    assert cfgs == ["dc1-spine1.cfg"]
    # Pre-commit backup must NOT be staged
    assert not (staged / "configs" / "dc1-spine1.pre-commit.cfg").exists()


def test_stage_snapshot_excludes_per_stanza_files(tmp_path):
    """deploy.py --check writes per-stanza files like
    dc1-spine1.routing-options.conf. These are partial configs
    used for the regression gate, not full devices, and Batfish
    cannot parse them as a complete device. Filter them out."""
    src = tmp_path / "build"
    src.mkdir()
    _write(src / "dc1-spine1.conf")
    _write(src / "dc1-spine1.routing-options.conf")
    _write(src / "dc1-spine1.chassis.conf")
    _write(src / "dc1-spine1.protocols.conf")

    staged = tmp_path / "staged"
    stage_snapshot(src, staged)

    cfgs = sorted(p.name for p in (staged / "configs").iterdir())
    assert cfgs == ["dc1-spine1.cfg"]


def test_stage_snapshot_empty_dir_raises(tmp_path):
    """No full-config .conf files in the snapshot dir is a hard
    error - the operator probably forgot to run deploy.py --full
    first, and we want to fail loud, not silently upload an empty
    snapshot to Batfish."""
    src = tmp_path / "build"
    src.mkdir()
    # Only stale per-stanza file, no full config
    _write(src / "dc1-spine1.routing-options.conf")

    staged = tmp_path / "staged"
    with pytest.raises(RuntimeError, match="no full-config .conf files"):
        stage_snapshot(src, staged)


def test_stage_snapshot_renames_to_cfg(tmp_path):
    """Batfish convention: configs/<host>.cfg, not .conf. The .cfg
    extension helps Batfish auto-detect file format (Junos vs IOS
    vs EOS). Pinned because if someone changes the rename, Batfish
    will fall back to plain text and emit a 'unknown file format'
    warning that's confusing to debug."""
    src = tmp_path / "build"
    src.mkdir()
    _write(src / "dc1-spine1.conf")

    staged = tmp_path / "staged"
    stage_snapshot(src, staged)

    assert (staged / "configs" / "dc1-spine1.cfg").exists()
    assert not (staged / "configs" / "dc1-spine1.conf").exists()


def test_stage_snapshot_creates_configs_subdir(tmp_path):
    """Batfish expects a `configs/` subdir under the snapshot root.
    Without it, init_snapshot fails with a confusing error. Pin the
    directory layout."""
    src = tmp_path / "build"
    src.mkdir()
    _write(src / "dc1-spine1.conf")

    staged = tmp_path / "staged"
    stage_snapshot(src, staged)

    assert (staged / "configs").is_dir()


# ----- check_reachable -----------------------------------------------

@pytest.fixture
def listening_port():
    """Spin up a local TCP listener on an ephemeral port for the
    duration of one test, return its port number, then tear down."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    accept_thread = threading.Thread(
        target=lambda: (sock.accept(), None) if False else None,  # we don't actually accept
        daemon=True,
    )
    accept_thread.start()

    yield port
    sock.close()


def test_check_reachable_passes_when_port_open(listening_port):
    # Should NOT raise
    check_reachable("127.0.0.1", port=listening_port, timeout=2.0)


def test_check_reachable_raises_on_refused():
    """An ephemeral port no one is listening on -> ConnectionRefusedError
    -> caught and re-raised as RuntimeError with the actionable message."""
    with pytest.raises(RuntimeError, match="Batfish coordinator unreachable"):
        # Port 1 on localhost is essentially guaranteed unused
        check_reachable("127.0.0.1", port=1, timeout=1.0)


def test_check_reachable_raises_on_dns_failure():
    """Bogus hostname -> socket.gaierror -> caught and re-raised."""
    with pytest.raises(RuntimeError, match="Batfish coordinator unreachable"):
        check_reachable("this-host-does-not-exist.invalid", port=9996, timeout=2.0)


def test_check_reachable_error_message_includes_actionable_steps():
    """The error message must tell the operator what to do, not just
    'connection refused'. Pinned so future refactors don't downgrade
    the error to a one-liner."""
    try:
        check_reachable("127.0.0.1", port=1, timeout=1.0)
    except RuntimeError as e:
        msg = str(e)
        assert "docker compose" in msg
        assert "BATFISH_HOST" in msg
        assert "evpn-lab-env" in msg
        assert "phase4-batfish/README.md" in msg
    else:
        pytest.fail("expected RuntimeError")


def test_check_reachable_default_port_is_coordinator():
    """The default port argument must be the Batfish coordinator
    (9996), NOT the worker (9997). Pin the constant binding."""
    assert BATFISH_COORDINATOR_PORT == 9996
