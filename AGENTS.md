# Agent instructions

Auto-Fill integrates with TileArc (`VK-TECH-LABS/vinayaka`). Railway is the active host. The GCP Cloud Run workflow is paused.

Tharun works on `tharun/<task>`. Sai Teja (GitHub `Gollapally123`) works on `saiteja/<task>`. They work in parallel. Read this file before editing.

## Integration branch

`staging` is the integration branch Railway staging deploys from. `main` is production. Open pull requests against `staging`. Do not push to `main`. Do not push to `staging` except by merging a pull request.

## Handoff, before any edit

1. Read `.handoff/ownership.json` and every `.handoff/*.json` record on open `tharun/*` and `saiteja/*` tasks.
2. Claim the task on your own branch by adding `.handoff/<developer>--<task>.json` (example: `.handoff/tharun--workday-create-account.json`). Required fields are `task`, `developer`, `branch`, `latest_commit`, `pr`, `files_touched`, `tests`, `blockers`, `next_action`, and `updated_at`. The shape is `.handoff/schema.json`.
3. If `files_touched` or the pull request diff includes a path in the ownership catalog, and another developer's open task already lists that path, stop. Do not edit it. Talk to that developer. The `handoff-override` label is only for a case they have agreed.
4. Critical shared files are the TileArc API contract, environment configuration, credential and token code, session persistence, and the human-approval guards. They are listed in `.handoff/ownership.json`.

The Handoff workflow rewrites `files_touched` from the branch diff on pushes to `tharun/*` and `saiteja/*` and on pull requests. It keeps a critical-file claim that is not in the diff yet. It keeps non-empty `tests`, `blockers`, and `next_action`. Clear `tests` when you want the action to refill it. The workflow posts one sticky pull request comment and fails the check on an ownership conflict.

## Branches

- Stay on your branch: `tharun/<task>` or `saiteja/<task>`.
- Never push, force-push, or commit to the other developer's branch, to `main`, or to a branch you do not own.
- Rebase on the latest `staging` before merge. Do not merge a stale branch.
- Resolve conflicts file by file. Never use a blanket ours or theirs (`-X ours`, `-X theirs`, or checking out `--ours` / `--theirs` for the whole tree), especially on the critical files above.

## Safeguards

Do not enable real account creation or final job submission in tests or in product code. `HUMAN_SUBMIT_ONLY` in `src/autofill/safeguards.py` stays true. Tests use local fixtures. Login on an existing session is separate from Submit, Apply, and Create account. There is no flag that turns final submission on.

## Checks

Pull requests to `staging` and `main`, and pushes to `staging`, run lint, typecheck, tests, and a package build (`.github/workflows/ci.yml`). `.github/workflows/gcp-staging-deploy.yml` is manual-only while Railway is the host.

## Staging deploys

A merge into `staging` deploys that commit to Railway `auto-fill-staging` after CI passes on the merge commit. GitHub Actions runs the deploy with a staging-only project token, so no developer needs a Railway account or approval. See `docs/DEPLOY.md`.
