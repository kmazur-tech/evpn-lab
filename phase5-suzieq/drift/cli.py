"""drift/cli.py - the only module that does I/O orchestration.

Entry point for the drift harness. Wires intent.collect() ->
state.collect() -> diff.compare() -> output. Two output formats:

  --json    machine-readable, consumed by Phase 6 CI stage 11.
            JSON shape is the contract.
  --human   table-formatted for operators running ad-hoc on
            netdevops-srv.

Exit codes (the contract Phase 6 CI relies on):
  0  no drift
  1  drift found (Phase 6 treats this as soft-fail / warn,
     per PROJECT_PLAN.md:200 "Soft fail = warn")
  2  tooling error (NetBox unreachable, parquet path missing,
     etc.). CI should distinguish "drift found" from "harness
     could not run" - the second is a real failure.

Run pattern (lab):
  docker compose run --rm drift python -m drift.cli --json
  docker compose run --rm drift python -m drift.cli --human

The container's docker-compose.yml mounts:
  - /suzieq/parquet (read-only) - the suzieq_parquet docker volume
  - /drift          (read-only) - the drift code, mounted from host
                    so iteration does not need image rebuild
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import List

import pynetbox

from .intent import collect as collect_intent
from .state import collect as collect_state, DEFAULT_PARQUET_DIR
from .diff import compare, Drift, SEVERITY_ERROR, SEVERITY_WARNING


EXIT_OK            = 0
EXIT_DRIFT_FOUND   = 1
EXIT_TOOLING_ERROR = 2


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="drift.cli",
        description="NetBox-vs-Suzieq drift harness (Phase 5 Part B)",
    )
    p.add_argument(
        "--namespace", default=os.environ.get("DRIFT_NAMESPACE", "dc1"),
        help="SuzieQ namespace == NetBox site slug (default: dc1)",
    )
    p.add_argument(
        "--parquet-dir", default=DEFAULT_PARQUET_DIR,
        help=f"Path to suzieq parquet store (default: {DEFAULT_PARQUET_DIR})",
    )
    p.add_argument(
        "--netbox-url", default=os.environ.get("NETBOX_URL"),
        help="NetBox base URL (default: $NETBOX_URL)",
    )
    p.add_argument(
        "--netbox-token", default=os.environ.get("NETBOX_TOKEN"),
        help="NetBox API token (default: $NETBOX_TOKEN)",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_const", const="json", dest="format")
    fmt.add_argument("--human", action="store_const", const="human", dest="format")
    p.set_defaults(format="json")
    return p.parse_args(argv)


def run(args) -> int:
    """Main run loop. Returns the process exit code. Split out from
    main() so tests can drive run() with hand-built args without
    parsing argv."""
    if not args.netbox_url or not args.netbox_token:
        print(
            "ERROR: NETBOX_URL and NETBOX_TOKEN must be set (env or --flags)",
            file=sys.stderr,
        )
        return EXIT_TOOLING_ERROR

    try:
        nb = pynetbox.api(args.netbox_url, token=args.netbox_token)
    except Exception as e:  # pynetbox raises in unpredictable ways
        print(f"ERROR: NetBox connection failed: {e}", file=sys.stderr)
        return EXIT_TOOLING_ERROR

    try:
        intent = collect_intent(nb, args.namespace)
    except Exception as e:
        print(f"ERROR: NetBox intent collection failed: {e}", file=sys.stderr)
        return EXIT_TOOLING_ERROR

    try:
        state = collect_state(args.namespace, args.parquet_dir)
    except Exception as e:
        print(f"ERROR: SuzieQ state read failed: {e}", file=sys.stderr)
        return EXIT_TOOLING_ERROR

    drifts = compare(intent, state)
    emit(drifts, args.namespace, args.format)

    if any(d.severity == SEVERITY_ERROR for d in drifts):
        return EXIT_DRIFT_FOUND
    return EXIT_OK


def emit(drifts: List[Drift], namespace: str, fmt: str) -> None:
    if fmt == "json":
        _emit_json(drifts, namespace)
    else:
        _emit_human(drifts, namespace)


def _emit_json(drifts: List[Drift], namespace: str) -> None:
    payload = {
        "namespace": namespace,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "drift_count": len(drifts),
        "error_count": sum(1 for d in drifts if d.severity == SEVERITY_ERROR),
        "warning_count": sum(1 for d in drifts if d.severity == SEVERITY_WARNING),
        "drifts": [d.to_dict() for d in drifts],
    }
    json.dump(payload, sys.stdout, indent=2, default=_json_default)
    sys.stdout.write("\n")


def _json_default(obj):
    """Coerce numpy/pandas scalars that pop out of state DataFrames
    so json.dump does not raise on them."""
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def _emit_human(drifts: List[Drift], namespace: str) -> None:
    if not drifts:
        print(f"namespace={namespace}: no drift")
        return
    print(f"namespace={namespace}: {len(drifts)} drift(s)")
    print("-" * 80)
    for d in drifts:
        marker = "ERR" if d.severity == SEVERITY_ERROR else "WRN"
        print(f"  [{marker}] {d.dimension:18s} {d.subject}")
        print(f"        {d.detail}")


def main(argv=None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
