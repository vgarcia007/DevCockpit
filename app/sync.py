import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select
from .changes import detect_changes
from .github import GitHubCliClient, GitHubError, RateLimitError, project_items, related_issues
from .models import ExternalTeamIssue, ExternalTeamSync, Issue, ObservedChange, Pull, Release, Repository, SyncMeta, UserAvatar

LOG = logging.getLogger(__name__)


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def login(user):
    return user.get("login") if user else None


def names(users):
    return [u["login"] for u in users or [] if u and u.get("login")]


def review_state(pr, reviews, requested):
    if pr.get("merged_at"):
        return "Merged"
    if pr["state"] == "closed":
        return "Closed"
    if pr.get("draft"):
        return "Draft"
    latest = {}
    for review in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        if review.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        author = login(review.get("user"))
        if author:
            latest[author.lower()] = review
    current = [r for r in latest.values() if r["state"] != "DISMISSED"]
    if any(r["state"] == "CHANGES_REQUESTED" for r in current):
        return "Changes Requested"
    if any(r["state"] == "APPROVED" for r in current):
        if requested:
            return "Waiting for Review"
        head_sha = (pr.get("head") or {}).get("sha")
        if head_sha and any(r.get("commit_id") and r["commit_id"] != head_sha for r in current if r["state"] == "APPROVED"):
            return "Approval before latest commit"
        return "Approved"
    return "Waiting for Review"


def ci_state(check_runs, combined_status):
    runs = check_runs.get("check_runs", [])
    contexts = combined_status.get("statuses", [])
    if any(r.get("conclusion") in ("failure", "timed_out", "action_required", "startup_failure") for r in runs) or any(s.get("state") in ("failure", "error") for s in contexts):
        return "Failing"
    if any(r.get("status") != "completed" for r in runs) or any(s.get("state") == "pending" for s in contexts):
        return "Running"
    if runs or contexts:
        return "Passing"
    return "Unknown"


def issue_data(item, repo_name, project, project_state, fields, cfg, priority_value=None, priority_state=None):
    project_item = project.get((item["repository_url"].removeprefix("https://api.github.com/repos/").lower(), item["number"])) if project else None
    status_field = cfg["workflow"]["status_field"]
    priority_field = cfg["priority"]["field"]
    if project_state != "available" or status_field not in fields:
        workflow, workflow_state = None, "unavailable"
    elif project_item is None:
        workflow, workflow_state = None, "not_in_project"
    elif not project_item["status"]:
        workflow, workflow_state = None, "no_status"
    else:
        workflow, workflow_state = project_item["status"], "known"
    if cfg["priority"]["source"] == "project":
        if project_state != "available" or priority_field not in fields or project_item is None:
            priority_value, priority_state = None, "unavailable"
        elif project_item["priority"] is None:
            priority_value, priority_state = None, "no_priority"
        else:
            priority_value, priority_state = project_item["priority"], "known"
    return dict(
        github_id=item["id"], repository_name=repo_name, number=item["number"], title=item["title"],
        body=item.get("body"), url=item["html_url"], state=item["state"], author=login(item.get("user")),
        assignees=names(item.get("assignees")), labels=[x["name"] for x in item.get("labels", [])],
        milestone=(item.get("milestone") or {}).get("title"), created_at=date(item["created_at"]),
        updated_at=date(item["updated_at"]), closed_at=date(item.get("closed_at")),
        workflow=workflow, workflow_state=workflow_state, priority=priority_value,
        priority_state=priority_state or "unavailable", project_item_id=project_item["id"] if project_item else None,
        related_pulls=[],
    )


def pull_data(item, detail, reviews, checks, status, related, repo_name):
    merged_at = detail.get("merged_at") or item.get("merged_at")
    state = "merged" if merged_at else item["state"]
    requested = names(detail.get("requested_reviewers")) + [
        "@" + team["slug"] for team in detail.get("requested_teams", []) if team.get("slug")
    ]
    return dict(
        github_id=item["id"], repository_name=repo_name, number=item["number"], title=item["title"],
        body=item.get("body"), url=item["html_url"], author=login(item.get("user")),
        assignees=names(detail.get("assignees")), requested_reviewers=requested,
        reviews=[{"author": login(r.get("user")), "state": r.get("state"),
                  "submitted_at": r.get("submitted_at"), "commit_id": r.get("commit_id")}
                 for r in reviews], draft=bool(item.get("draft")), state=state, merged=bool(merged_at),
        created_at=date(item["created_at"]), updated_at=date(item["updated_at"]),
        closed_at=date(item.get("closed_at")), merged_at=date(merged_at),
        base_branch=item["base"]["ref"], head_branch=item["head"]["ref"], head_sha=item["head"]["sha"],
        mergeable=detail.get("mergeable"), mergeable_state=detail.get("mergeable_state"),
        ci_state=checks, review_state=review_state({**item, "merged_at": merged_at}, reviews, requested) if status == "known" else "Unknown",
        related_issues=related,
    )


def release_data(item, repo_name):
    return dict(github_id=item["id"], repository_name=repo_name, name=item.get("name"),
                body=item.get("body"), tag=item["tag_name"], url=item["html_url"], created_at=date(item["created_at"]),
                published_at=date(item.get("published_at")), draft=bool(item.get("draft")),
                prerelease=bool(item.get("prerelease")), author=login(item.get("author")))


def search_assigned_issues(client, login_name, get=None):
    """Fetch accessible open issues assigned to one person, up to GitHub's search cap."""
    get = get or client.get
    items = []
    incomplete = False
    query = f"is:issue is:open assignee:{login_name}"
    for page in range(1, 11):
        response = get("/search/issues", q=query, per_page=100, page=page)
        if not isinstance(response, dict) or not isinstance(response.get("items"), list) or not isinstance(response.get("total_count"), int):
            raise GitHubError("Unexpected GitHub issue search response")
        batch = response["items"]
        items.extend(batch)
        incomplete = incomplete or bool(response.get("incomplete_results")) or response["total_count"] > 1000
        if len(batch) < 100 or len(items) >= min(response["total_count"], 1000):
            incomplete = incomplete or len(items) < min(response["total_count"], 1000)
            break
    return items[:1000], incomplete


ISSUE_FIELD_VALUES_QUERY = """
query($ids:[ID!]!) {
  nodes(ids:$ids) {
    ... on Issue {
      id
      issueFieldValues(first:100) {
        nodes {
          ... on IssueFieldSingleSelectValue {
            name
            field { ... on IssueFieldSingleSelect { name } }
          }
          ... on IssueFieldTextValue {
            value
            field { ... on IssueFieldText { name } }
          }
        }
      }
    }
  }
}
"""


def issue_field_priorities(client, issues, field):
    """Read one organization issue field for a batch of GitHub issues."""
    ids = [issue["node_id"] for issue in issues]
    nodes = client.graphql(ISSUE_FIELD_VALUES_QUERY, {"ids": ids}).get("nodes")
    if not isinstance(nodes, list) or len(nodes) != len(ids):
        raise GitHubError("Unexpected GitHub issue field response")
    result = {}
    for node_id, node in zip(ids, nodes):
        if not isinstance(node, dict) or node.get("id") != node_id:
            result[node_id] = (None, "unavailable")
            continue
        values = (node.get("issueFieldValues") or {}).get("nodes")
        if not isinstance(values, list):
            result[node_id] = (None, "unavailable")
            continue
        selected = next((value.get("name", value.get("value")) for value in values
                         if value and (value.get("field") or {}).get("name") == field), None)
        result[node_id] = (selected, "known" if selected is not None else "no_priority")
    return result


class SyncManager:
    def __init__(self, session_factory, config):
        self.sessions = session_factory
        self.config = config
        self.lock = threading.Lock()
        self.running = False
        self.scheduler_enabled = False
        self.timer = None
        self.next_sync_at = None
        with self.sessions() as session:
            saved_pause = session.get(SyncMeta, "rate_limit_until")
            saved_failures = session.get(SyncMeta, "rate_limit_failures")
        self.cooldown_until = date(saved_pause.value) if saved_pause and saved_pause.value else None
        self.rate_failures = int(saved_failures.value) if saved_failures and saved_failures.value else 0

    def enable_scheduler(self):
        self.scheduler_enabled = True
        if self.cooldown_until and self.cooldown_until > datetime.now(timezone.utc):
            self._schedule(self.cooldown_until)
        else:
            self.start()

    def _schedule(self, when):
        if self.timer:
            self.timer.cancel()
        self.next_sync_at = when
        self.timer = threading.Timer(max(0, (when - datetime.now(timezone.utc)).total_seconds()), self.start)
        self.timer.daemon = True
        self.timer.start()

    def pause_reason(self):
        return "rate_limit" if self.cooldown_until and self.cooldown_until > datetime.now(timezone.utc) else None

    def status(self):
        return {"running": self.running,
                "next_sync_at": self.next_sync_at.isoformat() if self.next_sync_at and not self.running else None,
                "rate_limit_until": self.cooldown_until.isoformat() if self.pause_reason() else None}

    def start(self):
        if self.pause_reason():
            if self.scheduler_enabled:
                self._schedule(self.cooldown_until)
            return False
        if not self.lock.acquire(blocking=False):
            return False
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.next_sync_at = None
        self.running = True
        thread = threading.Thread(target=self._run, daemon=True, name="github-sync")
        thread.start()
        return True

    def run_sync(self):
        if self.pause_reason():
            return False
        if not self.lock.acquire(blocking=False):
            return False
        self.running = True
        self._run()
        return True

    def _record_rate_limit(self, exc):
        self.rate_failures += 1
        now = datetime.now(timezone.utc)
        fallback = now + timedelta(seconds=min(60 * 2 ** min(self.rate_failures - 1, 6), 3600))
        self.cooldown_until = max(now + timedelta(seconds=1), exc.retry_at or fallback)
        with self.sessions() as session:
            session.merge(SyncMeta(key="rate_limit_until", value=self.cooldown_until.isoformat()))
            session.merge(SyncMeta(key="rate_limit_failures", value=str(self.rate_failures)))
            session.commit()
        LOG.warning("GitHub rate limit; next attempt after %s", self.cooldown_until.isoformat())

    def _clear_rate_limit(self):
        if not self.cooldown_until and not self.rate_failures:
            return
        self.cooldown_until = None
        self.rate_failures = 0
        with self.sessions() as session:
            session.merge(SyncMeta(key="rate_limit_until", value=""))
            session.merge(SyncMeta(key="rate_limit_failures", value="0"))
            session.commit()

    def _run(self):
        successes = 0
        rate_limited = False
        try:
            with GitHubCliClient(cache_sessions=self.sessions) as client:
                org_diagnostic = None
                org_checked = False
                for repo in self.config["repositories"]:
                    try:
                        self.sync_repo(client, repo)
                        successes += 1
                    except RateLimitError:
                        raise
                    except Exception as exc:
                        if isinstance(exc, GitHubError) and exc.status == 404 and not org_checked:
                            org_checked = True
                            organization = self.config.get("github", {}).get("organization")
                            if organization:
                                try:
                                    client.graphql("query($login:String!){organization(login:$login){login}}",
                                                   {"login": organization})
                                except RateLimitError:
                                    raise
                                except GitHubError as diagnostic:
                                    org_diagnostic = str(diagnostic)
                        if isinstance(exc, GitHubError) and exc.status == 404 and org_diagnostic:
                            exc = GitHubError(f"Organization access unavailable: {org_diagnostic}", 403)
                        if isinstance(exc, GitHubError):
                            LOG.warning("Sync failed for %s: %s", repo["name"], exc)
                        else:
                            LOG.exception("Sync failed for %s", repo["name"])
                        with self.sessions() as session:
                            row = session.get(Repository, repo["name"])
                            if not row:
                                row = Repository(name=repo["name"], full_name=repo["full_name"],
                                                 github_url="https://github.com/" + repo["full_name"],
                                                 project_number=repo.get("project_number"), project_state="unavailable")
                                session.add(row)
                            row.error = str(exc)
                            session.commit()
                self.rebuild_links()
                self.sync_team_issues(client)
                self._clear_rate_limit()
                LOG.info("Sync completed: %s/%s repositories", successes, len(self.config["repositories"]))
        except RateLimitError as exc:
            rate_limited = True
            self._record_rate_limit(exc)
        except Exception:
            LOG.exception("Sync interrupted")
        finally:
            try:
                if successes:
                    with self.sessions() as session:
                        session.merge(SyncMeta(key="last_success", value=datetime.now(timezone.utc).isoformat()))
                        session.commit()
            finally:
                self.running = False
                self.lock.release()
                if self.scheduler_enabled:
                    next_time = datetime.now(timezone.utc) + timedelta(seconds=int(self.config["sync"]["interval_seconds"]))
                    self._schedule(max(next_time, self.cooldown_until) if rate_limited and self.cooldown_until else next_time)

    def sync_team_issues(self, client):
        configured = {repo["full_name"].lower() for repo in self.config["repositories"]}
        request_times = []

        def limited_get(path, **params):
            now = time.monotonic()
            request_times[:] = [stamp for stamp in request_times if now - stamp < 60]
            if len(request_times) >= 25:
                time.sleep(max(0, 60 - (now - request_times[0])))
                now = time.monotonic()
                request_times[:] = [stamp for stamp in request_times if now - stamp < 60]
            request_times.append(now)
            return client.get(path, **params)

        for person in self.config["team"]:
            user = person["github"].lower()
            with self.sessions() as session:
                state = session.get(ExternalTeamSync, user)
                if state and state.last_attempt and datetime.now(timezone.utc) - state.last_attempt.replace(tzinfo=timezone.utc) < timedelta(minutes=5):
                    continue
            try:
                items, incomplete = search_assigned_issues(client, user, limited_get)
                rows_by_id = {}
                for item in items:
                    repository_url = item.get("repository_url") or ""
                    prefix = "https://api.github.com/repos/"
                    if not repository_url.startswith(prefix):
                        raise GitHubError("GitHub issue search omitted a repository")
                    full_name = repository_url[len(prefix):]
                    if full_name.lower() in configured or item.get("state") != "open" or "pull_request" in item:
                        continue
                    rows_by_id[item["id"]] = ExternalTeamIssue(login=user, github_id=item["id"],
                        repository_name=full_name, number=item["number"], title=item["title"],
                        url=item["html_url"], updated_at=date(item["updated_at"]))
            except RateLimitError:
                raise
            except Exception as exc:
                LOG.warning("External team issue search failed for %s: %s", user, exc)
                with self.sessions() as session:
                    state = session.get(ExternalTeamSync, user) or ExternalTeamSync(login=user)
                    state.last_attempt = datetime.now(timezone.utc)
                    state.error = str(exc)
                    session.add(state)
                    session.commit()
                continue
            with self.sessions() as session:
                session.execute(delete(ExternalTeamIssue).where(ExternalTeamIssue.login == user))
                session.add_all(rows_by_id.values())
                state = session.get(ExternalTeamSync, user) or ExternalTeamSync(login=user)
                state.last_attempt = datetime.now(timezone.utc)
                state.last_success = state.last_attempt
                state.error = None
                state.incomplete = incomplete
                session.add(state)
                session.commit()

    def rebuild_links(self):
        with self.sessions() as session:
            repos = {r.name: r.full_name.lower() for r in session.scalars(select(Repository)).all()}
            issues = {(repos[i.repository_name], i.number): i for i in session.scalars(select(Issue)).all() if i.repository_name in repos}
            for issue in issues.values():
                issue.related_pulls = []
            for pull in session.scalars(select(Pull)).all():
                for ref in pull.related_issues:
                    issue = issues.get((ref["repository"].lower(), ref["number"]))
                    if issue:
                        issue.related_pulls = [*issue.related_pulls, {"number": pull.number, "url": pull.url,
                            "state": pull.state, "repository": pull.repository_name}]
            session.commit()

    def sync_repo(self, client, repo):
        full = repo["full_name"]
        base = f"/repos/{full}"
        LOG.info("Syncing %s", repo["name"])
        info = client.get(base)
        warnings = []
        issues = [i for i in client.pages(f"{base}/issues", state="all") if "pull_request" not in i]
        pulls = list(client.pages(f"{base}/pulls", state="all"))
        releases = list(client.pages(f"{base}/releases"))
        avatars = {}

        def remember(user):
            if user and user.get("login") and user.get("avatar_url"):
                avatars[user["login"].lower()] = user["avatar_url"]

        for item in issues:
            remember(item.get("user"))
            for assignee in item.get("assignees") or []:
                remember(assignee)
        for item in pulls:
            remember(item.get("user"))
        for item in releases:
            remember(item.get("author"))
        project, fields, project_state = {}, set(), "not_configured"
        if repo.get("project_number") is not None:
            try:
                owner = repo.get("project_owner") or self.config.get("github", {}).get("organization") or full.split("/")[0]
                project, fields = project_items(client, owner, repo["project_number"],
                    self.config["workflow"]["status_field"], self.config["priority"]["field"],
                    repo.get("project_owner_type", "organization"))
                project_state = "available"
                if self.config["workflow"]["status_field"] not in fields:
                    warnings.append("Project status field is missing")
                if self.config["priority"]["source"] == "project" and self.config["priority"]["field"] not in fields:
                    warnings.append("Project priority field is missing")
            except GitHubError as exc:
                if isinstance(exc, RateLimitError):
                    raise
                project_state = "unavailable"
                warnings.append(f"Project unavailable: {exc}")
        issue_priority_field_available = False
        if self.config["priority"]["source"] == "issue_field":
            try:
                owner = full.split("/")[0]
                field_names = {f.get("name") for f in client.pages(f"/orgs/{owner}/issue-fields")}
                issue_priority_field_available = self.config["priority"]["field"] in field_names
                if not issue_priority_field_available:
                    warnings.append("Organization priority issue field is missing")
            except GitHubError as exc:
                if isinstance(exc, RateLimitError):
                    raise
                if exc.status != 404 or owner.casefold() == self.config.get("github", {}).get("organization", "").casefold():
                    warnings.append(f"Organization issue fields unavailable: {exc}")
        issue_priorities = {}
        if issue_priority_field_available:
            with_ids = [item for item in issues if item.get("node_id")]
            for offset in range(0, len(with_ids), 100):
                batch = with_ids[offset:offset + 100]
                try:
                    issue_priorities.update(issue_field_priorities(client, batch, self.config["priority"]["field"]))
                except GitHubError as exc:
                    if isinstance(exc, RateLimitError):
                        raise
                    warnings.append(f"Issue fields unavailable: {exc}")
        issue_rows = []
        for item in issues:
            priority, pstate = issue_priorities.get(item.get("node_id"), (None, "unavailable"))
            issue_rows.append(issue_data(item, repo["name"], project, project_state, fields, self.config, priority, pstate))
        try:
            relations = related_issues(client, [p for p in pulls if p["state"] == "open"])
        except GitHubError as exc:
            if isinstance(exc, RateLimitError):
                raise
            relations = {}
            warnings.append(f"Issue–PR links unavailable: {exc}")
        pull_rows = []
        for item in pulls:
            detail, reviews, checks, status = item, [], "Unknown", "known"
            if item["state"] == "open":
                try:
                    detail = client.get(f"{base}/pulls/{item['number']}")
                    reviews = list(client.pages(f"{base}/pulls/{item['number']}/reviews"))
                except GitHubError as exc:
                    if isinstance(exc, RateLimitError):
                        raise
                    status = "unavailable"
                    warnings.append(f"PR #{item['number']} reviews unavailable: {exc}")
                try:
                    sha = item["head"]["sha"]
                    runs = client.get(f"{base}/commits/{sha}/check-runs", per_page=100)
                    context = client.get(f"{base}/commits/{sha}/status")
                    if runs.get("total_count", 0) > len(runs.get("check_runs", [])):
                        all_runs = list(runs.get("check_runs", []))
                        page = 2
                        while len(all_runs) < runs["total_count"]:
                            extra = client.get(f"{base}/commits/{sha}/check-runs", per_page=100, page=page)
                            if not extra.get("check_runs"):
                                break
                            all_runs.extend(extra["check_runs"])
                            page += 1
                        runs = {"check_runs": all_runs}
                    checks = ci_state(runs, context)
                except GitHubError as exc:
                    if isinstance(exc, RateLimitError):
                        raise
                    warnings.append(f"PR #{item['number']} checks unavailable: {exc}")
            for user in detail.get("assignees") or []:
                remember(user)
            for user in detail.get("requested_reviewers") or []:
                remember(user)
            for review in reviews:
                remember(review.get("user"))
            pull_rows.append(pull_data(item, detail, reviews, checks, status, relations.get(item["number"], []), repo["name"]))
        issue_by_number = {row["number"]: row for row in issue_rows}
        for row in pull_rows:
            for ref in row["related_issues"]:
                if ref["repository"].lower() == full.lower() and ref["number"] in issue_by_number:
                    issue_by_number[ref["number"]]["related_pulls"].append({"number": row["number"], "url": row["url"], "state": row["state"]})
        release_rows = [release_data(item, repo["name"]) for item in releases]
        with self.sessions() as session:
            row = session.get(Repository, repo["name"])
            if not row:
                row = Repository(name=repo["name"])
                session.add(row)
            sync_time = datetime.now(timezone.utc)
            if row.last_sync:
                old_issues = {i.github_id: i for i in session.scalars(select(Issue).where(Issue.repository_name == repo["name"]))}
                old_pulls = {p.github_id: p for p in session.scalars(select(Pull).where(Pull.repository_name == repo["name"]))}
                old_releases = {r.github_id: r for r in session.scalars(select(Release).where(Release.repository_name == repo["name"]))}
                changes = detect_changes(old_issues, old_pulls, old_releases, issue_rows, pull_rows,
                    release_rows, self.config["workflow"]["values"])
                session.add_all(ObservedChange(repository_name=repo["name"], observed_at=sync_time, **change)
                                for change in changes)
                session.execute(delete(ObservedChange).where(ObservedChange.observed_at < sync_time - timedelta(days=30)))
            row.full_name = full
            row.github_url = info["html_url"]
            row.project_number = repo.get("project_number")
            row.project_state = project_state
            row.error = "; ".join(dict.fromkeys(warnings)) or None
            row.last_sync = sync_time
            for model, rows in ((Issue, issue_rows), (Pull, pull_rows), (Release, release_rows)):
                session.execute(delete(model).where(model.repository_name == repo["name"]))
                session.add_all(model(**data) for data in rows)
            for user_login, avatar_url in avatars.items():
                session.merge(UserAvatar(login=user_login, url=avatar_url))
            session.commit()
        LOG.info("%s: Issues %s, pull requests %s, project items %s, releases %s", repo["name"], len(issues), len(pulls), len(project), len(releases))
