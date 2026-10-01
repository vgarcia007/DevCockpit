import yaml
from app.accounts import AccountStore


def accounts_for(path):
    """Supply fake test credentials via encrypted storage rather than app config."""
    store = AccountStore(path.parent / 'keys/account.key', path.parent / 'data/accounts.enc')
    raw = yaml.safe_load(path.read_text())
    for provider in ('otrs', 'zabbix'):
        config = raw.get(provider)
        if isinstance(config, list):
            config = {key: value for part in config for key, value in part.items()}
        if config and config.get('enabled', True) and config.get('user') and config.get('password'):
            store.set(provider, config['url'], config['user'], config['password'])
    return store
