from app import __main__ as server


def test_default_port_and_override(monkeypatch):
    calls = []
    monkeypatch.setattr(server, 'create_app', lambda: type('Server', (), {
        'run': lambda self, **kwargs: calls.append(kwargs),
    })())
    monkeypatch.delenv('PORT', raising=False)
    server.main()
    monkeypatch.setenv('PORT', '8765')
    server.main()
    assert calls == [
        {'host': '127.0.0.1', 'port': 7777, 'debug': False},
        {'host': '127.0.0.1', 'port': 8765, 'debug': False},
    ]
