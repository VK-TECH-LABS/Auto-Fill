# Staging deploys

Railway project `tile-arc`, environment `staging`, service `auto-fill-staging`.
Staging is deployed by GitHub Actions. Nobody needs a Railway account, and no
deploy waits for a Railway approval.

## Developer flow

1. Work on your own branch: `tharun/<task>` or `saiteja/<task>` (see `AGENTS.md`).
2. Open a pull request into `staging`. CI runs lint (`ruff`), typecheck (`mypy`),
   the test suite (`pytest`), and the package build. The Handoff workflow checks
   file ownership.
3. When CI is green and the PR is reviewed, merge it in GitHub. Do not push to
   `staging` or `main` directly, and do not force-push.
4. The merge is a push to `staging`. CI runs again on the merge commit. If it
   passes, the `Deploy staging (Railway)` job in `.github/workflows/ci.yml`:
   - checks that the `RAILWAY_STAGING_TOKEN` secret is a project token scoped to
     `tile-arc` / `staging` only, and refuses to run otherwise;
   - checks that the commit is on `staging`, and skips the deploy when a newer
     commit is already live (deploys never go backwards);
   - deploys that exact commit to `auto-fill-staging`
     (`serviceInstanceDeployV2` with `commitSha`), waits until Railway reports
     `SUCCESS`, and checks that the deployment runs the same commit;
   - waits through a short stability window, then checks
     `/health`, `/health/ready` and the TileArc staging `/api/health`.
5. Watch the run under **Actions → CI** on the merge commit. The job summary
   lists the deployment ID and health results.

Deploys of this repo share one queue (`railway-staging-deploy-auto-fill`), so
two merges never deploy at the same time or out of order.

## When a deploy fails

- The job fails and later merges still deploy normally once they pass CI.
- Railway build and deploy logs are uploaded as the artifact
  `railway-staging-deploy-logs-<sha>` (kept 30 days).
- A failed build or a failed Railway health check never replaces the running
  instance, so staging keeps serving the previous commit. If the new instance
  crashes after it went live, the job rolls back to the previous deployment.
- Fix forward with a new PR. To retry the same commit (for example after a
  flaky test), use **Re-run failed jobs** on the CI run.

## Not used for staging

- Railway's own GitHub auto-deploy trigger for `auto-fill-staging` is turned off
  for the staging environment once this job is verified. It needed the pushing
  GitHub account to be linked to a Railway project member.
- `main` never deploys staging. Developer branches never deploy anywhere.
- `.github/workflows/gcp-staging-deploy.yml` stays manual-only.
- Production is not touched by this job; the token cannot reach it.

## Owner notes

- Secret: `RAILWAY_STAGING_TOKEN` (repository Actions secret). It is a Railway
  project token for `tile-arc` / `staging`. Do not share it, print it, or reuse a
  developer's token. To rotate it, create a new staging project token in Railway,
  replace the secret, then delete the old token.
