#!/usr/bin/env python3
"""Record vcrpy cassettes for enrich_from_netbox() against live NetBox.

Run from the phase3-nornir directory with NETBOX_URL and NETBOX_TOKEN
set in the environment (source evpn-lab-env/env.sh):

    cd phase3-nornir
    source ../../evpn-lab-env/env.sh
    python ../phase6-cicd/scripts/refresh-netbox-cassettes.py

Records one cassette per device into tests/cassettes/.  Existing
cassettes are overwritten.  Authorization headers are sanitized
and the real NetBox host is replaced with a placeholder before
writing, so no infrastructure IPs leak into the repo.
"""

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Ensure phase3-nornir is on sys.path so imports work
PHASE3 = Path(__file__).resolve().parents[2] / "phase3-nornir"
sys.path.insert(0, str(PHASE3))

import vcr

from tasks.enrich.main import enrich_from_netbox

CASSETTE_DIR = PHASE3 / "tests" / "cassettes"
DEVICES = ["dc1-spine1", "dc1-spine2", "dc1-leaf1", "dc1-leaf2"]

# Placeholder host used in cassettes and test_enrich_vcr.py so the
# real NetBox IP never appears in the repo.
PLACEHOLDER_HOST = "netbox.lab.local"


class _MockHost(dict):
    def __init__(self, name):
        super().__init__()
        self._name = name

    @property
    def name(self):
        return self._name


class _MockTask:
    def __init__(self, host):
        self.host = host


def _sanitize_request(request):
    if "Authorization" in request.headers:
        request.headers["Authorization"] = "Token sanitized"
    return request


def main():
    for var in ("NETBOX_URL", "NETBOX_TOKEN"):
        if not os.environ.get(var):
            print(f"ERROR: {var} not set in environment", file=sys.stderr)
            sys.exit(1)

    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)

    my_vcr = vcr.VCR(
        cassette_library_dir=str(CASSETTE_DIR),
        record_mode="all",
        decode_compressed_response=True,
        before_record_request=_sanitize_request,
    )

    ok = 0
    for device in DEVICES:
        cassette_path = CASSETTE_DIR / f"{device}.yaml"
        print(f"Recording {device} -> {cassette_path.name} ... ", end="", flush=True)

        host = _MockHost(device)
        task = _MockTask(host)

        try:
            with my_vcr.use_cassette(str(cassette_path)):
                result = enrich_from_netbox(task)
            if result.failed:
                print(f"FAILED: {result.result}")
                continue
            print(f"OK  ({result.result})")
            ok += 1
        except Exception as e:
            print(f"ERROR: {e}")

    # Replace the real NetBox host with the placeholder in all cassettes
    # so no infrastructure IPs leak into the repo.
    real_host = urlparse(os.environ["NETBOX_URL"]).hostname
    if real_host and real_host != PLACEHOLDER_HOST:
        for cassette_file in CASSETTE_DIR.glob("*.yaml"):
            text = cassette_file.read_text(encoding="utf-8")
            text = text.replace(real_host, PLACEHOLDER_HOST)
            cassette_file.write_text(text, encoding="utf-8")
        print(f"Sanitized: {real_host} -> {PLACEHOLDER_HOST}")

    print(f"\n{ok}/{len(DEVICES)} cassettes recorded in {CASSETTE_DIR}")
    if ok < len(DEVICES):
        sys.exit(1)


if __name__ == "__main__":
    main()
