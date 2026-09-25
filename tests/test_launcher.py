import signal
from pathlib import Path

from app import launcher


def test_restart_targets_only_this_checkout_and_its_children(monkeypatch):
    def process(cwd, parent, group, command):
        return dict(cwd=cwd, parent=parent, group=group, command=command,
                    started='123', state='S')
    running = {
        100: process(launcher.ROOT, 1, 100, [b'python', b'-m', b'app']),
        101: process(launcher.ROOT, 100, 100, [b'gh', b'api', b'graphql']),
        200: process(Path('/another/checkout'), 1, 200, [b'python', b'-m', b'app']),
        300: process(launcher.ROOT, 1, 300, [b'python', b'-m', b'http.server']),
    }
    stopped = []
    monkeypatch.setattr(launcher, 'processes', lambda: dict(running))
    def kill(pid, sig):
        assert sig == signal.SIGTERM
        stopped.append(pid)
        running.pop(pid, None)
    def kill_group(group, sig):
        for pid in list(running):
            if running[pid]['group'] == group:
                kill(pid, sig)
    monkeypatch.setattr(launcher.os, 'kill', kill)
    monkeypatch.setattr(launcher.os, 'killpg', kill_group)
    launcher.stop_existing()
    assert set(stopped) == {100, 101}
    assert set(running) == {200, 300}


def test_first_start_does_not_signal_other_processes(monkeypatch):
    monkeypatch.setattr(launcher, 'processes', lambda: {})
    def unexpected(*args):
        raise AssertionError('No process should be stopped')
    monkeypatch.setattr(launcher.os, 'kill', unexpected)
    monkeypatch.setattr(launcher.os, 'killpg', unexpected)
    launcher.stop_existing()
