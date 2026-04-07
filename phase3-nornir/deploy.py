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
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from nornir import InitNornir
from nornir_jinja2.plugins.tasks import template_file

from tasks.enrich import enrich_from_netbox
from tasks.deploy import napalm_deploy

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
    ("system.j2",            "system",            "system",            ""),
    ("routing_options.j2",   "routing-options",   "routing-options",   ""),
    ("chassis.j2",           "chassis",           "chassis",           ""),
    ("interfaces.j2",        "interfaces",        "interfaces",        ""),
    ("forwarding_options.j2","forwarding-options","forwarding-options",""),
    ("policy_options.j2",    "policy-options",    "policy-options",    ""),
    ("routing_instances.j2", "routing-instances", "routing-instances", ""),
    ("protocols.j2",         "protocols",         "protocols",         ""),
]


# Diff normalization. The Phase 2 baselines contain artifacts that are
# either device-emitted (`## Last changed`, `version`) or salted on each
# generation (`encrypted-password`). Templates can't reproduce them
# byte-for-byte, but the surrounding STRUCTURE must still match. Apply
# the same regexes to both sides before comparing - byte-exact compare
# of structure, content of these fields ignored.
NORMALIZE_RULES = [
    (re.compile(r'^## Last changed:.*\n', re.MULTILINE), ''),
    (re.compile(r'^version [^;]+;\n', re.MULTILINE), ''),
    (re.compile(r'encrypted-password "[^"]*"'), 'encrypted-password "<HASH>"'),
]


def normalize(text: str) -> str:
    for pattern, repl in NORMALIZE_RULES:
        text = pattern.sub(repl, text)
    return text


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


def render_and_diff(task, defaults, junos_root_hash, junos_admin_hash, jinja_env):
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
            jinja_env=jinja_env,
            defaults=defaults,
            junos_root_hash=junos_root_hash,
            junos_admin_hash=junos_admin_hash,
        )
        rendered = result.result

        # Persist render output for inspection. One file per (host, stanza).
        out_name = f"{task.host.name}.{label.replace('/', '_')}.conf"
        (BUILD_DIR / out_name).write_text(rendered, encoding="utf-8", newline="\n")

        baseline_stanza = extract_stanza(baseline_text, stanza_name, indent)

        rendered_norm = normalize(rendered).strip()
        baseline_norm = normalize(baseline_stanza).strip()

        if rendered_norm == baseline_norm:
            rows.append(f"  OK    {label}")
            continue

        any_diff = True
        diff = "".join(difflib.unified_diff(
            baseline_norm.splitlines(keepends=True),
            rendered_norm.splitlines(keepends=True),
            fromfile=f"baseline/{task.host.name}/{label}",
            tofile=f"rendered/{task.host.name}/{label}",
        ))
        rows.append(f"  DIFF  {label}:\n{diff}")

    header = f"{task.host.name} {'FAIL' if any_diff else 'PASS'}"
    return header + "\n" + "\n".join(rows)


def render_full_and_diff(task, defaults, junos_root_hash, junos_admin_hash, jinja_env):
    """Render main.j2 (full config) and diff vs the entire baseline file."""
    result = task.run(
        task=template_file,
        template="main.j2",
        path=str(TEMPLATE_DIR),
        jinja_env=jinja_env,
        defaults=defaults,
        junos_root_hash=junos_root_hash,
        junos_admin_hash=junos_admin_hash,
    )
    rendered = result.result

    out_path = BUILD_DIR / f"{task.host.name}.conf"
    out_path.write_text(rendered, encoding="utf-8", newline="\n")

    baseline_path = PHASE2_CONFIGS / f"{task.host.name}.conf"
    baseline_text = baseline_path.read_text(encoding="utf-8")

    rendered_norm = normalize(rendered).strip()
    baseline_norm = normalize(baseline_text).strip()

    if rendered_norm == baseline_norm:
        return f"{task.host.name} PASS  full config byte-exact"

    diff = "".join(difflib.unified_diff(
        baseline_norm.splitlines(keepends=True),
        rendered_norm.splitlines(keepends=True),
        fromfile=f"baseline/{task.host.name}",
        tofile=f"rendered/{task.host.name}",
    ))
    return f"{task.host.name} FAIL\n{diff}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Render each stanza + per-stanza diff vs baseline.")
    parser.add_argument("--full", action="store_true",
                        help="Render full main.j2 and diff against the whole baseline file.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render full + NAPALM compare_config against the live device. No commit.")
    parser.add_argument("--commit", action="store_true",
                        help="Render full + NAPALM load_replace_candidate + commit. Requires --full to pass first.")
    args = parser.parse_args()
    if not (args.check or args.full or args.dry_run or args.commit):
        args.check = True

    BUILD_DIR.mkdir(exist_ok=True)
    defaults = yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8"))

    # Build Jinja env with keep_trailing_newline=True so {% include %}
    # blocks in main.j2 don't lose the closing newline of each partial
    # (nornir-jinja2's default Environment omits this flag, gluing
    # consecutive stanzas together).
    jinja_env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )

    # Junos password hashes are salted SHA-512: same plaintext yields a
    # different ciphertext on every regeneration. They cannot be derived
    # deterministically. Templates render whatever env supplies (real
    # deploys), or a placeholder for --check (the diff normalizer
    # replaces both sides with <HASH> so structure still has to match).
    junos_root_hash = os.environ.get("JUNOS_ROOT_HASH", "$6$PLACEHOLDER$render-time-only")
    junos_admin_hash = os.environ.get("JUNOS_ADMIN_HASH", "$6$PLACEHOLDER$render-time-only")

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

    # NetBoxInventory2 sets host.hostname to primary_ip4 (the loopback,
    # unreachable from outside the fabric) and host.platform to the
    # NetBox platform name ("Junos") rather than the NAPALM driver
    # ("junos"). Both need overriding before NAPALM tasks run. The OOB
    # mgmt IP per device lives in env vars MGMT_<name with - as _>.
    for host in nr.inventory.hosts.values():
        env_key = f"MGMT_{host.name.replace('-', '_')}"
        mgmt = os.environ.get(env_key, "")
        # MGMT_* values are stored CIDR (e.g. 172.16.18.160/24); strip mask.
        if "/" in mgmt:
            mgmt = mgmt.split("/", 1)[0]
        if mgmt:
            host.hostname = mgmt
        host.platform = "junos"
        host.username = os.environ.get("JUNOS_SSH_USER")
        host.password = os.environ.get("JUNOS_SSH_PASSWORD")

    enrich_result = nr.run(task=enrich_from_netbox)
    for host, multi in enrich_result.items():
        print(f"  enrich {host}: {multi[0].result}")

    print()

    failed = False
    # For dry-run/commit we need the full config on disk first.
    use_full = args.full or args.dry_run or args.commit
    task_fn = render_full_and_diff if use_full else render_and_diff
    render_result = nr.run(
        task=task_fn,
        defaults=defaults,
        junos_root_hash=junos_root_hash,
        junos_admin_hash=junos_admin_hash,
        jinja_env=jinja_env,
    )
    for host in sorted(render_result):
        msg = render_result[host][0].result
        print(msg)
        if "FAIL" in msg.split("\n", 1)[0]:
            failed = True

    if failed and (args.dry_run or args.commit):
        print("\nABORT: regression diff vs Phase 2 baseline failed; refusing to deploy.")
        sys.exit(2)

    if args.dry_run or args.commit:
        if not (os.environ.get("JUNOS_SSH_USER") and os.environ.get("JUNOS_SSH_PASSWORD")):
            print("\nABORT: JUNOS_SSH_USER and JUNOS_SSH_PASSWORD must be set in env.")
            sys.exit(2)
        print()
        print(f"=== NAPALM {'COMMIT' if args.commit else 'DRY-RUN'} ===")
        deploy_result = nr.run(
            task=napalm_deploy,
            build_dir=BUILD_DIR,
            commit=args.commit,
        )
        for host in sorted(deploy_result):
            multi = deploy_result[host]
            head = multi[0]
            if head.failed:
                failed = True
                print(f"{host} FAILED: {head.exception}")
            else:
                print(f"{host} {head.result}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
