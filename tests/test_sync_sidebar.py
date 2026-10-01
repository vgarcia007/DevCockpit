from datetime import datetime, timedelta, timezone

import pytest
import yaml

from app.models import OTRSSyncState, Repository, SyncMeta, ZabbixSyncState, make_session
from app.otrs import OTRSClient, OTRSError, OTRSSyncManager
from app.web import create_app
from app.zabbix import ZabbixClient, ZabbixError, ZabbixSyncManager


def sidebar_app(tmp_path, otrs=True, zabbix=True):
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump({
        'repositories': [{'name': 'One', 'url': '/acme/one'}],
        'otrs': {'enabled': otrs, 'user': 'fake-user', 'password': 'private-password',
                 'url': 'https://tickets.example.com/', 'queue_ids': [1]},
        'zabbix': {'enabled': zabbix, 'user': 'fake-user', 'password': 'private-password',
                   'url': 'https://monitoring.example.com/',
                   'hosts': [{'host': 'app', 'environment': 'prod'}]},
    }))
    return create_app(path, tmp_path / 'sidebar.sqlite', auto_sync=False)


@pytest.mark.parametrize('otrs,zabbix,ids', [
    (False, False, ['github']), (True, False, ['github', 'otrs']),
    (False, True, ['github', 'zabbix']), (True, True, ['github', 'otrs', 'zabbix']),
])
def test_sidebar_shows_only_enabled_sources(tmp_path, otrs, zabbix, ids):
    client = sidebar_app(tmp_path, otrs, zabbix).test_client()
    data = client.get('/sync/status').get_json()
    assert [source['id'] for source in data['sources']] == ids
    assert all(source['state'] == 'pending' for source in data['sources'])
    for route in ('/', '/brief', '/issues'):
        page = client.get(route).get_data(as_text=True)
        assert page.count('data-sync-source=') == len(ids)
        assert 'data-zabbix-revision=' in page and 'id="sync-countdown"' in page
        for source in ids:
            assert f'aria-describedby="sync-details-{source}"' in page
        assert 'private-password' not in page and 'fake-user' not in page
    assert b'id="sync-button"' not in client.get('/').data


def test_sidebar_status_success_failure_running_and_rate_limit(tmp_path):
    app = sidebar_app(tmp_path)
    client = app.test_client()
    now = datetime.now(timezone.utc)
    with make_session(tmp_path / 'sidebar.sqlite')() as session:
        session.add(SyncMeta(key='last_success', value=now.isoformat()))
        session.add(OTRSSyncState(key='tickets', last_attempt=now, last_success=now, error='OTRS connection failed'))
        session.add(ZabbixSyncState(key='monitoring', last_attempt=now, last_success=now))
        session.commit()
    github = app.config['SYNC_MANAGER']
    otrs = app.config['OTRS_SYNC_MANAGER']
    zabbix = app.config['ZABBIX_SYNC_MANAGER']
    for manager in (github, otrs, zabbix):
        manager.next_sync_at = now + timedelta(minutes=5)
    sources = client.get('/sync/status').get_json()['sources']
    assert [source['state'] for source in sources] == ['healthy', 'error', 'healthy']
    assert sources[1]['display'] == 'Error' and sources[1]['last_success']
    assert 'Next sync:' in sources[1]['details'] and 'OTRS connection failed' in sources[1]['details']
    assert sources[2]['next_sync_at'].endswith('+00:00')
    assert b'is-error' in client.get('/').data
    for manager in (github, otrs, zabbix):
        manager.running = True
    assert all(source['state'] == 'running' and source['next_sync_at'] is None
               for source in client.get('/sync/status').get_json()['sources'])
    for manager in (github, otrs, zabbix):
        manager.running = False
    github.cooldown_until = now + timedelta(minutes=2)
    source = client.get('/sync/status').get_json()['sources'][0]
    assert source['state'] == 'paused' and source['display'] == 'Paused'
    assert 'rate limit' in source['details']
    github.cooldown_until = None
    with make_session(tmp_path / 'sidebar.sqlite')() as session:
        session.get(Repository, 'One').error = 'GitHub unavailable'
        session.commit()
    assert client.get('/sync/status').get_json()['sources'][0]['state'] == 'error'


@pytest.mark.parametrize('kind', ['otrs', 'zabbix'])
@pytest.mark.parametrize('failed', [False, True])
def test_optional_sync_schedules_next_attempt_after_success_or_failure(tmp_path, monkeypatch, kind, failed):
    module = __import__('app.' + kind, fromlist=['threading'])
    timers = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback, self.cancelled = delay, callback, False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(module.threading, 'Timer', Timer)
    sessions = make_session(tmp_path / 'schedule.sqlite')
    if kind == 'otrs':
        manager = OTRSSyncManager(sessions, {'interval_seconds': 900, 'excluded_states': [], 'attention_queue_ids': []})
        api, error, result = OTRSClient, OTRSError, {}
    else:
        manager = ZabbixSyncManager(sessions, {'interval_seconds': 60})
        api, error, result = ZabbixClient, ZabbixError, ([], [])

    def fetch(self):
        assert manager.running and manager.next_sync_at is None
        if failed:
            raise error('Connection failed')
        return result

    monkeypatch.setattr(api, 'fetch_all', fetch)
    manager.scheduler_enabled = True
    assert manager.run_sync()
    remaining = (manager.next_sync_at - datetime.now(timezone.utc)).total_seconds()
    interval = manager.config['interval_seconds']
    assert interval - 2 < remaining <= interval
    assert interval - 2 < timers[-1].delay <= interval
    assert not manager.running
    previous = timers[-1]
    assert manager.run_sync()
    assert previous.cancelled and timers[-1] is not previous
