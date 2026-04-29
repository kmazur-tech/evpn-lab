# Phase 6 - GitHub Actions CI/CD Pipeline

PR-time validation and (eventually) lab deployment for the EVPN-VXLAN fabric, glued onto the work done in phases 1-5. Every change to NetBox data, templates, or automation code now runs through render -> diff -> guard -> Batfish in a sandboxed CI runner before it can merge.

## Layout

GitHub Actions workflow files **must** live at `.github/workflows/` in the repo root - that's a hard GitHub requirement, not a project preference. Everything else (docs, helper scripts, cassette refresh tooling) lives here under `phase6-cicd/`:

```
.github/                                <- repo root, required by GitHub
  workflows/
    fabric-ci.yml                       PR-time CI (this phase)
    fabric-deploy.yml                   Lab deploy workflow (Phase 6.3, planned)
  yamllint.yml                          yamllint config used by the lint job
  dependabot.yml                        Weekly Action SHA updates

phase6-cicd/                            this directory
  README.md                             this file
  scripts/
    refresh-netbox-cassettes.py         Re-records vcrpy cassettes from live NetBox
```

## Status

Stage | Scope | State
---|---|---
6.1 | Test framework extensions (golden-file render, vcrpy enrich, mocked NAPALM, pytest-nornir) | Done. 154 phase-3 tests, 87% coverage, all offline.
6.2 | PR-time `fabric-ci.yml`: lint + unit matrix + render-pipeline + batfish | Done. Runs on every PR and push to `main`.
6.3 | Deploy `fabric-deploy.yml`: containerlab up, commit-confirmed, smoke gate, suzieq drift, teardown | Planned. Self-hosted runner, manual `workflow_dispatch`.
6.4 | Documentation, status badge, `phase6-cicd/CI.md` operations runbook | Partial - this README covers what's live; runbook follows when deploy lands.

## fabric-ci.yml - PR-time workflow

Triggered on every PR and push to `main`. Runs on GitHub-hosted `ubuntu-latest`, not the self-hosted lab runner - the public repo means PR contributors' code runs in CI, and we want that on GitHub's sandbox, not on infrastructure that has SSH keys to the lab. PR-time CI doesn't need lab access (vcrpy cassettes replay NetBox offline, NAPALM is mocked, Batfish unit tests use captured fixtures), so the GitHub-hosted runner is sufficient.

Jobs:

| Job | What it does | Hard-fail? |
|---|---|---|
| `lint` | yamllint + ruff + j2lint across all phase dirs | Yes |
| `unit (phase3-nornir)` | Full pytest suite, 154 tests, coverage gate at 85% | Yes |
| `unit (phase4-batfish)` | pybatfish unit tests, 60 tests | Warn-only* |
| `unit (phase5-suzieq)` | Drift harness suite, 362 tests | Warn-only* |
| `render + diff + guard` | Render templates from cassettes, byte-equality vs `expected/`, deploy-guard scan | Yes |
| `batfish` | pybatfish unit tests with captured fixtures | Yes |

\* Phase 4 and Phase 5 unit suites start as `continue-on-error: true` and transition to hard-fail after **5 consecutive green PRs OR 14 calendar days** from when this workflow first lands, whichever comes first. The transition date will be recorded here when it happens.

### Workflow security baseline

- **Workflow-level `permissions: {}`** - empty by default; each job declares its own minimal scope (`contents: read`, plus `pull-requests: write` only on the batfish job for future PR comments).
- **All Actions pinned to full commit SHAs** - tag-only refs (`@v4`) are blocked by the repo's "Require actions to be pinned to a full-length commit SHA" setting. Dependabot opens PRs to bump SHAs weekly.
- **Allowed actions allowlist** - repo settings restrict to `actions/*, github/*` plus anything under the `kmazur-tech` org. Marketplace-verified-creator shortcut is off.
- **Fork PR approval gate** - "Require approval for all external contributors" set in repo Actions settings; a maintainer has to click approve before any fork PR can spin up a runner.
- **Concurrency cancel-in-progress** - new push to a branch cancels the still-running CI for the previous push on that branch.

### Caching and artifacts

- pip cache keyed per-phase by hash of `requirements*.txt`. First run is a cache miss (one warning per job, expected); subsequent runs reuse the cache and skip the install delay.
- Phase 3 coverage HTML uploaded as `coverage-phase3-nornir`, 14-day retention.
- Rendered configs uploaded as `rendered-configs` from the render job, 14-day retention - lets you grab the produced configs from a failed PR without re-running the full pipeline.

## vcrpy cassettes

The `render + diff + guard` job runs `enrich_from_netbox()` against pre-recorded HTTP cassettes instead of a live NetBox. Cassettes live in [`phase3-nornir/tests/cassettes/`](../phase3-nornir/tests/cassettes/), one per device. They are checked into git so CI is fully offline.

Cassettes need a refresh whenever the NetBox schema changes (e.g. NetBox version bump) or when the lab data model changes (new devices, new VRFs). Refresh procedure:

```bash
# From a host that can reach the lab NetBox
cd phase3-nornir
source ../../evpn-lab-env/env.sh   # NETBOX_URL + NETBOX_TOKEN
python ../phase6-cicd/scripts/refresh-netbox-cassettes.py
```

The refresh script automatically:
- Replaces the real NetBox host with the placeholder `netbox.lab.local` (no infrastructure IPs leak into the repo).
- Strips `Authorization` headers.
- Writes one cassette per device into `phase3-nornir/tests/cassettes/`.

The CI render job warns when cassettes are older than 30 days. The warning doesn't block, but it's a hint that the snapshot is drifting from production NetBox.

## Production readiness checklist

This lab is a showcase, not a production deployment. The CI is designed to be honest about that gap. Before this pipeline could safely promote real device changes in a production environment:

- [ ] **Deploy authorization gate.** GitHub Environment `lab-deploy` with required reviewers (a human approves every device-touching deploy). Lab uses single-operator dispatch; production must not.
- [ ] **Self-hosted runner hardening.** Ephemeral / JIT mode (each job in a fresh environment), dedicated runner group locked to this repo, network egress firewall, secret rotation. The runner must be treated as untrusted after each job; rebuild-and-re-register has to be a documented one-step procedure.
- [ ] **Branch protection.** Required status checks on `main` (`lint`, all `unit (...)`, `render-pipeline`, `batfish`), no force-push, CODEOWNERS for `templates/`, `phase3-nornir/expected/`, `.github/workflows/`. Required N approving reviews with explicit dismiss-review policy. CODEOWNER review required on workflow / deploy paths.
- [ ] **Secret rotation + short-lived credentials.** Lab uses long-lived env-file secrets; production must rotate `JUNOS_SSH_PASSWORD` and `NETBOX_TOKEN` on a schedule. Prefer OIDC where the provider supports it to eliminate static secrets entirely.
- [ ] **Deploy failure alerting.** Lab is content with a `github-script` commit comment on `failure()`. Production needs Slack/email/PagerDuty with an explicit escalation path - a failed deploy means a Junos commit-confirmed window is counting down to auto-rollback, and the operator must know immediately.
- [ ] **Artifact retention review.** Rendered configs and Batfish output may contain operational data. 14-day retention is fine for the lab; review for compliance in a real environment.
- [ ] **Supply-chain controls beyond SHA pinning.** Dependency review, secret scanning, SAST. Worth enabling on the public repo regardless.

## Critical post-implementation test

Once `fabric-deploy.yml` lands and is wired up to the lab, the test that validates the entire safety architecture is **intentionally failing the smoke gate** (e.g. break ARP on one host, or stop a leaf container before smoke runs) and confirming that:

1. The smoke job fails.
2. `napalm_confirm_commit` is **not** called.
3. The Junos `commit confirmed 5` timer fires after 5 minutes and rolls back to the pre-deploy config.
4. SSH access remains intact throughout.

Without that end-to-end verification, the rest of the pipeline is theatre. The whole reason this is built around commit-confirmed is to prove the auto-rollback works on the real device, not just in a unit test.
