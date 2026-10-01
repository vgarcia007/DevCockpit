import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select

from app.accounts import AccountError, AccountStore, account_binding, github_preflight, server_url
from app.config import load_config, remove_legacy_credentials
from app.models import OTRSSyncState, OTRSTicket, SyncMeta, ZabbixSyncState, make_session
from app.otrs import OTRSClient, OTRSError
from app.web import create_app
from app.zabbix import ZabbixClient


def settings(tmp_path, **changes):
    data = {'repositories': [{'name': 'One', 'url': '/acme/one'}],
            'otrs': {'enabled': True, 'url': 'https://tickets.example.com/', 'queue_ids': [1]},
            'zabbix': {'enabled': True, 'url': 'https://monitor.example.com/zabbix/',
                       'hosts': [{'host': 'app', 'environment': 'prod'}]},
            'github': {'username': 'alice'}, 'team': [{'name': 'Alice', 'github': 'alice', 'otrs_user': 'agent'}]}
    data.update(changes)
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump(data))
    return path


def store(tmp_path):
    return AccountStore(tmp_path / 'keys/account.key', tmp_path / 'data/accounts.enc')


def csrf(client):
    page = client.get('/settings/accounts')
    assert page.status_code == 200
    assert page.headers['Cache-Control'] == 'no-store'
    return re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))[1]


def test_encrypted_accounts_roundtrip_permissions_and_urls(tmp_path):
    accounts = store(tmp_path)
    assert accounts.get('otrs', 'https://tickets.example.com/') is None
    assert not accounts.key_path.exists()
    first = accounts.set('otrs', 'https://TICKETS.example.com:443', 'unique-agent', 'unique-password')
    content = accounts.data_path.read_bytes()
    assert b'unique-agent' not in content and b'unique-password' not in content
    assert stat.S_IMODE(accounts.key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(accounts.data_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(accounts.key_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(accounts.data_path.parent.stat().st_mode) == 0o700
    reloaded = store(tmp_path)
    assert reloaded.get('otrs', 'https://tickets.example.com/') == first
    assert reloaded.get('otrs', 'https://different.example.com/') is None
    assert reloaded.get('zabbix', 'https://tickets.example.com/') is None
    nonce = json.loads(content)['nonce']
    reloaded.set('otrs', 'https://tickets.example.com/', 'other-agent', 'other-password')
    assert json.loads(accounts.data_path.read_bytes())['nonce'] != nonce
    reloaded.remove('otrs', 'https://tickets.example.com/')
    assert reloaded.get('otrs', 'https://tickets.example.com/') is None
    assert accounts.key_path.exists()


@pytest.mark.parametrize('damage', ['missing_key', 'wrong_key', 'broken_data'])
def test_damaged_storage_never_overwrites_accounts(tmp_path, damage):
    accounts = store(tmp_path)
    accounts.set('otrs', 'https://tickets.example.com/', 'agent', 'secret')
    if damage == 'missing_key':
        accounts.key_path.unlink()
    elif damage == 'wrong_key':
        accounts.key_path.write_bytes(b'x' * 32)
    else:
        accounts.data_path.write_bytes(b'corrupted')
    before = accounts.data_path.read_bytes()
    with pytest.raises(AccountError):
        accounts.set('otrs', 'https://tickets.example.com/', 'new', 'new-password')
    assert accounts.data_path.read_bytes() == before
    if damage == 'missing_key':
        assert not accounts.key_path.exists()


def test_storage_refuses_symlink_and_invalid_urls(tmp_path):
    accounts = store(tmp_path)
    accounts.data_path.parent.mkdir()
    target = tmp_path / 'unrelated'
    target.write_text('preserve')
    accounts.data_path.symlink_to(target)
    with pytest.raises(AccountError):
        accounts.get('otrs', 'https://tickets.example.com/')
    assert target.read_text() == 'preserve'
    for value in ('http://example.com/', 'https://user:password@example.com/', 'https://example.com/?q=x', 'https://example.com/#x'):
        with pytest.raises(ValueError):
            server_url(value)


def test_missing_accounts_pause_syncs_and_clear_cached_data(tmp_path, monkeypatch):
    path = settings(tmp_path)
    database = tmp_path / 'cache.sqlite'
    sessions = make_session(database)
    now = datetime.now(timezone.utc)
    with sessions() as db:
        db.add(OTRSSyncState(key='tickets', last_success=now))
        db.add(ZabbixSyncState(key='monitoring', last_success=now))
        db.commit()
    monkeypatch.setattr('app.sync.SyncManager.enable_scheduler', lambda self: None)
    monkeypatch.setattr(OTRSClient, 'fetch_all', lambda self: pytest.fail('Missing account must not connect'))
    monkeypatch.setattr(ZabbixClient, 'fetch_all', lambda self: pytest.fail('Missing account must not connect'))
    app = create_app(path, database, credential_store=store(tmp_path))
    for key in ('OTRS_SYNC_MANAGER', 'ZABBIX_SYNC_MANAGER'):
        manager = app.config[key]
        assert manager.account_state == 'account_required'
        assert not manager.running and manager.timer is None and manager.next_sync_at is None
        assert not manager.start()
    client = app.test_client()
    for route in ('/', '/team', '/issues', '/monitoring', '/settings/accounts'):
        assert client.get(route).status_code == 200
    sources = client.get('/sync/status').get_json()['sources']
    assert [s['state'] for s in sources[1:]] == ['account_required', 'account_required']
    with sessions() as db:
        assert db.get(OTRSSyncState, 'tickets') is None
        assert db.get(ZabbixSyncState, 'monitoring') is None


def test_save_restart_failed_change_busy_removal_and_secrets(tmp_path, monkeypatch, caplog):
    path, accounts = settings(tmp_path), store(tmp_path)
    database = tmp_path / 'cache.sqlite'
    app = create_app(path, database, auto_sync=False, credential_store=accounts)
    client = app.test_client()
    token = csrf(client)
    seen = []

    def fetch(self):
        seen.append((self.config['user'], self.config['password']))
        return {}

    monkeypatch.setattr(OTRSClient, 'fetch_all', fetch)
    password = 'Never-echo-this-password'
    response = client.post('/settings/accounts/otrs', data={'csrf_token': token, 'user': 'agent', 'password': password})
    assert response.status_code == 303 and len(seen) == 1
    account = accounts.get('otrs', 'https://tickets.example.com/')
    assert account['user'] == 'agent'
    assert 'password' not in app.config['COCKPIT_CONFIG']['otrs']
    manager = app.config['OTRS_SYNC_MANAGER']
    assert manager.run_sync()
    reloaded = create_app(path, database, auto_sync=False, credential_store=store(tmp_path))
    assert reloaded.config['OTRS_SYNC_MANAGER'].account_state is None
    assert reloaded.test_client().get('/sync/status').get_json()['sources'][1]['last_success']
    for route in ('/', '/settings/accounts', '/sync/status'):
        assert password not in client.get(route).get_data(as_text=True)
    assert password not in caplog.text
    assert password.encode() not in accounts.data_path.read_bytes()

    def failure(self):
        raise OTRSError('Invalid login')

    monkeypatch.setattr(OTRSClient, 'fetch_all', failure)
    response = client.post('/settings/accounts/otrs', data={'csrf_token': token, 'user': 'wrong', 'password': 'wrong-password'})
    assert response.status_code == 400
    assert accounts.get('otrs', 'https://tickets.example.com/') == account
    assert b'wrong-password' not in response.data
    assert b'Check connection and save</button>' in response.data  # Form stays usable after failure.
    manager.lock.acquire()
    try:
        assert client.post('/settings/accounts/otrs/remove', data={'csrf_token': token}).status_code == 409
    finally:
        manager.lock.release()
    assert client.post('/settings/accounts/otrs/remove', data={'csrf_token': token}).status_code == 303
    assert manager.account_state == 'account_required'
    assert accounts.get('otrs', 'https://tickets.example.com/') is None
    with make_session(database)() as db:
        assert db.get(OTRSSyncState, 'tickets') is None
        assert db.get(SyncMeta, 'account_binding:otrs') is None


def test_csrf_origin_host_and_disabled_integration(tmp_path, monkeypatch):
    app = create_app(settings(tmp_path, zabbix={'enabled': False}), tmp_path / 'cache.sqlite',
                     auto_sync=False, credential_store=store(tmp_path))
    client = app.test_client()
    token = csrf(client)
    monkeypatch.setattr(OTRSClient, 'fetch_all', lambda self: pytest.fail('Unsafe requests must not connect'))
    assert client.post('/settings/accounts/otrs', data={'user': 'agent', 'password': 'secret'}).status_code == 403
    payload = {'csrf_token': token, 'user': 'agent', 'password': 'secret'}
    assert client.post('/settings/accounts/otrs', data=payload, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/settings/accounts/otrs', data=payload, headers={'Host': 'evil.example'}).status_code == 403
    assert client.post('/settings/accounts/zabbix', data=payload).status_code == 404
    assert app.config['ZABBIX_SYNC_MANAGER'] is None


def test_account_change_clears_only_its_cache_and_starts_sync(tmp_path, monkeypatch):
    path, accounts = settings(tmp_path), store(tmp_path)
    accounts.set('otrs', 'https://tickets.example.com/', 'old-agent', 'old-secret')
    monkeypatch.setattr('app.sync.SyncManager.enable_scheduler', lambda self: None)
    monkeypatch.setattr('app.otrs.OTRSSyncManager.enable_scheduler', lambda self: None)
    app = create_app(path, tmp_path / 'cache.sqlite', credential_store=accounts)
    manager = app.config['OTRS_SYNC_MANAGER']
    started = []
    monkeypatch.setattr(manager, 'enable_scheduler', lambda: started.append(True))
    monkeypatch.setattr(OTRSClient, 'fetch_all', lambda self: {})
    with make_session(tmp_path / 'cache.sqlite')() as db:
        db.add(SyncMeta(key='unrelated', value='preserve'))
        db.add(OTRSSyncState(key='tickets', last_success=datetime.now(timezone.utc)))
        db.commit()
    client = app.test_client()
    assert client.post('/settings/accounts/otrs', data={'csrf_token': csrf(client), 'user': 'new-agent', 'password': 'new-secret'}).status_code == 303
    assert started == [True]
    with make_session(tmp_path / 'cache.sqlite')() as db:
        assert db.get(OTRSSyncState, 'tickets') is None
        assert db.get(SyncMeta, 'unrelated').value == 'preserve'
        assert db.get(SyncMeta, 'account_binding:otrs').value == account_binding('otrs', 'https://tickets.example.com/', accounts.get('otrs', 'https://tickets.example.com/'))


def test_migration_discards_credentials_preserves_settings(tmp_path):
    path = settings(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw['otrs'].update(user='old-agent', password='old-secret')
    raw['zabbix'].update(user='old-monitor', password='old-password')
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path)
    assert 'user' not in config['otrs'] and 'password' not in config['zabbix']
    assert remove_legacy_credentials(path)
    content = path.read_text()
    assert 'old-secret' not in content and 'old-password' not in content
    assert 'old-agent' not in content and 'old-monitor' not in content
    after = load_config(path)
    assert after['otrs']['queue_ids'] == [1]
    assert after['team'][0]['otrs_user'] == 'agent'
    assert after['github']['username'] == 'alice'
    assert not remove_legacy_credentials(path)


def test_github_preflight_reports_missing_or_unsigned_cli(monkeypatch):
    monkeypatch.setattr('app.accounts.shutil.which', lambda _: None)
    with pytest.raises(ValueError, match='required'):
        github_preflight()
    monkeypatch.setattr('app.accounts.shutil.which', lambda _: '/usr/bin/gh')
    monkeypatch.setattr('app.accounts.subprocess.run', lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match='not authenticated'):
        github_preflight()


def test_zabbix_account_setup_disabled_retention_and_url_change(tmp_path, monkeypatch):
    path, accounts = settings(tmp_path), store(tmp_path)
    app = create_app(path, tmp_path / 'cache.sqlite', auto_sync=False, credential_store=accounts)
    client = app.test_client()
    monkeypatch.setattr(ZabbixClient, 'fetch_all', lambda self: ([], []))
    assert client.post('/settings/accounts/zabbix', data={'csrf_token': csrf(client), 'user': 'monitor', 'password': 'monitor-secret'}).status_code == 303
    saved = accounts.get('zabbix', 'https://monitor.example.com/zabbix/')
    assert saved
    raw = yaml.safe_load(path.read_text())
    raw['zabbix']['enabled'] = False
    path.write_text(yaml.safe_dump(raw))
    disabled = create_app(path, tmp_path / 'cache.sqlite', auto_sync=False, credential_store=accounts)
    assert disabled.config['ZABBIX_SYNC_MANAGER'] is None
    assert accounts.get('zabbix', 'https://monitor.example.com/zabbix/') == saved
    raw['zabbix']['enabled'] = True
    path.write_text(yaml.safe_dump(raw))
    enabled = create_app(path, tmp_path / 'cache.sqlite', auto_sync=False, credential_store=accounts)
    assert enabled.config['ZABBIX_SYNC_MANAGER'].account_state is None
    raw['zabbix']['url'] = 'https://other.example.com/zabbix/'
    path.write_text(yaml.safe_dump(raw))
    changed = create_app(path, tmp_path / 'cache.sqlite', auto_sync=False, credential_store=accounts)
    assert changed.config['ZABBIX_SYNC_MANAGER'].account_state == 'account_required'
    assert accounts.get('zabbix', 'https://monitor.example.com/zabbix/') == saved


def test_storage_error_does_not_stop_dashboard_or_replace_key(tmp_path):
    accounts = store(tmp_path)
    accounts.set('otrs', 'https://tickets.example.com/', 'agent', 'secret')
    accounts.key_path.unlink()
    data = accounts.data_path.read_bytes()
    app = create_app(settings(tmp_path), tmp_path / 'cache.sqlite', auto_sync=False, credential_store=accounts)
    client = app.test_client()
    assert client.get('/').status_code == 200
    assert b'Account encryption key is missing' in client.get('/settings/accounts').data
    assert client.get('/sync/status').get_json()['sources'][1]['state'] == 'credential_error'
    assert not accounts.key_path.exists() and accounts.data_path.read_bytes() == data


def test_invalid_yaml_never_echoes_secret_context(tmp_path):
    path = tmp_path / 'config.yml'
    path.write_text('repositories: [\npassword: must-not-appear-in-errors')
    with pytest.raises(ValueError) as error:
        load_config(path)
    assert 'must-not-appear' not in str(error.value)


def test_atomic_storage_failure_keeps_previous_account(tmp_path, monkeypatch):
    accounts = store(tmp_path)
    original = accounts.set('otrs', 'https://tickets.example.com/', 'agent', 'secret')
    before = accounts.data_path.read_bytes()
    def failure(*args):
        raise OSError('Simulated disk error')
    monkeypatch.setattr('app.accounts.os.replace', failure)
    with pytest.raises(AccountError):
        accounts.set('otrs', 'https://tickets.example.com/', 'new', 'new-secret')
    assert accounts.data_path.read_bytes() == before
    assert accounts.get('otrs', 'https://tickets.example.com/') == original
    assert not list(accounts.data_path.parent.glob('.accounts.enc-*'))


def test_account_mutation_rejects_remote_clients(tmp_path):
    app = create_app(settings(tmp_path), tmp_path / 'cache.sqlite', auto_sync=False, credential_store=store(tmp_path))
    client = app.test_client()
    assert client.post('/settings/accounts/otrs', data={'csrf_token': csrf(client), 'user': 'agent', 'password': 'secret'},
                       environ_overrides={'REMOTE_ADDR': '192.0.2.1'}).status_code == 403
