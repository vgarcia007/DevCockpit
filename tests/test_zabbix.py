from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select

from app.config import load_config
from tests.account_helpers import accounts_for
from app.models import ZabbixHost, ZabbixProblem, ZabbixSyncState, make_session
from app.web import create_app
from app.zabbix import SEVERITIES, ZabbixClient, ZabbixError, problem_duration


def config_file(tmp_path, **changes):
    settings = {"url": "https://monitoring.example.com/zabbix/", "user": "test-user",
                "password": "private-password", "hosts": [
                    {"host": "app.example.com", "environment": "prod"},
                    {"host": "staging.example.com", "environment": "preprod"}]}
    settings.update(changes)
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump({"repositories": [{"name": "One", "url": "/acme/one"}],
                                    "zabbix": settings}), encoding="utf-8")
    return path


def snapshot():
    hosts = [dict(hostid=str(index), host=hostname, name=hostname, environment=environment,
                  enabled=True, trigger_count=3) for index, hostname, environment in
             [(1, "app.example.com", "prod"), (2, "staging.example.com", "preprod")]]
    problems = [dict(eventid=str(index), triggerid=str(100 + index), name=f"Problem {label}",
                    severity=index, started_at=datetime.now(timezone.utc),
                    hostids=["1" if index % 2 == 0 else "2"], acknowledged=True, suppressed=True)
                for index, label in enumerate(SEVERITIES)]
    return hosts, problems


@pytest.mark.parametrize("changes", [
    {"attention_min_severity": 6}, {"attention_min_severity": True}, {"interval_seconds": 5},
    {"history_days": 0}, {"history_days": 366}, {"history_days": True},
    {"url": "http://monitoring.example.com/"}, {"hosts": []},
    {"hosts": [{"host": "a", "environment": "unknown"}]},
    {"hosts": [{"host": "a", "environment": "prod"}, {"host": "a", "environment": "preprod"}]},
])
def test_invalid_zabbix_config_is_rejected(tmp_path, changes):
    with pytest.raises(ValueError, match="zabbix"):
        load_config(config_file(tmp_path, **changes))


def test_zabbix_sync_views_filters_failure_and_recovery(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    database = tmp_path / "monitoring.sqlite"
    app = create_app(path, database, auto_sync=False, credential_store=accounts_for(path))
    manager = app.config["ZABBIX_SYNC_MANAGER"]
    monkeypatch.setattr(ZabbixClient, "fetch_all", lambda self: snapshot())
    assert manager.run_sync()
    client = app.test_client()
    home = client.get("/").get_data(as_text=True)
    attention = home.split('id="needs-heading"', 1)[1].split("</section>", 1)[0]
    for label in SEVERITIES:
        assert f"Problem {label}" in attention
    assert "Acknowledged" in attention and "Suppressed" in attention
    assert "preprod" in attention and "prod" in attention
    assert "private-password" not in home and "test-user" not in home
    assert 'href="/monitoring"' in home
    status = client.get("/sync/status").get_json()
    assert status["zabbix_revision"]
    assert f'data-zabbix-revision="{status["zabbix_revision"]}"' in home
    monitoring = client.get("/monitoring")
    assert monitoring.status_code == 200
    assert b"Last successful sync" in monitoring.data
    warning = client.get("/monitoring?severity=2").get_data(as_text=True)
    assert "Problem Warning" in warning and "Problem High" not in warning
    assert '<option value="2" selected>Warning</option>' in warning
    preprod = client.get("/monitoring?environment=preprod").get_data(as_text=True)
    assert "Problem Disaster" in preprod and "Problem High" not in preprod
    production = client.get("/monitoring?host=app.example.com").get_data(as_text=True)
    assert "Problem High" in production and "Problem Disaster" not in production
    assert b"Problem Warning" in client.get("/monitoring?q=warning").data
    assert b"Problem Disaster" not in client.get("/monitoring?q=warning").data
    assert client.get("/monitoring?severity=6").status_code == 400
    app.config["COCKPIT_CONFIG"]["zabbix"]["attention_min_severity"] = 4
    attention = client.get("/").get_data(as_text=True).split('id="needs-heading"', 1)[1].split("</section>", 1)[0]
    assert "Problem High" in attention and "Problem Disaster" in attention
    assert "Problem Warning" not in attention
    assert b"Problem Warning" in client.get("/monitoring").data

    def failed(self):
        raise ZabbixError("Zabbix connection failed")

    monkeypatch.setattr(ZabbixClient, "fetch_all", failed)
    assert manager.run_sync()
    assert b"Zabbix sync failed" in client.get("/").data
    assert b"Problem High" in client.get("/monitoring").data
    with make_session(database)() as session:
        assert len(session.scalars(select(ZabbixProblem)).all()) == 6
        state = session.get(ZabbixSyncState, "monitoring")
        assert state.last_success and state.error
    assert client.get("/sync/status").get_json()["zabbix_revision"] != status["zabbix_revision"]
    monkeypatch.setattr(ZabbixClient, "fetch_all", lambda self: (snapshot()[0], []))
    assert manager.run_sync()
    assert b"Problem High" not in client.get("/").data
    page = client.get("/monitoring").data
    assert b"No open problems" in page and b"Zabbix sync failed" not in page


def test_disabled_zabbix_ignores_cached_monitoring_data(tmp_path):
    database = tmp_path / "disabled.sqlite"
    with make_session(database)() as session:
        hosts, problems = snapshot()
        session.add_all(ZabbixHost(**host) for host in hosts)
        session.add_all(ZabbixProblem(**problem) for problem in problems)
        session.commit()
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump({"repositories": [{"name": "One", "url": "/acme/one"}],
                                   "zabbix": {"enabled": False}}))
    app = create_app(path, database, auto_sync=False, credential_store=accounts_for(path))
    assert app.config["ZABBIX_SYNC_MANAGER"] is None
    client = app.test_client()
    assert client.get("/monitoring").status_code == 404
    for route in ("/", "/team", "/issues", "/statistics"):
        page = client.get(route)
        assert page.status_code == 200
        assert b'href="/monitoring"' not in page.data and b"Problem High" not in page.data
    assert client.get("/sync/status").get_json()["zabbix_revision"] == ""


class FakeSession:
    def __init__(self, missing_host=False):
        self.headers = {}
        self.methods = []
        self.closed = False
        self.missing_host = missing_host

    def post(self, url, json, **kwargs):
        method = json["method"]
        self.methods.append(method)
        if method != "user.login":
            assert self.headers["Authorization"] == "Bearer session-token"
        if method == "user.login":
            result = "session-token"
        elif method == "host.get":
            result = [{"hostid": "1", "host": "app.example.com", "name": "App", "status": "0"},
                      {"hostid": "2", "host": "staging.example.com", "name": "Staging", "status": "0"}]
            if self.missing_host:
                result.pop()
        elif method == "trigger.get":
            result = [{"triggerid": "10", "hosts": [{"hostid": "1"}, {"hostid": "2"}]}]
        elif method == "event.get":
            params = json["params"]
            if "eventids" in params:
                assert params["eventids"] == ["22"]
                result = [{"eventid": "22", "clock": "1790841630"}]
            else:
                assert params["value"] == 1
                assert params["problem_time_till"] - params["problem_time_from"] == 30 * 86400
                assert "time_from" not in params  # Older open problems also overlap the window.
                assert "acknowledged" not in params and "suppressed" not in params
                result = [{"eventid": "20", "objectid": "10", "name": "Shared problem", "severity": "0",
                           "clock": "1790841600", "acknowledged": "1", "suppressed": "1", "r_eventid": "0",
                           "hosts": [{"hostid": "1"}, {"hostid": "2"}]}] * 2
                result.append(dict(result[0], eventid="21", name="Brief incident", r_eventid="22"))
        else:
            assert method == "user.logout"
            result = True
        return SimpleNamespace(status_code=200, json=lambda: {"id": json["id"], "result": result})

    def close(self):
        self.closed = True


def test_api_client_maps_shared_problems_and_logs_out(tmp_path):
    settings = {**load_config(config_file(tmp_path))["zabbix"], "user": "test-user", "password": "private-password"}
    session = FakeSession()
    hosts, problems = ZabbixClient(settings, session).fetch_all()
    assert len(hosts) == 2 and len(problems) == 2
    assert problems[0]["hostids"] == ["1", "2"]
    assert problems[0]["acknowledged"] and problems[0]["suppressed"]
    assert problems[0]["resolved_at"] is None
    assert problem_duration(problems[1]["started_at"], problems[1]["resolved_at"]) == "30s"
    assert session.methods[-1] == "user.logout" and session.closed
    missing = FakeSession(missing_host=True)
    with pytest.raises(ZabbixError, match="missing or inaccessible"):
        ZabbixClient(settings, missing).fetch_all()
    assert missing.methods[-1] == "user.logout" and missing.closed


def test_brief_incident_is_visible_in_history_but_not_attention(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    app = create_app(path, tmp_path / 'history.sqlite', auto_sync=False, credential_store=accounts_for(path))
    manager = app.config['ZABBIX_SYNC_MANAGER']
    hosts, problems = snapshot()
    short = dict(problems[4], eventid='brief', name='Brief outage',
                 started_at=datetime.now(timezone.utc) - timedelta(seconds=45),
                 resolved_at=datetime.now(timezone.utc) - timedelta(seconds=15))
    monkeypatch.setattr(ZabbixClient, 'fetch_all', lambda self: (hosts, [problems[4], short]))
    assert manager.run_sync()
    client = app.test_client()
    history = client.get('/monitoring').get_data(as_text=True)
    assert 'Brief outage' in history and '30s' in history
    assert 'Last 30 days' in history
    assert 'Brief outage' not in client.get('/').get_data(as_text=True)
    assert 'Problem High' in client.get('/').get_data(as_text=True)
    assert b'Brief outage' not in client.get('/monitoring?status=open').data
    resolved = client.get('/monitoring?status=resolved').get_data(as_text=True)
    assert 'Brief outage' in resolved and 'Problem High' not in resolved
    assert '<option value="resolved" selected>' in resolved
    assert client.get('/monitoring?status=unknown').status_code == 400
    # A later sync keeps resolved history while removing problems from attention.
    closed = dict(problems[4], resolved_at=datetime.now(timezone.utc))
    monkeypatch.setattr(ZabbixClient, 'fetch_all', lambda self: (hosts, [closed, short]))
    assert manager.run_sync()
    assert b'Problem High' not in client.get('/').data
    assert b'Problem High' in client.get('/monitoring').data
    assert b'No open problems' in client.get('/monitoring').data


def test_history_pagination_and_missing_recovery(tmp_path):
    settings = {**load_config(config_file(tmp_path, history_days=7))['zabbix'], 'user': 'test-user', 'password': 'private-password'}

    class PagedSession(FakeSession):
        def post(self, url, json, **kwargs):
            if json['method'] != 'event.get':
                return super().post(url, json, **kwargs)
            params = json['params']
            assert params['problem_time_till'] - params['problem_time_from'] == 7 * 86400
            first = int(params.get('eventid_from', '1'))
            count = 500 if first == 1 else 1
            assert first in (1, 501)
            rows = [dict(eventid=str(index), objectid='10', name='Repeated incident', severity='2',
                         clock='1790841600', acknowledged='0', suppressed='0', r_eventid='0',
                         hosts=[{'hostid': '1'}]) for index in range(first, first + count)]
            return SimpleNamespace(status_code=200, json=lambda: {'id': json['id'], 'result': rows})

    assert len(ZabbixClient(settings, PagedSession()).fetch_all()[1]) == 501

    class MissingRecoverySession(FakeSession):
        def post(self, url, json, **kwargs):
            if json['method'] == 'event.get' and 'eventids' in json['params']:
                return SimpleNamespace(status_code=200, json=lambda: {'id': json['id'], 'result': []})
            return super().post(url, json, **kwargs)

    session = MissingRecoverySession()
    with pytest.raises(ZabbixError, match='recovery events'):
        ZabbixClient({**load_config(config_file(tmp_path))['zabbix'], 'user': 'test-user', 'password': 'private-password'}, session).fetch_all()
    assert session.methods[-1] == 'user.logout' and session.closed


def test_existing_zabbix_database_is_migrated(tmp_path):
    import sqlite3

    database = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(database) as connection:
        connection.execute('''CREATE TABLE zabbix_problems (
            eventid VARCHAR PRIMARY KEY, triggerid VARCHAR, name TEXT, severity INTEGER,
            started_at DATETIME, hostids JSON, acknowledged BOOLEAN, suppressed BOOLEAN)''')
        connection.execute('''INSERT INTO zabbix_problems VALUES
            ('1', '10', 'Existing problem', 2, '2026-10-01 10:00:00', '["1"]', 0, 0)''')
    sessions = make_session(database)
    with sessions() as session:
        problem = session.get(ZabbixProblem, '1')
        assert problem.name == 'Existing problem' and problem.resolved_at is None
    make_session(database)  # Migration is safe to run again.
