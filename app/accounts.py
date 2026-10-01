"""Local encrypted accounts; keys and ciphertext live outside the checkout."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AccountError(Exception):
    pass


def server_url(value):
    parts = urlsplit(value)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('Integration URL must be an HTTPS frontend URL without credentials, query or fragment')
    port = parts.port
    hostname = parts.hostname.lower()
    if ':' in hostname:
        hostname = '[' + hostname + ']'
    netloc = hostname + (f':{port}' if port and port != 443 else '')
    return urlunsplit(('https', netloc, parts.path.rstrip('/') + '/', '', ''))


def account_binding(provider, url, account):
    return hashlib.sha256((provider + '\0' + server_url(url) + '\0' + account['revision']).encode()).hexdigest()


def private_directory(path):
    if path.is_symlink():
        raise AccountError('Account storage directory must not be a symbolic link')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_write(path, content):
    if path.is_symlink():
        raise AccountError('Account storage file must not be a symbolic link')
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AccountStore:
    def __init__(self, key_path=None, data_path=None):
        self.key_path = Path(key_path or Path.home() / '.config/devcockpit/account.key')
        self.data_path = Path(data_path or Path.home() / '.local/share/devcockpit/accounts.enc')
        self.lock = threading.RLock()

    def _read(self, path):
        if path.is_symlink():
            raise AccountError('Account storage file must not be a symbolic link')
        path.chmod(0o600)
        return path.read_bytes()

    def _load(self):
        if any(path.is_symlink() for path in (self.key_path, self.data_path, self.key_path.parent, self.data_path.parent)):
            raise AccountError('Account storage must not use symbolic links')
        for directory in (self.key_path.parent, self.data_path.parent):
            if directory.exists():
                private_directory(directory)
        if not self.data_path.exists():
            if self.key_path.exists() and len(self._read(self.key_path)) != 32:
                raise AccountError('Account encryption key is invalid')
            return {}
        if not self.key_path.exists():
            raise AccountError('Account encryption key is missing; restore the matching key or reset both storage files')
        try:
            key = self._read(self.key_path)
            payload = json.loads(self._read(self.data_path))
            if len(key) != 32 or payload['version'] != 1:
                raise ValueError()
            nonce = base64.b64decode(payload['nonce'], validate=True)
            if len(nonce) != 12:
                raise ValueError()
            plain = AESGCM(key).decrypt(nonce, base64.b64decode(payload['ciphertext'], validate=True), b'devcockpit-accounts-v1')
            accounts = json.loads(plain)
            if not isinstance(accounts, dict) or any(not isinstance(a, dict) or
                not all(isinstance(a.get(k), str) and a[k] for k in ('user', 'password', 'revision')) for a in accounts.values()):
                raise ValueError()
            return accounts
        except (ValueError, KeyError, TypeError, InvalidTag) as exc:
            raise AccountError('Account storage cannot be decrypted; restore the matching files or reset both and enter accounts again') from exc

    def get(self, provider, url):
        with self.lock:
            try:
                return self._load().get(provider + ':' + server_url(url))
            except OSError as exc:
                raise AccountError('Account storage is not accessible; check file permissions') from exc

    def _change(self, provider, url, account):
        with self.lock:
            try:
                private_directory(self.key_path.parent)
                private_directory(self.data_path.parent)
                lock_path = self.data_path.parent / 'accounts.lock'
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'a') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    fcntl.flock(stream, fcntl.LOCK_EX)
                    accounts = self._load()
                    identity = provider + ':' + server_url(url)
                    if account is None:
                        accounts.pop(identity, None)
                    else:
                        accounts[identity] = account
                    if not self.key_path.exists():
                        # Never generate a replacement key for an existing ciphertext.
                        fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, 'wb') as key_file:
                            key_file.write(AESGCM.generate_key(bit_length=256))
                            key_file.flush()
                            os.fsync(key_file.fileno())
                    key = self._read(self.key_path)
                    nonce = secrets.token_bytes(12)
                    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(accounts).encode(), b'devcockpit-accounts-v1')
                    atomic_write(self.data_path, json.dumps({'version': 1,
                        'nonce': base64.b64encode(nonce).decode(), 'ciphertext': base64.b64encode(ciphertext).decode()}).encode())
            except OSError as exc:
                raise AccountError('Account storage could not be written; check file permissions') from exc

    def set(self, provider, url, user, password):
        if not isinstance(user, str) or not user or not isinstance(password, str) or not password:
            raise AccountError('Enter both username and password')
        account = {'user': user, 'password': password, 'revision': secrets.token_hex(16)}
        self._change(provider, url, account)
        return account

    def remove(self, provider, url):
        self._change(provider, url, None)


class AccountSyncMixin:
    """Pause optional syncs when credentials are missing, without retry timers."""
    def init_accounts(self, credential_provider):
        self.credential_provider = credential_provider
        self.account_state = None
        self.account_error = None

    def credentials_available(self):
        if self.credential_provider is None:
            return True  # Standalone clients/managers can receive an explicit ephemeral config.
        try:
            account = self.credential_provider()
            self.account_state = None if account else 'account_required'
            self.account_error = None
            return bool(account)
        except AccountError as exc:
            self.account_state, self.account_error = 'credential_error', str(exc)
            return False

    def client_config(self):
        if self.credential_provider is None:
            return dict(self.config)
        account = self.credential_provider()
        if not account:
            raise AccountError('Account required')
        return {**self.config, 'user': account['user'], 'password': account['password']}

    def schedule_next(self, when=None):
        if self.timer:
            self.timer.cancel()
        self.next_sync_at = when or datetime.now(timezone.utc) + timedelta(seconds=int(self.config['interval_seconds']))
        self.timer = threading.Timer(max(0, (self.next_sync_at - datetime.now(timezone.utc)).total_seconds()), self.start)
        self.timer.daemon = True
        self.timer.start()


def github_preflight():
    if not shutil.which('gh'):
        raise ValueError('GitHub CLI (gh) is required. Install it and run gh auth login.')
    try:
        result = subprocess.run(['gh', 'auth', 'status', '--hostname', 'github.com'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('Could not check GitHub authentication. Run gh auth status.') from exc
    if result.returncode:
        raise ValueError('GitHub CLI is not authenticated for github.com. Run gh auth login.')
