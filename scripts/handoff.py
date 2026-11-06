"""Handoff records for parallel Auto-Fill work on tharun/* and saiteja/* branches.

The pure helpers are covered by tests. Commands that talk to GitHub read
``GITHUB_TOKEN`` and ``GITHUB_REPOSITORY`` from the environment. This script
does not start the Auto-Fill service, create accounts, or submit applications.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

RECORD_KEYS = (
    "task",
    "developer",
    "branch",
    "latest_commit",
    "pr",
    "files_touched",
    "tests",
    "blockers",
    "next_action",
    "updated_at",
)
HUMAN_FIELDS = ("tests", "blockers", "next_action")
SKIP_RECORD_NAMES = frozenset({"ownership.json", "schema.json"})
HANDOFF_PREFIX = ".handoff/"
STICKY_MARKER = "<!-- handoff-sticky -->"
STATUS_CONTEXT = "Handoff / ownership"
PROTECTED_BRANCHES = frozenset({"main", "staging"})
DEFAULT_PREFIXES = {"tharun": "tharun/", "saiteja": "saiteja/"}
API_ROOT = "https://api.github.com"


class GitHubError(RuntimeError):
    """GitHub API response that is not a success."""

    def __init__(self, status: int, payload: object):
        super().__init__(f"GitHub API {status}: {payload}")
        self.status = status
        self.payload = payload


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_path(path: str) -> str:
    """Normalize a repo path. Only a literal ``./`` prefix is removed."""
    cleaned = path.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned[1:] if cleaned.startswith("/") else cleaned


def developer_prefixes(ownership: dict) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for developer in ownership.get("developers") or []:
        if not isinstance(developer, dict):
            continue
        ident = str(developer.get("id") or "")
        prefix = str(developer.get("branch_prefix") or "")
        if ident and prefix:
            prefixes[ident] = prefix
    return prefixes or dict(DEFAULT_PREFIXES)


def catalog_paths(ownership: dict) -> set[str]:
    paths: set[str] = set()
    for item in ownership.get("files") or []:
        if isinstance(item, dict) and item.get("path"):
            paths.add(norm_path(str(item["path"])))
    return paths


def parse_branch(branch: str, prefixes: dict[str, str]) -> tuple[str, str] | None:
    for developer, prefix in prefixes.items():
        if branch.startswith(prefix) and len(branch) > len(prefix):
            return developer, branch[len(prefix) :]
    return None


def record_filename(developer: str, task: str) -> str:
    safe_task = task.replace("/", "--").replace("\\", "--")
    return f"{developer}--{safe_task}.json"


def load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must be a JSON object")
    return payload


def load_ownership(path: Path) -> dict:
    if not path.is_file():
        return {
            "integration_branch": "staging",
            "production_branch": "main",
            "override_label": "handoff-override",
            "developers": [
                {"id": "tharun", "branch_prefix": "tharun/"},
                {"id": "saiteja", "branch_prefix": "saiteja/", "github_login": "Gollapally123"},
            ],
            "files": [],
        }
    return load_json(path)


def dump_record(path: Path, record: dict) -> None:
    ordered = {key: record[key] for key in RECORD_KEYS}
    for key, value in record.items():
        if key not in ordered:
            ordered[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")


def infer_tests(files: list[str]) -> str:
    tests = [
        path
        for path in files
        if path.startswith("tests/") and path.endswith(".py") and Path(path).name.startswith("test_")
    ]
    if not tests:
        return "pytest"
    return "pytest " + " ".join(tests)


def owned_files(files: list[str]) -> list[str]:
    owned: set[str] = set()
    for raw in files:
        path = norm_path(raw)
        if not path or path.startswith(HANDOFF_PREFIX):
            continue
        owned.add(path)
    return sorted(owned)


def merge_files_touched(existing: list[str] | None, diff: list[str], catalog: set[str]) -> list[str]:
    """Diff wins. Critical paths already claimed stay until a person removes them."""
    live = set(owned_files(diff))
    kept = {norm_path(path) for path in (existing or []) if norm_path(path) in catalog and norm_path(path) not in live}
    return sorted(live | kept)


def apply_update(existing: dict | None, generated: dict, catalog: set[str]) -> dict:
    extra: dict = {}
    if existing:
        for key, value in existing.items():
            if key not in RECORD_KEYS:
                extra[key] = value
    files = merge_files_touched(
        list(existing.get("files_touched") or []) if existing else [],
        list(generated["files_touched"]),
        catalog,
    )
    human = {key: generated[key] for key in HUMAN_FIELDS}
    if existing:
        for key in HUMAN_FIELDS:
            old = existing.get(key)
            if isinstance(old, str) and old.strip():
                human[key] = old
    pr = str(generated.get("pr") or "")
    if not pr and existing and existing.get("pr"):
        pr = str(existing["pr"])
    record = {
        "task": generated["task"],
        "developer": generated["developer"],
        "branch": generated["branch"],
        "latest_commit": generated["latest_commit"],
        "pr": pr,
        "files_touched": files,
        "tests": human["tests"],
        "blockers": human["blockers"],
        "next_action": human["next_action"],
        "updated_at": generated["updated_at"],
    }
    record.update(extra)
    return record


def write_record(
    root: Path,
    branch: str,
    commit: str,
    files: list[str],
    pr: str,
    ownership: dict,
    *,
    now: str | None = None,
) -> Path | None:
    parsed = parse_branch(branch, developer_prefixes(ownership))
    if parsed is None:
        return None
    developer, task = parsed
    path = root / ".handoff" / record_filename(developer, task)
    existing = load_json(path) if path.is_file() else None
    generated = {
        "task": task,
        "developer": developer,
        "branch": branch,
        "latest_commit": commit,
        "pr": pr,
        "files_touched": files,
        "tests": infer_tests(files),
        "blockers": "",
        "next_action": "",
        "updated_at": now or utc_now(),
    }
    dump_record(path, apply_update(existing, generated, catalog_paths(ownership)))
    return path


def conflicts(current_developer: str | None, current_files: list[str], open_tasks: list[dict]) -> list[dict]:
    mine = set(owned_files(current_files))
    found: list[dict] = []
    for task in open_tasks:
        other = str(task.get("developer") or "")
        if current_developer and other == current_developer:
            continue
        overlap = sorted(mine & set(owned_files(list(task.get("files_touched") or []))))
        if not overlap:
            continue
        found.append(
            {
                "developer": other,
                "task": str(task.get("task") or ""),
                "branch": str(task.get("branch") or ""),
                "pr": str(task.get("pr") or ""),
                "files": overlap,
            }
        )
    return found


def override_active(labels: list[str], ownership: dict) -> bool:
    name = str(ownership.get("override_label") or "handoff-override")
    return name in labels


def should_fail(found: list[dict], override: bool) -> bool:
    return bool(found) and not override


def push_allowed(target: str, current_branch: str, prefixes: dict[str, str] | None = None) -> bool:
    """Only the current tharun/* or saiteja/* branch may receive the bot commit."""
    if not target or target != current_branch:
        return False
    if target in PROTECTED_BRANCHES:
        return False
    parsed = parse_branch(target, prefixes or DEFAULT_PREFIXES)
    return parsed is not None


def parse_name_status(text: str) -> list[str]:
    names: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith(("R", "C")) and len(parts) >= 3:
            names.extend(parts[1:])
        elif len(parts) >= 2:
            names.append(parts[1])
    return names


def git_changed_files(base: str) -> list[str]:
    ref = f"origin/{base}"
    three_dot = ["git", "diff", "--name-status", "-M", f"{ref}...HEAD"]
    two_dot = ["git", "diff", "--name-status", "-M", ref, "HEAD"]
    try:
        out = subprocess.check_output(three_dot, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        out = subprocess.check_output(two_dot, text=True, stderr=subprocess.PIPE)
    return parse_name_status(out)


def render_comment(
    record: dict | None,
    found: list[dict],
    override: bool,
    branch: str,
    ownership: dict,
) -> str:
    integration = str(ownership.get("integration_branch") or "staging")
    production = str(ownership.get("production_branch") or "main")
    label = str(ownership.get("override_label") or "handoff-override")
    lines = [STICKY_MARKER, "", "## Handoff", ""]
    if record is None:
        lines.append(
            f"Branch `{branch}` is not a `tharun/<task>` or `saiteja/<task>` branch. "
            "No handoff record was written."
        )
    else:
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        for key in RECORD_KEYS:
            value = record.get(key, "")
            if isinstance(value, list):
                shown = ", ".join(value) if value else "(none)"
            else:
                shown = str(value).replace("\n", " ") or "(empty)"
            lines.append(f"| {key} | {shown} |")
    lines.extend(["", "```json"])
    payload = {
        "record": record,
        "conflicts": found,
        "override": override,
        "integration_branch": integration,
        "production_branch": production,
    }
    lines.append(json.dumps(payload, indent=2))
    lines.extend(["```", ""])
    if found:
        lines.append("## Ownership conflicts")
        lines.append("")
        for item in found:
            files = ", ".join(f"`{path}`" for path in item["files"])
            where = f" ({item['pr']})" if item.get("pr") else ""
            lines.append(f"- `{item['branch']}` ({item['developer']}) owns {files}{where}")
        lines.append("")
        if override:
            lines.append(f"Override label `{label}` is present, so this check passes.")
        else:
            lines.append(
                "This check fails while this pull request touches those files. "
                f"Apply `{label}` only when the other developer agrees."
            )
    else:
        lines.append("No ownership conflict with another developer's open task.")
    lines.extend(
        [
            "",
            f"Rebase on the latest `{integration}` before merge. "
            f"Do not push to `{production}` or to another developer's branch. "
            "Resolve conflicts file by file. Do not take a blanket ours or theirs.",
            "",
        ]
    )
    return "\n".join(lines)


def run_pull_request_check(
    *,
    ownership: dict,
    branch: str,
    files: list[str],
    labels: list[str],
    action: str,
    tasks: list[dict],
    record: dict | None,
) -> tuple[int, str]:
    parsed = parse_branch(branch, developer_prefixes(ownership))
    developer = parsed[0] if parsed else None
    if action == "closed":
        body = render_comment(record, [], False, branch, ownership)
        closed = body.replace("## Handoff", "## Handoff\n\nThis pull request is closed.", 1)
        return 0, closed
    found = conflicts(developer, files, tasks)
    override = override_active(labels, ownership)
    body = render_comment(record, found, override, branch, ownership)
    return (1 if should_fail(found, override) else 0), body


def collect_open_tasks(client: object, ownership: dict, *, current_branch: str, current_pr: int | None) -> list[dict]:
    """Open tasks are open developer pull requests, or claimed branches with no pull request yet."""
    prefixes = developer_prefixes(ownership)
    open_pulls = client.list_pulls("open")  # type: ignore[attr-defined]
    open_by_branch: dict[str, dict] = {}
    for pull in open_pulls:
        ref = str(pull["head"]["ref"])
        if parse_branch(ref, prefixes) is None:
            continue
        if current_pr is not None and int(pull["number"]) == int(current_pr):
            continue
        if ref == current_branch:
            continue
        open_by_branch[ref] = pull

    branch_names = set(open_by_branch)
    for name in client.list_branches():  # type: ignore[attr-defined]
        if parse_branch(name, prefixes) is None or name == current_branch:
            continue
        branch_names.add(name)

    tasks: list[dict] = []
    for ref in sorted(branch_names):
        parsed = parse_branch(ref, prefixes)
        if parsed is None:
            continue
        developer, task = parsed
        pull = open_by_branch.get(ref)
        if pull is None:
            closed = client.list_pulls("closed", head=ref)  # type: ignore[attr-defined]
            if closed:
                continue
            records = client.list_handoff_records(ref)  # type: ignore[attr-defined]
            if not records:
                continue
            pr_files: list[str] = []
            pr_url = ""
        else:
            records = client.list_handoff_records(ref)  # type: ignore[attr-defined]
            pr_files = list(client.list_pull_files(int(pull["number"])))  # type: ignore[attr-defined]
            pr_url = str(pull.get("html_url") or "")
        files: set[str] = set(pr_files)
        task_names: list[str] = []
        for record in records:
            files.update(str(path) for path in (record.get("files_touched") or []))
            if record.get("task"):
                task_names.append(str(record["task"]))
        tasks.append(
            {
                "task": ", ".join(task_names) if task_names else task,
                "developer": developer,
                "branch": ref,
                "pr": pr_url,
                "files_touched": sorted(files),
            }
        )
    return tasks


class GitHubClient:
    """Small GitHub REST client. The token is sent as a header and never logged."""

    def __init__(self, token: str, repository: str, transport=None):
        self.token = token
        self.repository = repository
        self._transport = transport or self._urlopen

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "autofill-handoff",
        }

    def _urlopen(self, method: str, url: str, body: dict | None, headers: dict[str, str]) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        if not raw:
            payload: object = {}
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {"message": raw.decode("utf-8", "replace")}
        return status, payload

    def request(self, method: str, path: str, body: dict | None = None) -> object:
        url = path if path.startswith("https://") else f"{API_ROOT}{path}"
        status, payload = self._transport(method, url, body, self._headers())
        if status >= 400:
            raise GitHubError(status, payload)
        return payload

    def paginate(self, path: str) -> list:
        items: list = []
        page = 1
        while page < 50:
            separator = "&" if "?" in path else "?"
            batch = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def list_pulls(self, state: str, head: str | None = None) -> list[dict]:
        params = {"state": state}
        if head:
            owner = self.repository.split("/")[0]
            params["head"] = f"{owner}:{head}"
        query = urllib.parse.urlencode(params)
        rows = self.paginate(f"/repos/{self.repository}/pulls?{query}")
        return [row for row in rows if isinstance(row, dict)]

    def list_branches(self) -> list[str]:
        rows = self.paginate(f"/repos/{self.repository}/branches")
        return [str(row["name"]) for row in rows if isinstance(row, dict) and row.get("name")]

    def list_pull_files(self, number: int) -> list[str]:
        rows = self.paginate(f"/repos/{self.repository}/pulls/{number}/files")
        names: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("filename"):
                names.append(str(row["filename"]))
            if row.get("previous_filename"):
                names.append(str(row["previous_filename"]))
        return names

    def list_handoff_records(self, ref: str) -> list[dict]:
        ref_q = urllib.parse.quote(ref, safe="")
        try:
            entries = self.request("GET", f"/repos/{self.repository}/contents/.handoff?ref={ref_q}")
        except GitHubError as exc:
            if exc.status == 404:
                return []
            raise
        if not isinstance(entries, list):
            return []
        records: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if entry.get("type") != "file" or not name.endswith(".json") or name in SKIP_RECORD_NAMES:
                continue
            quoted = urllib.parse.quote(str(entry["path"]), safe="/")
            payload = self.request("GET", f"/repos/{self.repository}/contents/{quoted}?ref={ref_q}")
            if not isinstance(payload, dict) or "content" not in payload:
                continue
            raw = base64.b64decode(str(payload["content"]))
            record = json.loads(raw.decode("utf-8"))
            if isinstance(record, dict):
                records.append(record)
        return records

    def list_comments(self, number: int) -> list[dict]:
        rows = self.paginate(f"/repos/{self.repository}/issues/{number}/comments")
        return [row for row in rows if isinstance(row, dict)]

    def upsert_comment(self, number: int, body: str) -> None:
        existing = [
            comment
            for comment in self.list_comments(number)
            if str(comment.get("body") or "").lstrip().startswith(STICKY_MARKER)
        ]
        if existing:
            comment_id = existing[0]["id"]
            self.request("PATCH", f"/repos/{self.repository}/issues/comments/{comment_id}", {"body": body})
            return
        self.request("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": body})

    def open_pr_url(self, branch: str) -> str:
        pulls = self.list_pulls("open", head=branch)
        if not pulls:
            return ""
        return str(pulls[0].get("html_url") or "")

    def post_status(self, sha: str, state: str, description: str) -> None:
        self.request(
            "POST",
            f"/repos/{self.repository}/statuses/{sha}",
            {"state": state, "description": description[:140], "context": STATUS_CONTEXT},
        )

    def dispatch_ci(self, ref: str) -> None:
        workflow = urllib.parse.quote("ci.yml")
        self.request("POST", f"/repos/{self.repository}/actions/workflows/{workflow}/dispatches", {"ref": ref})


def _ownership_from_root(root: Path) -> dict:
    return load_ownership(root / ".handoff" / "ownership.json")


def cmd_guard_push(branch: str, current: str, root: Path) -> int:
    prefixes = developer_prefixes(_ownership_from_root(root))
    if push_allowed(branch, current, prefixes):
        print("allowed=1")
        return 0
    print(f"refusing to push handoff record to {branch}", file=sys.stderr)
    return 1


def cmd_write_from_git(root: Path) -> int:
    branch = os.environ["HANDOFF_BRANCH"]
    commit = os.environ["HANDOFF_COMMIT"]
    base = os.environ.get("HANDOFF_BASE", "staging")
    ownership = _ownership_from_root(root)
    if parse_branch(branch, developer_prefixes(ownership)) is None:
        print("skip=1")
        return 0
    if not push_allowed(branch, branch, developer_prefixes(ownership)):
        print(f"refusing to write a handoff record for {branch}", file=sys.stderr)
        return 1
    files = git_changed_files(base)
    pr = ""
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if token and repository:
        try:
            pr = GitHubClient(token, repository).open_pr_url(branch)
        except GitHubError as exc:
            print(f"::warning::could not look up the pull request: {exc}", file=sys.stderr)
    path = write_record(root, branch, commit, files, pr, ownership)
    if path is None:
        print("skip=1")
        return 0
    print(f"path={path.relative_to(root).as_posix()}")
    return 0


def _local_record(root: Path, branch: str, ownership: dict) -> dict | None:
    parsed = parse_branch(branch, developer_prefixes(ownership))
    if parsed is None:
        return None
    path = root / ".handoff" / record_filename(*parsed)
    if not path.is_file():
        return None
    return load_json(path)


def cmd_check_pr(root: Path) -> int:
    event_path = os.environ["GITHUB_EVENT_PATH"]
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    pull = event["pull_request"]
    number = int(pull["number"])
    branch = str(pull["head"]["ref"])
    base = str(pull["base"]["ref"])
    labels = [str(label.get("name") or "") for label in pull.get("labels") or []]
    action = str(event.get("action") or "")
    ownership = _ownership_from_root(root)
    files = git_changed_files(base)
    record = _local_record(root, branch, ownership)
    token = os.environ["GITHUB_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    client = GitHubClient(token, repository)
    tasks = collect_open_tasks(client, ownership, current_branch=branch, current_pr=number)
    code, body = run_pull_request_check(
        ownership=ownership,
        branch=branch,
        files=files,
        labels=labels,
        action=action,
        tasks=tasks,
        record=record,
    )
    client.upsert_comment(number, body)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    description = "Ownership conflict" if code else "No ownership conflict"
    try:
        client.post_status(sha, "failure" if code else "success", description)
    except GitHubError as exc:
        print(f"::warning::could not post commit status: {exc}", file=sys.stderr)
    if code:
        for item in conflicts(
            parse_branch(branch, developer_prefixes(ownership))[0]
            if parse_branch(branch, developer_prefixes(ownership))
            else None,
            files,
            tasks,
        ):
            for path in item["files"]:
                print(f"::error file={path}::Owned by open task {item['branch']}")
    return code


def cmd_dispatch_ci() -> int:
    ref = os.environ["HANDOFF_BRANCH"]
    root = Path(os.environ.get("HANDOFF_ROOT", ".")).resolve()
    prefixes = developer_prefixes(_ownership_from_root(root))
    if not push_allowed(ref, ref, prefixes):
        print(f"::warning::not dispatching CI for {ref}", file=sys.stderr)
        return 0
    token = os.environ["GITHUB_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    try:
        GitHubClient(token, repository).dispatch_ci(ref)
    except GitHubError as exc:
        print(f"::warning::CI dispatch skipped: {exc}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update and check Auto-Fill handoff records.")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)

    guard = sub.add_parser("guard-push")
    guard.add_argument("--branch", required=True)
    guard.add_argument("--current", default="")

    sub.add_parser("write-from-git")
    sub.add_parser("check-pr")
    sub.add_parser("dispatch-ci")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.cmd == "guard-push":
        current = args.current or args.branch
        return cmd_guard_push(args.branch, current, root)
    if args.cmd == "write-from-git":
        return cmd_write_from_git(root)
    if args.cmd == "check-pr":
        return cmd_check_pr(root)
    if args.cmd == "dispatch-ci":
        return cmd_dispatch_ci()
    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
