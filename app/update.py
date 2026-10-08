"""Check whether the checkout running Dev-Cockpit differs from its origin branch."""

import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class GitUpdateChecker:
    def __init__(self, root, ttl=300, runner=None):
        self.root = Path(root)
        self.ttl = ttl
        self.runner = runner or subprocess.run
        self._lock = threading.Lock()
        self._checked_at = 0.0
        self._result = self._unavailable("Not checked yet")

    @staticmethod
    def _unavailable(error):
        return {"available": False, "error": error, "branch": None,
                "local_sha": None, "remote_sha": None, "url": None}

    def _run(self, args):
        try:
            result = self.runner(args, cwd=self.root, capture_output=True, text=True,
                                 timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(str(exc)) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or "git command failed").strip()
            raise RuntimeError(detail.splitlines()[-1] if detail else "git command failed")
        return result.stdout.strip()

    def check(self, force=False):
        now = time.monotonic()
        if not force and now - self._checked_at < self.ttl:
            return self._result
        with self._lock:
            now = time.monotonic()
            if not force and now - self._checked_at < self.ttl:
                return self._result
            try:
                branch = self._run(["git", "branch", "--show-current"])
                remote = self._run(["git", "config", "--get", "branch.%s.remote" % branch]) if branch else ""
                merge_ref = self._run(["git", "config", "--get", "branch.%s.merge" % branch]) if branch else ""
                if not branch or not remote or not merge_ref.startswith("refs/heads/"):
                    result = self._unavailable("Checkout has no tracked remote branch")
                else:
                    local_sha = self._run(["git", "rev-parse", "HEAD"])
                    remote_sha = self._run(["git", "ls-remote", remote, merge_ref]).split()[0]
                    remote_url = self._run(["git", "remote", "get-url", remote])
                    result = {"available": remote_sha != local_sha, "error": None,
                              "branch": branch, "local_sha": local_sha,
                              "remote_sha": remote_sha, "url": self._web_url(remote_url)}
            except (RuntimeError, IndexError) as exc:
                result = self._unavailable(str(exc))
            self._result = result
            self._checked_at = time.monotonic()
            return result

    @staticmethod
    def _web_url(remote_url):
        value = remote_url.strip()
        if value.startswith("git@") and ":" in value:
            host, path = value[4:].split(":", 1)
            return f"https://{host}/{path.removesuffix('.git')}"
        if value.startswith("ssh://"):
            parsed = urlsplit(value)
            return f"https://{parsed.hostname}/{parsed.path.lstrip('/').removesuffix('.git')}"
        if value.startswith(("http://", "https://")):
            return value.removesuffix(".git")
        return None
