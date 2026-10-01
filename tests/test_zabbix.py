from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select

from app.config import load_config
from app.models import ZabbixHost, ZabbixProblem, ZabbixSyncState, make_session
from app.web import create_app
from app.zabbix import SEVERITIES, ZabbixClient, ZabbixError


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
    {"url": "http://monitoring.example.com/"}, {"password": ""}, {"hosts": []},
    {"hosts": [{"host": "a", "environment": "unknown"}]},
    {"hosts": [{"host": "a", "environment": "prod"}, {"host": "a", "environment": "preprod"}]},
])
def test_invalid_zabbix_config_is_rejected(tmp_path, changes):
    with pytest.raises(ValueError, match="zabbix"):
        load_config(config_file(tmp_path, **changes))


def test_zabbix_sync_views_filters_failure_and_recovery(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    database = tmp_path / "monitoring.sqlite"
    app = create_app(path, database, auto_sync=False)
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
    app = create_app(path, database, auto_sync=False)
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
        elif method == "problem.get":
            assert json["params"]["recent"] is False
            assert "acknowledged" not in json["params"] and "suppressed" not in json["params"]
            result = [{"eventid": "20", "objectid": "10", "name": "Shared problem", "severity": "0",
                       "clock": "1790841600", "acknowledged": "1", "suppressed": "1"}] * 2
        else:
            assert method == "user.logout"
            result = True
        return SimpleNamespace(status_code=200, json=lambda: {"id": json["id"], "result": result})

    def close(self):
        self.closed = True


def test_api_client_maps_shared_problems_and_logs_out(tmp_path):
    settings = load_config(config_file(tmp_path))["zabbix"]
    session = FakeSession()
    hosts, problems = ZabbixClient(settings, session).fetch_all()
    assert len(hosts) == 2 and len(problems) == 1
    assert problems[0]["hostids"] == ["1", "2"]
    assert problems[0]["acknowledged"] and problems[0]["suppressed"]
    assert session.methods[-1] == "user.logout" and session.closed
    missing = FakeSession(missing_host=True)
    with pytest.raises(ZabbixError, match="missing or inaccessible"):
        ZabbixClient(settings, missing).fetch_all()
    assert missing.methods[-1] == "user.logout" and missing.closed
