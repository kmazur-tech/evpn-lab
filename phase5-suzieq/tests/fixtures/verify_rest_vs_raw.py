#!/usr/bin/env python3
"""Phase 5.1 REST API vs raw pyarrow schema-drift smoke test.

Queries the SuzieQ REST API for one row per table and compares
the column set against what raw pyarrow reads from the same
parquet store. The REST API path runs through the SuzieQ pandas
engine, which adds engine-computed columns at query time. The
raw pyarrow path sees only what's stored on disk.

Columns that appear in the REST response but NOT in raw parquet
= engine-computed columns. If a new one shows up on an image
bump, the test reports it as "NEW engine-computed column" -
production code that reads parquet directly would silently
default that column to None/NaN, which is exactly the class of
bug that burned Part C's `remoteVtepCnt` assertion.

## Relationship to the other live guards

  tests/test_live_schema_guards.py        - fast per-table unit
    guard that reads only raw parquet and asserts every column
    production code depends on is present. Catches schema
    renames and disappearing columns.

  tests/fixtures/verify_engine_vs_raw.py - (unused, replaced
    by this REST-based version after the sq-rest-server wedge
    fix landed in Phase 5.1)

  tests/fixtures/verify_rest_vs_raw.py   - THIS FILE. Catches
    NEW engine-computed columns by diffing the REST response
    against the raw parquet schema. The one live guard that
    exercises the full REST API path end to end.

## Why REST and not suzieq-cli

`suzieq-cli` shells in via stdin and parses CLI output. REST is
a stable wire protocol with HTTP status codes and JSON
responses; it's the public contract that a Phase 6 consumer
would use, so validating it end-to-end here is more valuable
than validating a CLI wrapper.

The other reason: shipping a `docker exec sq-poller suzieq-cli`
path in a test would couple the test to the docker topology on
the host. REST is just HTTP — works from any host that can
reach the REST port.

## How to run

Requires:
  - sq-rest-server reachable at http://<host>:<port>
  - SUZIEQ_API_KEY env var set to a valid key
  - SUZIEQ_LIVE_PARQUET_DIR pointing at the raw parquet store
    on disk (needed for the raw-side diff; pyarrow reads it
    directly, NOT via the REST server)
  - pyarrow installed

On netdevops-srv:

    SUZIEQ_REST_URL=http://127.0.0.1:8443 \\
    SUZIEQ_API_KEY=$(docker inspect sq-rest-server \\
        --format '{{range .Config.Env}}{{println .}}{{end}}' \\
        | awk -F= '/^SUZIEQ_API_KEY=/{print $2}') \\
    SUZIEQ_LIVE_PARQUET_DIR=/var/lib/docker/volumes/suzieq_parquet/_data \\
    SUZIEQ_NAMESPACE=dc1 \\
        docker run --rm \\
            --network host \\
            -v /var/lib/docker/volumes/suzieq_parquet/_data:/parquet:ro \\
            -v /tmp/verify_rest_vs_raw.py:/verify.py:ro \\
            -e SUZIEQ_REST_URL \\
            -e SUZIEQ_API_KEY \\
            -e SUZIEQ_LIVE_PARQUET_DIR=/parquet \\
            -e SUZIEQ_NAMESPACE \\
            --entrypoint python3 \\
            evpn-lab/phase5-drift:dev /verify.py

Or if pyarrow is installed locally on the host:

    SUZIEQ_REST_URL=http://127.0.0.1:8443 \\
    SUZIEQ_API_KEY=... \\
    SUZIEQ_LIVE_PARQUET_DIR=/var/lib/docker/volumes/suzieq_parquet/_data \\
    SUZIEQ_NAMESPACE=dc1 \\
        python3 verify_rest_vs_raw.py

Exit 0 on clean, 1 on unexpected drift.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


# Tables we compare, mapping from the (plural) parquet directory
# name used by state.py's TABLE_REGISTRY to the (sometimes
# singular) REST API verb. The REST API uses `route`, `mac`,
# `interface` (singular) despite the parquet layer using
# `routes`, `macs`, `interfaces` - verified empirically on
# 2026-04-11 against netdevops-srv:
#
#   route      -> 200    routes      -> 404
#   mac        -> 200    macs        -> 404
#   interface  -> 200    interfaces  -> 404
#   bgp        -> 200    evpnVni     -> 200
#   device     -> 200    arpnd       -> 200
#   sqPoller   -> 200
TABLES = [
    # (parquet_name, rest_name)
    ("bgp",        "bgp"),
    ("evpnVni",    "evpnVni"),
    ("routes",     "route"),
    ("macs",       "mac"),
    ("device",     "device"),
    ("interfaces", "interface"),
    ("arpnd",      "arpnd"),
    ("sqPoller",   "sqPoller"),
]


# Known engine-computed columns: the REST response exposes these
# but raw parquet does NOT have them. The whole point of this
# test is to detect NEW entries here on an image bump.
#
# Each entry should carry a comment explaining:
#   1. What the engine computes it from (usually a raw column)
#   2. Whether production code currently reads it (grep
#      phase5-suzieq/drift for the column name)
#   3. If production code DOES read it: how (raw direct read
#      would silently see None, so something else must be going
#      on - maybe the code computes it itself, or goes through
#      suzieq-cli)
#
# Source: ADR-7 in the Phase 5 review packet + empirical diffs
# captured on netdevops-srv 2026-04-11 against the current
# pinned suzieq image digest via this same script.
KNOWN_ENGINE_COMPUTED = {
    "evpnVni": {
        # ADR-7: computed as len(remoteVtepList) at query time.
        # Production code in drift/assertions/vtep.py computes
        # the count itself via _count_remote_vteps() because
        # the column does not exist in raw parquet. Part C's
        # first live run hit the silent-default case here.
        "remoteVtepCnt",
    },
    "bgp":        set(),
    "routes":     set(),
    "macs":       set(),
    "device":     set(),
    "interfaces": set(),
    "arpnd":      set(),
    "sqPoller":   {
        # statusStr is a human-readable mapping of the integer
        # `status` column which IS in raw parquet. The engine
        # converts 0->"OK", 1->"Command Not Found", etc. at
        # query time. Production code does NOT currently read
        # statusStr (grepped phase5-suzieq/drift on 2026-04-11 -
        # zero hits); assert_poll_health reads
        # pollExcdPeriodCount instead. Discovered by this
        # test's FIRST live run on netdevops-srv 2026-04-11 and
        # allowlisted here as a documented engine-computed
        # column. If a future assertion wants the human-readable
        # status, use raw `status` + a local int->str map, NOT
        # direct statusStr reads.
        "statusStr",
    },
}


def query_rest(rest_url: str, api_key: str, namespace: str, rest_table: str):
    """Call GET /api/v2/<rest_table>/show?namespace=<ns>&access_token=
    Returns a list of dicts, or None + error string. Uses
    `rest_table` (the REST API verb, sometimes singular - see
    TABLES mapping) not the parquet directory name."""
    url = (
        f"{rest_url.rstrip('/')}/api/v2/{rest_table}/show"
        f"?namespace={namespace}&access_token={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            body = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTPError {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"URLError: {e.reason}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    try:
        rows = json.loads(body)
    except json.JSONDecodeError as e:
        return None, f"JSON decode: {e}"
    if not isinstance(rows, list):
        return None, f"expected list, got {type(rows).__name__}"
    return rows, None


def query_raw(parquet_dir: Path, table: str):
    """Read one sample row from the coalesced parquet subtree
    for `table` via pyarrow.dataset with hive partitioning.
    Returns a list of column names, or None + error string."""
    import pyarrow.dataset as ds
    subtree = parquet_dir / "coalesced" / table
    if not subtree.exists():
        return None, f"no coalesced parquet at {subtree}"
    try:
        dataset = ds.dataset(str(subtree), partitioning="hive")
        df = dataset.to_table().to_pandas()
    except (FileNotFoundError, OSError) as e:
        return None, f"pyarrow read failed: {e}"
    if df.empty:
        return None, "parquet subtree is empty"
    return list(df.columns), None


def compare_table(rest_url, api_key, namespace, parquet_dir, parquet_table, rest_table):
    """Diff the REST /show column set vs the raw parquet column
    set for one (parquet_table, rest_table) pair.

    Failure condition: any column in REST that is NOT in raw
    AND not in the KNOWN_ENGINE_COMPUTED allowlist for this
    table. That's a NEW engine-computed column - production
    code reading raw parquet would silently default it.

    Informational (no fail): columns in raw but not in REST.
    The REST API deliberately curates a smaller view; raw
    parquet has many more columns that production code reads
    directly. A curated view narrower than the underlying
    storage is expected behavior, NOT drift.
    """
    rest_rows, rest_err = query_rest(rest_url, api_key, namespace, rest_table)
    if rest_err:
        return {
            "parquet_table": parquet_table,
            "rest_table": rest_table,
            "status": "skip",
            "reason": f"REST query failed: {rest_err}",
        }
    if not rest_rows:
        return {
            "parquet_table": parquet_table,
            "rest_table": rest_table,
            "status": "skip",
            "reason": "REST returned no rows",
        }

    raw_cols, raw_err = query_raw(parquet_dir, parquet_table)
    if raw_err:
        return {
            "parquet_table": parquet_table,
            "rest_table": rest_table,
            "status": "skip",
            "reason": f"raw query failed: {raw_err}",
        }

    rest_cols = set(rest_rows[0].keys())
    raw_cols_set = set(raw_cols)

    rest_only = rest_cols - raw_cols_set  # engine-computed candidates
    raw_only = raw_cols_set - rest_cols   # REST curation (informational)

    known_engine = KNOWN_ENGINE_COMPUTED.get(parquet_table, set())
    unexpected_rest_only = rest_only - known_engine

    # Only unexpected rest_only columns fail. raw_only is always
    # informational - the REST API curating a smaller view is
    # fine.
    status = "fail" if unexpected_rest_only else "pass"

    return {
        "parquet_table": parquet_table,
        "rest_table": rest_table,
        "status": status,
        "rest_cols": sorted(rest_cols),
        "raw_cols": sorted(raw_cols_set),
        "rest_only": sorted(rest_only),
        "raw_only": sorted(raw_only),
        "unexpected_rest_only": sorted(unexpected_rest_only),
        "known_engine_computed": sorted(known_engine),
    }


def format_result(r):
    label = r["parquet_table"]
    if r["rest_table"] != r["parquet_table"]:
        label = f"{r['parquet_table']} (rest:{r['rest_table']})"
    if r["status"] == "skip":
        return f"SKIP  {label:22s}  {r['reason']}"
    if r["status"] == "pass":
        msg = (
            f"PASS  {label:22s}  "
            f"rest={len(r['rest_cols'])} raw={len(r['raw_cols'])}"
        )
        if r["rest_only"]:
            msg += f"  engine-computed: {r['rest_only']}"
        return msg
    # fail
    lines = [f"FAIL  {label:22s}"]
    if r["unexpected_rest_only"]:
        lines.append(
            f"        NEW engine-computed columns (pyarrow would see "
            f"None/NaN, likely silent bug): {r['unexpected_rest_only']}"
        )
    return "\n".join(lines)


def main():
    rest_url = os.environ.get("SUZIEQ_REST_URL")
    api_key = os.environ.get("SUZIEQ_API_KEY")
    parquet_dir = os.environ.get("SUZIEQ_LIVE_PARQUET_DIR")
    namespace = os.environ.get("SUZIEQ_NAMESPACE", "dc1")

    missing = []
    if not rest_url:
        missing.append("SUZIEQ_REST_URL")
    if not api_key:
        missing.append("SUZIEQ_API_KEY")
    if not parquet_dir:
        missing.append("SUZIEQ_LIVE_PARQUET_DIR")
    if missing:
        print(
            f"ERROR: missing env vars: {', '.join(missing)}",
            file=sys.stderr,
        )
        sys.exit(2)

    parquet_dir = Path(parquet_dir)
    if not parquet_dir.is_dir():
        print(f"ERROR: {parquet_dir} does not exist", file=sys.stderr)
        sys.exit(2)

    print(
        f"=== Phase 5.1 REST-vs-raw schema drift "
        f"(rest={rest_url}, parquet={parquet_dir}, ns={namespace}) ==="
    )
    print()

    passed = failed = skipped = 0
    for parquet_table, rest_table in TABLES:
        r = compare_table(
            rest_url, api_key, namespace, parquet_dir,
            parquet_table, rest_table,
        )
        print(format_result(r))
        if r["status"] == "pass":
            passed += 1
        elif r["status"] == "fail":
            failed += 1
        else:
            skipped += 1

    print()
    print(f"=== Summary: {passed} passed, {failed} failed, {skipped} skipped ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
