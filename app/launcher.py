"""Restart only Dev-Cockpit processes belonging to this checkout."""

import fcntl
import os
from pathlib import Path
import signal
import sys
import time

from .config import ROOT, load_config


def processes():
    result = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            result[int(entry.name)] = {
                'parent': int(fields[1]), 'group': int(fields[2]),
                'started': fields[19], 'state': fields[0],
                'cwd': (entry / 'cwd').resolve(strict=True),
                'command': (entry / 'cmdline').read_bytes().split(b'\0')[:-1],
            }
        except (OSError, ValueError):
            continue
    return result


def stop_existing():
    snapshot = processes()
    roots = [pid for pid, process in snapshot.items()
             if process['cwd'] == ROOT and len(process['command']) == 3
             and Path(os.fsdecode(process['command'][0])).name.startswith('python')
             and process['command'][1:] == [b'-m', b'app']]
    targets = set(roots)
    while True:
        children = {pid for pid, process in snapshot.items() if process['parent'] in targets}
        if children <= targets:
            break
        targets.update(children)
    if not roots:
        return
    print('Stopping existing Dev-Cockpit and its running sync...', flush=True)
    for pid in roots:
        try:
            if snapshot[pid]['group'] == pid:
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for pid in targets - set(roots):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def remaining():
        current = processes()
        return [pid for pid in targets if pid in current
                and current[pid]['started'] == snapshot[pid]['started']
                and current[pid]['state'] != 'Z']

    deadline = time.monotonic() + 8
    while remaining() and time.monotonic() < deadline:
        time.sleep(0.1)
    for pid in remaining():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while remaining() and time.monotonic() < deadline:
        time.sleep(0.1)
    if remaining():
        sys.exit('Could not stop the previous Dev-Cockpit process. Start cancelled.')


def main():
    load_config()
    (ROOT / 'instance').mkdir(exist_ok=True)
    with (ROOT / 'instance' / 'start.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('Another Dev-Cockpit restart is already in progress.')
        stop_existing()
        if os.getpgrp() != os.getpid():
            os.setsid()
        print('Starting Dev-Cockpit...', flush=True)
        os.execv(sys.executable, [sys.executable, '-m', 'app'])


if __name__ == '__main__':
    main()
