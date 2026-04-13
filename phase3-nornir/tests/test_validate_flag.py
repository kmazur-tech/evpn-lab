"""Tests for the Phase 3 deploy.py --validate wiring (Phase 4 hook).

The actual Batfish call lives in phase4-batfish/validate.py and is
exercised by phase4-batfish/tests/. This module pins the deploy.py
SIDE of the wiring: argument parsing, the helper function that
invokes the script, exit-code handling, and the missing-script
warning path.
"""
import sys
from pathlib import Path

import pytest

from deploy import run_batfish_validation, _BATFISH_SCRIPT_MISSING


# ----- run_batfish_validation helper ---------------------------------

class _FakeRunner:
    """Records the args it was called with so tests can assert on
    the command line constructed for validate.py."""
    def __init__(self, return_code: int = 0):
        self.calls = []
        self.return_code = return_code

    def __call__(self, argv):
        self.calls.append(argv)
        return self.return_code


def test_run_batfish_validation_invokes_script_with_correct_args(tmp_path):
    """The helper must build the right argv for validate.py:
    [python, <script>, --snapshot, <build_dir>]"""
    script = tmp_path / "validate.py"
    script.write_text("# fake")
    build = tmp_path / "build"
    build.mkdir()

    runner = _FakeRunner(return_code=0)
    rc = run_batfish_validation(script, build, runner=runner)

    assert rc == 0
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == sys.executable
    assert argv[1] == str(script)
    assert argv[2] == "--snapshot"
    assert argv[3] == str(build)


def test_run_batfish_validation_propagates_success(tmp_path):
    script = tmp_path / "validate.py"
    script.write_text("# fake")
    runner = _FakeRunner(return_code=0)
    rc = run_batfish_validation(script, tmp_path, runner=runner)
    assert rc == 0


def test_run_batfish_validation_propagates_failure(tmp_path):
    """validate.py exit 1 must propagate so deploy.py main() can
    abort the chain. This is the regression class we care about: if
    Batfish reports a failure, deploy.py MUST NOT proceed to NAPALM."""
    script = tmp_path / "validate.py"
    script.write_text("# fake")
    runner = _FakeRunner(return_code=1)
    rc = run_batfish_validation(script, tmp_path, runner=runner)
    assert rc == 1


def test_run_batfish_validation_propagates_arbitrary_exit_code(tmp_path):
    """validate.py uses exit 2 for ABORT cases (snapshot dir
    invalid, BATFISH_HOST not set, etc). The helper must pass any
    exit code through unchanged so deploy.py can preserve the
    distinction in its own ABORT message."""
    script = tmp_path / "validate.py"
    script.write_text("# fake")
    runner = _FakeRunner(return_code=2)
    rc = run_batfish_validation(script, tmp_path, runner=runner)
    assert rc == 2


def test_run_batfish_validation_missing_script_returns_sentinel(tmp_path, capsys):
    """If phase4-batfish/validate.py doesn't exist on disk, the
    helper returns the _BATFISH_SCRIPT_MISSING sentinel and prints
    a WARN. deploy.py main() then skips the validation step rather
    than aborting - the case where someone runs Phase 3 against an
    older repo that doesn't have Phase 4 yet."""
    missing = tmp_path / "does-not-exist" / "validate.py"
    runner = _FakeRunner(return_code=999)
    rc = run_batfish_validation(missing, tmp_path, runner=runner)

    assert rc == _BATFISH_SCRIPT_MISSING
    # Runner must NOT have been called when the script doesn't exist
    assert runner.calls == []
    captured = capsys.readouterr()
    assert "WARN" in captured.out
    assert str(missing) in captured.out


def test_run_batfish_validation_default_runner_is_subprocess_call():
    """When runner is None (the default), the helper falls back to
    subprocess.call. This is the production path. We don't actually
    invoke it (would require a real validate.py) - we just confirm
    that passing runner=None doesn't error and the helper resolves
    the default lazily."""
    # Use a non-existent script so we exit on the missing-script
    # branch BEFORE the runner is needed. This proves the default
    # is lazy and doesn't blow up at import time.
    missing = Path("/tmp/this-script-does-not-exist-xyz.py")
    rc = run_batfish_validation(missing, Path("/tmp"))  # runner=None
    assert rc == _BATFISH_SCRIPT_MISSING


def test_batfish_script_missing_sentinel_is_negative():
    """Sentinel must NOT collide with any real subprocess exit code.
    POSIX exit codes are 0-255; negative is safe."""
    assert _BATFISH_SCRIPT_MISSING < 0
