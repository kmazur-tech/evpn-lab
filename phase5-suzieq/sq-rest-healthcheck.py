#!/usr/bin/env python3
"""Real HTTP healthcheck for sq-rest-server.

Run periodically by Docker's `healthcheck` directive. Replaces
the earlier `</dev/tcp/127.0.0.1/8000` shell check, which only
verified TCP accept - a wedged uvicorn worker still accepts TCP
connections via the listening socket, so the bash check kept
passing for three days while every HTTP request was reset at
the server. This script fixes that.

## What it checks

Makes one authenticated HTTP request to the cheapest
non-trivial endpoint in the SuzieQ REST API (`device/show` -
4 rows on a 4-device lab, no joins, no aggregations) and
verifies:

  1. TCP connect succeeds (same as the old check)
  2. HTTP response is 200 OK (NEW - catches the wedge state)
  3. Response body parses as a JSON list (NEW - catches
     "200 but empty/malformed body" failure modes)

Fails with a non-zero exit on any mismatch. Docker marks the
container unhealthy after the configured number of retries and
the operator sees it in `docker ps` / `docker compose ps`.

## Why a Python script and not an inline CMD-SHELL

The authenticated endpoint needs the API key from the
SUZIEQ_API_KEY env var, and the response body check needs a
JSON parse. Both are trivial in Python and awkward as a
multi-line shell command in a YAML healthcheck block.
A standalone script also makes the check reviewable: the
check logic lives next to the production code, not buried in
compose-file escape hell.

## Why this is bind-mounted, not baked into the image

The upstream netenglabs/suzieq image is digest-pinned. Adding
a healthcheck script to it would require a child image build
step (extending suzieq-image/Dockerfile the same way the
devtype patcher does) just for one file. Bind-mounting from
the host keeps the Phase 5 image build path unchanged and the
healthcheck file editable without a rebuild.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HOST = os.environ.get("SQ_REST_HEALTHCHECK_HOST", "127.0.0.1")
PORT = os.environ.get("SQ_REST_HEALTHCHECK_PORT", "8000")
NS = os.environ.get("SQ_REST_HEALTHCHECK_NAMESPACE", "dc1")
TIMEOUT_SEC = float(os.environ.get("SQ_REST_HEALTHCHECK_TIMEOUT", "5"))


def main() -> int:
    api_key = os.environ.get("SUZIEQ_API_KEY")
    if not api_key:
        # No API key means we cannot make an authenticated call -
        # fail loudly instead of silently passing.
        print("SUZIEQ_API_KEY env var missing", file=sys.stderr)
        return 1

    url = (
        f"http://{HOST}:{PORT}/api/v2/device/show"
        f"?namespace={NS}&access_token={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as resp:
            if resp.status != 200:
                print(
                    f"HTTP {resp.status} from {url}",
                    file=sys.stderr,
                )
                return 1
            body = resp.read()
    except urllib.error.HTTPError as e:
        print(f"HTTPError {e.code}: {e.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"URLError: {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(
            f"unexpected {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        print(
            f"JSON parse failed: {e}. Body head: {body[:200]!r}",
            file=sys.stderr,
        )
        return 1

    if not isinstance(parsed, list):
        print(
            f"expected list response, got {type(parsed).__name__}",
            file=sys.stderr,
        )
        return 1

    # Empty list is acceptable - the lab might have zero devices
    # during a destroy/deploy cycle. The point of this check is
    # "the server parses the request and returns a well-formed
    # response", not "there are devices".
    return 0


if __name__ == "__main__":
    sys.exit(main())
