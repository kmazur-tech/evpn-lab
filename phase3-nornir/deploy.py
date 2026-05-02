#!/usr/bin/env python3
"""Phase 3 entry point: NetBox -> Nornir -> render -> guard -> deploy.

Modes:
  --check               Per-stanza render + byte-diff vs phase3-nornir/expected/.
                        No device contact.
  --full                Render full main.j2 + byte-diff the whole config vs
                        phase3-nornir/expected/<host>.conf.
  --dry-run             Implies --full + on-disk deploy guard + NAPALM
                        compare_config against the live device. No commit.
  --commit              Implies --full + on-disk guard + NAPALM
                        load_replace_candidate + plain commit. Pair with
                        --commit-message <marker> in CI so a later
                        --rollback-marker run can find this commit.
  --liveness-gate       Inner safety net for --commit: do `commit confirmed N`
                        with revert_in=LIVENESS_REVERT_IN_SECONDS, sleep
                        LIVENESS_WAIT_SECONDS, run liveness on every host,
                        and only then call napalm_confirm_commit. If any
                        host fails liveness, exit nonzero and let Junos
                        auto-rollback at the deadline.
  --rollback-marker M   Walks each device's commit history for an entry
                        whose log == M, then rolls back to the state
                        BEFORE that commit and commits the rollback.
                        Used by CI when smoke or drift fails after a
                        flagged --commit.

Per-stanza checks (--check) are driven by the STANZAS table below;
adding a template = one row. The regression baselines live in
phase3-nornir/expected/, NOT phase2-fabric/configs/ (those are the
clab startup configs - see README "What about phase2-fabric/configs?").
"""

import argparse
import difflib
import os
import re
import sys
import time
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from nornir import InitNornir
from nornir.core.plugins.inventory import TransformFunctionRegister
from nornir_jinja2.plugins.tasks import template_file

from tasks.transform import fabric_inventory_transform

# Register the transform function with Nornir's plugin system before
# InitNornir runs. Nornir 3.x looks up transform_function via the
# registry, not by dotted-path import.
TransformFunctionRegister.register("fabric_inventory_transform", fabric_inventory_transform)

from nornir_napalm.plugins.tasks import napalm_confirm_commit

from tasks.enrich import enrich_from_netbox, derive_login_hash
from tasks.deploy import (
    LIVENESS_REVERT_IN_SECONDS,
    LIVENESS_WAIT_SECONDS,
    liveness_check,
    napalm_deploy,
    restore_from_marker,
)
from tasks.backup import pre_commit_backup

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


# Diff normalization. Strips device-emitted artifacts the templates
# can't reproduce: `## Last changed` (device timestamp), `version`
# (device-emitted), and `encrypted-password "..."` content (which uses
# the random salt the device generated when it first hashed the
# plaintext, vs our deterministic $6$evpnlab1$ render-time hash).
#
# This normalization is for the REGRESSION DIFF only. It does NOT
# touch what gets written to disk. Templates render real values; the
# golden-file comparison just ignores the salt content because the
# bytes are noise from a structural-equivalence perspective.
#
# History: an earlier version of deploy.py had a bug where the
# operator-visible "diff" output read `out.result` from the
# napalm_configure return value, but napalm_configure returns the
# diff in `.diff`, not `.result`. So `out.result` was always None and
# `out.result or ""` always became empty - every commit printed "no
# diff" regardless of what NAPALM was actually doing internally.
# Combined with a separate render-time bug that produced placeholder
# hashes, the lab got the placeholder committed onto all 4 devices
# and SSH locked everyone out. The on-disk deploy guard
# (assert_safe_to_deploy below) is the postmortem fix that prevents
# the bad-bytes class of bug at the rendered-file layer, BEFORE any
# NAPALM call. See feedback_never_normalize_secrets_into_deploy.md.
NORMALIZE_RULES = [
    (re.compile(r'^## Last changed:.*\n', re.MULTILINE), ''),
    (re.compile(r'^version [^;]+;\n', re.MULTILINE), ''),
    (re.compile(r'encrypted-password "[^"]*"'), 'encrypted-password "<HASH>"'),
]


def normalize(text: str) -> str:
    for pattern, repl in NORMALIZE_RULES:
        text = pattern.sub(repl, text)
    return text


# Sentinel return code for "the validate.py script does not exist".
# Distinct from any real exit code so tests and main() can branch on
# it. Negative numbers are not valid POSIX exit codes so this won't
# collide with subprocess.call() return values.
_BATFISH_SCRIPT_MISSING = -1


def run_batfish_validation(script_path, build_dir, runner=None):
    """Invoke Phase 4 validate.py against `build_dir`. Returns the
    subprocess exit code, or _BATFISH_SCRIPT_MISSING if the script
    doesn't exist on disk.

    `runner` is a dependency injection seam for tests - in production
    it defaults to subprocess.call. Tests pass a fake that records the
    args and returns a canned exit code so the helper can be exercised
    without actually running validate.py or needing a Batfish container.
    """
    if not script_path.exists():
        print(f"  WARN: {script_path} not found, skipping --validate")
        return _BATFISH_SCRIPT_MISSING
    if runner is None:
        import subprocess
        runner = subprocess.call
    return runner([sys.executable, str(script_path), "--snapshot", str(build_dir)])


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

    Catches placeholder/sentinel strings and malformed encrypted-password
    shapes at the rendered-file layer, before the bytes ever reach
    NAPALM. NAPALM `compare_config` is honest about secret fields - it
    DOES show encrypted-password changes in its diff output - but by
    the time NAPALM sees the candidate, the bad bytes have already
    been written to disk by the renderer. The point of this guard is
    to fail fast at render time so a deploy never starts with bad
    bytes in the first place. See feedback_never_normalize_secrets_into_deploy.md.
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
                        help="Render full + NAPALM load_replace_candidate + commit. "
                             "Requires --full to pass first.")
    parser.add_argument("--commit-message",
                        help="With --commit: set the Junos commit comment (log) "
                             "to this string. Used by CI as a unique marker so a "
                             "later --rollback-marker run can locate this commit "
                             "in `show system commit` and revert to its predecessor.")
    parser.add_argument("--liveness-gate", action="store_true",
                        help="With --commit: enable the inner safety net. The "
                             f"commit becomes `commit confirmed "
                             f"{LIVENESS_REVERT_IN_SECONDS}`; deploy.py then "
                             f"waits {LIVENESS_WAIT_SECONDS}s, runs liveness on "
                             "every host, and calls napalm_confirm_commit. If any "
                             "host is unreachable, the confirm is skipped and "
                             "Junos auto-rolls back at the deadline. CI uses this "
                             "to catch mgmt-plane breakage before smoke runs.")
    parser.add_argument("--rollback-marker",
                        help="Skip render / guard / deploy. For each host, walk "
                             "`show system commit`, find the entry whose log "
                             "matches this string, and roll back to the state "
                             "BEFORE that commit. Used by CI when smoke or drift "
                             "fails after a flagged --commit.")
    parser.add_argument("--target",
                        help="Restrict deploy to a single host (phased rollout).")
    parser.add_argument("--validate", action="store_true",
                        help="Phase 4: invoke phase4-batfish/validate.py against build/ "
                             "after render. Requires the Batfish container running on "
                             "netdevops-srv (see phase4-batfish/README.md). Off by default.")
    args = parser.parse_args()
    if args.rollback_marker and (args.check or args.full or args.dry_run or args.commit):
        parser.error("--rollback-marker cannot be combined with --check, --full, "
                     "--dry-run, or --commit (it walks commit history and reverts; "
                     "it does not render or deploy)")
    if args.commit_message and not args.commit:
        parser.error("--commit-message only makes sense with --commit")
    if args.liveness_gate and not args.commit:
        parser.error("--liveness-gate only makes sense with --commit")
    if not (args.check or args.full or args.dry_run or args.commit or args.rollback_marker):
        args.check = True

    # build/ must exist before any InitNornir call -- nornir.yml sets
    # log_file: build/nornir.log and Python's logging handler opens
    # the file at config-time, so a missing directory crashes Nornir
    # before the rest of the script runs. Create unconditionally.
    BUILD_DIR.mkdir(exist_ok=True)

    # ----- --rollback-marker short circuit -----
    # Used by CI when smoke or drift fails after a flagged --commit. We
    # do NOT render or guard - those ran before the original commit.
    # restore_from_marker walks `show system commit` per device, locates
    # the entry whose log matches the marker, and rolls back to the
    # state BEFORE that commit.
    if args.rollback_marker:
        if not (os.environ.get("JUNOS_SSH_USER") and os.environ.get("JUNOS_SSH_PASSWORD")):
            print("\nABORT: JUNOS_SSH_USER and JUNOS_SSH_PASSWORD must be set in env.")
            sys.exit(2)
        nr = InitNornir(
            config_file=str(Path(__file__).resolve().parent / "nornir.yml"),
            inventory={
                "options": {
                    "nb_url": os.environ["NETBOX_URL"],
                    "nb_token": os.environ["NETBOX_TOKEN"],
                    "ssl_verify": False,
                    "flatten_custom_fields": True,
                    "filter_parameters": {
                        "site": "dc1",
                        "status": "active",
                        "platform": "junos",
                    },
                },
            },
        )
        deploy_runner = nr.filter(name=args.target) if args.target else nr
        if args.target:
            print(f"TARGET: {args.target} (phased rollback)")
        print(f"=== Rollback to state before marker {args.rollback_marker!r} ===")
        rb_result = deploy_runner.run(task=restore_from_marker, marker=args.rollback_marker)
        rb_failed = False
        for host in sorted(rb_result):
            head = rb_result[host][0]
            if head.failed:
                rb_failed = True
                print(f"  {host} ROLLBACK FAILED: {head.exception}")
            else:
                print(f"  {host} {head.result}")
        sys.exit(1 if rb_failed else 0)

    # Clear any stale renders from previous runs. Stale files in
    # build/ can carry obsolete content (e.g. an old PLACEHOLDER hash
    # from a buggy template version) and mislead the deploy guard
    # if it scans the wrong file. The directory itself is created
    # before the --rollback-marker short circuit above; just wipe files.
    for old in BUILD_DIR.iterdir():
        if old.is_file():
            old.unlink()
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
    #
    # TODO Phase 8 (CIS/PCI-DSS hardening): split into separate
    # JUNOS_ROOT_PASSWORD / JUNOS_ADMIN_PASSWORD env vars and derive
    # two distinct hashes. CIS Junos benchmark requires unique
    # credentials per account; this single-hash shortcut is a Phase 3
    # lab simplification. The system.j2 template already takes
    # `junos_root_hash` and `junos_admin_hash` as separate variables,
    # so the only change needed is to wire two `derive_login_hash`
    # calls (one per env var) here in deploy.py.
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
                "flatten_custom_fields": True,
                "filter_parameters": {
                    "site": "dc1",
                    "status": "active",
                    "platform": "junos",
                },
            },
        },
    )
    # Inventory mutation (mgmt IP override, NAPALM driver, SSH creds) is
    # done by the transform_function wired in nornir.yml ->
    # tasks.transform.fabric_inventory_transform. Idiomatic Nornir.

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
        print("\nABORT: regression diff vs phase3-nornir/expected/ failed; refusing to deploy.")
        sys.exit(2)

    # Phase 4 Batfish validation, opt-in via --validate. Runs AFTER
    # render (so build/ has fresh configs) and BEFORE any NAPALM
    # contact. Aborts the deploy chain if Batfish reports any failure.
    if args.validate and not failed:
        print()
        print("=== Phase 4 Batfish validation ===")
        rc = run_batfish_validation(
            REPO_ROOT / "phase4-batfish" / "validate.py",
            BUILD_DIR,
        )
        if rc == _BATFISH_SCRIPT_MISSING:
            pass  # warning already printed by helper
        elif rc != 0:
            print(f"\nABORT: Batfish validation failed (exit {rc}); refusing to deploy.")
            if args.dry_run or args.commit:
                sys.exit(2)
            failed = True

    if args.dry_run or args.commit:
        if not (os.environ.get("JUNOS_SSH_USER") and os.environ.get("JUNOS_SSH_PASSWORD")):
            print("\nABORT: JUNOS_SSH_USER and JUNOS_SSH_PASSWORD must be set in env.")
            sys.exit(2)

        # Independent on-disk guard - catches sentinel/placeholder
        # strings and malformed encrypted-password shapes at the
        # rendered-file layer, before the bytes ever reach NAPALM.
        # Defense in depth: even if a render bug ever produces a
        # placeholder again, this layer rejects it before NAPALM is
        # asked to compare or commit anything.
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

        # Optional phased rollout: commit to one host at a time.
        deploy_runner = nr.filter(name=args.target) if args.target else nr
        if args.target:
            print(f"\nTARGET: {args.target} (phased rollout)")

        # Pre-change snapshot. Cheap insurance: if a deploy breaks
        # something AND the auto-rollback also fails, this is the
        # known-good config to manually restore.
        print()
        print("=== Pre-change backup ===")
        backup_result = deploy_runner.run(task=pre_commit_backup, build_dir=BUILD_DIR)
        for host in sorted(backup_result):
            head = backup_result[host][0]
            if head.failed:
                print(f"  {host} FAILED: {head.exception}")
                failed = True
            else:
                print(f"  {host} {head.result}")
        if failed:
            print("\nABORT: pre-change backup failed; refusing to deploy.")
            sys.exit(2)

        print()
        print(f"=== NAPALM {'COMMIT' if args.commit else 'DRY-RUN'} ===")
        if args.commit and args.commit_message:
            print(f"  commit marker: {args.commit_message!r}")
        if args.commit and args.liveness_gate:
            print(f"  inner gate: commit confirmed {LIVENESS_REVERT_IN_SECONDS}, "
                  f"liveness wait {LIVENESS_WAIT_SECONDS}s")
        deploy_result = deploy_runner.run(
            task=napalm_deploy,
            build_dir=BUILD_DIR,
            commit=args.commit,
            commit_message=args.commit_message,
            revert_in=LIVENESS_REVERT_IN_SECONDS if (args.commit and args.liveness_gate) else None,
        )
        for host in sorted(deploy_result):
            multi = deploy_result[host]
            head = multi[0]
            if head.failed:
                failed = True
                print(f"{host} FAILED: {head.exception}")
            else:
                print(f"{host} {head.result}")

        # Inner safety net: wait for the commit to settle, prove SSH still
        # works on every host, then confirm. If the new config broke the
        # mgmt plane (placeholder hash, mgmt VRF misconfig, etc.) liveness
        # fails on that host, we skip confirm, and Junos auto-rolls back
        # at the revert_in deadline.
        if args.commit and args.liveness_gate and not failed:
            print()
            print(f"=== Inner gate: sleep {LIVENESS_WAIT_SECONDS}s, then liveness ===")
            time.sleep(LIVENESS_WAIT_SECONDS)
            live_result = deploy_runner.run(task=liveness_check)
            live_failed = []
            for host in sorted(live_result):
                head = live_result[host][0]
                if head.failed:
                    print(f"  {host} LIVENESS FAILED: {head.exception}")
                    live_failed.append(host)
                else:
                    print(f"  {host} {head.result}")
            if live_failed:
                print()
                print(f"LIVENESS FAILED on: {', '.join(live_failed)}")
                print("NOT confirming commit. Junos will auto-rollback at the "
                      f"revert_in={LIVENESS_REVERT_IN_SECONDS}s deadline.")
                failed = True
            else:
                print()
                print("=== Inner gate: confirm commit (clear rollback timer) ===")
                confirm_result = deploy_runner.run(task=napalm_confirm_commit)
                for host in sorted(confirm_result):
                    head = confirm_result[host][0]
                    if head.failed:
                        failed = True
                        print(f"  {host} CONFIRM FAILED: {head.exception}")
                    else:
                        print(f"  {host} confirmed")

        if args.commit and args.commit_message and not failed:
            print()
            print(f"Marker {args.commit_message!r} now in `show system commit` on each "
                  f"device. To revert: deploy.py --rollback-marker '{args.commit_message}'")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
