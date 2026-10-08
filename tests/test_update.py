import subprocess

from app.update import GitUpdateChecker


def runner_factory(remote_sha):
    def runner(args, **kwargs):
        command = args[1:]
        if command[:2] == ["branch", "--show-current"]:
            output = "main\n"
        elif command[:2] == ["rev-parse", "HEAD"]:
            output = "local-sha\n"
        elif command[:3] == ["config", "--get", "branch.main.remote"]:
            output = "origin\n"
        elif command[:3] == ["config", "--get", "branch.main.merge"]:
            output = "refs/heads/main\n"
        elif command[:2] == ["ls-remote", "origin"]:
            output = f"{remote_sha}\trefs/heads/main\n"
        elif command[:3] == ["remote", "get-url", "origin"]:
            output = "git@github.com:example/devcockpit.git\n"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, "")
    return runner


def test_checkout_matches_origin_and_is_cached(tmp_path):
    calls = []
    runner = runner_factory("local-sha")
    checker = GitUpdateChecker(tmp_path, runner=lambda args, **kwargs: (calls.append(args) or runner(args, **kwargs)))
    result = checker.check()
    assert result == {"available": False, "error": None, "branch": "main",
                      "local_sha": "local-sha", "remote_sha": "local-sha",
                      "url": "https://github.com/example/devcockpit"}
    assert checker.check() == result
    assert len(calls) == 6


def test_checkout_reports_remote_difference(tmp_path):
    result = GitUpdateChecker(tmp_path, runner=runner_factory("remote-sha")).check()
    assert result["available"] is True
    assert result["branch"] == "main"


def test_untracked_or_invalid_checkout_is_quiet(tmp_path):
    def runner(args, **kwargs):
        if args[1:3] == ["branch", "--show-current"]:
            return subprocess.CompletedProcess(args, 0, "\n", "")
        raise AssertionError(args)
    result = GitUpdateChecker(tmp_path, runner=runner).check()
    assert result["available"] is False
    assert result["error"] == "Checkout has no tracked remote branch"
