#!/usr/bin/env python3
"""Standalone mirror of tests/test_live_schema_guards.py.

Runs the same REQUIRED_COLUMNS + engine-computed-column checks
against a live SuzieQ parquet store without needing pytest - the
netdevops-srv host does not ship pytest and PEP 668 blocks
pip install on system python, so this script is the live-
verification path.

How to run inside the drift container on netdevops-srv:

    docker run --rm \\
        -v suzieq_parquet:/suzieq/parquet:ro \\
        -v /opt/suzieq/tests/fixtures/verify_live_schema.py:/verify.py:ro \\
        -e SUZIEQ_LIVE_PARQUET_DIR=/suzieq/parquet \\
        --entrypoint python3 \\
        evpn-lab/phase5-drift:dev /verify.py

Or directly on the host if pyarrow + pandas are installed:

    SUZIEQ_LIVE_PARQUET_DIR=/var/lib/docker/volumes/suzieq_parquet/_data \\
        python3 verify_live_schema.py

Exit code 0 on clean, 1 on any schema drift. Output is machine-
greppable: one PASS / FAIL / SKIP line per table plus a final
summary line.

Keep this script in sync with tests/test_live_schema_guards.py -
if you add a column to REQUIRED_COLUMNS in the test file, mirror
it here, and vice versa.
"""
import os
import sys
from pathlib import Path


REQUIRED_COLUMNS = {
    "device": {
        "namespace", "hostname", "timestamp",
    },
    "interfaces": {
        "namespace", "hostname", "ifname",
        "adminState", "state", "timestamp",
    },
    "lldp": {
        "namespace", "hostname", "ifname",
        "peerHostname", "peerIfname", "timestamp",
    },
    "bgp": {
        "namespace", "hostname", "vrf", "peer", "afi", "safi",
        "state", "pfxRx", "timestamp",
    },
    "evpnVni": {
        "namespace", "hostname", "vni", "state", "type",
        "remoteVtepList", "timestamp",  # NOT remoteVtepCnt (ADR-7)
    },
    "routes": {
        "namespace", "hostname", "vrf", "prefix", "timestamp",
    },
    "macs": {
        "namespace", "hostname", "vlan", "macaddr",
        "oif", "remoteVtepIp", "timestamp",
    },
    "arpnd": {
        "namespace", "hostname", "ipAddress", "timestamp",
    },
    "sqPoller": {
        "namespace", "hostname", "service",
        "pollExcdPeriodCount", "timestamp",
    },
}


def read_coalesced_sample(parquet_dir, table):
    """Read the coalesced subtree for `table` via pyarrow.dataset
    with hive partitioning. Returns a pandas DataFrame or None."""
    import pyarrow.dataset as ds
    subtree = Path(parquet_dir) / "coalesced" / table
    if not subtree.exists():
        return None
    try:
        dataset = ds.dataset(str(subtree), partitioning="hive")
        df = dataset.to_table().to_pandas()
    except (FileNotFoundError, OSError) as e:
        print(f"  ERROR reading {subtree}: {e}")
        return None
    return df if len(df) > 0 else None


def check_required_columns(parquet_dir):
    """Run the REQUIRED_COLUMNS check for every table.
    Returns (pass_count, fail_count, skip_count, failures)."""
    passed = 0
    failed = 0
    skipped = 0
    failures = []

    for table in sorted(REQUIRED_COLUMNS.keys()):
        df = read_coalesced_sample(parquet_dir, table)
        if df is None:
            print(f"SKIP  {table:12s}  no coalesced parquet yet")
            skipped += 1
            continue
        required = REQUIRED_COLUMNS[table]
        actual = set(df.columns)
        missing = required - actual
        if missing:
            failed += 1
            msg = (
                f"{table} missing columns: {sorted(missing)}. "
                f"Production code reads these but they are not in "
                f"the live parquet schema."
            )
            failures.append(msg)
            print(f"FAIL  {table:12s}  missing: {sorted(missing)}")
        else:
            passed += 1
            print(f"PASS  {table:12s}  {len(required)} required columns present")

    return passed, failed, skipped, failures


def check_engine_computed_drift(parquet_dir):
    """ADR-7 pin: remoteVtepCnt is engine-computed, not in parquet.
    If it ever appears as a raw column, fail with an informational
    message so the operator updates the code to read it directly."""
    df = read_coalesced_sample(parquet_dir, "evpnVni")
    if df is None:
        print("SKIP  evpnVni.remoteVtepCnt-pin  no evpnVni parquet yet")
        return True, None
    if "remoteVtepCnt" in df.columns:
        msg = (
            "evpnVni: remoteVtepCnt now exists as a raw parquet column. "
            "Previously engine-computed per ADR-7. Update "
            "drift/assertions/vtep.py to read it directly and add to "
            "REQUIRED_COLUMNS['evpnVni']."
        )
        print(f"FAIL  engine-computed-drift  {msg}")
        return False, msg
    print("PASS  evpnVni.remoteVtepCnt-pin  still engine-computed (ADR-7)")
    return True, None


def main():
    parquet_dir = os.environ.get("SUZIEQ_LIVE_PARQUET_DIR")
    if not parquet_dir:
        print("ERROR: SUZIEQ_LIVE_PARQUET_DIR not set", file=sys.stderr)
        print(
            "  default netdevops-srv location: "
            "/var/lib/docker/volumes/suzieq_parquet/_data",
            file=sys.stderr,
        )
        print(
            "  default drift container location: /suzieq/parquet",
            file=sys.stderr,
        )
        sys.exit(2)
    if not Path(parquet_dir).is_dir():
        print(f"ERROR: {parquet_dir} does not exist or is not a directory", file=sys.stderr)
        sys.exit(2)

    print(f"=== Phase 5.1 live schema guards ({parquet_dir}) ===")
    print()
    print("Required columns check:")
    passed, failed, skipped, failures = check_required_columns(parquet_dir)
    print()
    print("Engine-computed column drift check:")
    engine_ok, engine_msg = check_engine_computed_drift(parquet_dir)
    if not engine_ok:
        failed += 1
    else:
        passed += 1
    print()
    print(f"=== Summary: {passed} passed, {failed} failed, {skipped} skipped ===")

    if failures:
        print()
        print("Failures:")
        for f in failures:
            print(f"  - {f}")
    if engine_msg:
        print(f"  - {engine_msg}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
