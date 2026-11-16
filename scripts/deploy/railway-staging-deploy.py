#!/usr/bin/env python3
"""Deploy one exact commit to the Railway *staging* environment.

Called by the `deploy-staging` job in .github/workflows/ci.yml after CI has
passed on a push to `staging`. Uses only the Python standard library.

Required env:
  RAILWAY_TOKEN          Railway PROJECT token scoped to tile-arc/staging
                         (GitHub Actions secret RAILWAY_STAGING_TOKEN). Never printed.
  DEPLOY_SHA             full 40-char commit SHA to deploy
  RAILWAY_PROJECT_ID     expected project id (guard)
  RAILWAY_ENVIRONMENT_ID expected staging environment id (guard)
  DEPLOY_STAGES          services to deploy, stages separated by ';',
                         services within a stage by ','  e.g. "idA,idB;idC"
Optional env:
  HEALTH_CHECKS          space-separated URLs that must return HTTP 200
  HEALTH_SHA_URL         JSON health URL whose .git.sha must equal DEPLOY_SHA
                         and whose .status and .checks.database/.checks.redis
                         must be "healthy"
  DEPLOY_BRANCH          branch the SHA must be reachable from (default staging)
  DEPLOY_TIMEOUT_SECONDS per-stage timeout (default 2400)
  LOG_DIR                where failure logs are written (uploaded as artifact)
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://backboard.railway.com/graphql/v2"
TOKEN = os.environ.get("RAILWAY_TOKEN", "")
SHA = os.environ.get("DEPLOY_SHA", "").strip()
PROJECT = os.environ.get("RAILWAY_PROJECT_ID", "")
ENV = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
STAGES = [
    [s.strip() for s in st.split(",") if s.strip()]
    for st in os.environ.get("DEPLOY_STAGES", "").split(";")
    if st.strip()
]
BRANCH = os.environ.get("DEPLOY_BRANCH", "staging")
TIMEOUT = int(os.environ.get("DEPLOY_TIMEOUT_SECONDS", "2400"))
LOG_DIR = os.environ.get("LOG_DIR", "railway-deploy-logs")
SUMMARY = os.environ.get("GITHUB_STEP_SUMMARY")
TERMINAL_BAD = {"FAILED", "CRASHED", "REMOVED", "SKIPPED", "NEEDS_APPROVAL"}
lines: list[str] = []


def log(msg):
    print(msg, flush=True)
    lines.append(msg)


def die(msg, code=1):
    log(f"::error::{msg}")
    write_summary(ok=False)
    sys.exit(code)


def write_summary(ok):
    if SUMMARY:
        with open(SUMMARY, "a") as f:
            f.write(f"## Railway staging deploy {'succeeded' if ok else 'FAILED'}\n\n")
            f.write(f"Commit `{SHA}`\n\n```\n" + "\n".join(lines[-80:]) + "\n```\n")


def gql(query, variables=None, retries=4):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            API,
            data=body,
            headers={
                "Project-Access-Token": TOKEN,
                "Content-Type": "application/json",
                "User-Agent": "tilearc-staging-deploy/1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.load(r)
            if out.get("errors"):
                raise RuntimeError("; ".join(e.get("message", "?") for e in out["errors"]))
            return out["data"]
        except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
            if attempt == retries - 1:
                raise
            log(f"railway api retry {attempt + 1}: {e}")
            time.sleep(5 * (attempt + 1))


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def is_ancestor(a, b):
    return git("merge-base", "--is-ancestor", a, b).returncode == 0


def latest(service, status=None):
    q = """query($p:String!,$e:String!,$s:String!,$st:DeploymentStatusInput){
      deployments(first:1,input:{projectId:$p,environmentId:$e,serviceId:$s,status:$st}){
        edges{node{id status meta canRollback}}}}"""
    v = {"p": PROJECT, "e": ENV, "s": service, "st": {"in": [status]} if status else None}
    edges = gql(q, v)["deployments"]["edges"]
    return edges[0]["node"] if edges else None


def deployment(dep_id):
    return gql("query($id:String!){deployment(id:$id){id status meta canRollback}}", {"id": dep_id})["deployment"]


def save_logs(service, dep_id):
    os.makedirs(LOG_DIR, exist_ok=True)
    for kind in ("buildLogs", "deploymentLogs"):
        try:
            query = "query($d:String!){" + kind + "(deploymentId:$d,limit:500){timestamp message}}"
            rows = gql(query, {"d": dep_id})[kind]
            path = os.path.join(LOG_DIR, f"{service}-{dep_id}-{kind}.log")
            with open(path, "w") as f:
                f.write("\n".join(f"{r.get('timestamp', '')} {r.get('message', '')}" for r in rows))
            log(f"saved {kind} for {dep_id} ({len(rows)} lines) -> {path}")
            for r in rows[-25:]:
                print(f"  | {r.get('message', '')[:300]}")
        except Exception as e:  # logs are best effort
            log(f"could not fetch {kind} for {dep_id}: {e}")


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tilearc-staging-deploy/1", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main():
    if not TOKEN:
        die("RAILWAY_TOKEN is empty: set the RAILWAY_STAGING_TOKEN Actions secret (owner action).")
    if len(SHA) != 40 or not STAGES or not PROJECT or not ENV:
        die("DEPLOY_SHA (40 chars), DEPLOY_STAGES, RAILWAY_PROJECT_ID and RAILWAY_ENVIRONMENT_ID are required.")

    # 1. The token must be a project token for exactly tile-arc/staging.
    tok = gql("{projectToken{projectId environmentId}}")["projectToken"]
    if tok["projectId"] != PROJECT or tok["environmentId"] != ENV:
        die("RAILWAY_STAGING_TOKEN is not scoped to the expected project/staging environment; refusing to deploy.")
    log(f"token scope OK (project {PROJECT[:8]}, environment {ENV[:8]} = staging)")

    # 2. The commit must be on the staging branch.
    git("fetch", "--no-tags", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
    if not is_ancestor(SHA, f"origin/{BRANCH}"):
        die(f"{SHA} is not reachable from origin/{BRANCH}; refusing to deploy.")

    # 3. Never move a service backwards: skip if a newer commit is already live.
    all_services = [s for st in STAGES for s in st]
    current = {}
    for s in all_services:
        d = latest(s, "SUCCESS")
        current[s] = d
        live = (d or {}).get("meta", {}).get("commitHash")
        log(f"service {s[:8]}: live {live[:12] if live else 'none'} ({d['id'] if d else '-'})")
        if live and live != SHA and is_ancestor(SHA, live):
            log(
                f"::notice::{SHA[:12]} is older than live {live[:12]} on {s[:8]}; a newer deploy already ran. Skipping."
            )
            write_summary(ok=True)
            return

    # 4. Deploy stage by stage; stop at the first failure.
    for n, stage in enumerate(STAGES, 1):
        started = {}
        for s in stage:
            live = (current[s] or {}).get("meta", {}).get("commitHash")
            if live == SHA and (current[s] or {}).get("status") == "SUCCESS":
                log(f"stage {n}: {s[:8]} already runs {SHA[:12]}; not redeploying")
                continue
            deploy = (
                "mutation($s:String!,$e:String!,$c:String)"
                "{serviceInstanceDeployV2(serviceId:$s,environmentId:$e,commitSha:$c)}"
            )
            dep_id = gql(deploy, {"s": s, "e": ENV, "c": SHA})["serviceInstanceDeployV2"]
            started[s] = dep_id
            log(f"stage {n}: deploying {SHA[:12]} to {s[:8]} -> deployment {dep_id}")
        deadline = time.time() + TIMEOUT
        pending = dict(started)
        while pending:
            if time.time() > deadline:
                for s, d in pending.items():
                    save_logs(s, d)
                die(f"stage {n} timed out after {TIMEOUT}s waiting for {list(pending)}")
            time.sleep(15)
            for s, d in list(pending.items()):
                dep = deployment(d)
                st = dep["status"]
                if st == "SUCCESS":
                    got = (dep.get("meta") or {}).get("commitHash")
                    if got != SHA:
                        die(f"{s[:8]} deployment {d} is SUCCESS but runs {got}, expected {SHA}")
                    log(f"stage {n}: {s[:8]} SUCCESS at {SHA[:12]} ({d})")
                    pending.pop(s)
                elif st in TERMINAL_BAD:
                    save_logs(s, d)
                    prev = current.get(s)
                    log(f"stage {n}: {s[:8]} ended {st} ({d}); later stages NOT deployed")
                    if st == "CRASHED" and prev and prev.get("id") != d:
                        # A crash after go-live replaced the old instance: roll back to the
                        # previous SUCCESS deployment (same image, no rebuild). Build/health
                        # failures (FAILED) never replace the running instance, so no rollback.
                        try:
                            gql("mutation($id:String!){deploymentRollback(id:$id)}", {"id": prev["id"]})
                            log(f"rolled {s[:8]} back to previous deployment {prev['id']}")
                        except Exception as e:
                            log(f"rollback of {s[:8]} failed: {e}")
                    die(f"deploy of {SHA[:12]} failed on service {s[:8]} with status {st}")

    # 5. Stability window: services must still be SUCCESS (catches crash loops).
    time.sleep(int(os.environ.get("STABILITY_SECONDS", "60")))
    for s in all_services:
        d = latest(s)
        if not d or d["status"] != "SUCCESS" or d["meta"].get("commitHash") != SHA:
            if d:
                save_logs(s, d["id"])
            die(f"{s[:8]} not stable on {SHA[:12]}: {d and d['status']} {d and d['meta'].get('commitHash')}")
    log("all services stable on " + SHA[:12])

    # 6. Health checks.
    deadline = time.time() + 300
    for url in os.environ.get("HEALTH_CHECKS", "").split():
        while True:
            try:
                code, _ = http_get(url)
                if code == 200:
                    log(f"health OK {url}")
                    break
            except Exception as e:
                code = str(e)
            if time.time() > deadline:
                die(f"health check failed {url}: {code}")
            time.sleep(10)
    url = os.environ.get("HEALTH_SHA_URL")
    if url:
        while True:
            problem = None
            try:
                _, body = http_get(url)
                h = json.loads(body)
                live = (h.get("git") or {}).get("sha")
                checks = h.get("checks") or {}

                def state(v):
                    return v.get("status") if isinstance(v, dict) else v

                bad = [k for k in ("database", "redis") if state(checks.get(k)) != "healthy"]
                if h.get("status") != "healthy":
                    bad.append("status=" + str(h.get("status")))
                if live != SHA:
                    problem = f"serving {live}"
                elif bad:
                    problem = f"unhealthy dependencies: {bad}"
            except Exception as e:
                problem = str(e)
            if not problem:
                log(f"health OK {url}: git.sha={SHA[:12]}, database+redis healthy")
                break
            if time.time() > deadline:
                die(f"{url}: {problem}")
            time.sleep(10)
    write_summary(ok=True)


if __name__ == "__main__":
    main()
