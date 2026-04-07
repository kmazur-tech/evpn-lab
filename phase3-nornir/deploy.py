#!/usr/bin/env python3
"""Phase 3 entry point: NetBox -> Nornir -> render -> diff -> (deploy).

Current scope: --check mode only. Renders each Phase 3 template stanza
that has landed so far for every fabric device and diffs against the
matching stanza in phase2-fabric/configs/<host>.conf. Templates are
added incrementally; each new partial gets a row in STANZAS below.
"""

import argparse
import difflib
import os
import re
import sys
from pathlib import Path

import yaml
from nornir import InitNornir
from nornir_jinja2.plugins.tasks import template_file

from tasks.enrich import enrich_from_netbox

REPO_ROOT = Path(__file__).resolve().parent.parent
PHASE2_CONFIGS = REPO_ROOT / "phase2-fabric" / "configs"
BUILD_DIR = Path(__file__).resolve().parent / "build"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "junos"
DEFAULTS_FILE = Path(__file__).resolve().parent / "vars" / "junos_defaults.yml"

# Templates landed so far. Each entry: (template_path_under_TEMPLATE_DIR,
# stanza_label_for_logs, baseline_stanza_name, baseline_indent).
# baseline_indent is the leading whitespace prefix used to locate a nested
# stanza in the baseline (e.g. lo0 lives at 4-space indent inside
# `interfaces { ... }`).
STANZAS = [
    ("routing_options.j2", "routing-options", "routing-options", ""),
    ("chassis.j2",         "chassis",         "chassis",         ""),
    ("interfaces/loopback.j2", "lo0",         "lo0",             "    "),
]


def extract_stanza(text: str, name: str, indent: str = "") -> str:
    """Pull a Junos stanza by name and indentation level (brace-balanced).

    `indent` is the literal whitespace before the stanza name in the
    baseline file. Pass "" for top-level stanzas, "    " for stanzas
    inside one level of nesting, etc.
    """
    pattern = re.compile(rf"^{re.escape(indent)}{re.escape(name)} \{{", re.MULTILINE)
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


def render_and_diff(task, defaults):
    """Render every landed template and diff against its baseline stanza.

    Returns a multi-line string with one OK/DIFF row per stanza.
    """
    baseline_path = PHASE2_CONFIGS / f"{task.host.name}.conf"
    baseline_text = baseline_path.read_text(encoding="utf-8")

    rows = []
    any_diff = False
    for tmpl, label, stanza_name, indent in STANZAS:
        result = task.run(
            task=template_file,
            template=tmpl,
            path=str(TEMPLATE_DIR),
            defaults=defaults,
        )
        rendered = result.result

        # Persist render output for inspection. One file per (host, stanza).
        out_name = f"{task.host.name}.{label.replace('/', '_')}.conf"
        (BUILD_DIR / out_name).write_text(rendered, encoding="utf-8", newline="\n")

        baseline_stanza = extract_stanza(baseline_text, stanza_name, indent)

        if rendered.strip() == baseline_stanza.strip():
            rows.append(f"  OK    {label}")
            continue

        any_diff = True
        diff = "".join(difflib.unified_diff(
            baseline_stanza.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"baseline/{task.host.name}/{label}",
            tofile=f"rendered/{task.host.name}/{label}",
        ))
        rows.append(f"  DIFF  {label}:\n{diff}")

    header = f"{task.host.name} {'FAIL' if any_diff else 'PASS'}"
    return header + "\n" + "\n".join(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Render + diff vs baseline. Currently the only mode.")
    parser.parse_args()

    BUILD_DIR.mkdir(exist_ok=True)
    defaults = yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8"))

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
    render_result = nr.run(task=render_and_diff, defaults=defaults)
    for host in sorted(render_result):
        msg = render_result[host][0].result
        print(msg)
        if "FAIL" in msg.split("\n", 1)[0]:
            failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
