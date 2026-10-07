import json
import subprocess
from datetime import datetime, timezone

import pytest
import yaml
from sqlalchemy import select

from app.github import GitHubCliClient, GitHubError, RateLimitError
from app.models import ApiCache, Repository, SecurityAlert, SecuritySyncState, SyncMeta, make_session
from app.security import SOURCES, alert_data, sync_security
from app.web import create_app

REPO = {"name": "One", "full_name": "acme/one"}
NOW = "2026-10-07T12:00:00Z"
SECRET = "private-secret-must-not-survive"


def payload(source, state="open", number=1):
    row = dict(number=number, state=state, html_url=f"https://github.com/acme/one/security/{source}/{number}", created_at=NOW)
    if source == "dependabot":
        row.update(security_advisory={"summary": "Unsafe dependency", "identifiers": [{"value": "CVE-2026-1234"}]},
                   security_vulnerability={"severity": "critical", "first_patched_version": {"identifier": "2.0"}},
                   dependency={"package": {"name": "example", "ecosystem": "pip"}, "manifest_path": "requirements.txt"})
    elif source == "code-scanning":
        row.update(rule={"id": "py/sql-injection", "description": "SQL injection", "security_severity_level": "high"},
                   most_recent_instance={"location": {"path": "app.py", "start_line": 12}, "message": {"text": SECRET}})
    else:
        row.update(secret=SECRET, secret_type="api_key", secret_type_display_name="Exposed API key")
    return row


def seeded_sessions(tmp_path):
    sessions = make_session(tmp_path / "security.sqlite")
    with sessions() as db:
        db.add(Repository(name="One", full_name="acme/one", github_url="https://github.com/acme/one"))
        db.commit()
    return sessions


class Client:
    def __init__(self, fail=None, empty=False):
        self.fail = fail
        self.empty = empty

    def security_pages(self, path):
        source = path.split("/")[-2]
        if not self.empty:
            yield payload(source)
        if source == self.fail:
            raise GitHubError("Source inaccessible", 403)


def test_snapshots_are_independent_and_atomic(tmp_path):
    sessions = seeded_sessions(tmp_path)
    sync_security(sessions, Client(), REPO)
    with sessions() as db:
        assert len(db.scalars(select(SecurityAlert)).all()) == 3
        assert all(s.last_success and not s.error for s in db.scalars(select(SecuritySyncState)))
        previous = db.get(SecuritySyncState, ("One", "dependabot")).last_success
    sync_security(sessions, Client(fail="dependabot", empty=True), REPO)
    with sessions() as db:
        alerts = db.scalars(select(SecurityAlert)).all()
        assert len(alerts) == 1 and alerts[0].source == "dependabot"
        state = db.get(SecuritySyncState, ("One", "dependabot"))
        assert state.error and state.last_success == previous
        assert db.get(SyncMeta, "security_revision")


@pytest.mark.parametrize("source,state,group", [
    ("dependabot", "auto_dismissed", "dismissed"), ("dependabot", "fixed", "fixed"),
    ("code-scanning", "dismissed", "dismissed"), ("code-scanning", "fixed", "fixed"),
    ("secret-scanning", "resolved", "fixed"), ("secret-scanning", "open", "open"),
])
def test_normalization(source, state, group):
    row = alert_data(payload(source, state), "One", source)
    assert row["status_group"] == group
    assert SECRET not in json.dumps(row, default=str)
    if source == "dependabot":
        assert row["fix_version"] == "2.0" and "CVE-2026-1234" in row["identifiers"]
    elif source == "code-scanning":
        assert row["location"] == "app.py:12"
    else:
        assert row["severity"] == "unknown"
        item = payload(source, "resolved")
        item["resolution"] = "false_positive"
        assert alert_data(item, "One", source)["status_group"] == "dismissed"


@pytest.mark.parametrize("cursor", ["page=2", "after=abc%2B123"])
def test_security_pagination_and_no_raw_cache(tmp_path, cursor):
    sessions = seeded_sessions(tmp_path)
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        next_link = f'Link: <https://api.github.com/repos/acme/one/secret-scanning/alerts?per_page=100&{cursor}>; rel="next"\n' if len(calls) == 1 else ''
        return subprocess.CompletedProcess(args, 0, 'HTTP/2.0 200 OK\nETag: "test"\n' + next_link + '\n' + json.dumps([payload("secret-scanning", number=len(calls))]), '')
    client = GitHubCliClient(runner=runner, cache_sessions=sessions)
    client.cache_identity = "alice"
    assert [r["number"] for r in client.security_pages('/repos/acme/one/secret-scanning/alerts')] == [1, 2]
    expected = "after=abc+123" if cursor.startswith("after") else cursor
    assert expected in calls[1]
    with sessions() as db:
        assert db.scalars(select(ApiCache)).all() == []


def test_rate_limit_preserves_snapshot_and_propagates(tmp_path):
    sessions = seeded_sessions(tmp_path)
    sync_security(sessions, Client(), REPO)
    class Limited:
        def security_pages(self, path):
            raise RateLimitError("Rate limited", 429)
    with pytest.raises(RateLimitError):
        sync_security(sessions, Limited(), REPO)
    with sessions() as db:
        assert len(db.scalars(select(SecurityAlert)).all()) == 3
        assert "Rate limited" in db.get(SecuritySyncState, ("One", "dependabot")).error


def test_page_filters_without_warnings_and_removed_repository(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump({"repositories": [{"name": "One", "url": "/acme/one"}, {"name": "Two", "url": "/acme/two"}]}))
    db_path = tmp_path / "security.sqlite"
    app = create_app(path, db_path, auto_sync=False)
    sessions = make_session(db_path)
    sync_security(sessions, Client(), REPO)
    closed = alert_data(payload("dependabot", "fixed", 2), "One", "dependabot")
    closed["title"] = "Already fixed"
    with sessions() as db:
        db.add(SecurityAlert(**closed))
        other = alert_data(payload("dependabot", number=3), "Two", "dependabot")
        other["title"] = "Other repository alert"
        db.add(SecurityAlert(**other))
        db.commit()
    client = app.test_client()
    html = client.get('/vulnerabilities').get_data(as_text=True)
    assert all(title in html for title in ("Unsafe dependency", "SQL injection", "Exposed API key", "Other repository alert"))
    assert "Already fixed" not in html and SECRET not in html
    assert html.index("Unsafe dependency") < html.index("SQL injection") < html.index("Exposed API key")
    assert "Security coverage is incomplete" not in html
    assert 'class="app-notice' not in html
    assert 'aria-current="page"' in html and 'data-auto-filter' in html
    selected = client.get('/vulnerabilities?repo=One&type=dependabot&severity=critical&q=CVE-2026-1234').get_data(as_text=True)
    assert "Unsafe dependency" in selected and "SQL injection" not in selected and "Other repository alert" not in selected
    assert "Security coverage is incomplete" not in selected
    assert "Already fixed" in client.get('/vulnerabilities?state=fixed').get_data(as_text=True)
    assert "Already fixed" in client.get('/vulnerabilities?state=').get_data(as_text=True)
    assert "No matching alerts" in client.get('/vulnerabilities?q=does-not-exist').get_data(as_text=True)
    assert client.get('/sync/status').get_json()["security_revision"]
    sync_security(sessions, Client(fail="dependabot"), REPO)
    with sessions() as db:
        db.get(Repository, "One").error = "Repository sync warning"
        db.commit()
    quiet = client.get('/vulnerabilities').get_data(as_text=True)
    assert 'class="app-notice' not in quiet
    main = quiet.split('<main class="app-main">', 1)[1].split("</main>", 1)[0]
    assert "Source inaccessible" not in main and "Repository sync warning" not in main
    assert "Unsafe dependency" in quiet
    empty = client.get('/vulnerabilities?q=does-not-exist').get_data(as_text=True)
    assert "Some security sources are unavailable" not in empty
    assert "Repository sync warning" in client.get('/issues').get_data(as_text=True)
    path.write_text(yaml.safe_dump({"repositories": [{"name": "Two", "url": "/acme/two"}]}))
    create_app(path, db_path, auto_sync=False)
    with sessions() as db:
        assert all(a.repository_name == "Two" for a in db.scalars(select(SecurityAlert)))
        assert db.scalars(select(SecuritySyncState)).all() == []


def test_secret_error_does_not_persist_sensitive_message(tmp_path):
    sessions = seeded_sessions(tmp_path)
    class Failing(Client):
        def security_pages(self, path):
            if "secret-scanning" in path:
                raise GitHubError(SECRET, 403)
            return super().security_pages(path)
    sync_security(sessions, Failing(), REPO)
    with sessions() as db:
        assert SECRET not in db.get(SecuritySyncState, ("One", "secret-scanning")).error
    assert SECRET.encode() not in (tmp_path / "security.sqlite").read_bytes()
