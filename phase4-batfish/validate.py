#!/usr/bin/env python3
"""Phase 4 entry point: offline validation of rendered configs via Batfish.

Pipeline shape:
  1. Take --snapshot DIR (typically phase3-nornir/build/)
  2. Stage the .conf files into a temp dir Batfish expects:
       <staged>/configs/<host>.cfg
  3. Verify the Batfish server is reachable on TCP/9996 (fail fast
     with a clear error before any pybatfish API call)
  4. Connect to the Batfish server. Host comes from $BATFISH_HOST env
     var (set by evpn-lab-env/env.sh) or --bf-host CLI override.
  5. Init the snapshot
  6. Run every check in questions.ALL_CHECKS
  7. Print a per-check report and exit 0/1

Standalone usage:
  source ../../evpn-lab-env/env.sh   # sets BATFISH_HOST
  python phase4-batfish/validate.py --snapshot phase3-nornir/build/

Wired into deploy.py via the --validate flag (Phase 3, opt-in stage).

CI invocation: deploy.py --validate, OR validate.py directly as a
GitHub Actions stage. Both paths use this entry point and read
BATFISH_HOST from env (CI workflow injects it from secrets).
"""

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List

# Silence pybatfish's noisy startup INFO logs unless --debug
logging.getLogger("pybatfish").setLevel(logging.WARNING)

from pybatfish.client.session import Session  # noqa: E402

# Make `import questions` work whether validate.py is run from repo root
# or from inside phase4-batfish/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from questions import ALL_CHECKS, ALL_DIFFS, CheckResult, DiffSummary  # noqa: E402


# Batfish coordinator + worker ports. Standard for batfish/allinone
# image. Not configurable on the server side without rebuilding the
# image, so they're constants here too.
BATFISH_COORDINATOR_PORT = 9996
BATFISH_WORKER_PORT = 9997
DEFAULT_NETWORK = "evpn-lab"


def check_reachable(host: str, port: int = BATFISH_COORDINATOR_PORT, timeout: float = 5.0) -> None:
    """TCP-connect probe. Raises RuntimeError with an actionable
    message if the Batfish coordinator is not reachable. Runs BEFORE
    any pybatfish API call - pybatfish's Session() constructor is
    lazy and doesn't surface a clean error until the first real call,
    which then comes out as a confusing nested traceback. This probe
    fails fast with a clear message instead.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except (socket.timeout, ConnectionRefusedError, socket.gaierror, OSError) as e:
        raise RuntimeError(
            f"Batfish coordinator unreachable at {host}:{port} ({type(e).__name__}: {e}).\n"
            f"\n"
            f"Is the container running?\n"
            f"  ssh root@{host} 'docker compose -f /opt/batfish/docker-compose.yml ps'\n"
            f"\n"
            f"Is BATFISH_HOST set correctly?\n"
            f"  source <repo-root>/../evpn-lab-env/env.sh\n"
            f"  echo $BATFISH_HOST\n"
            f"\n"
            f"See phase4-batfish/README.md for one-time deployment instructions."
        ) from e
    finally:
        sock.close()


def stage_snapshot(src_dir: Path, staged_root: Path) -> Path:
    """Copy *.conf files from `src_dir` into the layout Batfish expects:
    <staged_root>/configs/<host>.cfg

    Batfish auto-detects vendor by file content; the .cfg extension is
    Batfish convention but not strictly required. We rename to .cfg for
    consistency with the docs and to avoid Batfish warning about an
    unknown file format.
    """
    configs_dir = staged_root / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for conf in sorted(src_dir.glob("*.conf")):
        # Skip pre-commit backups and per-stanza files - we only want the
        # full main.j2 renders. The full ones are named <host>.conf with
        # no extra dots in the basename. Per-stanza files have shapes
        # like <host>.routing-options.conf.
        if conf.stem.count(".") > 0:
            continue
        if conf.name.endswith(".pre-commit.conf"):
            continue
        target = configs_dir / f"{conf.stem}.cfg"
        shutil.copy2(conf, target)
        count += 1
    if count == 0:
        raise RuntimeError(
            f"no full-config .conf files found in {src_dir} - did you "
            f"run `python phase3-nornir/deploy.py --full` first?"
        )
    return staged_root


def run_checks(bf: Session) -> List[CheckResult]:
    results = []
    for check_fn in ALL_CHECKS:
        try:
            r = check_fn(bf)
        except Exception as e:
            r = CheckResult(
                name=check_fn.__name__.removeprefix("check_"),
                passed=False,
                summary=f"check raised exception: {type(e).__name__}: {e}",
            )
        results.append(r)
    return results


def run_diffs(bf: Session, ref_name: str, cand_name: str) -> List[DiffSummary]:
    """Run every entry in ALL_DIFFS and collect the results.
    Differential analysis is informational - exceptions become a
    DiffSummary with the error in the summary field rather than
    bubbling up. The deploy never fails on a differential."""
    results = []
    for diff_fn in ALL_DIFFS:
        try:
            d = diff_fn(bf, ref_name, cand_name)
        except Exception as e:
            d = DiffSummary(
                name=diff_fn.__name__.removeprefix("diff_"),
                summary=f"diff raised exception: {type(e).__name__}: {e}",
                added=[],
                removed=[],
            )
        results.append(d)
    return results


def print_diff_report(diffs: List[DiffSummary]) -> None:
    """Human-readable differential summary. Pure information - no
    pass/fail, no exit code influence."""
    print()
    print("=" * 60)
    print(" Batfish differential analysis (candidate vs reference)")
    print("=" * 60)
    for d in diffs:
        print(f"  [DIFF] {d.name:25}  {d.summary}")
        for entry in d.added:
            print(f"    + {entry}")
        for entry in d.removed:
            print(f"    - {entry}")
    print("=" * 60)


def print_report(results: List[CheckResult]) -> bool:
    """Print a human-readable report. Returns True if all passed."""
    print()
    print("=" * 60)
    print(" Batfish validation report")
    print("=" * 60)
    all_passed = True
    for r in results:
        marker = "OK  " if r.passed else "FAIL"
        print(f"  [{marker}] {r.name:25}  {r.summary}")
        if not r.passed and r.detail:
            for line in r.detail.splitlines():
                print(f"           {line}")
        if not r.passed:
            all_passed = False
    print("=" * 60)
    if all_passed:
        print(f" RESULT: PASS ({len(results)} check(s))")
    else:
        failed = sum(1 for r in results if not r.passed)
        print(f" RESULT: FAIL ({failed}/{len(results)} check(s) failed)")
    print("=" * 60)
    return all_passed


def render_json_report(
    results: List[CheckResult],
    diffs: "List[DiffSummary] | None" = None,
) -> str:
    """Machine-readable JSON report. CI consumers (Phase 6 PR-comment
    bot, GitHub Actions summary) parse this format. Stable contract:
    top-level dict with `result` (PASS|FAIL), `passed`/`failed`/`total`
    counts, `checks` (list of {name, passed, summary, detail}), and
    optional `diffs` (list of {name, summary, added, removed}) when
    differential analysis was run. Mirrors the human-readable report's
    data exactly so the two formats can never disagree."""
    passed_count = sum(1 for r in results if r.passed)
    failed_count = len(results) - passed_count
    payload = {
        "result": "PASS" if failed_count == 0 else "FAIL",
        "total": len(results),
        "passed": passed_count,
        "failed": failed_count,
        "checks": [asdict(r) for r in results],
    }
    if diffs is not None:
        payload["diffs"] = [asdict(d) for d in diffs]
    return json.dumps(payload, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Phase 4 - Batfish offline config validation",
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        help="Path to a directory containing rendered <host>.conf files "
             "(typically phase3-nornir/build/)",
    )
    parser.add_argument(
        "--bf-host",
        default=None,
        help="Batfish server hostname/IP (default: $BATFISH_HOST env var). "
             "CLI flag overrides env. Hard-fails if neither is set.",
    )
    parser.add_argument(
        "--network",
        default=DEFAULT_NETWORK,
        help=f"Batfish network name (default: {DEFAULT_NETWORK})",
    )
    parser.add_argument(
        "--snapshot-name",
        default="rendered",
        help="Batfish snapshot name (default: rendered)",
    )
    parser.add_argument(
        "--reference-snapshot",
        default=None,
        help="Path to a REFERENCE snapshot dir to compare the candidate "
             "against (typically phase3-nornir/expected/, the renderer's "
             "golden file). When set, validate.py initializes both "
             "snapshots and runs differential analysis after the regular "
             "checks. Differential output is informational only - exit "
             "code is unaffected.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. text (default) is human-readable; json is "
             "machine-readable for CI consumers (Phase 6 PR-comment bot).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose pybatfish logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger("pybatfish").setLevel(logging.DEBUG)

    src_dir = Path(args.snapshot).resolve()
    if not src_dir.is_dir():
        print(f"ERROR: --snapshot {src_dir} is not a directory", file=sys.stderr)
        sys.exit(2)

    ref_dir = None
    if args.reference_snapshot:
        ref_dir = Path(args.reference_snapshot).resolve()
        if not ref_dir.is_dir():
            print(f"ERROR: --reference-snapshot {ref_dir} is not a directory", file=sys.stderr)
            sys.exit(2)

    # Resolve Batfish host: --bf-host CLI flag wins, otherwise BATFISH_HOST
    # env var, otherwise hard-fail with a clear pointer to env.sh.
    bf_host = args.bf_host or os.environ.get("BATFISH_HOST")
    if not bf_host:
        print(
            "ERROR: BATFISH_HOST env var not set and --bf-host not given.\n"
            "  source <repo-root>/../evpn-lab-env/env.sh\n"
            "or pass --bf-host <ip> on the command line.\n"
            "See phase4-batfish/README.md.",
            file=sys.stderr,
        )
        sys.exit(2)

    diffs: List[DiffSummary] = []
    with tempfile.TemporaryDirectory(prefix="bf-snap-") as staged:
        staged_root = stage_snapshot(src_dir, Path(staged) / "candidate")
        configs = list((staged_root / "configs").iterdir())
        print(f"Staged {len(configs)} candidate config(s) -> {staged_root}")
        for c in sorted(configs):
            print(f"  {c.name}")

        if ref_dir is not None:
            ref_root = stage_snapshot(ref_dir, Path(staged) / "reference")
            ref_configs = list((ref_root / "configs").iterdir())
            print(f"Staged {len(ref_configs)} reference config(s) -> {ref_root}")

        # Reachability probe BEFORE any pybatfish API call. Fails fast
        # with an actionable message if Batfish is down or BATFISH_HOST
        # points at the wrong place.
        print(f"\nProbing Batfish at {bf_host}:{BATFISH_COORDINATOR_PORT}...")
        try:
            check_reachable(bf_host)
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        print("  reachable")

        print(f"Connecting to Batfish at {bf_host}:{BATFISH_COORDINATOR_PORT}...")
        bf = Session(host=bf_host)

        bf.set_network(args.network)
        cand_name = args.snapshot_name
        print(f"Initializing candidate snapshot '{cand_name}' (this can take 30-60s)...")
        bf.init_snapshot(str(staged_root), name=cand_name, overwrite=True)

        results = run_checks(bf)

        if ref_dir is not None:
            ref_name = f"{cand_name}-reference"
            print(f"Initializing reference snapshot '{ref_name}'...")
            bf.init_snapshot(str(ref_root), name=ref_name, overwrite=True)
            print("Running differential analysis...")
            # Re-set the active snapshot to the candidate so any
            # post-diff checks (none today) see the right state.
            bf.set_snapshot(cand_name)
            diffs = run_diffs(bf, ref_name=ref_name, cand_name=cand_name)

    if args.format == "json":
        print(render_json_report(results, diffs if diffs else None))
        all_passed = all(r.passed for r in results)
    else:
        all_passed = print_report(results)
        if diffs:
            print_diff_report(diffs)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
