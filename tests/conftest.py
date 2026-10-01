import pytest
from app.accounts import AccountStore


@pytest.fixture(autouse=True)
def isolated_default_account_store(tmp_path, monkeypatch):
    # Tests must never inspect or change the user's real account files.
    store = AccountStore(tmp_path / 'keys/account.key', tmp_path / 'data/accounts.enc')
    monkeypatch.setattr('app.web.AccountStore', lambda: store)
