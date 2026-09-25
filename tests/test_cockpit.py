import json
import re
import subprocess
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.github import GitHubCliClient, project_items
from app.changes import detect_changes
from app.models import Issue, ObservedChange, Pull, Release, Repository, UserAvatar, make_session
from app.sync import SyncManager, ci_state, issue_data, issue_field_priority, pull_data, review_state
from app.web import age_days, create_app, github_project_url, latest_releases, published_releases, release_window_start, workflow_readable


CFG = {
    "workflow": {"status_field": "Status", "values": {"backlog": "Backlog", "ready": "Ready",
                 "in_progress": "In Progress", "in_review": "In Review", "done": "Done"}},
    "priority": {"source": "project", "field": "Priority"},
    "repositories": [{"name": "One", "url": "/acme/one", "full_name": "acme/one", "project_number": 1}],
    "github": {"organization": "acme", "username": "alice"},
    "team": [{"github": "alice", "name": "Alice"}],
    "sync": {"interval_seconds": 60},
}


def issue_payload(number=10):
    return {"id": number, "number": number, "repository_url": "https://api.github.com/repos/acme/one",
            "title": "Fix login", "body": "A clear description", "html_url": f"https://github.com/acme/one/issues/{number}",
            "state": "open", "user": {"login": "bob", "avatar_url": "https://avatars.githubusercontent.com/u/2"}, "assignees": [{"login": "alice", "avatar_url": "https://avatars.githubusercontent.com/u/1"}],
            "labels": [{"name": "bug"}], "milestone": None, "created_at": "2026-09-18T00:00:00Z",
            "updated_at": "2026-09-20T00:00:00Z", "closed_at": None}


def project_item(status="In Progress", priority="Urgent"):
    return {("acme/one", 10): {"id": "PVT_item", "status": status, "priority": priority}}


def test_workflow_and_priority_are_only_github_values():
    payload = issue_payload()
    fields = {"Status", "Priority"}
    assigned = issue_data(payload, "One", project_item(), "available", fields, CFG)
    assert assigned["workflow"] == "In Progress"
    assert assigned["priority"] == "Urgent"
    missing = issue_data(payload, "One", {}, "available", fields, CFG)
    assert missing["workflow"] is None and missing["workflow_state"] == "not_in_project"
    assert missing["priority_state"] == "unavailable"
    blank = issue_data(payload, "One", project_item(None, None), "available", fields, CFG)
    assert blank["workflow_state"] == "no_status"
    assert blank["priority_state"] == "no_priority"
    no_project = issue_data(payload, "One", {}, "not_configured", set(), CFG)
    assert no_project["workflow_state"] == "unavailable"
    assert no_project["workflow"] is None
    ready = issue_data(payload, "One", project_item("Ready", "High"), "available", fields, CFG)
    assert ready["workflow"] == "Ready"
    assert assigned["workflow"] != "Ready"


def test_observed_changes_use_prior_github_snapshots():
    old_issue = SimpleNamespace(state="open", workflow="Backlog", workflow_state="known",
                                priority="High", priority_state="known")
    old_pull = SimpleNamespace(state="open", requested_reviewers=[], review_state="Approved", ci_state="Passing")
    issues = [{"github_id": 10, "number": 10, "title": "Fix login", "url": "https://github.com/acme/one/issues/10",
               "state": "open", "workflow": "Ready", "workflow_state": "known", "priority": "Urgent", "priority_state": "known"}]
    pulls = [{"github_id": 20, "number": 20, "title": "Login PR", "url": "https://github.com/acme/one/pull/20",
              "state": "open", "requested_reviewers": ["alice"], "review_state": "Changes Requested", "ci_state": "Failing"}]
    changes = detect_changes({10: old_issue}, {20: old_pull}, {}, issues, pulls, [], CFG["workflow"]["values"])
    assert {change["kind"] for change in changes} == {"ready", "urgent", "review_requested", "changes_requested", "ci_failing"}
    old_issue.workflow_state = "unavailable"
    assert "ready" not in {change["kind"] for change in detect_changes({10: old_issue}, {}, {}, issues, [], [], CFG["workflow"]["values"])}
    old_issue.priority_state = "no_priority"
    old_issue.priority = None
    assert "urgent" in {change["kind"] for change in detect_changes({10: old_issue}, {}, {}, issues, [], [], CFG["workflow"]["values"])}
    old_issue.priority_state = "unavailable"
    assert "urgent" not in {change["kind"] for change in detect_changes({10: old_issue}, {}, {}, issues, [], [], CFG["workflow"]["values"])}


def test_issue_field_priority_source_is_independent():
    cfg = {**CFG, "priority": {"source": "issue_field", "field": "Priority"}}
    row = issue_data(issue_payload(), "One", project_item("Ready", "Low"), "available",
                     {"Status", "Priority"}, cfg, "High", "known")
    assert row["priority"] == "High"
    assert row["workflow"] == "Ready"


def test_missing_project_status_field_is_not_reported_as_zero_work():
    repo = SimpleNamespace(project_state="available", error="Project status field is missing")
    assert not workflow_readable(repo)
    repo.error = "Project priority field is missing"
    assert workflow_readable(repo)


def test_project_links_follow_configured_owner_and_type():
    repo = {"full_name": "acme/one", "project_number": 13}
    assert github_project_url(repo, "acme") == "https://github.com/orgs/acme/projects/13"
    assert github_project_url({**repo, "project_owner": "alice", "project_owner_type": "user"}, "acme") == "https://github.com/users/alice/projects/13"
    assert github_project_url({**repo, "project_number": None}, "acme") is None


def test_pr_review_ci_and_age_states():
    pr = {"state": "open", "draft": False, "merged_at": None}
    assert review_state({**pr, "draft": True}, [], []) == "Draft"
    assert review_state(pr, [], ["alice"]) == "Waiting for Review"
    assert review_state(pr, [{"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"}], []) == "Changes Requested"
    assert review_state(pr, [{"user": {"login": "alice"}, "state": "APPROVED"}], []) == "Approved"
    assert review_state({**pr, "head": {"sha": "new"}},
                        [{"user": {"login": "alice"}, "state": "APPROVED", "commit_id": "old"}], []) == "Approval before latest commit"
    assert review_state({**pr, "merged_at": "2026-09-20T00:00:00Z"}, [], []) == "Merged"
    assert ci_state({"check_runs": [{"status": "completed", "conclusion": "failure"}]}, {"statuses": []}) == "Failing"
    assert ci_state({"check_runs": [{"status": "in_progress"}]}, {"statuses": []}) == "Running"
    assert ci_state({"check_runs": [{"status": "completed", "conclusion": "success"}]}, {"statuses": []}) == "Passing"
    assert ci_state({"check_runs": []}, {"statuses": []}) == "Unknown"
    assert age_days(datetime.now(timezone.utc) - timedelta(days=6, hours=1)) == 6


def test_merged_pull_is_stored_as_merged():
    payload = {"id": 20, "number": 20, "title": "Merged work", "body": None,
        "html_url": "https://github.com/acme/one/pull/20", "state": "closed",
        "merged_at": "2026-09-20T00:00:00Z", "closed_at": "2026-09-20T00:00:00Z",
        "created_at": "2026-09-18T00:00:00Z", "updated_at": "2026-09-20T00:00:00Z",
        "user": {"login": "alice"}, "draft": False,
        "base": {"ref": "main"}, "head": {"ref": "work", "sha": "abc"}}
    row = pull_data(payload, payload, [], "Unknown", "known", [], "One")
    assert row["state"] == "merged" and row["merged"] is True
    assert row["review_state"] == "Merged"


def test_latest_release_excludes_drafts_and_prereleases():
    def release(tag, day, draft=False, prerelease=False):
        row = Release(github_id=day, repository_name="One", name=tag, tag=tag, url="x",
                      created_at=datetime(2026, 9, day, tzinfo=timezone.utc),
                      published_at=datetime(2026, 9, day, tzinfo=timezone.utc),
                      draft=draft, prerelease=prerelease)
        return row
    rows = [release("v1", 18), release("v2-beta", 22, prerelease=True),
            release("v3-draft", 23, draft=True), release("v2", 20)]
    assert [r.tag for r in latest_releases(rows, "One")] == ["v2", "v1"]
    assert rows[1].prerelease is True


def test_recently_shipped_uses_published_github_releases_for_three_calendar_months():
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 24, 15, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    since = release_window_start(now, "quarter")
    assert since == datetime(2026, 6, 24, 15, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert release_window_start(now, "today").day == 24
    assert release_window_start(now, "week").day == 21
    assert release_window_start(datetime(2026, 5, 31, 10, tzinfo=timezone.utc), "quarter").day == 28

    def release(tag, published_at, draft=False, prerelease=False, repository="One"):
        return SimpleNamespace(tag=tag, published_at=published_at, draft=draft,
                               prerelease=prerelease, repository_name=repository)

    boundary = since.astimezone(timezone.utc).replace(tzinfo=None)
    rows = [release("old", boundary - timedelta(seconds=1)),
            release("boundary", boundary),
            release("draft", boundary + timedelta(days=1), draft=True),
            release("unpublished", None),
            release("prerelease", boundary + timedelta(days=2), prerelease=True),
            release("other-repo", boundary + timedelta(days=3), repository="Two")]
    assert [row.tag for row in published_releases(rows, since, "One")] == ["prerelease", "boundary"]


def test_rest_pagination():
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps([[{"id": 1}], [{"id": 2}]]), "")
    with GitHubCliClient(runner=runner) as client:
        assert [r["id"] for r in client.pages("/repos/acme/one/issues")] == [1, 2]
    assert len(calls) == 1
    assert "--paginate" in calls[0] and "--slurp" in calls[0]
    assert calls[0][:4] == ["gh", "api", "repos/acme/one/issues", "--method"]
    assert "shell" not in calls[0]


def test_cli_uses_login_without_inherited_token_variables(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ignored-test-value")
    monkeypatch.setenv("GH_TOKEN", "ignored-test-value")
    seen = []
    def runner(args, **kwargs):
        seen.append((args, kwargs))
        payload = {"data": {"viewer": {"login": "alice"}}} if args[2] == "graphql" else {"full_name": "acme/one"}
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
    with GitHubCliClient(runner=runner) as client:
        assert client.get("/repos/acme/one")["full_name"] == "acme/one"
        assert client.graphql("query { viewer { login } }", {})["viewer"]["login"] == "alice"
    assert all(call[0][:2] == ["gh", "api"] for call in seen)
    assert all(call[1]["check"] is True and "shell" not in call[1] for call in seen)
    assert all("GITHUB_TOKEN" not in call[1]["env"] and "GH_TOKEN" not in call[1]["env"] for call in seen)
    assert seen[1][0][2:] == ["graphql", "--input", "-"]
    assert json.loads(seen[1][1]["input"])["query"].startswith("query")


def test_issue_field_priority_reads_selected_github_option():
    def runner(args, **kwargs):
        assert args[2].endswith("/issue-field-values")
        payload = [[{"issue_field_name": "Priority", "value": 42,
                     "single_select_option": {"name": "High"}}]]
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
    with GitHubCliClient(runner=runner) as client:
        assert issue_field_priority(client, "acme/one", 10, "Priority") == ("High", "known")


def test_project_items_paginate_across_pages():
    class Fake:
        def graphql(self, query, variables):
            second = variables["cursor"] == "next"
            number = 11 if second else 10
            return {"organization": {"projectV2": {
                "fields": {"nodes": [{"name": "Status"}, {"name": "Priority"}],
                           "pageInfo": {"hasNextPage": False, "endCursor": None}},
                "items": {"nodes": [{"id": str(number), "content": {"number": number,
                    "repository": {"nameWithOwner": "acme/one"}},
                    "status": {"name": "Ready"}, "priority": {"issueFieldValue": {"name": "High"}}}],
                    "pageInfo": {"hasNextPage": not second, "endCursor": None if second else "next"}}}}}
    items, fields = project_items(Fake(), "acme", 1, "Status", "Priority")
    assert set(items) == {("acme/one", 10), ("acme/one", 11)}
    assert fields == {"Status", "Priority"}
    assert items[("acme/one", 10)]["priority"] == "High"


class CliResponse:
    def __init__(self, status_code, json):
        self.status_code = status_code
        self.payload = json


def api_handler(request):
    path = request.url.path
    if path.startswith("/repos/acme/broken"):
        return CliResponse(404, json={"message": "Not Found"})
    if path == "/graphql":
        query = json.loads(request.content)["query"]
        if "nodes(ids:" in query:
            return CliResponse(200, json={"data": {"nodes": [{"id": "PR20", "number": 20,
                "closingIssuesReferences": {"nodes": [{"number": 10, "repository": {"nameWithOwner": "acme/one"}}],
                                            "pageInfo": {"hasNextPage": False, "endCursor": None}}}]}})
        return CliResponse(200, json={"data": {"organization": {"projectV2": {
            "id": "P1", "title": "Work", "fields": {"nodes": [{"name": "Status"}, {"name": "Priority"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}},
            "items": {"nodes": [{"id": "PVT_item", "content": {"id": "I10", "number": 10,
                "repository": {"nameWithOwner": "acme/one"}}, "status": {"name": "In Progress"},
                "priority": {"name": "Urgent"}}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
    if path == "/repos/acme/one":
        return CliResponse(200, json={"html_url": "https://github.com/acme/one"})
    if path == "/repos/acme/one/issues":
        return CliResponse(200, json=[issue_payload(), {**issue_payload(20), "pull_request": {"url": "x"}}])
    if path == "/repos/acme/one/pulls":
        return CliResponse(200, json=[{"id": 20, "node_id": "PR20", "number": 20, "title": "Fix login PR",
            "body": "Closes #10", "html_url": "https://github.com/acme/one/pull/20", "state": "open",
            "user": {"login": "alice"}, "draft": False, "created_at": "2026-09-18T00:00:00Z",
            "updated_at": "2026-09-20T00:00:00Z", "closed_at": None, "merged_at": None,
            "base": {"ref": "main"}, "head": {"ref": "fix", "sha": "abc"}}])
    if path == "/repos/acme/one/pulls/20":
        return CliResponse(200, json={"assignees": [], "requested_reviewers": [{"login": "bob"}],
            "mergeable": True, "mergeable_state": "clean"})
    if path == "/repos/acme/one/pulls/20/reviews":
        return CliResponse(200, json=[])
    if path == "/repos/acme/one/commits/abc/check-runs":
        return CliResponse(200, json={"total_count": 1,
            "check_runs": [{"status": "completed", "conclusion": "failure"}]})
    if path == "/repos/acme/one/commits/abc/status":
        return CliResponse(200, json={"statuses": []})
    if path == "/repos/acme/one/releases":
        released = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        return CliResponse(200, json=[{"id": 30, "name": "v1", "tag_name": "v1", "html_url": "https://github.com/acme/one/releases/tag/v1",
            "created_at": released, "published_at": released,
            "draft": False, "prerelease": False, "author": {"login": "alice"}}])
    raise AssertionError(path)


def cli_runner(args, **kwargs):
    path = "/graphql" if args[2] == "graphql" else "/" + args[2].lstrip("/")
    request = SimpleNamespace(url=SimpleNamespace(path=path),
                              content=(kwargs.get("input") or "").encode())
    response = api_handler(request)
    if response.status_code >= 400:
        raise subprocess.CalledProcessError(1, args, stderr="gh: Not Found (HTTP 404)")
    payload = [response.payload] if "--paginate" in args else response.payload
    return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")


def test_sync_isolated_repos_cache_rebuild_and_views(tmp_path, monkeypatch):
    import app.sync as sync_module
    monkeypatch.setattr(sync_module, "GitHubCliClient", lambda: GitHubCliClient(runner=cli_runner))
    config = tmp_path / "config.yml"
    config.write_text("""github:\n  organization: acme\n  username: alice\nrepositories:\n  - name: Broken\n    url: /acme/broken\n  - name: One\n    url: /acme/one\n    project_number: 1\nteam:\n  - github: alice\n    name: Alice\npriority:\n  source: project\n  field: Priority\nsync:\n  interval_seconds: 60\n""")

    def sync_to(path):
        app = create_app(config_path=config, database_path=path, auto_sync=False)
        assert app.config["SYNC_MANAGER"].run_sync()
        session_factory = make_session(path)
        with session_factory() as session:
            issues = session.scalars(select(Issue)).all()
            pulls = session.scalars(select(Pull)).all()
            releases = session.scalars(select(Release)).all()
            repos = session.scalars(select(Repository)).all()
            assert len(issues) == len(pulls) == len(releases) == 1
            assert next(r for r in repos if r.name == "Broken").error
            assert issues[0].workflow == "In Progress"
            assert issues[0].priority == "Urgent"
            assert issues[0].related_pulls[0]["number"] == 20
            assert pulls[0].ci_state == "Failing"
            assert pulls[0].review_state == "Waiting for Review"
            assert session.get(UserAvatar, "alice").url == "https://avatars.githubusercontent.com/u/1"
            assert session.scalars(select(ObservedChange)).all() == []
            facts = (issues[0].workflow, issues[0].priority, pulls[0].review_state,
                     pulls[0].ci_state, releases[0].tag)
        return app, facts

    app, first = sync_to(tmp_path / "first.sqlite")
    _, rebuilt = sync_to(tmp_path / "rebuilt.sqlite")
    assert first == rebuilt
    with app.test_client() as client:
        home = client.get("/")
        assert home.status_code == 200
        text = home.get_data(as_text=True)
        assert "urgent" in text.lower() and "ci failing" in text.lower() and "waiting for review" in text.lower()
        assert 'href="/planning"' not in text
        assert "All Ready" in text
        assert "No In Progress issue" not in text
        assert client.get("/brief").status_code == 200
        assert client.get("/team").status_code == 200
        now_page = client.get("/now")
        assert now_page.status_code == 200
        assert "Fix login" in now_page.get_data(as_text=True)
        assert client.get("/planning").status_code == 404
        assert client.get("/issues").status_code == 200
        assert client.get("/pulls").status_code == 200
        assert client.get("/board").status_code == 200
        repositories_page = client.get("/repositories")
        assert repositories_page.status_code == 200
        assert 'href="https://github.com/orgs/acme/projects/1" target="_blank" rel="noopener noreferrer"' in repositories_page.get_data(as_text=True)
        assert client.get("/releases").status_code == 200
        assert client.get("/search?q=login").status_code == 200
        assert client.get("/repositories/One").status_code == 200
        unavailable = client.get("/repositories/Broken").get_data(as_text=True)
        assert "Workflow data unavailable" in unavailable
        assert "Ready cannot be determined" in unavailable
        assert client.get("/issues/One/10").status_code == 200
        assert client.get("/pulls/One/20").status_code == 200
        assert client.post("/issues/One/10").status_code == 405
        assert client.post("/board").status_code == 405
        for path in ("/", "/brief", "/now", "/team", "/issues", "/pulls", "/board",
                     "/repositories/One", "/releases", "/search?q=login"):
            page = client.get(path).get_data(as_text=True)
            github_links = re.findall(r'<a\b[^>]*href="https://github\.com[^\"]*"[^>]*>', page)
            assert github_links, path
            assert all('target="_blank"' in link and 'rel="noopener noreferrer"' in link
                       for link in github_links), path
    session_factory = make_session(tmp_path / "first.sqlite")
    with session_factory() as session:
        issue = session.scalars(select(Issue)).one()
        issue.workflow = "Ready"
        issue.priority = "High"
        session.commit()
    with app.test_client() as client:
        page = client.get("/team").get_data(as_text=True)
        assert "Ready next" in page
        person = page.split('data-member="alice alice"', 1)[1].split('</article>', 1)[0]
        assert 'class="team-work-label state-now"' not in person
        assert "Fix login" in person.split("Ready next", 1)[1]
        assert "Fix login" not in client.get("/now").get_data(as_text=True)
        assert "High priority" in client.get("/").get_data(as_text=True)
        assert "high priority" in client.get("/brief").get_data(as_text=True)

    with session_factory() as session:
        session.scalars(select(Issue)).one().workflow = "Backlog"
        session.commit()
    with app.test_client() as client:
        assert "Fix login" in client.get("/").get_data(as_text=True)
        assert "High priority" in client.get("/").get_data(as_text=True)
        assert "Fix login" not in client.get("/issues?status=Ready").get_data(as_text=True)

    with session_factory() as session:
        session.scalars(select(Issue)).one().state = "closed"
        session.scalars(select(Pull)).one().state = "merged"
        session.commit()
    with app.test_client() as client:
        assert "Fix login" not in client.get("/issues").get_data(as_text=True)
        assert "Fix login" in client.get("/issues?state=").get_data(as_text=True)
        assert "Fix login" in client.get("/issues?state=closed").get_data(as_text=True)
        assert "Fix login PR" not in client.get("/pulls").get_data(as_text=True)
        assert "Fix login PR" in client.get("/pulls?state=").get_data(as_text=True)
        assert "Fix login PR" in client.get("/pulls?state=merged").get_data(as_text=True)
        shipped = client.get("/").get_data(as_text=True).split('id="shipped-heading"', 1)[1].split('pulse-section', 1)[0]
        assert "One v1 released" in shipped
        assert "Fix login PR" not in shipped and "Issue #10" not in shipped
        brief_shipped = client.get("/brief").get_data(as_text=True).split("<h2>Recently shipped</h2>", 1)[1].split("<h2>Latest releases</h2>", 1)[0]
        assert "One v1 released" in brief_shipped
        assert "Fix login PR" not in brief_shipped and "Issue #10" not in brief_shipped
