import json
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.github import GitHubCliClient, GitHubError, RateLimitError
from app.models import ApiCache, make_session
from app.sync import SyncManager
from app.web import create_app


def response(args, status, body, headers=None):
    header_lines = "".join(f"{key}: {value}\n" for key, value in (headers or {}).items())
    return subprocess.CompletedProcess(args, 0 if status < 300 else 1,
                                       f"HTTP/2.0 {status} Test\n{header_lines}\n{json.dumps(body)}", "")


def test_etag_cache_persists_across_clients_and_handles_pages(tmp_path):
    sessions = make_session(tmp_path / "cache.sqlite")
    calls = []
    state = {"version": 1, "account": "alice"}

    def runner(args, **kwargs):
        path = args[2]
        if path == "user":
            return response(args, 200, {"login": state["account"]})
        page = 2 if "page=2" in args else 1
        etag = f'"v{state["version"]}-p{page}"'
        conditional = f"If-None-Match: {etag}" in args
        calls.append((page, conditional))
        if conditional:
            return response(args, 304, "", {"ETag": etag})
        headers = {"ETag": etag}
        if page == 1:
            headers["Link"] = '<https://api.github.com/repos/acme/one/issues?per_page=100&page=2>; rel="next"'
        return response(args, 200, [{"number": state["version"] * 10 + page}], headers)

    with GitHubCliClient(runner=runner, cache_sessions=sessions) as client:
        assert [item["number"] for item in client.pages("repos/acme/one/issues")] == [11, 12]
    with GitHubCliClient(runner=runner, cache_sessions=sessions) as client:
        assert [item["number"] for item in client.pages("repos/acme/one/issues")] == [11, 12]
    assert calls == [(1, False), (2, False), (1, True), (2, True)]
    with sessions() as session:
        page_one = next(row for row in session.scalars(select(ApiCache))
                        if dict(json.loads(row.key)[-1]).get("page") == 1)
        page_one.body = "{bad json"
        session.commit()
    with GitHubCliClient(runner=runner, cache_sessions=sessions) as client:
        assert [item["number"] for item in client.pages("repos/acme/one/issues")] == [11, 12]
    assert calls[-3:] == [(1, True), (1, False), (2, True)]

    state["version"] = 2
    with GitHubCliClient(runner=runner, cache_sessions=sessions) as client:
        assert [item["number"] for item in client.pages("repos/acme/one/issues")] == [21, 22]
    state["account"] = "bob"
    with GitHubCliClient(runner=runner, cache_sessions=sessions) as client:
        assert [item["number"] for item in client.pages("repos/acme/one/issues")] == [21, 22]
    assert calls[-2:] == [(1, False), (2, False)]


@pytest.mark.parametrize("headers,expected_min", [
    ({"Retry-After": "120", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"}, 115),
    ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 180)}, 175),
])
def test_rate_limit_headers_control_retry_time(headers, expected_min):
    def runner(args, **kwargs):
        return response(args, 403, {"message": "API rate limit exceeded"}, headers)

    with GitHubCliClient(runner=runner) as client, pytest.raises(RateLimitError) as caught:
        client.get("repos/acme/one")
    assert (caught.value.retry_at - datetime.now(timezone.utc)).total_seconds() >= expected_min


def test_graphql_limit_and_permission_error_are_distinct():
    def runner(args, **kwargs):
        if args[2] == "graphql":
            return subprocess.CompletedProcess(args, 1,
                'HTTP/2.0 200 OK\nX-RateLimit-Remaining: 0\n\n'
                '{"errors":[{"message":"API rate limit exceeded"}]}', "")
        return response(args, 403, {"message": "Resource not accessible by integration"})

    with GitHubCliClient(runner=runner) as client:
        with pytest.raises(RateLimitError):
            client.graphql("query { viewer { login } }", {})
        with pytest.raises(GitHubError) as caught:
            client.get("repos/acme/private")
        assert not isinstance(caught.value, RateLimitError)


def test_cooldown_survives_restart_and_blocks_manual_start(tmp_path):
    sessions = make_session(tmp_path / "cooldown.sqlite")
    config = {"repositories": [], "team": [], "sync": {"interval_seconds": 300}}
    manager = SyncManager(sessions, config)
    manager._record_rate_limit(RateLimitError("secondary rate limit"))
    first = manager.cooldown_until
    assert 55 <= (first - datetime.now(timezone.utc)).total_seconds() <= 60
    assert not manager.start()
    restarted = SyncManager(sessions, config)
    assert restarted.pause_reason() == "rate_limit"
    assert restarted.status()["rate_limit_until"] == first.isoformat()
    assert not restarted.start()
    restarted._record_rate_limit(RateLimitError("secondary rate limit"))
    assert 115 <= (restarted.cooldown_until - datetime.now(timezone.utc)).total_seconds() <= 120


def test_sidebar_status_and_manual_sync_respect_cooldown(tmp_path):
    config = tmp_path / "config.yml"
    config.write_text("repositories:\n  - name: One\n    url: /acme/one\n", encoding="utf-8")
    app = create_app(config_path=config, database_path=tmp_path / "web.sqlite", auto_sync=False)
    manager = app.config["SYNC_MANAGER"]
    manager._record_rate_limit(RateLimitError("secondary rate limit"))
    with app.test_client() as client:
        status = client.get("/sync/status").get_json()
        assert status["rate_limit_until"] and status["server_time"]
        assert client.post("/sync").get_json()["reason"] == "rate_limit"
        assert 'id="sync-countdown"' in client.get("/").get_data(as_text=True)
        assert 'id="sync-countdown"' in client.get("/brief").get_data(as_text=True)


def test_next_sync_is_scheduled_after_completed_manual_run(tmp_path, monkeypatch):
    import app.sync as sync_module
    sessions = make_session(tmp_path / "schedule.sqlite")
    config = {"repositories": [], "team": [], "sync": {"interval_seconds": 300}}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(sync_module, "GitHubCliClient", FakeClient)
    manager = SyncManager(sessions, config)
    manager.enable_scheduler()
    for _ in range(100):
        if manager.next_sync_at:
            break
        time.sleep(.01)
    first = manager.next_sync_at
    assert first is not None
    assert 295 <= (first - datetime.now(timezone.utc)).total_seconds() <= 300
    assert manager.start()
    for _ in range(100):
        if manager.next_sync_at and manager.next_sync_at > first:
            break
        time.sleep(.01)
    assert manager.next_sync_at > first
    manager.timer.cancel()


def test_rate_limit_stops_remaining_repositories(tmp_path, monkeypatch):
    import app.sync as sync_module
    sessions = make_session(tmp_path / "stop.sqlite")
    config = {"repositories": [{"name": "One"}, {"name": "Two"}], "team": [],
              "sync": {"interval_seconds": 300}}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(sync_module, "GitHubCliClient", FakeClient)
    manager = SyncManager(sessions, config)
    attempted = []

    def sync_repo(client, repo):
        attempted.append(repo["name"])
        raise RateLimitError("secondary rate limit")

    manager.sync_repo = sync_repo
    assert manager.run_sync()
    assert attempted == ["One"]
    assert manager.pause_reason() == "rate_limit"
    assert not manager.run_sync()
