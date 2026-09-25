import logging
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from flask import Flask, abort, redirect, render_template, request, url_for, jsonify
from sqlalchemy import select
from .config import ROOT, load_config
from .models import Issue, ObservedChange, Pull, Release, Repository, SyncMeta, UserAvatar, make_session
from .sync import SyncManager

LOG = logging.getLogger(__name__)
PRIORITIES = ["Urgent", "High", "Medium", "Low"]
LOCAL_TZ = ZoneInfo("Europe/Berlin")


def age_days(value):
    if not value:
        return None
    return max(0, (datetime.now(timezone.utc) - value.replace(tzinfo=timezone.utc)).days)


def open_days(pull):
    end = pull.merged_at or pull.closed_at or datetime.now(timezone.utc)
    return max(0, (end.replace(tzinfo=timezone.utc) - pull.created_at.replace(tzinfo=timezone.utc)).days)


def fmt_date(value):
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%d.%m.%Y") if value else "—"


def fmt_time(value):
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M") if value else "—"


def brief_as_text(attention, team, reviews, ready, shipped, cards, last_sync):
    def cell(value):
        return " ".join(str(value or "").split())

    def item_label(item):
        kind = "PR " if isinstance(item, Pull) else ""
        return f"{cell(item.repository_name)} {kind}#{item.number} | {cell(item.title)}"

    def section(label, total, shown):
        return f"{label} ({total} total, {shown} shown)"

    lines = ["BRIEF", f"Last sync: {fmt_time(last_sync) if last_sync else 'pending'}", ""]
    lines.append(section("NEEDS ME", len(attention), min(len(attention), 4)))
    for reason, item, why in attention[:4]:
        lines.append(f"- {cell(reason)} | {item_label(item)} | {cell(why)}")
    if not attention:
        lines.append("- None")

    lines.extend(["", "TEAM NOW"])
    for member in team:
        person = member["person"]
        lines.append(
            f"- {cell(person['name'])} (@{cell(person['github'])}) | "
            f"Now {len(member['active'])} | Review {len(member['review'])} | "
            f"Ready next {len(member['ready_next'])} | Assigned backlog {len(member['assigned_backlog'])}"
        )
        if member["active"]:
            lines.append(f"  Now: {item_label(member['active'][0])}")
    if not team:
        lines.append("- None")

    lines.extend(["", section("REVIEWS", len(reviews), min(len(reviews), 3))])
    for pull in reviews[:3]:
        lines.append(f"- {item_label(pull)} | Opened {fmt_date(pull.created_at)} | "
                     f"Open {age_days(pull.created_at)}d")
    if not reviews:
        lines.append("- None")

    lines.extend(["", section("READY + UNASSIGNED", len(ready), min(len(ready), 4))])
    for issue in ready[:4]:
        priority = f" | {cell(issue.priority)}" if issue.priority_state == "known" else (
            " | Unavailable" if issue.priority_state == "unavailable" else "")
        lines.append(f"- {item_label(issue)}{priority}")
    if not ready:
        lines.append("- None")

    lines.extend(["", section("RECENTLY SHIPPED", len(shipped), min(len(shipped), 3))])
    for release in shipped[:3]:
        prerelease = " | Prerelease" if release.prerelease else ""
        lines.append(f"- {fmt_date(release.published_at)} | {cell(release.repository_name)} | "
                     f"{cell(release.tag)}{prerelease}")
    if not shipped:
        lines.append("- None")

    lines.extend(["", "LATEST RELEASES"])
    for card in cards:
        latest = card["latest"]
        if latest:
            release = latest[0]
            lines.append(f"- {cell(card['repo'].name)} | {cell(release.tag)} | "
                         f"{fmt_date(release.published_at or release.created_at)}")
        else:
            lines.append(f"- {cell(card['repo'].name)} | No releases")
    if not cards:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def brief_with_prompt(brief_text, prompt_path=None):
    path = Path(prompt_path or ROOT / "brief_prompt.txt")
    return path.read_text(encoding="utf-8").rstrip() + "\n\n" + brief_text


def release_window_start(now, period):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return midnight
    if period == "week":
        return midnight - timedelta(days=midnight.weekday())
    month_index = now.year * 12 + now.month - 1 - 3
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return now.replace(year=year, month=month, day=min(now.day, monthrange(year, month)[1]))


def published_releases(releases, since, repository=""):
    return sorted((release for release in releases
                   if release.published_at and not release.draft
                   and release.published_at.replace(tzinfo=timezone.utc) >= since
                   and (not repository or release.repository_name == repository)),
                  key=lambda release: release.published_at, reverse=True)


def github_project_url(repository, organization):
    number = repository.get("project_number")
    if number is None:
        return None
    owner = repository.get("project_owner") or organization or repository["full_name"].split("/")[0]
    kind = "users" if repository.get("project_owner_type") == "user" else "orgs"
    return f"https://github.com/{kind}/{owner}/projects/{number}"


def priority_label(issue):
    return issue.priority if issue.priority_state == "known" else ("No priority" if issue.priority_state == "no_priority" else "Unavailable")


def workflow_label(issue):
    return issue.workflow if issue.workflow_state == "known" else {
        "not_in_project": "Not in project", "no_status": "No status"
    }.get(issue.workflow_state, "Unavailable")


def latest_releases(releases, repository):
    items = [r for r in releases if r.repository_name == repository and not r.draft and not r.prerelease and r.published_at]
    return sorted(items, key=lambda r: r.published_at or r.created_at, reverse=True)[:2]


def workflow_readable(repo):
    return repo.project_state == "available" and "Project status field is missing" not in (repo.error or "")


def create_app(config_path=None, database_path=None, auto_sync=True):
    cfg = load_config(config_path)
    instance = ROOT / "instance"
    instance.mkdir(exist_ok=True)
    sessions = make_session(database_path or instance / "cockpit.sqlite")
    configured = {repo["name"] for repo in cfg["repositories"]}
    configured_repositories = {repo["name"]: repo for repo in cfg["repositories"]}
    with sessions() as session:
        for existing in session.scalars(select(Repository)).all():
            if existing.name not in configured:
                session.delete(existing)
        for repo in cfg["repositories"]:
            if not session.get(Repository, repo["name"]):
                session.add(Repository(name=repo["name"], full_name=repo["full_name"],
                    github_url="https://github.com/" + repo["full_name"],
                    project_number=repo.get("project_number"), project_state="not_synced"))
        session.commit()
    manager = SyncManager(sessions, cfg)
    app = Flask(__name__)
    app.config["COCKPIT_CONFIG"] = cfg
    app.config["SYNC_MANAGER"] = manager
    app.jinja_env.filters["date"] = fmt_date
    app.jinja_env.filters["datetime"] = fmt_time
    app.jinja_env.filters["age"] = age_days
    app.jinja_env.filters["open_days"] = open_days
    app.jinja_env.filters["priority"] = priority_label
    app.jinja_env.filters["workflow"] = workflow_label

    def snapshot():
        with sessions() as session:
            return (
                session.scalars(select(Repository)).all(),
                session.scalars(select(Issue)).all(),
                session.scalars(select(Pull)).all(),
                session.scalars(select(Release)).all(),
                session.get(SyncMeta, "last_success"),
            )

    def status(issue, key):
        return issue.workflow_state == "known" and issue.workflow == cfg["workflow"]["values"].get(key)

    def ordered_issues(items):
        return sorted(items, key=lambda i: (PRIORITIES.index(i.priority) if i.priority in PRIORITIES else 4, i.repository_name.lower(), i.number))

    def filter_repo(items):
        repo = request.args.get("repo", "")
        return [i for i in items if not repo or i.repository_name == repo]

    def common(repos, meta):
        warnings = {}
        for repo in repos:
            if repo.error:
                warnings.setdefault(repo.error, []).append(repo.name)
        with sessions() as session:
            avatars = {user.login: user.url for user in session.scalars(select(UserAvatar)).all()}
        return {"repositories": repos, "last_sync": datetime.fromisoformat(meta.value) if meta else None,
                "sync_running": manager.running, "github_user": cfg.get("github", {}).get("username", ""),
                "avatars": avatars,
                "workflow_coverage": sum(workflow_readable(r) for r in repos),
                "workflow_total": len(repos),
                "sync_warnings": [{"error": error, "repositories": names} for error, names in warnings.items()]}

    def team_data(issues, pulls):
        result = []
        for person in cfg["team"]:
            user = person["github"].lower()
            assigned = [i for i in issues if i.state == "open" and user in [a.lower() for a in i.assignees]]
            result.append({"person": person,
                           "active": [i for i in assigned if status(i, "in_progress")],
                           "review": [i for i in assigned if status(i, "in_review")],
                           "ready_next": ordered_issues([i for i in assigned if status(i, "ready")]),
                           "assigned_backlog": ordered_issues([i for i in assigned if status(i, "backlog")]),
                           "next": [i for i in assigned if status(i, "ready") or status(i, "backlog")],
                           "prs": [p for p in pulls if p.state == "open" and (p.author or "").lower() == user]})
        return result

    def attention(issues, pulls):
        result = []
        for i in issues:
            if i.state == "open" and i.priority_state == "known" and i.priority == "Urgent":
                result.append(("URGENT", i, "Open issue with GitHub priority Urgent"))
            if i.state == "open" and status(i, "done"):
                result.append(("WORKFLOW", i, "Project status Done, but GitHub issue is open"))
        for p in pulls:
            if p.state != "open":
                continue
            if p.ci_state == "Failing":
                result.append(("CI FAILING", p, "Checks or commit status failed"))
            if p.review_state == "Changes Requested":
                result.append(("CHANGES REQUESTED", p, "Latest meaningful review requests changes"))
            elif p.review_state == "Waiting for Review":
                result.append(("WAITING FOR REVIEW", p, "Open, non-draft PR without completed approval"))
            elif p.review_state == "Approval before latest commit":
                result.append(("REVIEW MAY BE STALE", p, "Approval was submitted before the latest PR commit"))
        return result

    def lead_attention(issues, pulls):
        signals = attention(issues, pulls)
        high_issues = [issue for issue in issues if issue.state == "open"
                       and issue.priority_state == "known" and issue.priority == "High"]
        high_issues.sort(key=lambda issue: issue.updated_at, reverse=True)
        high_issues.sort(key=lambda issue: not (status(issue, "ready") and not issue.assignees))
        high = [("HIGH PRIORITY", issue,
                 "GitHub priority High · " + workflow_label(issue)
                 + (" · unassigned" if not issue.assignees else "")) for issue in high_issues]
        return [signal for signal in signals if signal[0] == "URGENT"] + high + [
            signal for signal in signals if signal[0] != "URGENT"]

    def repo_cards(repos, issues, pulls, releases):
        cards = []
        for r in repos:
            ri = [i for i in issues if i.repository_name == r.name]
            rp = [p for p in pulls if p.repository_name == r.name and p.state == "open"]
            cards.append({"repo": r, "workflow_available": workflow_readable(r),
                          "project_url": github_project_url(configured_repositories[r.name], cfg.get("github", {}).get("organization")),
                          "counts": {k: sum(status(i, k) for i in ri) for k in cfg["workflow"]["values"]},
                          "urgent": sum(i.state == "open" and i.priority_state == "known" and i.priority == "Urgent" for i in ri),
                          "open_prs": len(rp), "waiting": sum(p.review_state == "Waiting for Review" for p in rp),
                          "ci_failing": sum(p.ci_state == "Failing" for p in rp),
                          "latest": latest_releases(releases, r.name)})
        return cards

    def recent_releases(releases, apply_filters=False):
        period = request.args.get("period", "quarter") if apply_filters else "quarter"
        repository = request.args.get("progress_repo", request.args.get("repo", "")) if apply_filters else ""
        since = release_window_start(datetime.now(LOCAL_TZ), period)
        return published_releases(releases, since, repository)

    @app.get("/")
    def home():
        repos, issues, pulls, releases, meta = snapshot()
        open_issues = [i for i in issues if i.state == "open"]
        open_pulls = [p for p in pulls if p.state == "open"]
        ready = ordered_issues([i for i in open_issues if status(i, "ready")])
        counts = {p: sum(priority_label(i) == p for i in ready) for p in PRIORITIES + ["No priority"]}
        counts["Unavailable"] = sum(priority_label(i) == "Unavailable" for i in ready)
        with sessions() as session:
            observed = session.scalars(select(ObservedChange).where(
                ObservedChange.repository_name.in_(configured),
                ObservedChange.observed_at >= datetime.now(timezone.utc) - timedelta(days=30),
            ).order_by(ObservedChange.observed_at.desc(), ObservedChange.id.desc())).all()
            changes = [{"event": event, "observed_ms": int(event.observed_at.replace(tzinfo=timezone.utc).timestamp() * 1000)}
                       for event in observed]
        return render_template("home.html", **common(repos, meta), attention=lead_attention(open_issues, open_pulls),
            observed_changes=changes,
            team=team_data(open_issues, open_pulls), ready=ready, ready_counts=counts,
            ready_unassigned=sum(not i.assignees for i in ready),
            ready_available=[i for i in ready if not i.assignees],
            ready_assigned=[i for i in ready if i.assignees],
            ready_status=cfg["workflow"]["values"]["ready"],
            pulls=sorted(open_pulls, key=lambda p: p.created_at),
            pr_counts={key: sum(p.review_state == key for p in open_pulls) for key in ["Waiting for Review", "Changes Requested", "Approval before latest commit", "Approved", "Draft"]},
            ci_failing=sum(p.ci_state == "Failing" for p in open_pulls), shipped=recent_releases(releases, apply_filters=True),
            cards=repo_cards(repos, issues, pulls, releases), backlog=sum(status(i, "backlog") for i in open_issues),
            in_progress=sum(status(i, "in_progress") for i in open_issues), in_review=sum(status(i, "in_review") for i in open_issues),
            team_members=cfg["team"])

    @app.get("/brief")
    def brief():
        repos, issues, pulls, releases, meta = snapshot()
        open_issues = [i for i in issues if i.state == "open"]
        open_pulls = [p for p in pulls if p.state == "open"]
        ready = ordered_issues([i for i in open_issues if status(i, "ready") and not i.assignees])
        view = dict(attention=lead_attention(open_issues, open_pulls), team=team_data(open_issues, open_pulls),
                    reviews=[p for p in open_pulls if p.review_state in ("Waiting for Review", "Changes Requested", "Approval before latest commit")],
                    ready=ready, shipped=recent_releases(releases), cards=repo_cards(repos, issues, pulls, releases))
        shared = common(repos, meta)
        return render_template("brief.html", **shared, **view,
                               brief_export_text=brief_with_prompt(
                                   brief_as_text(**view, last_sync=shared["last_sync"])))

    @app.get("/now")
    def now_page():
        repos, issues, _, _, meta = snapshot()
        active = [issue for issue in issues if issue.state == "open" and status(issue, "in_progress")]
        sections = [{"repo": repo, "issues": sorted(
            (issue for issue in active if issue.repository_name == repo.name),
            key=lambda issue: issue.updated_at, reverse=True)} for repo in repos]
        return render_template("now.html", **common(repos, meta),
            sections=[section for section in sections if section["issues"]], active_count=len(active))

    @app.get("/team")
    def team():
        repos, issues, pulls, _, meta = snapshot()
        return render_template("team.html", **common(repos, meta), team=team_data(issues, pulls))

    @app.get("/issues")
    def issues_page():
        repos, issues, _, _, meta = snapshot()
        rows = filter_repo(issues)
        for param, fn in (("status", lambda i: workflow_label(i)), ("priority", lambda i: priority_label(i)),
                          ("state", lambda i: i.state), ("member", lambda i: ",".join(i.assignees)),
                          ("label", lambda i: ",".join(i.labels))):
            value = request.args.get(param, "open" if param == "state" else "")
            if value:
                rows = [i for i in rows if value.lower() in fn(i).lower()]
        if request.args.get("unassigned") == "1":
            rows = [i for i in rows if not i.assignees]
        q = request.args.get("q", "").lower().strip()
        if q:
            rows = [i for i in rows if q in " ".join([i.repository_name, str(i.number), i.title, i.author or "", *i.assignees, *i.labels]).lower()]
        sort = request.args.get("sort", "updated")
        if sort == "priority":
            rows = ordered_issues(rows)
        else:
            rows = sorted(rows, key=lambda i: getattr(i, "created_at" if sort == "created" else "updated_at"), reverse=True)
        return render_template("issues.html", **common(repos, meta), issues=rows, cfg=cfg)

    @app.get("/pulls")
    def pulls_page():
        repos, _, pulls, _, meta = snapshot()
        rows = filter_repo(pulls)
        for param, fn in (("state", lambda p: p.state), ("author", lambda p: p.author or ""),
                          ("reviewer", lambda p: ",".join(p.requested_reviewers)),
                          ("review", lambda p: p.review_state), ("ci", lambda p: p.ci_state)):
            value = request.args.get(param, "open" if param == "state" else "")
            if value:
                rows = [p for p in rows if value.lower() in fn(p).lower()]
        if request.args.get("draft") in ("0", "1"):
            rows = [p for p in rows if p.draft == (request.args["draft"] == "1")]
        q = request.args.get("q", "").lower().strip()
        if q:
            rows = [p for p in rows if q in " ".join([p.repository_name, str(p.number), p.title, p.author or "", *p.assignees, *p.requested_reviewers]).lower()]
        rows.sort(key=lambda p: p.created_at, reverse=request.args.get("sort") != "oldest")
        return render_template("pulls.html", **common(repos, meta), pulls=rows)

    @app.get("/releases")
    def releases_page():
        repos, _, _, releases, meta = snapshot()
        rows = sorted(filter_repo(releases), key=lambda r: r.published_at or r.created_at, reverse=True)
        return render_template("releases.html", **common(repos, meta), releases=rows)

    @app.get("/board")
    def board():
        repos, issues, _, _, meta = snapshot()
        columns = [(key, label, ordered_issues([i for i in filter_repo(issues) if status(i, key)]))
                   for key, label in cfg["workflow"]["values"].items()]
        return render_template("board.html", **common(repos, meta), columns=columns)

    @app.get("/repositories")
    def repositories_page():
        repos, issues, pulls, releases, meta = snapshot()
        return render_template("repositories.html", **common(repos, meta), cards=repo_cards(repos, issues, pulls, releases))

    @app.get("/repositories/<repo>")
    def repository_detail(repo):
        repos, issues, pulls, releases, meta = snapshot()
        row = next((r for r in repos if r.name == repo), None)
        if not row:
            abort(404)
        ri = [i for i in issues if i.repository_name == repo]
        rp = [p for p in pulls if p.repository_name == repo]
        rr = [r for r in releases if r.repository_name == repo]
        groups = {key: ordered_issues([i for i in ri if i.state == "open" and status(i, key)]) for key in cfg["workflow"]["values"]}
        return render_template("repo_detail.html", **common(repos, meta), repo=row, workflow_available=workflow_readable(row), groups=groups,
            ready_status=cfg["workflow"]["values"]["ready"], backlog_status=cfg["workflow"]["values"]["backlog"],
            ready_unassigned=[i for i in groups["ready"] if not i.assignees],
            priorities={p: sum(priority_label(i) == p for i in ri if i.state == "open") for p in PRIORITIES + ["No priority", "Unavailable"]},
            pulls=sorted([p for p in rp if p.state == "open"], key=lambda p: p.created_at),
            releases=sorted(rr, key=lambda r: r.published_at or r.created_at, reverse=True),
            shipped=recent_releases(rr))

    @app.get("/issues/<repo>/<int:number>")
    def issue_detail(repo, number):
        repos, issues, _, _, meta = snapshot()
        issue = next((i for i in issues if i.repository_name == repo and i.number == number), None)
        if not issue:
            abort(404)
        repository = next((r for r in repos if r.name == repo), None)
        return render_template("issue_detail.html", **common(repos, meta), issue=issue, repo=repository)

    @app.get("/pulls/<repo>/<int:number>")
    def pull_detail(repo, number):
        repos, _, pulls, _, meta = snapshot()
        pull = next((p for p in pulls if p.repository_name == repo and p.number == number), None)
        if not pull:
            abort(404)
        return render_template("pull_detail.html", **common(repos, meta), pull=pull)

    @app.get("/search")
    def search():
        repos, issues, pulls, _, meta = snapshot()
        q = request.args.get("q", "").lower().strip()
        matching_issues = [i for i in issues if q and q in " ".join([i.repository_name, str(i.number), i.title, i.author or "", *i.assignees, *i.labels]).lower()]
        matching_pulls = [p for p in pulls if q and q in " ".join([p.repository_name, str(p.number), p.title, p.author or "", *p.assignees, *p.requested_reviewers]).lower()]
        return render_template("search.html", **common(repos, meta), q=q, issues=matching_issues[:100], pulls=matching_pulls[:100])

    @app.post("/sync")
    def sync_now():
        started = manager.start()
        return jsonify({"started": started, "running": manager.running})

    @app.get("/sync/status")
    def sync_status():
        with sessions() as session:
            meta = session.get(SyncMeta, "last_success")
            errors = [{"repository": r.name, "error": r.error} for r in session.scalars(select(Repository)).all() if r.error]
        return jsonify({"running": manager.running, "last_success": meta.value if meta else None, "errors": errors})

    if auto_sync:
        manager.start()
        interval = int(cfg["sync"]["interval_seconds"])
        def schedule():
            import threading
            manager.start()
            timer = threading.Timer(interval, schedule)
            timer.daemon = True
            timer.start()
        import threading
        timer = threading.Timer(interval, schedule)
        timer.daemon = True
        timer.start()
    return app
