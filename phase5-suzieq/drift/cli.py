"""drift/cli.py - the only module that does I/O orchestration.

Entry point for the drift harness. Wires intent.collect() ->
state.collect() -> diff.compare() -> output. Two output formats:

  --json    machine-readable, consumed by Phase 6 CI stage 11.
            JSON shape is the contract.
  --human   table-formatted for operators running ad-hoc on
            netdevops-srv.

Exit codes (the contract Phase 6 CI relies on):
  0  no error-severity drift (warning-severity drifts allowed; or
     for --mode timeseries: harness ran cleanly, timeseries results
     are observations not pass/fail and never produce a non-zero
     exit on their own)
  1  one or more error-severity drifts / assertion failures found.
     Phase 6 deploy workflow hard-fails on this (drift-check job
     uses retry-with-backoff; persistent exit 1 triggers
     rollback-on-failure). The earlier "soft-fail / warn" plan was
     promoted to hard-fail when the marker-based outer rollback
     landed in Phase 6.3.
  2  tooling error (NetBox unreachable, parquet path missing,
     bad CLI args, etc.). CI should distinguish "drift found"
     from "harness could not run" - the second is a real failure.

Run pattern (lab):
  docker compose run --rm drift python -m drift.cli --json
  docker compose run --rm drift python -m drift.cli --human
  docker compose run --rm drift python -m drift.cli \\
      --mode timeseries --window 1h --human

The container's docker-compose.yml mounts:
  - /suzieq/parquet (read-only) - the suzieq_parquet docker volume
  - /drift          (read-only) - the drift code, mounted from host
                    so iteration does not need image rebuild
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import List

import pynetbox

from .intent import collect as collect_intent
from .state import collect as collect_state, DEFAULT_PARQUET_DIR
from .diff import compare, Drift, SEVERITY_ERROR, SEVERITY_WARNING
from .assertions import run_all as run_all_assertions
from .timeseries.partition import parse_duration
from .timeseries.reader import TimeWindow, window_read
from .timeseries.queries import QUERIES
from .timeseries.envelope import (
    build_envelope,
    emit_human as ts_emit_human,
    emit_json as ts_emit_json,
    HEARTBEAT_TABLE,
)


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
    p.add_argument(
        "--mode", default=os.environ.get("DRIFT_MODE", "drift"),
        choices=("drift", "assertions", "all", "timeseries"),
        help=(
            "What to run: "
            "'drift' = NetBox-vs-SuzieQ intent diff (Part B, default - "
            "needs NetBox); "
            "'assertions' = state-only invariants via the assertions "
            "package (Part C - no NetBox access needed, suitable for "
            "systemd-timer scheduling); "
            "'all' = drift+assertions, combined output; "
            "'timeseries' = Part D time-window queries (bgp_flaps, "
            "route_churn, mac_mobility) over the parquet history. "
            "No NetBox needed. Use --window or --from/--to to set the "
            "window. Output is the timeseries envelope, NOT the "
            "drift envelope."
        ),
    )
    p.add_argument(
        "--window", default=None,
        help=(
            "Timeseries mode: window duration relative to now, e.g. "
            "'5m', '1h', '24h', '7d'. Mutually exclusive with "
            "--from/--to. Smallest practical resolution is 1m "
            "(poller cadence)."
        ),
    )
    p.add_argument(
        "--from", dest="from_epoch", type=int, default=None,
        help=(
            "Timeseries mode: absolute window start as unix epoch "
            "seconds. Pair with --to. Mutually exclusive with --window."
        ),
    )
    p.add_argument(
        "--to", dest="to_epoch", type=int, default=None,
        help="Timeseries mode: absolute window end as unix epoch seconds.",
    )
    p.add_argument(
        "--exit-nonzero-on-degraded", action="store_true", default=False,
        help=(
            "Timeseries mode opt-in (Phase 5.1): when the envelope "
            "self-check reports status='degraded', exit with "
            "EXIT_DRIFT_FOUND (1) instead of EXIT_OK (0). Default "
            "OFF - preserves ADR-11 'timeseries observations are "
            "never pass/fail' for normal runs. Intended for "
            "operators who want systemd `OnFailure=` to fire on "
            "degraded status without waiting for a Phase 6 "
            "consumer. See phase5-suzieq/README.md for the "
            "systemd drop-in pattern."
        ),
    )
    return p.parse_args(argv)


def resolve_window(args, now=None) -> TimeWindow:
    """Translate the CLI window flags into a concrete TimeWindow.

    Accepts EITHER --window <duration> (relative to now) OR
    --from <epoch> --to <epoch> (absolute), but not both.

    Raises ValueError on invalid combinations - the CLI catches
    this and returns EXIT_TOOLING_ERROR with the message printed
    to stderr.

    The `now` arg is injectable so tests can pin the relative
    window math without monkey-patching time.time.
    """
    has_relative = args.window is not None
    has_absolute = args.from_epoch is not None or args.to_epoch is not None

    if has_relative and has_absolute:
        raise ValueError(
            "--window is mutually exclusive with --from/--to; "
            "pass exactly one form"
        )

    if has_absolute:
        if args.from_epoch is None or args.to_epoch is None:
            raise ValueError(
                "--from and --to must be passed together "
                "(both as unix epoch seconds)"
            )
        if args.from_epoch >= args.to_epoch:
            raise ValueError(
                f"--from ({args.from_epoch}) must be strictly less than "
                f"--to ({args.to_epoch})"
            )
        return TimeWindow(args.from_epoch, args.to_epoch)

    if has_relative:
        seconds = parse_duration(args.window)
        if seconds <= 0:
            raise ValueError(f"--window must be a positive duration, got {args.window!r}")
        if now is None:
            now = int(time.time())
        return TimeWindow(now - seconds, now)

    raise ValueError(
        "--mode timeseries requires either --window or --from/--to"
    )


def run_timeseries(args) -> int:
    """Part D entry point. Reads the requested window for each table
    needed by the registered queries, runs each query against its
    WindowedTable, builds the envelope, emits, returns exit code.

    Exit codes:
      0   harness ran cleanly (timeseries results are observations,
          not pass/fail - they never produce non-zero on their own)
      2   tooling error (bad window args, parquet path missing)

    Notably there is NO exit code 1 from this mode. The whole point
    of timeseries queries is to surface neutral observations the
    operator interprets in context. A flap count of 1000 in the last
    hour is alarming, but the harness has no business deciding what
    threshold makes a result "fail" - that's a job for assertions
    (--mode assertions) which DO have pass/fail semantics.
    """
    try:
        window = resolve_window(args)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_TOOLING_ERROR

    # Read each distinct table once and cache, so two queries
    # against the same table don't re-walk the parquet store.
    # HEARTBEAT_TABLE (sqPoller) is read in addition to the query
    # tables so the envelope self-check can evaluate poller
    # liveness - it is not used by any registered query, but it
    # is the one table in the SuzieQ schema that gets new rows
    # every poll cycle regardless of fabric state, making it the
    # right source for a heartbeat signal.
    query_tables = {entry.table for entry in QUERIES.values()}
    tables_needed = sorted(query_tables | {HEARTBEAT_TABLE})
    windowed_tables = {}
    files_read_by_table = {}
    for table in tables_needed:
        try:
            wt = window_read(
                table=table,
                namespace=args.namespace,
                start_epoch=window.start_epoch,
                end_epoch=window.end_epoch,
                parquet_dir=args.parquet_dir,
            )
        except Exception as e:
            print(
                f"ERROR: parquet read failed for table {table!r}: {e}",
                file=sys.stderr,
            )
            return EXIT_TOOLING_ERROR
        windowed_tables[table] = wt
        files_read_by_table[table] = wt.files_read

    # Run queries in registry order so the envelope output is stable.
    results = []
    for name, entry in QUERIES.items():
        wt = windowed_tables[entry.table]
        try:
            results.append(entry.fn(wt))
        except Exception as e:
            print(
                f"ERROR: query {name!r} failed: {e}",
                file=sys.stderr,
            )
            return EXIT_TOOLING_ERROR

    envelope = build_envelope(
        namespace=args.namespace,
        window=window,
        results=results,
        files_read_by_table=files_read_by_table,
        # Pass the windowed tables so the envelope's self-check can
        # inspect row freshness and flag a "degraded" status when the
        # poller has silently stopped feeding data. By default this
        # does NOT change the exit code - degraded is an
        # observational signal for Phase 6 consumers, not a failure
        # per ADR-11. The --exit-nonzero-on-degraded flag below
        # opts into promoting it to EXIT_DRIFT_FOUND so operators
        # can wire systemd OnFailure= to the hourly timer without
        # waiting for a Phase 6 consumer.
        windowed_tables=windowed_tables,
    )

    if args.format == "json":
        ts_emit_json(envelope)
    else:
        ts_emit_human(envelope)

    # Opt-in exit-code-on-degraded (Phase 5.1). Default stays EXIT_OK
    # to preserve ADR-11; flag flips the contract for operators who
    # want the timer unit to surface degradation immediately via
    # systemd OnFailure= instead of waiting for a Phase 6 consumer.
    if getattr(args, "exit_nonzero_on_degraded", False):
        if envelope.get("status") == "degraded":
            return EXIT_DRIFT_FOUND

    return EXIT_OK


def run(args) -> int:
    """Main run loop. Returns the process exit code. Split out from
    main() so tests can drive run() with hand-built args without
    parsing argv.

    Four modes:
      drift       = Part B NetBox-vs-state diff. Needs NetBox creds.
      assertions  = Part C state-only invariant checks. Skips
                    NetBox entirely - suitable for systemd-timer
                    scheduling because it has no external
                    credential dependency.
      all         = drift + assertions, combined into one output record.
      timeseries  = Part D time-window queries. Different output
                    envelope (richer time-series shape, not the
                    drift {result, total, passed, failed} shape).
                    No NetBox needed.
    """
    # Timeseries has its own collect/emit path - dispatch early so
    # we don't pay for the latest-snapshot state read it doesn't
    # need.
    if args.mode == "timeseries":
        return run_timeseries(args)

    # State is always needed for drift / assertions / all modes.
    try:
        state = collect_state(args.namespace, args.parquet_dir)
    except Exception as e:
        print(f"ERROR: SuzieQ state read failed: {e}", file=sys.stderr)
        return EXIT_TOOLING_ERROR

    drifts: List[Drift] = []

    if args.mode in ("drift", "all"):
        # NetBox is required for intent collection.
        if not args.netbox_url or not args.netbox_token:
            print(
                "ERROR: NETBOX_URL and NETBOX_TOKEN must be set for "
                "mode={} (env or --flags)".format(args.mode),
                file=sys.stderr,
            )
            return EXIT_TOOLING_ERROR
        try:
            nb = pynetbox.api(args.netbox_url, token=args.netbox_token)
        except Exception as e:
            print(f"ERROR: NetBox connection failed: {e}", file=sys.stderr)
            return EXIT_TOOLING_ERROR
        try:
            intent = collect_intent(nb, args.namespace)
        except Exception as e:
            print(f"ERROR: NetBox intent collection failed: {e}", file=sys.stderr)
            return EXIT_TOOLING_ERROR
        drifts.extend(compare(intent, state))

    if args.mode in ("assertions", "all"):
        # Pure state-only check - no NetBox needed. This is the
        # mode the systemd timer runs because it has zero external
        # dependencies beyond the local parquet store.
        try:
            drifts.extend(run_all_assertions(state))
        except Exception as e:
            print(f"ERROR: assertion run failed: {e}", file=sys.stderr)
            return EXIT_TOOLING_ERROR

    # Sort for stable output regardless of mode
    drifts.sort(key=lambda d: (d.dimension, d.subject))
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
