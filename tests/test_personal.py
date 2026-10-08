from copy import deepcopy
from datetime import datetime, timezone

import pytest
import yaml
from sqlalchemy import select

from app.config import load_config
from app.github import GitHubError, RateLimitError
from app.models import ExternalTeamIssue, Issue, ObservedChange, PersonalProject, PersonalProjectItem, Repository, make_session
from app.personal import fetch_project, sync_projects, work_items
from app.web import create_app


CFG = {
    "github": {"username": "alice", "organization": "acme"},
    "repositories": [{"name": "One", "url": "/acme/one", "full_name": "acme/one", "project_number": 1}],
    "team": [{"github": "alice", "name": "Alice", "project_number": 7}],
    "workflow": {"status_field": "Status", "values": {"backlog": "Backlog", "ready": "Ready",
        "in_progress": "In Progress", "in_review": "In Review", "done": "Done"}},
    "priority": {"source": "project", "field": "Priority"}, "sync": {"interval_seconds": 300},
}


def draft(status="In Progress", item_id="PVTI_1"):
    stamp = datetime.now(timezone.utc).isoformat()
    return {"id": item_id, "isArchived": False, "createdAt": stamp, "updatedAt": stamp,
        "status": {"name": status} if status else None, "priority": {"name": "Urgent"},
        "content": {"__typename": "DraftIssue", "id": "DI_1", "title": "Personal task",
            "body": "Details", "createdAt": stamp, "updatedAt": stamp, "creator": {"login": "alice"},
            "assignees": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}


class Client:
    def __init__(self, items=None):
        self.items = items if items is not None else [draft()]
        self.fields = ["Status", "Priority"]
        self.error = None
        self.paginate = False

    def graphql(self, query, variables):
        if self.error:
            raise self.error
        assert variables["owner"] == "alice"
        if "fields(first:100" in query:
            return {"user": {"projectV2": {"fields": {"nodes": [{"name": name} for name in self.fields],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        first = self.paginate and variables["cursor"] is None
        items = self.items[:1] if first else self.items[1:] if self.paginate else self.items
        return {"user": {"projectV2": {"title": "My tasks", "items": {"nodes": deepcopy(items),
            "pageInfo": {"hasNextPage": first, "endCursor": "next" if first else None}}}}}


@pytest.mark.parametrize("value", [0, -1, True, "7", 1.5])
def test_config_project_number_validation(tmp_path, value):
    cfg = deepcopy(CFG)
    cfg["team"][0]["project_number"] = value
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="team.project_number"):
        load_config(path)


def test_pagination_missing_fields_and_inaccessible_items():
    client = Client([draft(), draft(item_id="PVTI_2"), {"id": "hidden", "content": None}])
    client.paginate = True
    client.fields = ["Status"]
    title, items, incomplete = fetch_project(client, "alice", 7, CFG)
    assert title == "My tasks" and len(items) == 2 and incomplete
    assert items["PVTI_1"]["fields"] == ["Status"]


def test_draft_lifecycle_conversion_and_cache(tmp_path):
    sessions = make_session(tmp_path / "test.sqlite")
    client = Client()
    assert sync_projects(sessions, client, CFG) == 1
    with sessions() as session:
        row = work_items(session, [], [], CFG)[0]
        assert row.is_draft and row.assignees == ["alice"] and row.actual_assignees == []
        assert row.number is None and row.state == "open"
    client.items[0]["status"] = {"name": "Done"}
    sync_projects(sessions, client, CFG)
    with sessions() as session:
        row = session.scalars(select(PersonalProjectItem)).one()
        assert row.completed_at is not None
        assert work_items(session, [], [], CFG)[0].state == "closed"
        assert session.scalars(select(ObservedChange)).one().kind == "workflow"
    client.error = GitHubError("No access")
    assert sync_projects(sessions, client, CFG) == 0
    with sessions() as session:
        assert session.get(PersonalProject, "alice").error == "No access"
        assert session.scalars(select(PersonalProjectItem)).one().completed_at is not None
    client.error = RateLimitError("Rate limit")
    with pytest.raises(RateLimitError):
        sync_projects(sessions, client, CFG)
    client.error = None
    client.items[0]["status"] = {"name": "Ready"}
    sync_projects(sessions, client, CFG)
    with sessions() as session:
        assert session.scalars(select(PersonalProjectItem)).one().completed_at is None
    content = client.items[0]["content"]
    content.update(__typename="Issue", id="I_42", number=42, url="https://github.com/acme/other/issues/42",
        state="OPEN", closedAt=None, repository={"nameWithOwner": "acme/other"}, author={"login": "bob"})
    sync_projects(sessions, client, CFG)
    with sessions() as session:
        assert len(session.scalars(select(PersonalProjectItem)).all()) == 1
        assert not work_items(session, [], [], CFG)[0].is_draft
    client.items[0]["isArchived"] = True
    sync_projects(sessions, client, CFG)
    with sessions() as session:
        assert work_items(session, [], [], CFG) == []
    client.items = []
    sync_projects(sessions, client, CFG)
    with sessions() as session:
        assert session.scalars(select(PersonalProjectItem)).all() == []


def test_initial_done_and_changed_config_do_not_invent_history(tmp_path):
    sessions = make_session(tmp_path / "test.sqlite")
    client = Client([draft("Done")])
    sync_projects(sessions, client, CFG)
    with sessions() as session:
        assert work_items(session, [], [], CFG)[0].closed_at is None
    cfg = deepcopy(CFG)
    cfg["team"][0]["project_number"] = 8
    with sessions() as session:
        assert work_items(session, [], [], cfg) == []
    client.error = GitHubError("No access")
    sync_projects(sessions, client, cfg)
    with sessions() as session:
        state = session.get(PersonalProject, "alice")
        assert state.number == 8 and state.last_success is None
        assert session.scalars(select(PersonalProjectItem)).all() == []


def test_overlap_preserves_repository_and_fallback(tmp_path):
    sessions = make_session(tmp_path / "test.sqlite")
    client = Client()
    client.items[0]["content"].update(__typename="Issue", id="I_42", number=42, state="OPEN",
        repository={"nameWithOwner": "acme/one"}, url="https://github.com/acme/one/issues/42", closedAt=None)
    sync_projects(sessions, client, CFG)
    repo = Repository(name="One", full_name="acme/one", github_url="https://github.com/acme/one")
    issue = Issue(github_id=42, repository_name="One", number=42, title="Repo issue", state="open", body=None,
        url="https://github.com/acme/one/issues/42", assignees=["bob"], labels=[], related_pulls=[],
        workflow="Backlog", workflow_state="known", priority="Low", priority_state="known")
    with sessions() as session:
        rows = work_items(session, [issue], [repo], CFG)
        assert len(rows) == 1 and rows[0].workflow == "Backlog" and rows[0].priority == "Low"
        assert rows[0].assignees == ["bob", "alice"] and rows[0].actual_assignees == ["bob"]
        cfg = deepcopy(CFG)
        cfg["repositories"][0]["project_number"] = None
        assert work_items(session, [issue], [repo], cfg)[0].workflow == "In Progress"


def test_personal_tasks_render_and_filter_everywhere(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(CFG))
    db = tmp_path / "test.sqlite"
    app = create_app(config_path=path, database_path=db, auto_sync=False)
    sessions = make_session(db)
    client = Client()
    sync_projects(sessions, client, CFG)
    browser = app.test_client()
    for url in ("/", "/brief", "/now", "/team", "/work", "/issues", "/search?q=Personal", "/statistics"):
        response = browser.get(url)
        assert response.status_code == 200, url
        assert "Personal task" in response.get_data(as_text=True), url
        assert "#None" not in response.get_data(as_text=True), url
    assert "Personal task" in browser.get("/issues?member=alice").get_data(as_text=True)
    assert "Personal task" in browser.get("/issues?source=personal:alice").get_data(as_text=True)
    for query in ("unassigned=1", "repo=One", "source=repository", "state=closed"):
        assert "Personal task" not in browser.get("/issues?" + query).get_data(as_text=True)
    client.items[0]["status"] = {"name": "Done"}
    sync_projects(sessions, client, CFG)
    assert "Personal task" not in browser.get("/issues").get_data(as_text=True)
    assert "Personal task" in browser.get("/issues?state=closed").get_data(as_text=True)
    assert "Personal task" in browser.get("/statistics").get_data(as_text=True)
    assert browser.get("/sync/status").status_code == 200
    assert browser.get("/notifications").status_code == 200


def test_partial_page_failure_preserves_complete_snapshot(tmp_path):
    sessions = make_session(tmp_path / "test.sqlite")
    client = Client()
    sync_projects(sessions, client, CFG)

    class BrokenPage(Client):
        def graphql(self, query, variables):
            if variables.get("cursor") == "next":
                raise GitHubError("Second page failed")
            return super().graphql(query, variables)

    broken = BrokenPage([draft(item_id="replacement")])
    broken.paginate = True
    assert sync_projects(sessions, broken, CFG) == 0
    with sessions() as session:
        assert session.scalars(select(PersonalProjectItem)).one().item_id == "PVTI_1"
        assert session.get(PersonalProject, "alice").last_success is not None


def test_missing_fields_and_draft_issue_field_priority(tmp_path):
    sessions = make_session(tmp_path / "test.sqlite")
    cfg = deepcopy(CFG)
    cfg["priority"]["source"] = "issue_field"
    client = Client()
    sync_projects(sessions, client, cfg)
    with sessions() as session:
        row = work_items(session, [], [], cfg)[0]
        assert row.priority == "Urgent" and row.priority_state == "known"
    client.fields = []
    client.items[0]["status"] = client.items[0]["priority"] = None
    sync_projects(sessions, client, cfg)
    with sessions() as session:
        row = work_items(session, [], [], cfg)[0]
        assert row.workflow_state == row.priority_state == "unavailable"
        assert "Status" in session.get(PersonalProject, "alice").error


def test_real_issue_field_and_external_search_deduplication(tmp_path):
    cfg = deepcopy(CFG)
    cfg["priority"]["source"] = "issue_field"
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(cfg))
    db = tmp_path / "test.sqlite"
    app = create_app(config_path=path, database_path=db, auto_sync=False)
    sessions = make_session(db)

    class FieldClient(Client):
        def graphql(self, query, variables):
            if "issueFieldValues" in query:
                return {"nodes": [{"id": "I_42", "issueFieldValues": {"nodes": [
                    {"name": "High", "field": {"name": "Priority"}}]}}]}
            return super().graphql(query, variables)

    client = FieldClient()
    client.items[0]["content"].update(__typename="Issue", id="I_42", number=42, state="OPEN",
        repository={"nameWithOwner": "acme/other"}, url="https://github.com/acme/other/issues/42", closedAt=None)
    sync_projects(sessions, client, cfg)
    with sessions() as session:
        assert work_items(session, [], [], cfg)[0].priority == "High"
        session.add(ExternalTeamIssue(login="alice", github_id=42, repository_name="acme/other",
            number=42, title="Personal task", url="https://github.com/acme/other/issues/42", updated_at=datetime.now(timezone.utc)))
        session.commit()
    browser = app.test_client()
    for url in ("/team", "/work"):
        page = browser.get(url).get_data(as_text=True)
        assert page.count('title="Personal task"') == (1 if url == "/team" else 2)
        assert "Other assigned issues <span>1</span>" not in page
    assert "Personal task" in browser.get("/issues?priority=High").get_data(as_text=True)
    assert "Personal task" not in browser.get("/issues?priority=Urgent").get_data(as_text=True)
