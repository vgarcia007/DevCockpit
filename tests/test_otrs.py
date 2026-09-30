from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select

from app.config import load_config
from app.models import ExternalTeamIssue, Issue, OTRSObservedChange, OTRSSyncState, OTRSTicket, OTRSTicketStat, ObservedChange, Pull, Release, make_session
from app.otrs import OTRSClient, OTRSError, OTRSSyncManager, ResponsibleParser, excluded, parse_csv
from app.web import create_app


CSV = ('"Ticketnummer";"Erstellt";"Geschlossen";"Status";"Priorität";"Queue";"Besitzer";"Betreff"\n'
       '"123";"2026-09-28 14:05:00";"";"open";"3 normal";"Development";"alice";"CSV; subject"\n')


def config_file(tmp_path, otrs=None):
    config = {"repositories": [{"name": "One", "url": "/acme/one"}]}
    if otrs is not None:
        defaults = {"url": "https://tickets.example.com/", "queue_ids": [1, 3, 4, 2],
                    "attention_queue_ids": [1, 2], "highlight_queue_ids": [1],
                    "excluded_states": ["closed", "geschlossen", "zusammengefasst"]}
        if isinstance(otrs, list):
            config["otrs"] = [{key: value} for key, value in defaults.items()] + otrs
        elif otrs.get("enabled") is False:
            config["otrs"] = otrs
        else:
            config["otrs"] = {**defaults, **otrs}
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_config_accepts_existing_credential_list_and_optional_absence(tmp_path):
    path = config_file(tmp_path, [{"user": "agent"}, {"password": "secret"}])
    settings = load_config(path)["otrs"]
    assert settings["queue_ids"] == [1, 3, 4, 2]
    assert settings["interval_seconds"] == 900
    assert settings["attention_queue_ids"] == [1, 2]
    assert settings["highlight_queue_ids"] == [1]
    assert settings["enabled"] is True
    assert settings["user"] == "agent"
    assert "otrs" not in load_config(config_file(tmp_path))
    assert load_config(config_file(tmp_path, {"enabled": False}))["otrs"] == {"enabled": False}


def test_enabled_otrs_requires_explicit_url_and_queue_ids(tmp_path):
    path = config_file(tmp_path, {"user": "agent", "password": "secret"})
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["otrs"].pop("url")
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="otrs.url is required"):
        load_config(path)

    config["otrs"]["url"] = "https://tickets.example.com/"
    config["otrs"].pop("queue_ids")
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="otrs.queue_ids is required"):
        load_config(path)


def test_otrs_optional_queue_rules_default_to_empty(tmp_path):
    path = config_file(tmp_path, {"user": "agent", "password": "secret"})
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    for setting in ("attention_queue_ids", "highlight_queue_ids", "excluded_states"):
        config["otrs"].pop(setting)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    settings = load_config(path)["otrs"]
    assert settings["attention_queue_ids"] == []
    assert settings["highlight_queue_ids"] == []
    assert settings["excluded_states"] == []


def test_responsible_parser_ignores_popup_details_and_reads_email():
    html = ('<div id="TicketInfo"><fieldset><label>Verantwortlicher:</label><p class="Value">'
            'Jane Example <span>⌕</span><div id="ResponsibleDetails"><fieldset>'
            '<label>Vorname:</label><p class="Value">Jane</p>'
            '<label>E-Mail:</label><p class="Value">jane@example.com</p>'
            '</fieldset></div></p></fieldset></div>')
    parser = ResponsibleParser()
    parser.feed(html)
    assert parser.responsible == "Jane Example ⌕"
    assert parser.email == "jane@example.com"


def test_csv_parser_reads_quoted_subject_and_local_creation_time():
    row = parse_csv(CSV.encode())["123"]
    assert row["subject"] == "CSV; subject"
    assert row["created_at"] == datetime(2026, 9, 28, 12, 5)
    assert row["closed_at"] is None
    closed_csv = CSV.replace('"2026-09-28 14:05:00";"";',
                             '"2026-09-28 14:05:00";"2026-09-29 10:30:00";')
    assert parse_csv(closed_csv.encode())["123"]["closed_at"] == datetime(2026, 9, 29, 8, 30)
    with pytest.raises(OTRSError, match="columns"):
        parse_csv(CSV.replace('"Geschlossen";', '').encode())
    with pytest.raises(OTRSError, match="columns"):
        parse_csv(b"<html>login</html>")


def test_work_page_matches_assignee_and_both_otrs_roles(tmp_path):
    path = config_file(tmp_path, {"user": "agent", "password": "secret"})
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings["github"] = {"username": "alice"}
    settings["team"] = [{"github": "alice", "name": "Alice", "otrs_user": "alice"},
                        {"github": "bob", "name": "Bob", "otrs_user": "bob"}]
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    database = tmp_path / "work.sqlite"
    app = create_app(path, database, auto_sync=False)
    now = datetime.now(timezone.utc)
    with make_session(database)() as session:
        for number, assignees, title in [(1, ["alice"], "Alice issue"),
                                          (2, ["bob"], "Bob issue"),
                                          (3, ["alice"], "Closed issue")]:
            session.add(Issue(github_id=number, repository_name="One", number=number, title=title,
                url=f"https://github.com/acme/one/issues/{number}", state="closed" if number == 3 else "open",
                assignees=assignees, labels=[], created_at=now, updated_at=now,
                workflow_state="unavailable", priority_state="unavailable"))
        session.add(ExternalTeamIssue(login="alice", github_id=10, repository_name="acme/practice",
            number=10, title="Outside issue", url="https://github.com/acme/practice/issues/10", updated_at=now))
        for number, author, reviewers, title in [(11, "alice", [], "Alice PR"),
                                                  (12, "bob", ["alice"], "Review PR")]:
            session.add(Pull(github_id=number, repository_name="One", number=number, title=title,
                url=f"https://github.com/acme/one/pull/{number}", author=author,
                assignees=[], requested_reviewers=reviewers, reviews=[], draft=False, state="open",
                merged=False, created_at=now, updated_at=now, base_branch="main", head_branch="feature",
                head_sha="abc"))
        session.add_all([
            OTRSTicket(number="100", subject="Alice owns", queue="Support", state="open",
                priority="normal", owner=" Alice ", responsible="Somebody Else", created_at=now),
            OTRSTicket(number="101", subject="Alice responsible", queue="Support", state="open",
                priority="normal", owner="bob", responsible="Jane Example",
                responsible_email="alice@example.com", created_at=now),
            OTRSTicket(number="102", subject="Bob only", queue="Support", state="open",
                priority="normal", owner="bob", responsible="Jane Example",
                responsible_email="jane@example.com", created_at=now),
        ])
        session.commit()
    client = app.test_client()
    own = client.get("/work")
    assert own.status_code == 200
    for title in ("Alice issue", "Outside issue", "Alice PR", "Review PR", "Alice owns", "Alice responsible"):
        assert title.encode() in own.data
    for title in ("Bob issue", "Closed issue", "Bob only"):
        assert title.encode() not in own.data
    bob = client.get("/work?person=bob")
    assert b"Bob issue" in bob.data and b"Bob only" in bob.data
    assert b"Alice issue" not in bob.data
    assert client.get("/work?person=unknown").status_code == 404
    home = client.get("/").data
    assert b"Alice owns" in home and b"Alice responsible" in home
    assert b"Bob only" in home
    team = client.get("/team").data
    assert b"OTRS tickets" in team and b"Alice responsible" in team
    now_page = client.get("/now").data
    assert b"Open OTRS tickets" in now_page and b"Alice owns" in now_page
    brief = client.get("/brief").data
    assert b"OTRS 2" in brief and b"OTRS tickets 2" in brief
    search = client.get("/search?q=Alice+responsible").data
    assert b"Alice responsible" in search and b"Bob only" not in search


def test_client_uses_agent_session_and_csv_without_saving_profile():
    calls = []

    class FakeSession:
        def post(self, url, data, timeout):
            calls.append(("post", dict(data)))
            content = CSV.encode() if dict(data).get("ResultForm") == "CSV" else b"<html>logged in</html>"
            return SimpleNamespace(content=content, headers={"Content-Type": "text/csv" if content == CSV.encode() else "text/html"},
                                   raise_for_status=lambda: None)

        def get(self, url, params, timeout):
            calls.append(("get", params))
            if params.get("Action") == "AgentTicketZoom":
                return SimpleNamespace(text=('<div id="TicketInfo"><label>Verantwortlicher:</label>'
                    '<p class="Value">Jane Example<div id="ResponsibleDetails"><label>E-Mail:</label>'
                    '<p class="Value">jane@example.com</p></div></p></div>'), raise_for_status=lambda: None)
            return SimpleNamespace(text='<input name="ChallengeToken" value="fresh">', raise_for_status=lambda: None)

        def close(self):
            pass

    client = OTRSClient({"url": "https://example.org/", "user": "agent", "password": "secret",
                         "queue_ids": [1, 3, 4, 2], "excluded_states": []}, FakeSession(),
                        include_responsible=True)
    rows = client.fetch_all()
    assert len(rows) == 1
    assert rows["123"]["responsible"] == "Jane Example"
    assert rows["123"]["responsible_email"] == "jane@example.com"
    searches = [data for method, data in calls if method == "post" and data.get("ResultForm") == "CSV"]
    assert [data["QueueIDs"] for data in searches] == ["1", "3", "4", "2"]
    assert all(data["ChallengeToken"] == "fresh" and "SaveProfile" not in data and "StateIDs" not in data
               for data in searches)
    assert sum(params.get("Action") == "AgentTicketZoom" for method, params in calls if method == "get") == 1


def test_client_returns_terminal_states_for_change_detection():
    client = OTRSClient({"url": "https://example.org/", "queue_ids": [1]}, session=SimpleNamespace(close=lambda: None))
    client.login = lambda: None
    client.all_in_queue = lambda _queue: {
        str(index): {"state": state} for index, state in enumerate(
            ["closed", " geschlossen ", "Zusammengefasst", "offen", "neu", "warten zur Erinnerung"])
    }
    rows = client.fetch_all()
    assert set(rows) == {str(i) for i in range(6)}
    assert all(row["queue_id"] == 1 for row in rows.values())
    assert excluded(rows["1"]["state"], {"excluded_states": ["closed", "geschlossen", "zusammengefasst"]})


def test_failed_sync_keeps_previous_snapshot_and_page_is_optional(tmp_path, monkeypatch):
    sessions = make_session(tmp_path / "tickets.sqlite")
    with sessions() as session:
        session.add(OTRSTicket(number="123", subject="Existing", queue="Development", state="open",
                               priority="normal", owner="alice", created_at=datetime(2026, 9, 28)))
        session.commit()

    def fail(_self):
        raise OTRSError("OTRS did not return a CSV export")

    monkeypatch.setattr(OTRSClient, "fetch_all", fail)
    settings = {"url": "https://example.org/", "user": "agent", "password": "secret",
                "queue_ids": [1], "interval_seconds": 900}
    assert OTRSSyncManager(sessions, settings).run_sync()
    with sessions() as session:
        assert session.scalar(select(OTRSTicket).where(OTRSTicket.number == "123")).subject == "Existing"
        assert session.get(OTRSSyncState, "tickets").error == "OTRS did not return a CSV export"

    app = create_app(config_file(tmp_path, {"user": "agent", "password": "secret"}), tmp_path / "web.sqlite", auto_sync=False)
    response = app.test_client().get("/tickets")
    assert response.status_code == 200
    assert b"Tickets" in response.data and b">Alle</span>" in response.data
    app = create_app(config_file(tmp_path), tmp_path / "off.sqlite", auto_sync=False)
    assert app.test_client().get("/tickets").status_code == 404


def test_ticket_filters_search_highlight_and_links(tmp_path):
    database = tmp_path / "filtered.sqlite"
    app = create_app(config_file(tmp_path, {"user": "agent", "password": "secret"}), database,
                     auto_sync=False)
    with make_session(database)() as session:
        session.add_all(OTRSTicket(number=str(1000 + index), subject=f"Ordinary {index}",
                                   queue="General Support", state="offen", priority="3 normal", owner="bob",
                                   created_at=datetime(2026, 9, 1)) for index in range(54))
        session.add(OTRSTicket(number="99999", subject="Special portal request",
                               queue="Support::Portal", queue_id=1, state="neu", priority="5 very high",
                               owner="alice", responsible="Jane Example", responsible_email="jane@example.com",
                               created_at=datetime(2026, 9, 28)))
        session.commit()
    client = app.test_client()
    page = client.get("/tickets")
    assert page.status_code == 200
    assert b"Seite 1 von 2" in page.data
    assert b'ticket-highlight' in page.data
    assert b'href="https://tickets.example.com/index.pl?Action=AgentTicketZoom;TicketNumber=99999" target="_blank"' in page.data
    assert client.get("/tickets?page=2").status_code == 200
    for query in ("queue=Support%3A%3APortal", "status=neu", "priority=5+very+high",
                  "owner=ALICE", "responsible=JANE", "q=jane%40example.com", "q=99999", "q=portal"):
        result = client.get("/tickets?" + query)
        assert result.status_code == 200
        assert b"Special portal request" in result.data
        assert b"Ordinary 1" not in result.data
    assert b"Keine passenden Tickets" in client.get("/tickets?q=not-present").data
    assert b"Keine passenden Tickets" in client.get("/tickets?q=%25").data
    assert b"sort=oldest" in client.get("/tickets?sort=oldest").data


def test_home_attention_lists_only_selected_otrs_queues(tmp_path):
    database = tmp_path / "attention.sqlite"
    app = create_app(config_file(tmp_path, {"user": "agent", "password": "secret"}), database,
                     auto_sync=False)
    with make_session(database)() as session:
        session.add_all([
            OTRSTicket(number="111", subject="Portal needs help", queue="Support::Portal", queue_id=1,
                       state="neu", priority="3 normal", owner="alice", created_at=datetime(2026, 9, 28)),
            OTRSTicket(number="222", subject="Development needs help", queue="Engineering::Development", queue_id=2,
                       state="offen", priority="3 normal", owner="bob", created_at=datetime(2026, 9, 27)),
            OTRSTicket(number="333", subject="Ordinary ticket", queue="General Support", queue_id=3,
                       state="offen", priority="3 normal", owner="bob", created_at=datetime(2026, 9, 26)),
            OTRSTicket(number="444", subject="Already closed", queue="Support::Portal", queue_id=1,
                       state="geschlossen", priority="3 normal", owner="alice", created_at=datetime(2026, 9, 25)),
        ])
        session.commit()
    response = app.test_client().get("/")
    assert response.status_code == 200
    assert b"OTRS tickets" in response.data
    assert b"Portal needs help" in response.data and b"Development needs help" in response.data
    assert b"Ordinary ticket" not in response.data and b"Already closed" not in response.data
    assert b'AgentTicketZoom;TicketNumber=111" target="_blank"' in response.data
    brief = app.test_client().get("/brief")
    assert brief.status_code == 200
    assert b"OTRS need attention" in brief.data and b"Portal needs help" in brief.data
    assert b"OTRS NEED ATTENTION" in brief.data


def test_sync_observes_entry_change_and_completion_once(tmp_path, monkeypatch):
    config = load_config(config_file(tmp_path, {"user": "agent", "password": "secret"}))["otrs"]
    sessions = make_session(tmp_path / "events.sqlite")

    def row(number, state="offen", priority="3 normal", queue_id=1):
        return dict(number=number, subject=f"Request {number}", queue=f"Queue {queue_id}",
                    queue_id=queue_id, state=state, priority=priority, owner="alice",
                    created_at=datetime(2026, 9, 28),
                    closed_at=datetime(2026, 9, 29) if state == "geschlossen" else None)

    snapshots = [
        {"1": row("1"), "2": row("2", queue_id=3)},
        {"1": row("1", "neu", "5 very high"), "2": row("2", queue_id=1), "3": row("3")},
        {"1": row("1", "geschlossen", "5 very high"), "2": row("2", queue_id=1),
         "3": row("3")},
        {"1": row("1", "geschlossen", "5 very high"), "2": row("2", queue_id=1),
         "3": row("3")},
    ]
    monkeypatch.setattr(OTRSClient, "fetch_all", lambda _self: snapshots.pop(0))
    manager = OTRSSyncManager(sessions, config)
    for expected in ([], ["otrs_changed", "otrs_attention", "otrs_attention"],
                     ["otrs_changed", "otrs_attention", "otrs_attention", "otrs_closed"],
                     ["otrs_changed", "otrs_attention", "otrs_attention", "otrs_closed"]):
        assert manager.run_sync()
        with sessions() as session:
            events = session.scalars(select(OTRSObservedChange).order_by(OTRSObservedChange.id)).all()
            assert [event.kind for event in events] == expected
            if len(expected) == 4:
                assert session.get(OTRSTicket, "1") is None
                assert session.get(OTRSTicket, "2").queue_id == 1
                assert session.get(OTRSTicketStat, "1").state == "geschlossen"
                assert session.get(OTRSTicketStat, "1").closed_at == datetime(2026, 9, 29)
    assert "Status offen → neu" in events[0].detail
    assert "Priority 3 normal → 5 very high" in events[0].detail


def test_disabled_otrs_purges_cache_and_needs_no_credentials(tmp_path):
    database = tmp_path / "disabled.sqlite"
    with make_session(database)() as session:
        session.add(OTRSTicket(number="123", subject="Private", queue="Queue 1", queue_id=1,
                               state="offen", priority="normal", owner="agent", created_at=datetime(2026, 9, 28)))
        session.add(OTRSTicketStat(number="123", subject="Private", queue="Queue 1", queue_id=1,
                                   state="offen", created_at=datetime(2026, 9, 28)))
        session.add(OTRSSyncState(key="tickets", last_success=datetime.now(timezone.utc)))
        session.add(OTRSObservedChange(number="123", queue_id=1, queue="Queue 1", kind="otrs_attention",
                                       title="Private", url="https://tickets.example.com/index.pl?Action=AgentTicketZoom;TicketNumber=123",
                                       detail="Entered", observed_at=datetime.now(timezone.utc)))
        session.commit()
    path = config_file(tmp_path, {"enabled": False})
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings["github"] = {"username": "alice"}
    settings["team"] = [{"github": "alice", "name": "Alice", "otrs_user": "agent"}]
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    app = create_app(path, database, auto_sync=False)
    assert app.config["OTRS_SYNC_MANAGER"] is None
    with make_session(database)() as session:
        assert session.scalars(select(OTRSTicket)).all() == []
        assert session.scalars(select(OTRSTicketStat)).all() == []
        assert session.scalars(select(OTRSSyncState)).all() == []
        assert session.scalars(select(OTRSObservedChange)).all() == []
        now = datetime.now(timezone.utc)
        session.add(Issue(github_id=1, repository_name="One", number=1, title="GitHub work continues",
            url="https://github.com/acme/one/issues/1", state="open", assignees=["alice"],
            labels=[], created_at=now, updated_at=now, workflow="In Progress",
            workflow_state="known", priority_state="unavailable"))
        session.commit()
    assert app.test_client().get("/tickets").status_code == 404
    statistics = app.test_client().get("/statistics")
    assert statistics.status_code == 200 and b"OTRS tickets" not in statistics.data
    assert b"OTRS tickets" not in app.test_client().get("/").data
    for path in ("/team", "/brief", "/work", "/now", "/search?q=Private"):
        response = app.test_client().get(path)
        assert response.status_code == 200
        assert b"OTRS tickets" not in response.data
        assert b"Private" not in response.data
        if path != "/search?q=Private":
            assert b"GitHub work continues" in response.data
    assert app.test_client().get("/notifications").get_json()["otrs_origin"] is None


def test_statistics_combines_weekly_github_and_otrs_activity(tmp_path):
    database = tmp_path / "statistics.sqlite"
    config = config_file(tmp_path, {"user": "agent", "password": "secret"})
    settings = yaml.safe_load(config.read_text(encoding="utf-8"))
    settings["repositories"][0]["project_number"] = 1
    settings["repositories"].append({"name": "Other", "url": "/acme/other"})
    config.write_text(yaml.safe_dump(settings), encoding="utf-8")
    app = create_app(config, database, auto_sync=False)
    inside = datetime(2026, 9, 20, 22, 0)  # Monday midnight in Berlin
    before = inside - timedelta(minutes=1)
    after = datetime(2026, 9, 27, 22, 0)  # Next Monday midnight in Berlin
    with make_session(database)() as session:
        for number, title, created, closed, state in (
            (1, "Issue this week", inside, inside, "closed"),
            (2, "Issue before week", before, after, "closed"),
        ):
            session.add(Issue(github_id=number, repository_name="One", number=number,
                title=title, url=f"https://github.com/acme/one/issues/{number}",
                state=state, created_at=created, updated_at=created, closed_at=closed))
        session.add(Issue(github_id=3, repository_name="Other", number=3,
            title="Other repository issue", url="https://github.com/acme/other/issues/3",
            state="closed", created_at=inside, updated_at=inside, closed_at=inside))
        session.add(Pull(github_id=11, repository_name="One", number=11, title="Dependabot merge",
            url="https://github.com/acme/one/pull/11", author="dependabot[bot]", draft=False,
            state="merged", merged=True, created_at=before, updated_at=inside,
            merged_at=inside, base_branch="main", head_branch="bot", head_sha="abc"))
        session.add(Pull(github_id=12, repository_name="Other", number=12,
            title="Other repository merge", url="https://github.com/acme/other/pull/12",
            author="alice", draft=False, state="merged", merged=True,
            created_at=before, updated_at=inside, merged_at=inside,
            base_branch="main", head_branch="feature", head_sha="def"))
        for number, tag, draft in ((21, "v1", False), (22, "draft-v2", True)):
            session.add(Release(github_id=number, repository_name="One", name=tag, tag=tag,
                url=f"https://github.com/acme/one/releases/tag/{tag}",
                created_at=inside, published_at=inside, draft=draft, prerelease=False))
        session.add(Release(github_id=23, repository_name="Other", name="Other release", tag="other-v1",
            url="https://github.com/acme/other/releases/tag/other-v1",
            created_at=inside, published_at=inside, draft=False, prerelease=False))
        for number, subject, queue_id, created, closed, state in (
            ("100", "Created and closed", 1, inside, inside, "geschlossen"),
            ("101", "Earlier ticket closed", 1, before, inside, "closed"),
            ("102", "Other queue", 999, inside, inside, "closed"),
            ("103", "Undated closed ticket", 1, before, None, "zusammengefasst"),
        ):
            session.add(OTRSTicketStat(number=number, subject=subject, queue="Development",
                queue_id=queue_id, state=state, created_at=created, closed_at=closed))
        session.add(OTRSSyncState(key="tickets", last_success=inside))
        session.commit()

    with app.test_client() as client:
        response = client.get("/statistics?week=2026-W39")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'href="/statistics?week=2026-W38"' in html
        assert 'href="/statistics?week=2026-W40"' in html
        assert 'href="/statistics?week=2026-W39"' in html or "Week 39" in html
        assert '<option value="projects" selected>With projects</option>' in html
        issues = html.split('id="statistics-issues"', 1)[1].split("</section>", 1)[0]
        pulls = html.split('id="statistics-pulls"', 1)[1].split("</section>", 1)[0]
        releases = html.split('id="statistics-releases"', 1)[1].split("</section>", 1)[0]
        tickets = html.split('id="statistics-tickets"', 1)[1].split("</section>", 1)[0]
        assert "Issue this week" in issues and "Issue before week" not in issues
        assert "Other repository issue" not in issues
        assert "<strong>1</strong><span>Created</span>" in issues
        assert "<strong>1</strong><span>Closed</span>" in issues
        assert "Dependabot merge" in pulls and "<strong>1</strong><span>Merged</span>" in pulls
        assert "Other repository merge" not in pulls
        assert "v1" in releases and "draft-v2" not in releases
        assert "Other release" not in releases
        assert "<strong>1</strong><span>Published</span>" in releases
        assert "Created and closed" in tickets and "Earlier ticket closed" in tickets
        assert "Other queue" not in tickets
        assert "<strong>1</strong><span>Created</span>" in tickets
        assert "<strong>2</strong><span>Closed</span>" in tickets
        assert "1 closed ticket without a closure date" in tickets
        assert "TicketNumber=100" in tickets
        all_repositories = client.get("/statistics?week=2026-W39&scope=all").get_data(as_text=True)
        assert '<option value="all" selected>All configured repositories</option>' in all_repositories
        assert 'href="/statistics?week=2026-W38&amp;scope=all"' in all_repositories
        assert 'href="/statistics?week=2026-W40&amp;scope=all"' in all_repositories
        assert "Other repository issue" in all_repositories
        assert "Other repository merge" in all_repositories
        assert "Other release" in all_repositories
        assert "<strong>2</strong><span>Closed</span>" in all_repositories
        assert "<strong>2</strong><span>Published</span>" in all_repositories
        assert "TicketNumber=100" in all_repositories
        assert client.get("/statistics?week=2026-W54").status_code == 404


def test_notifications_merge_sources_and_paginate(tmp_path):
    database = tmp_path / "merged.sqlite"
    app = create_app(config_file(tmp_path, {"user": "agent", "password": "secret"}), database,
                     auto_sync=False)
    now = datetime.now(timezone.utc)
    with make_session(database)() as session:
        for index in range(21):
            session.add(ObservedChange(repository_name="One", kind="ready", number=index,
                                       title=f"Issue {index}", url=f"https://github.com/acme/one/issues/{index}",
                                       detail="Ready", observed_at=now - timedelta(minutes=index + 1)))
        session.add(OTRSObservedChange(number="123", queue_id=1, queue="Service Portal",
                                       kind="otrs_attention", title="Ticket 123", detail="Entered",
                                       url="https://tickets.example.com/index.pl?Action=AgentTicketZoom;TicketNumber=123",
                                       observed_at=now - timedelta(minutes=2, seconds=30)))
        session.commit()
    client = app.test_client()
    since = (now - timedelta(minutes=4)).isoformat()
    first = client.get("/notifications", query_string={"since": since, "alert_since": since}).get_json()
    assert first["unread_count"] == first["new_count"] == 4
    assert first["otrs_origin"] == "https://tickets.example.com"
    assert [item["source"] for item in first["events"][:3]] == ["github", "github", "otrs"]
    assert first["events"][2]["url"].endswith("TicketNumber=123")
    assert first["next_before"].startswith(("g:", "o:"))
    second = client.get("/notifications", query_string={"before": first["next_before"]}).get_json()
    assert len(first["events"]) == 20 and len(second["events"]) == 2
    assert second["next_before"] is None


def test_legacy_ticket_snapshot_is_notification_baseline(tmp_path, monkeypatch):
    config = load_config(config_file(tmp_path, {"user": "agent", "password": "secret"}))["otrs"]
    sessions = make_session(tmp_path / "legacy.sqlite")
    with sessions() as session:
        session.add(OTRSTicket(number="123", queue="Support::Portal", queue_id=None,
                               subject="Existing", state="offen", priority="3 normal", owner="alice",
                               created_at=datetime(2026, 9, 28)))
        session.add(OTRSSyncState(key="tickets", last_success=datetime.now(timezone.utc)))
        session.commit()
    monkeypatch.setattr(OTRSClient, "fetch_all", lambda _self: {
        "123": dict(number="123", queue="Support::Portal", queue_id=1,
                    subject="Existing", state="offen", priority="3 normal", owner="alice",
                    created_at=datetime(2026, 9, 28))})
    assert OTRSSyncManager(sessions, config).run_sync()
    with sessions() as session:
        assert session.scalars(select(OTRSObservedChange)).all() == []
        assert session.get(OTRSTicket, "123").queue_id == 1


def test_custom_otrs_queue_rules_drive_home_and_highlight(tmp_path):
    config = {"user": "agent", "password": "secret", "queue_ids": [3, 77],
              "attention_queue_ids": [77], "highlight_queue_ids": [3],
              "excluded_states": ["done"]}
    database = tmp_path / "custom.sqlite"
    app = create_app(config_file(tmp_path, config), database, auto_sync=False)
    with make_session(database)() as session:
        session.add_all([
            OTRSTicket(number="3", subject="Highlighted", queue="Special", queue_id=3,
                       state="offen", priority="normal", owner="alice", created_at=datetime(2026, 9, 28)),
            OTRSTicket(number="77", subject="Attention", queue="Custom", queue_id=77,
                       state="offen", priority="normal", owner="bob", created_at=datetime(2026, 9, 28)),
        ])
        session.commit()
    home = app.test_client().get("/").data
    assert b"Attention" in home and b"Highlighted" not in home
    tickets = app.test_client().get("/tickets").data
    assert tickets.count(b'class="ticket-highlight"') == 1
    assert b'href="/tickets?queue=Special"' in tickets


def test_notification_cursor_handles_many_events_at_same_sync_time(tmp_path):
    database = tmp_path / "same-time.sqlite"
    app = create_app(config_file(tmp_path, {"user": "agent", "password": "secret"}), database,
                     auto_sync=False)
    timestamp = datetime.now(timezone.utc) - timedelta(minutes=1)
    with make_session(database)() as session:
        for index in range(25):
            session.add(OTRSObservedChange(number=str(index), queue_id=1, queue="Portal",
                kind="otrs_attention", title=f"Ticket {index}", detail="Entered",
                url=f"https://tickets.example.com/index.pl?Action=AgentTicketZoom;TicketNumber={index}",
                observed_at=timestamp))
        session.add(ObservedChange(repository_name="One", kind="ready", number=1,
            title="GitHub issue", url="https://github.com/acme/one/issues/1",
            detail="Ready", observed_at=timestamp))
        session.commit()
    client = app.test_client()
    first = client.get("/notifications").get_json()
    second = client.get("/notifications", query_string={"before": first["next_before"]}).get_json()
    assert len(first["events"]) == 20 and len(second["events"]) == 6
    assert first["events"][0]["source"] == "github"
    assert len({event["id"] for event in first["events"] + second["events"]}) == 26
    assert second["next_before"] is None
