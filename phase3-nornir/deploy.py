#!/usr/bin/env python3
"""Phase 3 entry point: NetBox -> Nornir -> render -> diff -> (deploy).

Current scope: --check mode only. Renders the routing_options.j2 stanza
for every fabric device and diffs against the matching stanza in
phase2-fabric/configs/<host>.conf. Templates and modes will be added
incrementally as more partials land.
"""

import argparse
import difflib
import os
import re
import sys
from pathlib import Path

from nornir import InitNornir
from nornir_jinja2.plugins.tasks import template_file

from tasks.enrich import enrich_from_netbox

REPO_ROOT = Path(__file__).resolve().parent.parent
PHASE2_CONFIGS = REPO_ROOT / "phase2-fabric" / "configs"
BUILD_DIR = Path(__file__).resolve().parent / "build"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "junos"


def extract_stanza(text: str, name: str) -> str:
    """Pull a top-level Junos stanza by name (brace-balanced)."""
    pattern = re.compile(rf"^{re.escape(name)} \{{", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    start = match.start()
    depth = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1] + "\n"
        i += 1
    return ""


def render_and_diff(task):
    """Render routing_options.j2 and diff vs phase2 baseline stanza."""
    result = task.run(
        task=template_file,
        template="routing_options.j2",
        path=str(TEMPLATE_DIR),
    )
    rendered = result.result

    out_path = BUILD_DIR / f"{task.host.name}.routing_options.conf"
    out_path.write_text(rendered, encoding="utf-8", newline="\n")

    baseline_path = PHASE2_CONFIGS / f"{task.host.name}.conf"
    baseline_text = baseline_path.read_text(encoding="utf-8")
    baseline_stanza = extract_stanza(baseline_text, "routing-options")

    if rendered.strip() == baseline_stanza.strip():
        return f"OK    {task.host.name}: routing-options matches baseline"

    diff = difflib.unified_diff(
        baseline_stanza.splitlines(keepends=True),
        rendered.splitlines(keepends=True),
        fromfile=f"baseline/{task.host.name}",
        tofile=f"rendered/{task.host.name}",
    )
    return f"DIFF  {task.host.name}:\n" + "".join(diff)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Render + diff vs baseline. Currently the only mode.")
    args = parser.parse_args()

    BUILD_DIR.mkdir(exist_ok=True)

    # Nornir's config merge REPLACES inventory.options wholesale rather than
    # deep-merging, so secrets and filter_parameters must travel together in
    # one dict. Keep filter_parameters here in source (not YAML) so the merge
    # is single-sourced and obvious.
    nr = InitNornir(
        config_file=str(Path(__file__).resolve().parent / "nornir.yml"),
        inventory={
            "options": {
                "nb_url": os.environ["NETBOX_URL"],
                "nb_token": os.environ["NETBOX_TOKEN"],
                "ssl_verify": False,
                "flatten_custom_fields": False,
                "filter_parameters": {
                    "site": "dc1",
                    "status": "active",
                    "platform": "junos",
                },
            },
        },
    )

    enrich_result = nr.run(task=enrich_from_netbox)
    for host, multi in enrich_result.items():
        print(f"  enrich {host}: {multi[0].result}")

    print()

    failed = False
    render_result = nr.run(task=render_and_diff)
    for host, multi in render_result.items():
        msg = multi[0].result
        print(msg)
        if msg.startswith("DIFF"):
            failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
