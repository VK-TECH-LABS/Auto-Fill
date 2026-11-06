"""Handoff records and workflow guards.

These tests do not start the service, create accounts, or submit applications.
They use local files and a fake GitHub client only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.handoff import (
    STICKY_MARKER,
    GitHubClient,
    apply_update,
    catalog_paths,
    collect_open_tasks,
    conflicts,
    infer_tests,
    merge_files_touched,
    override_active,
    parse_branch,
    parse_name_status,
    push_allowed,
    record_filename,
    render_comment,
    run_pull_request_check,
    should_fail,
    write_record,
)

ROOT = Path(__file__).resolve().parents[1]


def _ownership() -> dict:
    return json.loads((ROOT / ".handoff" / "ownership.json").read_text(encoding="utf-8"))


def test_catalog_paths_exist():
    ownership = _ownership()
    paths = catalog_paths(ownership)
    assert "docs/TILEARC_INTEGRATION.md" in paths
    assert "docs/HTTP_SERVICE.md" in paths
    assert "src/autofill/service/schemas.py" in paths
    assert "src/autofill/service/auth.py" in paths
    assert "src/autofill/credentials.py" in paths
    assert "src/autofill/service/sessions.py" in paths
    assert "src/autofill/safeguards.py" in paths
    assert ownership["integration_branch"] == "staging"
    assert ownership["production_branch"] == "main"
    assert ownership["override_label"] == "handoff-override"
    for path in paths:
        assert (ROOT / path).is_file(), path


def test_parse_branch_and_filename():
    prefixes = {"tharun": "tharun/", "saiteja": "saiteja/"}
    assert parse_branch("tharun/workday-create-account", prefixes) == ("tharun", "workday-create-account")
    assert parse_branch("saiteja/api/contract", prefixes) == ("saiteja", "api/contract")
    assert parse_branch("staging", prefixes) is None
    assert parse_branch("main", prefixes) is None
    assert parse_branch("cursor/dev-workflow-handoff-f809", prefixes) is None
    assert record_filename("saiteja", "api/contract") == "saiteja--api--contract.json"


def test_push_guard_refuses_protected_and_other_developer():
    prefixes = {"tharun": "tharun/", "saiteja": "saiteja/"}
    assert push_allowed("saiteja/api", "saiteja/api", prefixes)
    assert push_allowed("tharun/api", "tharun/api", prefixes)
    assert not push_allowed("main", "main", prefixes)
    assert not push_allowed("staging", "staging", prefixes)
    assert not push_allowed("tharun/api", "saiteja/api", prefixes)
    assert not push_allowed("saiteja/api", "tharun/api", prefixes)
    assert not push_allowed("cursor/task", "cursor/task", prefixes)


def test_guard_push_cli():
    script = ROOT / "scripts" / "handoff.py"
    allowed = subprocess.run(
        [sys.executable, str(script), "guard-push", "--branch", "saiteja/api", "--current", "saiteja/api"],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    refused = subprocess.run(
        [sys.executable, str(script), "guard-push", "--branch", "staging", "--current", "staging"],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert allowed.returncode == 0
    assert refused.returncode == 1


def test_merge_keeps_critical_claims_and_human_fields():
    catalog = catalog_paths(_ownership())
    merged = merge_files_touched(
        ["docs/TILEARC_INTEGRATION.md", "src/autofill/engine.py", ".handoff/tharun--api.json"],
        ["src/autofill/login.py", ".handoff/tharun--api.json"],
        catalog,
    )
    assert merged == ["docs/TILEARC_INTEGRATION.md", "src/autofill/login.py"]
    updated = apply_update(
        {
            "task": "api",
            "developer": "tharun",
            "branch": "tharun/api",
            "latest_commit": "old",
            "pr": "https://example.test/pull/1",
            "files_touched": ["docs/TILEARC_INTEGRATION.md"],
            "tests": "pytest tests/test_service.py",
            "blockers": "waiting on TileArc",
            "next_action": "rebase on staging",
            "updated_at": "2026-10-01T00:00:00Z",
            "note": "keep me",
        },
        {
            "task": "api",
            "developer": "tharun",
            "branch": "tharun/api",
            "latest_commit": "abc123",
            "pr": "",
            "files_touched": ["src/autofill/login.py"],
            "tests": "pytest",
            "blockers": "",
            "next_action": "",
            "updated_at": "2026-10-24T00:00:00Z",
        },
        catalog,
    )
    assert updated["latest_commit"] == "abc123"
    assert updated["pr"] == "https://example.test/pull/1"
    assert updated["tests"] == "pytest tests/test_service.py"
    assert updated["blockers"] == "waiting on TileArc"
    assert updated["next_action"] == "rebase on staging"
    assert updated["note"] == "keep me"
    assert "docs/TILEARC_INTEGRATION.md" in updated["files_touched"]
    assert "src/autofill/login.py" in updated["files_touched"]
    assert infer_tests(["src/autofill/login.py", "tests/test_login.py"]) == "pytest tests/test_login.py"


def test_write_record_round_trip(tmp_path: Path):
    ownership = _ownership()
    first = write_record(
        tmp_path,
        "saiteja/session-store",
        "abc123",
        ["src/autofill/session.py", "tests/test_memory.py"],
        "",
        ownership,
        now="2026-10-24T17:00:00Z",
    )
    assert first is not None
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["developer"] == "saiteja"
    assert payload["task"] == "session-store"
    assert payload["files_touched"] == ["src/autofill/session.py", "tests/test_memory.py"]
    assert payload["tests"] == "pytest tests/test_memory.py"
    assert payload["updated_at"] == "2026-10-24T17:00:00Z"
    write_record(
        tmp_path,
        "saiteja/session-store",
        "def456",
        ["src/autofill/session.py"],
        "https://example.test/pull/9",
        ownership,
        now="2026-10-24T18:00:00Z",
    )
    again = json.loads(first.read_text(encoding="utf-8"))
    assert again["latest_commit"] == "def456"
    assert again["pr"] == "https://example.test/pull/9"
    assert again["tests"] == "pytest tests/test_memory.py"
    assert write_record(tmp_path, "staging", "abc", [], "", ownership) is None


def test_conflicts_are_other_developer_only():
    tasks = [
        {
            "developer": "saiteja",
            "task": "auth",
            "branch": "saiteja/auth",
            "pr": "https://example.test/pull/3",
            "files_touched": ["src/autofill/service/auth.py", ".handoff/saiteja--auth.json"],
        },
        {
            "developer": "tharun",
            "task": "other",
            "branch": "tharun/other",
            "pr": "",
            "files_touched": ["src/autofill/service/auth.py"],
        },
    ]
    assert conflicts("tharun", ["src/autofill/service/auth.py"], tasks)[0]["branch"] == "saiteja/auth"
    assert conflicts("saiteja", ["src/autofill/service/auth.py"], [tasks[0]]) == []
    assert [item["branch"] for item in conflicts("saiteja", ["src/autofill/service/auth.py"], tasks)] == [
        "tharun/other"
    ]
    assert conflicts(None, [".handoff/readme.json"], tasks) == []
    assert not should_fail([], False)
    assert should_fail([{"files": ["src/autofill/service/auth.py"]}], False)
    assert not should_fail([{"files": ["src/autofill/service/auth.py"]}], True)
    assert override_active(["handoff-override"], _ownership())


def test_collect_open_tasks_skips_merged_branches():
    class FakeGitHub:
        def list_pulls(self, state, head=None):
            if state == "open":
                return [
                    {
                        "number": 70,
                        "html_url": "https://example.test/pull/70",
                        "head": {"ref": "saiteja/auth"},
                    }
                ]
            if head == "tharun/service-token-previous":
                return [{"number": 61, "merged_at": "2026-10-23T01:15:24Z", "head": {"ref": head}}]
            return []

        def list_branches(self):
            return [
                "main",
                "staging",
                "saiteja/auth",
                "saiteja/sessions",
                "tharun/service-token-previous",
                "cursor/example",
            ]

        def list_pull_files(self, number):
            assert number == 70
            return ["src/autofill/service/auth.py"]

        def list_handoff_records(self, ref):
            if ref == "saiteja/sessions":
                return [
                    {
                        "task": "sessions",
                        "developer": "saiteja",
                        "files_touched": ["src/autofill/service/sessions.py"],
                    }
                ]
            return []

    tasks = collect_open_tasks(FakeGitHub(), _ownership(), current_branch="tharun/api", current_pr=80)
    by_branch = {task["branch"]: task for task in tasks}
    assert set(by_branch) == {"saiteja/auth", "saiteja/sessions"}
    assert "src/autofill/service/auth.py" in by_branch["saiteja/auth"]["files_touched"]
    assert by_branch["saiteja/sessions"]["files_touched"] == ["src/autofill/service/sessions.py"]


def test_check_fails_without_override_and_passes_with_label():
    ownership = _ownership()
    tasks = [
        {
            "developer": "saiteja",
            "task": "auth",
            "branch": "saiteja/auth",
            "pr": "https://example.test/pull/3",
            "files_touched": ["src/autofill/credentials.py"],
        }
    ]
    code, body = run_pull_request_check(
        ownership=ownership,
        branch="tharun/login",
        files=["src/autofill/credentials.py"],
        labels=[],
        action="opened",
        tasks=tasks,
        record=None,
    )
    assert code == 1
    assert body.startswith(STICKY_MARKER)
    assert "src/autofill/credentials.py" in body
    assert "Rebase on the latest `staging`" in body
    passed, overridden = run_pull_request_check(
        ownership=ownership,
        branch="tharun/login",
        files=["src/autofill/credentials.py"],
        labels=["handoff-override"],
        action="labeled",
        tasks=tasks,
        record={"task": "login", "developer": "tharun", "branch": "tharun/login", "files_touched": []},
    )
    assert passed == 0
    assert "handoff-override" in overridden
    closed, _closed_body = run_pull_request_check(
        ownership=ownership,
        branch="tharun/login",
        files=["src/autofill/credentials.py"],
        labels=[],
        action="closed",
        tasks=tasks,
        record=None,
    )
    assert closed == 0


def test_parse_name_status_includes_renames():
    text = "M\tsrc/autofill/login.py\nR100\tsrc/old.py\tsrc/new.py\nD\tsrc/autofill/session.py\n"
    assert parse_name_status(text) == [
        "src/autofill/login.py",
        "src/old.py",
        "src/new.py",
        "src/autofill/session.py",
    ]


def test_github_client_upsert_and_missing_handoff_directory():
    calls: list[tuple[str, str]] = []

    def transport(method, url, body, headers):
        calls.append((method, url))
        assert headers["Authorization"].startswith("Bearer ")
        assert "token-value" not in url
        if url.endswith("/comments?per_page=100&page=1") and method == "GET":
            return 200, []
        if method == "POST" and url.endswith("/comments"):
            assert body["body"].startswith(STICKY_MARKER)
            return 201, {"id": 5}
        if "/contents/.handoff?ref=" in url:
            return 404, {"message": "Not Found"}
        if url.endswith("/dispatches"):
            assert body == {"ref": "saiteja/auth"}
            return 204, {}
        return 200, []

    client = GitHubClient("token-value", "VK-TECH-LABS/Auto-Fill", transport=transport)
    client.upsert_comment(12, f"{STICKY_MARKER}\nhello\n")
    assert client.list_handoff_records("saiteja/auth") == []
    client.dispatch_ci("saiteja/auth")
    assert ("POST", "https://api.github.com/repos/VK-TECH-LABS/Auto-Fill/issues/12/comments") in calls
    assert any(method == "GET" and "/contents/.handoff?ref=saiteja%2Fauth" in url for method, url in calls)


def test_github_client_updates_existing_sticky_comment():
    def transport(method, url, body, headers):
        del headers
        if method == "GET" and url.endswith("/comments?per_page=100&page=1"):
            return 200, [{"id": 9, "body": f"{STICKY_MARKER}\nold\n"}]
        if method == "PATCH" and url.endswith("/comments/9"):
            assert "new" in body["body"]
            return 200, {"id": 9}
        raise AssertionError(method + " " + url)

    client = GitHubClient("token-value", "VK-TECH-LABS/Auto-Fill", transport=transport)
    client.upsert_comment(4, f"{STICKY_MARKER}\nnew\n")


def _on_block(text: str) -> str:
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line == "on:" or line.startswith("on:"))
    collected = [lines[start]]
    for line in lines[start + 1 :]:
        if line == "" or line.startswith((" ", "\t", "#")):
            collected.append(line)
            continue
        break
    return "\n".join(collected)


def test_gcp_workflow_is_paused_and_preserved():
    text = (ROOT / ".github/workflows/gcp-staging-deploy.yml").read_text(encoding="utf-8")
    assert "paused: Railway is active host, Oct 2026" in text
    block = _on_block(text)
    assert "workflow_dispatch" in block
    assert "pull_request" not in block
    assert "push" not in block
    for preserved in (
        "gcloud run deploy autofill-engine",
        "GCP_PROJECT_ID: vk-tilearc-staging",
        "pytest-gates",
        "sa-github-deployer@vk-tilearc-staging.iam.gserviceaccount.com",
        "max-instances=1",
        "/health/ready",
        "github.event_name == 'push'",
    ):
        assert preserved in text


def test_ci_triggers_and_minimal_permissions():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    block = _on_block(text)
    assert "pull_request:" in block
    assert "[staging, main]" in block
    assert "push:" in block
    assert "branches: [staging]" in block
    assert "workflow_dispatch:" in block
    assert "contents: read" in text
    assert "contents: write" not in text
    assert "ruff check src tests scripts" in text
    assert "mypy" in text
    assert "pytest" in text
    assert "python -m build" in text


def test_handoff_workflow_scope_and_permissions():
    text = (ROOT / ".github/workflows/handoff.yml").read_text(encoding="utf-8")
    assert 'branches:\n      - "tharun/**"\n      - "saiteja/**"' in text
    assert "contents: write" in text
    assert "pull-requests: write" in text
    assert "statuses: write" in text
    assert "actions: write" in text
    for forbidden in ("id-token:", "packages:", "administration:", "security-events:"):
        assert forbidden not in text
    assert "refusing protected branch" in text
    assert "git push origin" in text


def test_render_comment_has_machine_readable_record():
    ownership = _ownership()
    body = render_comment(
        {
            "task": "auth",
            "developer": "tharun",
            "branch": "tharun/auth",
            "latest_commit": "abc",
            "pr": "",
            "files_touched": ["src/autofill/service/auth.py"],
            "tests": "pytest",
            "blockers": "",
            "next_action": "rebase on staging",
            "updated_at": "2026-10-24T00:00:00Z",
        },
        [],
        False,
        "tharun/auth",
        ownership,
    )
    encoded = body.split("```json", 1)[1].split("```", 1)[0]
    payload = json.loads(encoded)
    assert payload["record"]["branch"] == "tharun/auth"
    assert payload["integration_branch"] == "staging"
    assert payload["conflicts"] == []
