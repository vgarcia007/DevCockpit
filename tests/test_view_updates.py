from pathlib import Path
import shutil
import subprocess

import pytest


def test_view_updates_do_not_interrupt_users_and_restore_position():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the browser-script regression checks')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / 'tests/view_updates.cjs'), str(root / 'app/static/app.js')],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
