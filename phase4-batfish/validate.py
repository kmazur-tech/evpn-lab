#!/usr/bin/env python3
"""Phase 4 entry point: offline validation of rendered configs via Batfish.

Pipeline shape:
  1. Take --snapshot DIR (typically phase3-nornir/build/)
  2. Stage the .conf files into a temp dir Batfish expects:
       <staged>/configs/<host>.cfg
  3. Connect to the Batfish server (default: netdevops-srv.lab.local:9996, the
     netdevops-srv container - override with --bf-host)
  4. Init the snapshot
  5. Run every check in questions.ALL_CHECKS
  6. Print a per-check report and exit 0/1

Standalone usage:
  python phase4-batfish/validate.py --snapshot phase3-nornir/build/

Wired into deploy.py via the --validate flag (Phase 3, opt-in stage).

CI invocation: deploy.py --validate, OR validate.py directly as a
GitHub Actions stage. Both paths use this entry point.
"""

import argparse
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List

# Silence pybatfish's noisy startup INFO logs unless --debug
logging.getLogger("pybatfish").setLevel(logging.WARNING)

from pybatfish.client.session import Session  # noqa: E402

# Make `import questions` work whether validate.py is run from repo root
# or from inside phase4-batfish/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from questions import ALL_CHECKS, CheckResult  # noqa: E402


DEFAULT_BF_HOST = "netdevops-srv.lab.local"
DEFAULT_NETWORK = "evpn-lab"


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
        default=DEFAULT_BF_HOST,
        help=f"Batfish server hostname/IP (default: {DEFAULT_BF_HOST})",
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

    with tempfile.TemporaryDirectory(prefix="bf-snap-") as staged:
        staged_root = stage_snapshot(src_dir, Path(staged))
        configs = list((staged_root / "configs").iterdir())
        print(f"Staged {len(configs)} config(s) -> {staged_root}")
        for c in sorted(configs):
            print(f"  {c.name}")

        print(f"\nConnecting to Batfish at {args.bf_host}:9996...")
        try:
            bf = Session(host=args.bf_host)
        except Exception as e:
            print(f"ERROR: cannot reach Batfish server: {e}", file=sys.stderr)
            print(
                f"\nIs the container running on {args.bf_host}?\n"
                f"  ssh root@{args.bf_host} 'docker compose -f /opt/batfish/docker-compose.yml ps'\n"
                f"See phase4-batfish/README.md for setup instructions.",
                file=sys.stderr,
            )
            sys.exit(2)

        bf.set_network(args.network)
        print(f"Initializing snapshot '{args.snapshot_name}' (this can take 30-60s)...")
        bf.init_snapshot(str(staged_root), name=args.snapshot_name, overwrite=True)

        results = run_checks(bf)

    all_passed = print_report(results)
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
