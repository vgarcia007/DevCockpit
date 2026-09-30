import json
import re
import sqlite3
import subprocess
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select

from app.github import GitHubCliClient, GitHubError, project_items
from app.changes import detect_changes
from app.models import ExternalTeamIssue, ExternalTeamSync, Issue, ObservedChange, Pull, Release, Repository, UserAvatar, make_session
from app.sync import SyncManager, ci_state, issue_data, issue_field_priorities, pull_data, review_state, search_assigned_issues
from app.web import age_days, brief_as_text, brief_with_prompt, create_app, github_project_url, in_statistics_week, latest_releases, published_releases, release_timeline, release_window_start, statistics_week_bounds, workflow_readable


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


def test_statistics_week_uses_berlin_boundaries_and_iso_week_year():
    start, end = statistics_week_bounds("2026-W39")
    assert start.isoformat() == "2026-09-21T00:00:00+02:00"
    assert end.isoformat() == "2026-09-28T00:00:00+02:00"
    assert in_statistics_week(datetime(2026, 9, 20, 22), start, end)
    assert not in_statistics_week(datetime(2026, 9, 20, 21, 59), start, end)
    assert not in_statistics_week(datetime(2026, 9, 27, 22), start, end)
    assert statistics_week_bounds("2026-W01")[0].date().isoformat() == "2025-12-29"
    spring_start, spring_end = statistics_week_bounds("2026-W13")
    assert spring_start.utcoffset() == timedelta(hours=1)
    assert spring_end.utcoffset() == timedelta(hours=2)
    with pytest.raises(ValueError):
        statistics_week_bounds("2026-W54")


def test_pull_filter_can_hide_dependabot_without_hiding_other_authors(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("""github:
  organization: acme
  username: alice
repositories:
  - name: One
    url: /acme/one
team:
  - github: alice
    name: Alice
""", encoding="utf-8")
    database = tmp_path / "pulls.sqlite"
    app = create_app(config_path=config, database_path=database, auto_sync=False)
    now = datetime.now(timezone.utc)
    with make_session(database)() as session:
        for number, author, title in ((1, "alice", "Human update"),
                                      (2, "dependabot[bot]", "Dependency update"),
                                      (3, "dependabot-helper", "Helper update")):
            session.add(Pull(github_id=number, repository_name="One", number=number,
                             title=title, url=f"https://github.com/acme/one/pull/{number}",
                             author=author, draft=False, state="open", merged=False,
                             created_at=now, updated_at=now, base_branch="main",
                             head_branch="work", head_sha=str(number)))
        session.commit()

    with app.test_client() as client:
        default = client.get("/pulls").get_data(as_text=True)
        assert all(title in default for title in ("Human update", "Dependency update", "Helper update"))
        hidden = client.get("/pulls?hide_dependabot=1").get_data(as_text=True)
        assert "Dependency update" not in hidden
        assert "Human update" in hidden and "Helper update" in hidden
        assert '<option value="1" selected>Hide Dependabot</option>' in hidden
        assert "Human update" in client.get("/pulls?hide_dependabot=1&author=alice").get_data(as_text=True)


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


def test_release_timeline_uses_calendar_distance_and_separates_nearby_nodes():
    def release(number, stamp, draft=False, prerelease=False):
        return SimpleNamespace(github_id=number, published_at=datetime.fromisoformat(stamp) if stamp else None,
                               draft=draft, prerelease=prerelease)

    timeline = release_timeline([
        release(5, "2026-04-01T12:00:00", draft=True),
        release(4, None),
        release(3, "2026-03-01T12:00:00", prerelease=True),
        release(2, "2026-01-01T12:01:00"),
        release(1, "2026-01-01T12:00:00"),
    ])
    assert [node["release"].github_id for node in timeline["nodes"]] == [1, 2, 3]
    assert timeline["nodes"][0]["top"] != timeline["nodes"][1]["top"]
    assert timeline["nodes"][2]["left"] - timeline["nodes"][0]["left"] > 200
    assert [tick["label"] for tick in timeline["ticks"]] == ["Jan 2026", "Feb", "Mar"]
    assert release_timeline([release(5, "2026-04-01T12:00:00", draft=True)])["nodes"] == []


def test_existing_release_cache_gains_notes_column(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE releases (
            github_id INTEGER PRIMARY KEY, repository_name VARCHAR, name VARCHAR, tag VARCHAR,
            url VARCHAR, created_at DATETIME, published_at DATETIME, draft BOOLEAN,
            prerelease BOOLEAN, author VARCHAR)""")
        connection.execute("""INSERT INTO releases VALUES
            (30, 'One', 'v1', 'v1', 'https://github.com/acme/one/releases/tag/v1',
             '2026-01-01 00:00:00', '2026-01-01 00:00:00', 0, 0, 'alice')""")
    sessions = make_session(path)
    with sessions() as session:
        release = session.get(Release, 30)
        assert release.tag == "v1" and release.body is None
        release.body = "Release notes"
        session.commit()
    with make_session(path)() as session:
        assert session.get(Release, 30).body == "Release notes"


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


def test_brief_text_contains_visible_facts_without_html_or_extra_items():
    issue = SimpleNamespace(repository_name="One", number=10, title="Fix\nlogin",
                            priority="Urgent", priority_state="known")
    pull = Pull(repository_name="One", number=20, title="Login PR",
                created_at=datetime.now(timezone.utc) - timedelta(days=6))
    release = SimpleNamespace(repository_name="One", tag="v1", prerelease=False,
                              published_at=datetime(2026, 9, 24))
    member = {"person": {"name": "Alice", "github": "alice"}, "active": [issue],
              "review": [], "ready_next": [], "assigned_backlog": []}
    cards = [{"repo": SimpleNamespace(name="One"), "latest": [release]}]
    result = brief_as_text([("URGENT", issue, "GitHub priority Urgent")] * 5,
                           [member], [pull] * 4, [issue] * 5, [release] * 4, cards, None)
    assert "NEED ATTENTION (5 total, 4 shown)" in result
    assert result.count("GitHub priority Urgent") == 4
    assert "One #10 | Fix login" in result and "Fix\nlogin" not in result
    assert "Alice (@alice) | Now 1" in result
    assert "REVIEWS (4 total, 3 shown)" in result
    assert "READY + UNASSIGNED (5 total, 4 shown)" in result
    assert "RECENTLY SHIPPED (4 total, 3 shown)" in result
    assert "One | v1 | 24.09.2026" in result
    assert "<a " not in result


def test_brief_prompt_is_read_from_file_each_time(tmp_path):
    prompt = tmp_path / "brief_prompt.txt"
    prompt.write_text("First instruction\n\nHere is the data:  \n", encoding="utf-8")
    assert brief_with_prompt("BRIEF\n- One #10\n", prompt) == (
        "First instruction\n\nHere is the data:\n\nBRIEF\n- One #10\n")
    prompt.write_text("Updated instruction", encoding="utf-8")
    assert brief_with_prompt("BRIEF\n", prompt).startswith("Updated instruction\n\nBRIEF")


def test_team_page_orders_people_by_display_name(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("""repositories:
  - name: One
    url: /example/one
team:
  - github: zoe
    name: Zoe
  - github: bob
    name: Bob
  - github: anne
    name: Änne
  - github: alice
    name: Alice
""", encoding="utf-8")
    app = create_app(config_path=config, database_path=tmp_path / "team.sqlite", auto_sync=False)
    page = app.test_client().get("/team").get_data(as_text=True)
    names = [page.index(f">{name}</a></h2>") for name in ("Alice", "Änne", "Bob", "Zoe")]
    assert names == sorted(names)
    assert "Other repository assignments have not been checked yet." in page
    assert "No tracked active or assigned work" not in page
    sessions = make_session(tmp_path / "team.sqlite")
    with sessions() as session:
        session.add(ExternalTeamSync(login="alice", last_attempt=datetime.now(timezone.utc),
                                     error="Search unavailable"))
        session.commit()
    page = app.test_client().get("/team").get_data(as_text=True)
    assert "could not be refreshed" in page
    assert "No tracked active or assigned work" not in page


def test_board_filters_by_person_and_repository(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("""repositories:
  - name: One
    url: /acme/one
  - name: Two
    url: /acme/two
team:
  - github: alice
    name: Alice
""", encoding="utf-8")
    database = tmp_path / "board.sqlite"
    app = create_app(config_path=config, database_path=database, auto_sync=False)
    now = datetime.now(timezone.utc)
    cases = [
        (1, "One", "Shared work", ["alice", "bob"]),
        (2, "One", "Open slot", []),
        (3, "Two", "Other repo", ["alice"]),
        (4, "One", "Different person", ["malice"]),
    ]
    with make_session(database)() as session:
        for number, repo, title, assignees in cases:
            session.add(Issue(github_id=number, repository_name=repo, number=number,
                              title=title, url=f"https://github.com/acme/{repo.lower()}/issues/{number}",
                              state="open", created_at=now, updated_at=now,
                              workflow="Ready", workflow_state="known", assignees=assignees))
        session.commit()

    with app.test_client() as client:
        page = client.get("/board").get_data(as_text=True)
        assert 'name="person"' in page
        assert "Alice (@alice)" in page and "@bob" in page and "@malice" in page

        page = client.get("/board?person=ALICE").get_data(as_text=True)
        assert "Shared work" in page and "Other repo" in page
        assert "Different person" not in page and "Open slot" not in page
        assert '<option value="alice" selected>' in page

        page = client.get("/board?repo=One&person=alice").get_data(as_text=True)
        assert "Shared work" in page and "Other repo" not in page
        assert '<option value="One" selected>' in page
        assert '<option value="alice" selected>' in page

        page = client.get("/board?person=bob").get_data(as_text=True)
        assert "Shared work" in page and "Other repo" not in page

        page = client.get("/board?person=~unassigned").get_data(as_text=True)
        assert "Open slot" in page and "Shared work" not in page


def test_notifications_count_only_recent_configured_changes_and_paginate(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("repositories:\n  - name: One\n    url: /acme/one\n", encoding="utf-8")
    database = tmp_path / "notifications.sqlite"
    app = create_app(config_path=config, database_path=database, auto_sync=False)
    sessions = make_session(database)
    now = datetime.now(timezone.utc)
    with sessions() as session:
        for number in range(22):
            session.add(ObservedChange(repository_name="One", kind="ready", number=number,
                                       title=f"Issue {number}", url=f"https://github.com/acme/one/issues/{number}",
                                       detail="Moved to Ready", observed_at=now - timedelta(minutes=number + 1)))
        session.add(ObservedChange(repository_name="Other", kind="ready", number=99, title="Hidden",
                                   url="https://github.com/acme/other/issues/99", detail="Other repo", observed_at=now))
        session.add(ObservedChange(repository_name="One", kind="ready", number=100, title="Old",
                                   url="https://github.com/acme/one/issues/100", detail="Old event",
                                   observed_at=now - timedelta(days=31)))
        session.commit()
    with app.test_client() as client:
        since = (now - timedelta(minutes=5, seconds=30)).isoformat()
        response = client.get("/notifications", query_string={"since": since, "alert_since": since})
        assert response.status_code == 200
        data = response.get_json()
        assert data["unread_count"] == data["new_count"] == 5
        assert len(data["events"]) == 20
        assert data["next_before"] is not None
        assert data["events"][0]["title"] == "Issue 0"
        older = client.get("/notifications", query_string={"before": data["next_before"]}).get_json()
        assert [item["title"] for item in older["events"]] == ["Issue 20", "Issue 21"]
        assert older["next_before"] is None
        assert client.get("/notifications?since=invalid").status_code == 400
        assert client.get("/notifications?before=-1").status_code == 400


def test_visit_changes_render_separate_link_and_completion_button(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("repositories:\n  - name: One\n    url: /acme/one\n", encoding="utf-8")
    database = tmp_path / "visit.sqlite"
    app = create_app(config_path=config, database_path=database, auto_sync=False)
    sessions = make_session(database)
    with sessions() as session:
        session.add(ObservedChange(repository_name="One", kind="ready", number=10, title="Ready issue",
                                   url="https://github.com/acme/one/issues/10", detail="Moved to Ready",
                                   observed_at=datetime.now(timezone.utc)))
        session.commit()
        event_id = session.scalars(select(ObservedChange.id)).one()
    page = app.test_client().get("/").get_data(as_text=True)
    assert re.search(rf'data-change-key="{event_id}:\d+"', page)
    assert 'id="visit-completed" hidden' in page
    assert 'id="visit-completed-toggle"' in page
    assert re.search(r'<a class="visit-event"[^>]+>.*?</a><button class="visit-check"', page, re.S)
def test_rest_pagination():
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        page = 2 if "page=2" in args else 1
        link = 'Link: <https://api.github.com/repos/acme/one/issues?per_page=100&page=2>; rel="next"\n' if page == 1 else ''
        return subprocess.CompletedProcess(args, 0, f'HTTP/2.0 200 OK\n{link}\n{json.dumps([{"id": page}])}', "")
    with GitHubCliClient(runner=runner) as client:
        assert [r["id"] for r in client.pages("/repos/acme/one/issues")] == [1, 2]
    assert len(calls) == 2
    assert "--paginate" not in calls[0] and "-i" in calls[0]
    assert "page=2" in calls[1]
    assert calls[0][:4] == ["gh", "api", "repos/acme/one/issues", "--method"]
    assert "shell" not in calls[0]


def test_external_team_search_caches_results_and_preserves_them_on_failure(tmp_path):
    sessions = make_session(tmp_path / "external.sqlite")
    manager = SyncManager(sessions, CFG)

    class SearchClient:
        calls = 0
        fail = False

        def get(self, path, **params):
            self.calls += 1
            assert path == "/search/issues"
            assert params == {"q": "is:issue is:open assignee:alice", "per_page": 100, "page": 1}
            if self.fail:
                raise GitHubError("Search unavailable")
            return {"total_count": 1, "incomplete_results": False, "items": [{
                "id": 40, "repository_url": "https://api.github.com/repos/acme/practice",
                "number": 7, "title": "Practice project", "html_url": "https://github.com/acme/practice/issues/7",
                "state": "open", "updated_at": "2026-09-25T00:00:00Z",
            }]}

    client = SearchClient()
    manager.sync_team_issues(client)
    with sessions() as session:
        assert session.scalars(select(ExternalTeamIssue)).one().title == "Practice project"
        assert session.get(ExternalTeamSync, "alice").last_success
    client.fail = True
    manager.sync_team_issues(client)
    assert client.calls == 1  # Five-minute refresh interval.
    with sessions() as session:
        session.get(ExternalTeamSync, "alice").last_attempt = datetime.now(timezone.utc) - timedelta(minutes=6)
        session.commit()
    manager.sync_team_issues(client)
    with sessions() as session:
        assert session.scalars(select(ExternalTeamIssue)).one().title == "Practice project"
        assert session.get(ExternalTeamSync, "alice").error == "Search unavailable"


def test_external_team_search_marks_truncated_results():
    class SearchClient:
        def get(self, path, **params):
            return {"total_count": 1001, "incomplete_results": True, "items": []}

    items, incomplete = search_assigned_issues(SearchClient(), "alice")
    assert items == [] and incomplete


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
    assert all(call[1]["check"] is False and "shell" not in call[1] for call in seen)
    assert all("GITHUB_TOKEN" not in call[1]["env"] and "GH_TOKEN" not in call[1]["env"] for call in seen)
    assert seen[1][0][2:] == ["graphql", "--input", "-", "-i"]
    assert json.loads(seen[1][1]["input"])["query"].startswith("query")


def test_issue_field_priorities_read_values_and_cleared_fields_in_one_batch():
    class Fake:
        def graphql(self, query, variables):
            assert "issueFieldValues" in query
            assert variables == {"ids": ["issue-1", "issue-2", "issue-3"]}
            return {"nodes": [
                {"id": "issue-1", "issueFieldValues": {"nodes": [
                    {"name": "High", "field": {"name": "Priority"}},
                    {"name": "Large", "field": {"name": "Effort"}}]}},
                {"id": "issue-2", "issueFieldValues": {"nodes": []}},
                None,
            ]}

    issues = [{"node_id": f"issue-{number}"} for number in (1, 2, 3)]
    assert issue_field_priorities(Fake(), issues, "Priority") == {
        "issue-1": ("High", "known"),
        "issue-2": (None, "no_priority"),
        "issue-3": (None, "unavailable"),
    }


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
    if path == "/user":
        return CliResponse(200, json={"login": "alice"})
    if path == "/search/issues":
        return CliResponse(200, json={"total_count": 4, "incomplete_results": False, "items": [
            {"id": 40, "repository_url": "https://api.github.com/repos/acme/practice",
             "number": 7, "title": "Practice project", "html_url": "https://github.com/acme/practice/issues/7",
             "state": "open", "updated_at": "2026-09-25T00:00:00Z"},
            {"id": 10, "repository_url": "https://api.github.com/repos/acme/one",
             "number": 10, "title": "Fix login", "html_url": "https://github.com/acme/one/issues/10",
             "state": "open", "updated_at": "2026-09-20T00:00:00Z"},
            {"id": 41, "repository_url": "https://api.github.com/repos/acme/practice",
             "number": 8, "title": "Practice PR", "html_url": "https://github.com/acme/practice/pull/8",
             "state": "open", "pull_request": {}, "updated_at": "2026-09-25T00:00:00Z"},
            {"id": 42, "repository_url": "https://api.github.com/repos/acme/practice",
             "number": 9, "title": "Closed practice", "html_url": "https://github.com/acme/practice/issues/9",
             "state": "closed", "updated_at": "2026-09-25T00:00:00Z"},
        ]})
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
            "created_at": released, "published_at": released, "body": "Changes:\n<script>alert(1)</script>",
            "draft": False, "prerelease": False, "author": {"login": "alice"}}])
    raise AssertionError(path)


def cli_runner(args, **kwargs):
    path = "/graphql" if args[2] == "graphql" else "/" + args[2].lstrip("/")
    request = SimpleNamespace(url=SimpleNamespace(path=path),
                              content=(kwargs.get("input") or "").encode())
    response = api_handler(request)
    if response.status_code >= 400:
        raise subprocess.CalledProcessError(1, args, stderr="gh: Not Found (HTTP 404)")
    return subprocess.CompletedProcess(args, 0, f'HTTP/2.0 200 OK\nETag: "test-{path}"\n\n{json.dumps(response.payload)}', "")


def test_sync_isolated_repos_cache_rebuild_and_views(tmp_path, monkeypatch):
    import app.sync as sync_module
    monkeypatch.setattr(sync_module, "GitHubCliClient", lambda **kwargs: GitHubCliClient(runner=cli_runner, **kwargs))
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
            assert releases[0].body == "Changes:\n<script>alert(1)</script>"
            assert session.get(UserAvatar, "alice").url == "https://avatars.githubusercontent.com/u/1"
            assert session.scalars(select(ObservedChange)).all() == []
            external = session.scalars(select(ExternalTeamIssue)).all()
            assert [(item.login, item.repository_name, item.number) for item in external] == [("alice", "acme/practice", 7)]
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
        brief = client.get("/brief")
        assert brief.status_code == 200
        brief_html = brief.get_data(as_text=True)
        assert 'id="brief-copy-prompt"' in brief_html
        assert 'id="brief-export-text" hidden' in brief_html
        assert 'id="brief-export-panel"' not in brief_html
        assert brief_html.index("Erstelle aus dem folgenden technischen Arbeitsstand") < brief_html.index("Hier sind die Rohdaten:") < brief_html.index("BRIEF\n")
        assert client.get("/team").status_code == 200
        team_html = client.get("/team").get_data(as_text=True)
        assert "Other assigned issues" in team_html and "Practice project" in team_html
        assert "acme/practice" in team_html
        assert "Practice PR" not in team_html and "Closed practice" not in team_html
        assert "1 assigned issue outside configured repositories" in text
        now_page = client.get("/now")
        assert now_page.status_code == 200
        assert "Fix login" in now_page.get_data(as_text=True)
        assert client.get("/planning").status_code == 404
        assert client.get("/issues").status_code == 200
        assert "Practice project" not in client.get("/issues").get_data(as_text=True)
        assert client.get("/pulls").status_code == 200
        assert client.get("/board").status_code == 200
        assert "Practice project" not in client.get("/board").get_data(as_text=True)
        repositories_page = client.get("/repositories")
        assert repositories_page.status_code == 200
        assert 'href="https://github.com/orgs/acme/projects/1" target="_blank" rel="noopener noreferrer"' in repositories_page.get_data(as_text=True)
        releases_page = client.get("/releases")
        assert releases_page.status_code == 200
        releases_html = releases_page.get_data(as_text=True)
        assert 'id="release-timeline-heading"' in releases_html
        assert 'aria-controls="release-notes-30"' in releases_html
        assert 'id="release-notes-30"' in releases_html
        assert "Changes:\n&lt;script&gt;alert(1)&lt;/script&gt;" in releases_html
        assert "<script>alert(1)</script>" not in releases_html
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

    with session_factory() as session:
        for number, repo, draft, prerelease in ((31, "One", False, True),
                                                 (32, "One", True, False),
                                                 (33, "Broken", False, False)):
            session.add(Release(github_id=number, repository_name=repo, name=f"v{number}",
                                tag=f"v{number}", body=None,
                                url=f"https://github.com/acme/{repo.lower()}/releases/tag/v{number}",
                                created_at=datetime(2025, 1, 1), published_at=datetime(2025, 1, 2),
                                draft=draft, prerelease=prerelease, author="alice"))
        session.commit()
    with app.test_client() as client:
        one_page = client.get("/releases?repo=One").get_data(as_text=True)
        one_timeline = one_page.split('class="release-timeline-section"', 1)[1]
        assert "v32" in one_page  # Draft remains in the table.
        assert 'data-release-node="30"' in one_timeline
        assert 'data-release-node="31"' in one_timeline
        assert 'data-release-node="32"' not in one_timeline
        assert 'data-release-node="33"' not in one_timeline
        assert "No release notes available." in one_timeline
        broken_timeline = client.get("/releases?repo=Broken").get_data(as_text=True).split('class="release-timeline-section"', 1)[1]
        assert 'data-release-node="33"' in broken_timeline
        assert 'data-release-node="30"' not in broken_timeline
