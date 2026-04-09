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

from tasks.enrich import enrich_from_netbox, derive_login_hash
from tasks.deploy import napalm_deploy

REPO_ROOT = Path(__file__).resolve().parent.parent
# Phase 3 golden-file regression baselines. These are maintained as
# "the last known-good rendered output" - when a template change lands,
# re-run `deploy.py --update-expected` (or copy build/ -> expected/
# manually) and commit both template + expected in the same PR.
#
# NOTE: phase2-fabric/configs/*.conf are separately maintained as the
# clab startup configs (Phase 2 hand-written). They do NOT need to
# match this directory - clab boots devices with the Phase 2 configs,
# then Phase 3 --commit overwrites with canonical rendered output.
EXPECTED_DIR = Path(__file__).resolve().parent / "expected"
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


# Diff normalization. Strips fields that are either device-emitted
# artifacts the templates can't reproduce (`## Last changed`, `version`)
# or salt-randomized opaque blobs (`encrypted-password`).
#
# The regression gate's job is "templates produce structurally identical
# output to baselines". The text content of a SHA-512 crypt blob is
# noise (different salts of the same plaintext yield different bytes),
# not structure. Comparing encrypted-password text adds zero signal -
# any drift there is expected and uninformative.
#
# CRITICAL: This normalization is for the REGRESSION DIFF only. It does
# NOT touch what gets written to disk. The deploy path independently
# scans the rendered file with assert_safe_to_deploy() before any
# NAPALM call to verify every encrypted-password line is a real,
# valid $6$ crypt hash and contains no placeholder/sentinel strings.
# That on-disk guard is the actual safety net for SECRET-DATA fields.
#
# History: an earlier version had this normalization but NO on-disk
# guard. A bug rendered placeholder hashes to disk, the regression
# gate masked them on both sides and reported PASS, NAPALM
# `compare_config` masked SECRET-DATA fields and reported "no diff",
# and --commit then loaded the placeholder onto all 4 devices,
# causing a lab-wide credential lockout. The two-layer design here
# (normalize the gate + grep the file independently) prevents recurrence.
# See feedback_never_normalize_secrets_into_deploy.md.
NORMALIZE_RULES = [
    (re.compile(r'^## Last changed:.*\n', re.MULTILINE), ''),
    (re.compile(r'^version [^;]+;\n', re.MULTILINE), ''),
    (re.compile(r'encrypted-password "[^"]*"'), 'encrypted-password "<HASH>"'),
]


def normalize(text: str) -> str:
    for pattern, repl in NORMALIZE_RULES:
        text = pattern.sub(repl, text)
    return text


# Sentinel strings the deploy guard refuses to push to a device. Any
# rendered config containing one of these is rejected before any NAPALM
# call. The list grows as new templates are added; treat it as the
# "things that mean someone forgot to set an env var" inventory.
DEPLOY_SENTINELS = [
    "PLACEHOLDER",
    "render-time-only",
    "TODO",
    "REPLACE_ME",
    "<HASH>",
]

# Every encrypted-password line MUST match this shape. SHA-512 crypt
# format: $6$<salt>$<86-char-hash>. Anything else (placeholder, plain
# text, malformed) is rejected.
HASH_SHAPE_RE = re.compile(r'encrypted-password "(\$6\$[^$]+\$[A-Za-z0-9./]{86})"')


def assert_safe_to_deploy(rendered: str, host_name: str) -> None:
    """Independent grep guard. Runs BEFORE any NAPALM call.

    Junos `compare_config` masks SECRET-DATA fields (encrypted-password,
    keys, certs) so a placeholder hash will NOT show up in NAPALM diff
    output. The only reliable check is to scan the on-disk rendered
    file ourselves before handing it to NAPALM.
    """
    for sentinel in DEPLOY_SENTINELS:
        if sentinel in rendered:
            raise RuntimeError(
                f"{host_name}: rendered config contains sentinel "
                f"'{sentinel}' - refusing to deploy."
            )
    for line in rendered.splitlines():
        if "encrypted-password" not in line:
            continue
        if not HASH_SHAPE_RE.search(line):
            raise RuntimeError(
                f"{host_name}: encrypted-password line does not match "
                f"valid SHA-512 crypt shape - refusing to deploy.\n"
                f"  line: {line.strip()}"
            )


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
    baseline_path = EXPECTED_DIR / f"{task.host.name}.conf"
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

    baseline_path = EXPECTED_DIR / f"{task.host.name}.conf"
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
    parser.add_argument("--target",
                        help="Restrict deploy to a single host (phased rollout).")
    args = parser.parse_args()
    if not (args.check or args.full or args.dry_run or args.commit):
        args.check = True

    # Clear any stale renders from previous runs. Stale files in
    # build/ can carry obsolete content (e.g. an old PLACEHOLDER hash
    # from a buggy template version) and mislead the deploy guard
    # if it scans the wrong file. Nuke and recreate every run.
    if BUILD_DIR.exists():
        for old in BUILD_DIR.iterdir():
            if old.is_file():
                old.unlink()
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

    # Real, deterministic hash from env plaintext + fixed salt. Hard
    # fails if env is missing (no placeholder fallback - that bug cost
    # us a credential lockout in commit c2c0b42). Both root and admin
    # use the same lab plaintext.
    login_hash = derive_login_hash()
    junos_root_hash = login_hash
    junos_admin_hash = login_hash

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

        # Independent on-disk guard. Junos `compare_config` masks
        # SECRET-DATA fields (encrypted-password etc.), so a placeholder
        # hash silently passes NAPALM dry-run. The only safe check is
        # to grep the rendered file ourselves before any NAPALM call.
        print()
        print("=== Pre-deploy on-disk guard ===")
        guard_failed = False
        for h in sorted(nr.inventory.hosts):
            cfg_path = BUILD_DIR / f"{h}.conf"
            try:
                assert_safe_to_deploy(cfg_path.read_text(encoding="utf-8"), h)
                print(f"  {h} OK")
            except RuntimeError as e:
                print(f"  {e}")
                guard_failed = True
        if guard_failed:
            print("\nABORT: pre-deploy guard rejected at least one rendered config.")
            sys.exit(2)

        print()
        print(f"=== NAPALM {'COMMIT' if args.commit else 'DRY-RUN'} ===")
        print("NOTE: Junos `compare_config` MASKS SECRET-DATA fields. A")
        print("'no diff' result here means 'no diff in non-secret fields'.")
        print("Trust the on-disk guard above for secret-field validation.")
        # Optional phased rollout: commit to one host at a time.
        deploy_runner = nr.filter(name=args.target) if args.target else nr
        if args.target:
            print(f"TARGET: {args.target} (phased rollout)")
        deploy_result = deploy_runner.run(
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
