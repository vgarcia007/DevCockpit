import logging
import re
import unicodedata
import secrets
import hmac
import ipaddress
from calendar import month_abbr, monthrange
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from flask import Flask, abort, redirect, render_template, request, url_for, jsonify, session as browser_session
from sqlalchemy import and_, delete, func, or_, select
from .config import ROOT, load_config
from .models import ExternalTeamIssue, ExternalTeamSync, Issue, ObservedChange, OTRSObservedChange, OTRSSyncState, OTRSTicket, OTRSTicketStat, Pull, Release, Repository, SyncMeta, UserAvatar, make_session
from .otrs import OTRSSyncManager, OTRSClient, OTRSError
from .sync import SyncManager
from .models import ZabbixHost, ZabbixProblem, ZabbixSyncState
from .zabbix import SEVERITIES, ZabbixSyncManager, ZabbixClient, ZabbixError, host_url, problem_duration, problem_url
from .accounts import AccountStore, AccountError, account_binding

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


def statistics_week_bounds(week):
    """Return the Berlin calendar-week interval, with an exclusive end."""
    if not re.fullmatch(r"\d{4}-W\d{2}", week):
        raise ValueError("Invalid ISO week")
    start = datetime.strptime(week + "-1", "%G-W%V-%u").replace(tzinfo=LOCAL_TZ)
    if start.strftime("%G-W%V") != week:
        raise ValueError("Invalid ISO week")
    return start, start + timedelta(days=7)


def in_statistics_week(value, start, end):
    return bool(value and start <= value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ) < end)


def brief_as_text(attention, team, reviews, ready, shipped, cards, last_sync, attention_tickets=None):
    def cell(value):
        return " ".join(str(value or "").split())

    def item_label(item):
        kind = "PR " if isinstance(item, Pull) else ""
        return f"{cell(item.repository_name)} {kind}#{item.number} | {cell(item.title)}"

    def section(label, total, shown):
        return f"{label} ({total} total, {shown} shown)"

    lines = ["BRIEF", f"Last sync: {fmt_time(last_sync) if last_sync else 'pending'}", ""]
    lines.append(section("NEED ATTENTION", len(attention), min(len(attention), 4)))
    for reason, item, why in attention[:4]:
        lines.append(f"- {cell(reason)} | {item_label(item)} | {cell(why)}")
    if not attention:
        lines.append("- None")

    if attention_tickets:
        lines.extend(["", section("OTRS NEED ATTENTION", len(attention_tickets), min(len(attention_tickets), 4))])
        for ticket in attention_tickets[:4]:
            lines.append(f"- {cell(ticket.queue)} {cell(ticket.number)} | {cell(ticket.subject)} | "
                         f"{cell(ticket.state)} · {cell(ticket.priority)}")

    lines.extend(["", "TEAM NOW"])
    for member in team:
        person = member["person"]
        lines.append(
            f"- {cell(person['name'])} (@{cell(person['github'])}) | "
            f"Now {len(member['active'])} | Review {len(member['review'])} | "
            f"Ready next {len(member['ready_next'])} | Assigned backlog {len(member['assigned_backlog'])}"
            + (f" | OTRS tickets {len(member['tickets'])}" if member.get("tickets") else "")
        )
        if member["active"]:
            lines.append(f"  Now: {item_label(member['active'][0])}")
        if member.get("tickets"):
            ticket = member["tickets"][0]
            lines.append(f"  OTRS: {cell(ticket.queue)} {cell(ticket.number)} | {cell(ticket.subject)}")
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


def team_sort_key(person):
    name = unicodedata.normalize("NFKD", (person.get("name") or person["github"]).casefold())
    return ("".join(char for char in name if not unicodedata.combining(char)),
            person["github"].casefold())


def otrs_user_matches(ticket, user):
    user = (user or "").strip().casefold()
    if not user:
        return False
    email = (ticket.responsible_email or "").strip().casefold()
    return user in {(ticket.owner or "").strip().casefold(),
                    (ticket.responsible or "").strip().casefold(),
                    email, email.partition("@")[0] if "@" in email else ""}


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


def release_timeline(releases):
    """Position published releases on one calendar-scaled, collision-free track."""
    published = sorted((r for r in releases if r.published_at and not r.draft),
                       key=lambda r: (r.published_at, r.github_id))
    if not published:
        return {"nodes": [], "ticks": [], "width": 0, "height": 0}

    dates = [r.published_at.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ) for r in published]
    first_month = dates[0].year * 12 + dates[0].month - 1
    last_month = dates[-1].year * 12 + dates[-1].month - 1
    month_width = 112
    ticks = []
    for month_index in range(first_month, last_month + 1):
        year, zero_month = divmod(month_index, 12)
        ticks.append({"left": 28 + (month_index - first_month) * month_width,
                      "label": f"{month_abbr[zero_month + 1]} {year}" if zero_month == 0 or month_index == first_month
                      else month_abbr[zero_month + 1]})

    row_ends = []
    nodes = []
    for release, when in zip(published, dates):
        month_index = when.year * 12 + when.month - 1
        fraction = ((when.day - 1) + (when.hour * 3600 + when.minute * 60 + when.second) / 86400) / monthrange(when.year, when.month)[1]
        left = round(28 + (month_index - first_month + fraction) * month_width, 2)
        row = next((index for index, end in enumerate(row_ends) if left >= end), len(row_ends))
        if row == len(row_ends):
            row_ends.append(0)
        row_ends[row] = left + 150
        nodes.append({"release": release, "left": left, "top": 46 + row * 48})

    return {"nodes": nodes, "ticks": ticks,
            "width": 28 + (last_month - first_month + 1) * month_width + 150,
            "height": 100 + len(row_ends) * 48}


def workflow_readable(repo):
    return repo.project_state == "available" and "Project status field is missing" not in (repo.error or "")


def create_app(config_path=None, database_path=None, auto_sync=True, credential_store=None):
    cfg = load_config(config_path)
    instance = ROOT / "instance"
    instance.mkdir(exist_ok=True)
    sessions = make_session(database_path or instance / "cockpit.sqlite")
    otrs_config = cfg.get("otrs")
    otrs_enabled = bool(otrs_config and otrs_config.get("enabled"))
    zabbix_config = cfg.get("zabbix")
    zabbix_enabled = bool(zabbix_config and zabbix_config.get("enabled"))
    account_store = credential_store or AccountStore()
    integration_configs = {"otrs": otrs_config, "zabbix": zabbix_config}

    def clear_account_cache(provider, binding=None):
        tables = (OTRSTicket, OTRSTicketStat, OTRSSyncState, OTRSObservedChange) if provider == "otrs" else (ZabbixProblem, ZabbixHost, ZabbixSyncState)
        with sessions() as db:
            for table in tables:
                db.execute(delete(table))
            key = "account_binding:" + provider
            db.execute(delete(SyncMeta).where(SyncMeta.key == key))
            if binding:
                db.add(SyncMeta(key=key, value=binding))
            db.commit()

    def credentials_for(provider):
        account = account_store.get(provider, integration_configs[provider]["url"])
        if not account:
            clear_account_cache(provider)
            return None
        binding = account_binding(provider, integration_configs[provider]["url"], account)
        with sessions() as db:
            previous = db.get(SyncMeta, "account_binding:" + provider)
        if not previous or previous.value != binding:
            clear_account_cache(provider, binding)
        return account
    configured = {repo["name"] for repo in cfg["repositories"]}
    configured_repositories = {repo["name"]: repo for repo in cfg["repositories"]}
    with sessions() as session:
        if not otrs_enabled:
            session.execute(delete(OTRSTicket))
            session.execute(delete(OTRSTicketStat))
            session.execute(delete(OTRSSyncState))
            session.execute(delete(OTRSObservedChange))
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
    otrs_manager = OTRSSyncManager(sessions, otrs_config,
                                   include_responsible=any(person.get("otrs_user") for person in cfg["team"]),
                                   credential_provider=lambda: credentials_for("otrs")) if otrs_enabled else None
    zabbix_manager = ZabbixSyncManager(sessions, zabbix_config,
                                    credential_provider=lambda: credentials_for("zabbix")) if zabbix_enabled else None
    integration_managers = {"otrs": otrs_manager, "zabbix": zabbix_manager}
    for provider, sync_manager in integration_managers.items():
        if sync_manager and not sync_manager.credentials_available():
            clear_account_cache(provider)
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=65536)
    app.config["ACCOUNT_STORE"] = account_store
    app.config["COCKPIT_CONFIG"] = cfg
    app.config["SYNC_MANAGER"] = manager
    app.config["OTRS_SYNC_MANAGER"] = otrs_manager
    app.config["ZABBIX_SYNC_MANAGER"] = zabbix_manager
    app.jinja_env.filters["date"] = fmt_date
    app.jinja_env.filters["datetime"] = fmt_time
    app.jinja_env.filters["age"] = age_days
    app.jinja_env.filters["open_days"] = open_days
    app.jinja_env.filters["priority"] = priority_label
    app.jinja_env.filters["workflow"] = workflow_label
    app.jinja_env.filters["monitoring_duration"] = problem_duration

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

    def sync_sources(meta, otrs_state, zabbix_state, errors):
        def timestamp(value):
            if isinstance(value, datetime):
                return value.replace(tzinfo=timezone.utc).isoformat()
            return value

        github = manager.status()
        sources = [{"id": "github", "name": "GitHub", **github,
                    "last_success": meta.value if meta else None,
                    "error": "\n".join(f'{item["repository"]}: {item["error"]}' for item in errors) or None}]
        for key, name, sync_manager, state in (("otrs", "OTRS", otrs_manager, otrs_state),
                                             ("zabbix", "Zabbix", zabbix_manager, zabbix_state)):
            if sync_manager:
                sources.append({"id": key, "name": name, "running": sync_manager.running,
                    "last_success": timestamp(state.last_success) if state else None,
                    "next_sync_at": timestamp(sync_manager.next_sync_at) if not sync_manager.running else None,
                    "error": sync_manager.account_error or (state.error if state else None),
                    "account_state": sync_manager.account_state, "busy": sync_manager.lock.locked(), "rate_limit_until": None})
        for source in sources:
            source["state"] = source.get("account_state") or ("running" if source["running"] else "paused" if source["rate_limit_until"]
                               else "error" if source["error"] else "healthy" if source["last_success"] else "pending")
            source["display"] = {"running": "Syncing…", "paused": "Paused", "error": "Error",
                "account_required": "Account required", "credential_error": "Storage error"}.get(source["state"], "Waiting")
            if source["state"] in ("healthy", "pending") and source["next_sync_at"]:
                seconds = max(0, ceil((datetime.fromisoformat(source["next_sync_at"]) - datetime.now(timezone.utc)).total_seconds()))
                hours, remainder = divmod(seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                source["display"] = (f"{hours}:" if hours else "") + f"{minutes:02d}:{seconds:02d}"
            details = [source["name"] + " · " + {"running": "Sync running", "paused": "GitHub rate limit · sync paused",
                "error": "Last sync failed", "healthy": "Last sync successful", "pending": "Waiting for first sync",
                "account_required": "Account required · open Settings → Accounts", "credential_error": "Account storage error · open Settings → Accounts"}[source["state"]]]
            details.append("Last successful sync: " + (fmt_time(datetime.fromisoformat(source["last_success"])) if source["last_success"] else "None yet"))
            if source["next_sync_at"]:
                details.append("Next sync: " + fmt_time(datetime.fromisoformat(source["next_sync_at"])))
            if source["error"]:
                details.append("Last sync error: " + source["error"])
            source["details"] = "\n".join(details)
        return sources

    def common(repos, meta):
        warnings = {}
        for repo in repos:
            if repo.error:
                warnings.setdefault(repo.error, []).append(repo.name)
        with sessions() as session:
            avatars = {user.login: user.url for user in session.scalars(select(UserAvatar)).all()}
            otrs_state = session.get(OTRSSyncState, "tickets") if otrs_manager else None
            zabbix_state = session.get(ZabbixSyncState, "monitoring") if zabbix_manager else None
        return {"repositories": repos, "last_sync": datetime.fromisoformat(meta.value) if meta else None,
                "sync_sources": sync_sources(meta, otrs_state, zabbix_state,
                    [{"repository": repo.name, "error": repo.error} for repo in repos if repo.error]),
                "sync_server_time": datetime.now(timezone.utc).isoformat(),
                "account_notices": [{"name": provider.upper() if provider == "otrs" else "Zabbix",
                    "error": sync_manager.account_error} for provider, sync_manager in integration_managers.items()
                    if sync_manager and sync_manager.account_state],
                "otrs_last_success": otrs_state.last_success if otrs_state else None,
                "zabbix_enabled": zabbix_enabled, "zabbix_state": zabbix_state,
                "zabbix_revision": zabbix_state.last_attempt.isoformat() if zabbix_state and zabbix_state.last_attempt else "",
                "sync_running": manager.running, "github_user": cfg.get("github", {}).get("username", ""),
                "otrs_enabled": otrs_manager is not None,
                "avatars": avatars,
                "workflow_coverage": sum(workflow_readable(r) for r in repos),
                "workflow_total": len(repos),
                "sync_warnings": [{"error": error, "repositories": names} for error, names in warnings.items()]}

    def team_data(issues, pulls):
        result = []
        with sessions() as session:
            outside = session.scalars(select(ExternalTeamIssue).order_by(ExternalTeamIssue.updated_at.desc())).all()
            outside_state = {state.login: state for state in session.scalars(select(ExternalTeamSync)).all()}
            tickets = session.scalars(select(OTRSTicket).order_by(
                OTRSTicket.created_at.desc(), OTRSTicket.number.desc())).all() if otrs_manager else []
        configured_full_names = {repo["full_name"].lower() for repo in cfg["repositories"]}
        for person in sorted(cfg["team"], key=team_sort_key):
            user = person["github"].lower()
            assigned = [i for i in issues if i.state == "open" and user in [a.lower() for a in i.assignees]]
            result.append({"person": person,
                           "active": [i for i in assigned if status(i, "in_progress")],
                           "review": [i for i in assigned if status(i, "in_review")],
                           "ready_next": ordered_issues([i for i in assigned if status(i, "ready")]),
                           "assigned_backlog": ordered_issues([i for i in assigned if status(i, "backlog")]),
                           "next": [i for i in assigned if status(i, "ready") or status(i, "backlog")],
                           "prs": [p for p in pulls if p.state == "open" and (p.author or "").lower() == user],
                           "tickets": [ticket for ticket in tickets if otrs_user_matches(ticket, person.get("otrs_user"))],
                           "external": [i for i in outside if i.login == user and i.repository_name.lower() not in configured_full_names],
                           "external_sync": outside_state.get(user)})
        return result

    def otrs_attention_tickets():
        if not otrs_manager:
            return []
        excluded_states = [state.strip().casefold() for state in otrs_config["excluded_states"]]
        with sessions() as session:
            return session.scalars(select(OTRSTicket).where(
                OTRSTicket.queue_id.in_(otrs_config["attention_queue_ids"]),
                ~func.lower(func.trim(OTRSTicket.state)).in_(excluded_states),
            ).order_by(OTRSTicket.created_at.desc(), OTRSTicket.number.desc())).all()

    def monitoring_data():
        if not zabbix_manager:
            return [], []
        selection = {item["host"]: item for item in zabbix_config["hosts"]}
        with sessions() as session:
            hosts = session.scalars(select(ZabbixHost).where(ZabbixHost.host.in_(selection))).all()
            problems = session.scalars(select(ZabbixProblem).order_by(
                ZabbixProblem.started_at.desc(), ZabbixProblem.severity.desc())).all()
        by_id = {host.hostid: host for host in hosts}
        for host in hosts:
            host.environment = selection[host.host]["environment"]
            host.url = host_url(zabbix_config, host)
        visible = []
        for problem in problems:
            problem.hosts = [by_id[hostid] for hostid in problem.hostids if hostid in by_id]
            if problem.hosts:
                problem.url = problem_url(zabbix_config, problem)
                problem.severity_label = SEVERITIES[problem.severity]
                visible.append(problem)
        return sorted(hosts, key=lambda host: (host.environment, host.name.casefold())), visible

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
        attention_tickets = otrs_attention_tickets()
        _, zabbix_problems = monitoring_data()
        monitoring_attention = [problem for problem in zabbix_problems
                                if problem.resolved_at is None and
                                problem.severity >= zabbix_config["attention_min_severity"]] if zabbix_manager else []
        github_attention = lead_attention(open_issues, open_pulls)
        return render_template("home.html", **common(repos, meta), attention=github_attention,
            attention_tickets=attention_tickets,
            monitoring_attention=monitoring_attention,
            attention_count=len(github_attention) + len(attention_tickets) + len(monitoring_attention),
            otrs_url=cfg["otrs"]["url"].rstrip("/") + "/index.pl" if otrs_manager else None,
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
        view = dict(attention=lead_attention(open_issues, open_pulls),
                    attention_tickets=otrs_attention_tickets(), team=team_data(open_issues, open_pulls),
                    reviews=[p for p in open_pulls if p.review_state in ("Waiting for Review", "Changes Requested", "Approval before latest commit")],
                    ready=ready, shipped=recent_releases(releases), cards=repo_cards(repos, issues, pulls, releases))
        shared = common(repos, meta)
        return render_template("brief.html", **shared, **view,
                               otrs_url=otrs_config["url"].rstrip("/") + "/index.pl" if otrs_manager else None,
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
            sections=[section for section in sections if section["issues"]], active_count=len(active),
            otrs_people=[entry for entry in team_data(issues, []) if entry["tickets"]] if otrs_manager else [],
            otrs_url=otrs_config["url"].rstrip("/") + "/index.pl" if otrs_manager else None)

    @app.get("/team")
    def team():
        repos, issues, pulls, _, meta = snapshot()
        return render_template("team.html", **common(repos, meta), team=team_data(issues, pulls),
                               otrs_url=otrs_config["url"].rstrip("/") + "/index.pl" if otrs_manager else None)

    @app.get("/work")
    def work_page():
        own_login = cfg.get("github", {}).get("username", "").strip()
        people = {person["github"].casefold(): person for person in cfg["team"]}
        if own_login and own_login.casefold() not in people:
            people[own_login.casefold()] = {"github": own_login, "name": own_login}
        selected = request.args.get("person", own_login).strip().casefold()
        if selected and selected not in people:
            abort(404)
        person = people.get(selected)
        repos, issues, pulls, _, meta = snapshot()
        assigned = ordered_issues([issue for issue in issues if person and issue.state == "open"
                                   and any(login.casefold() == selected for login in issue.assignees)])
        authored = sorted([pull for pull in pulls if person and pull.state == "open"
                           and ((pull.author or "").casefold() == selected
                                or any(login.casefold() == selected for login in pull.assignees))],
                          key=lambda pull: pull.updated_at, reverse=True)
        reviews = sorted([pull for pull in pulls if person and pull.state == "open"
                          and pull not in authored
                          and any(login.casefold() == selected for login in pull.requested_reviewers)],
                         key=lambda pull: pull.updated_at, reverse=True)
        with sessions() as session:
            external = session.scalars(select(ExternalTeamIssue).where(
                func.lower(ExternalTeamIssue.login) == selected
            ).order_by(ExternalTeamIssue.updated_at.desc())).all() if person else []
            external_sync = session.get(ExternalTeamSync, selected) if person else None
            owner = (person.get("otrs_user") or "").strip() if person else ""
            tickets = [ticket for ticket in session.scalars(select(OTRSTicket).order_by(
                OTRSTicket.created_at.desc(), OTRSTicket.number.desc())).all()
                if otrs_user_matches(ticket, owner)] if otrs_manager and owner else []
            otrs_state = session.get(OTRSSyncState, "tickets") if otrs_manager else None
        configured_full_names = {repo["full_name"].casefold() for repo in cfg["repositories"]}
        external = [issue for issue in external if issue.repository_name.casefold() not in configured_full_names]
        return render_template("work.html", **common(repos, meta), person=person,
                               people=sorted(people.values(), key=team_sort_key), own_login=own_login,
                               issues=assigned, external=external, external_sync=external_sync,
                               authored=authored, reviews=reviews, tickets=tickets,
                               otrs_state=otrs_state,
                               otrs_url=otrs_config["url"].rstrip("/") + "/index.pl" if otrs_manager else None)

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

    @app.get("/tickets")
    def tickets_page():
        if otrs_manager is None:
            abort(404)
        page = request.args.get("page", "1")
        if not page.isdecimal() or int(page) < 1:
            abort(404)
        page = int(page)
        per_page = 50
        active_filters = {name: request.args.get(name, "").strip()
                          for name in ("queue", "status", "priority", "owner", "responsible", "q", "sort")}
        def contains_text(column, value):
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            return column.ilike(f"%{escaped}%", escape="\\")

        conditions = []
        for name, column in (("queue", OTRSTicket.queue), ("status", OTRSTicket.state),
                             ("priority", OTRSTicket.priority)):
            if active_filters[name]:
                conditions.append(column == active_filters[name])
        if active_filters["owner"]:
            conditions.append(contains_text(OTRSTicket.owner, active_filters["owner"]))
        if active_filters["responsible"]:
            conditions.append(or_(contains_text(OTRSTicket.responsible, active_filters["responsible"]),
                                  contains_text(OTRSTicket.responsible_email, active_filters["responsible"])))
        if active_filters["q"]:
            conditions.append(or_(*(contains_text(column, active_filters["q"]) for column in
                                    (OTRSTicket.number, OTRSTicket.subject, OTRSTicket.queue,
                                     OTRSTicket.state, OTRSTicket.priority, OTRSTicket.owner,
                                     OTRSTicket.responsible, OTRSTicket.responsible_email))))
        sort = active_filters["sort"] if active_filters["sort"] in ("newest", "oldest") else "newest"
        ordering = (OTRSTicket.created_at.asc(), OTRSTicket.number.asc()) if sort == "oldest" else (
            OTRSTicket.created_at.desc(), OTRSTicket.number.desc())
        with sessions() as session:
            total = session.scalar(select(func.count()).select_from(OTRSTicket).where(*conditions)) or 0
            pages = max(1, (total + per_page - 1) // per_page)
            if page > pages:
                abort(404)
            tickets = session.scalars(select(OTRSTicket).where(*conditions).order_by(*ordering)
                                      .offset((page - 1) * per_page).limit(per_page)).all()
            queues = sorted(set(session.scalars(select(OTRSTicket.queue).distinct()).all()))
            highlight_queues = sorted(set(session.scalars(select(OTRSTicket.queue).where(
                OTRSTicket.queue_id.in_(otrs_config["highlight_queue_ids"]))).all()))
            statuses = session.scalars(select(OTRSTicket.state).distinct().order_by(OTRSTicket.state)).all()
            priorities = session.scalars(select(OTRSTicket.priority).distinct().order_by(OTRSTicket.priority)).all()
            otrs_state = session.get(OTRSSyncState, "tickets")
        repos, _, _, _, meta = snapshot()
        return render_template("tickets.html", **common(repos, meta), tickets=tickets,
                               total=total, page=page, pages=pages, otrs_state=otrs_state,
                               queues=queues, statuses=statuses, priorities=priorities,
                               highlight_queues=highlight_queues,
                               highlight_queue_ids=otrs_config["highlight_queue_ids"],
                               active_filters={key: value for key, value in active_filters.items() if value},
                               otrs_running=otrs_manager.running,
                               otrs_url=cfg["otrs"]["url"].rstrip("/") + "/index.pl")

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
        if request.args.get("hide_dependabot") == "1":
            rows = [p for p in rows if (p.author or "").lower() not in ("dependabot[bot]", "dependabot")]
        q = request.args.get("q", "").lower().strip()
        if q:
            rows = [p for p in rows if q in " ".join([p.repository_name, str(p.number), p.title, p.author or "", *p.assignees, *p.requested_reviewers]).lower()]
        rows.sort(key=lambda p: p.created_at, reverse=request.args.get("sort") != "oldest")
        return render_template("pulls.html", **common(repos, meta), pulls=rows)

    @app.get("/releases")
    def releases_page():
        repos, _, _, releases, meta = snapshot()
        rows = sorted(filter_repo(releases), key=lambda r: r.published_at or r.created_at, reverse=True)
        return render_template("releases.html", **common(repos, meta), releases=rows,
                               timeline=release_timeline(rows))

    @app.get("/statistics")
    def statistics_page():
        current_start, _ = statistics_week_bounds(datetime.now(LOCAL_TZ).strftime("%G-W%V"))
        week = request.args.get("week") or current_start.strftime("%G-W%V")
        try:
            start, end = statistics_week_bounds(week)
        except ValueError:
            abort(404)
        if start > current_start:
            abort(404)
        repos, issues, pulls, releases, meta = snapshot()
        scope = "all" if request.args.get("scope") == "all" else "projects"
        selected_repositories = (configured if scope == "all" else
            {name for name, repo in configured_repositories.items() if repo.get("project_number") is not None})
        issues = [item for item in issues if item.repository_name in selected_repositories]
        pulls = [item for item in pulls if item.repository_name in selected_repositories]
        releases = [item for item in releases if item.repository_name in selected_repositories]
        def weekly(items, field):
            return sorted((item for item in items if in_statistics_week(getattr(item, field), start, end)),
                          key=lambda item: getattr(item, field), reverse=True)

        issue_created = weekly(issues, "created_at")
        issue_closed = weekly((item for item in issues if item.state == "closed"), "closed_at")
        pull_merged = weekly((item for item in pulls if item.merged), "merged_at")
        released = weekly((item for item in releases if not item.draft), "published_at")
        ticket_created, ticket_closed, otrs_sync, undated_closed = [], [], None, 0
        if otrs_manager:
            with sessions() as session:
                tickets = session.scalars(select(OTRSTicketStat).where(
                    OTRSTicketStat.queue_id.in_(otrs_config["queue_ids"]))).all()
                otrs_sync = session.get(OTRSSyncState, "tickets")
            ticket_created = weekly(tickets, "created_at")
            closed_states = {state.strip().casefold() for state in otrs_config["excluded_states"]}
            closed_tickets = [item for item in tickets if item.state.strip().casefold() in closed_states]
            ticket_closed = weekly(closed_tickets, "closed_at")
            undated_closed = sum(item.closed_at is None for item in closed_tickets)
        previous_week = (start - timedelta(days=7)).strftime("%G-W%V")
        next_week = (start + timedelta(days=7)).strftime("%G-W%V") if start < current_start else None
        scope_args = {"scope": "all"} if scope == "all" else {}
        return render_template("statistics.html", **common(repos, meta), week=week, scope=scope,
            week_start=start, week_end=end - timedelta(days=1),
            previous_week_url=url_for("statistics_page", week=previous_week, **scope_args),
            next_week_url=url_for("statistics_page", week=next_week, **scope_args) if next_week else None,
            issue_created=issue_created, issue_closed=issue_closed,
            pull_merged=pull_merged, released=released, ticket_created=ticket_created,
            ticket_closed=ticket_closed, otrs_sync=otrs_sync, undated_closed=undated_closed,
            otrs_url=otrs_config["url"].rstrip("/") + "/index.pl" if otrs_manager else None)

    @app.get("/monitoring")
    def monitoring_page():
        if not zabbix_manager:
            abort(404)
        hosts, problems = monitoring_data()
        filters = {key: request.args.get(key, "").strip() for key in ("host", "environment", "severity", "q", "status")}
        if filters["status"] not in ("", "open", "resolved"):
            abort(400)
        visible = problems
        if filters["status"]:
            visible = [problem for problem in visible
                       if (problem.resolved_at is None) == (filters["status"] == "open")]
        if filters["host"] or filters["environment"]:
            visible = [problem for problem in visible if any(
                (not filters["host"] or host.host == filters["host"]) and
                (not filters["environment"] or host.environment == filters["environment"])
                for host in problem.hosts)]
        if filters["severity"]:
            if filters["severity"] not in {str(value) for value in range(len(SEVERITIES))}:
                abort(400)
            visible = [problem for problem in visible if problem.severity == int(filters["severity"])]
        if filters["q"]:
            query = filters["q"].casefold()
            visible = [problem for problem in visible if query in " ".join(
                [problem.name, problem.severity_label, *[host.name for host in problem.hosts],
                 *[host.host for host in problem.hosts]]).casefold()]
        cached = {host.host: host for host in hosts}
        cards = [{"config": config, "host": cached.get(config["host"]),
                  "problem_count": sum(problem.resolved_at is None and
                      any(host.host == config["host"] for host in problem.hosts) for problem in problems)}
                 for config in zabbix_config["hosts"]]
        repos, _, _, _, meta = snapshot()
        return render_template("monitoring.html", **common(repos, meta), problems=visible,
                               host_cards=cards, filters=filters, severities=SEVERITIES,
                               zabbix_running=zabbix_manager.running, history_days=zabbix_config["history_days"])

    @app.get("/board")
    def board():
        filters = {}
        if request.args.get("repo"):
            filters["repo"] = request.args["repo"]
        person = request.args.get("person", "")
        if person.casefold() == "~unassigned":
            filters["unassigned"] = "1"
        elif person:
            filters["member"] = person
        return redirect(url_for("issues_page", **filters))

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
        with sessions() as session:
            tickets = session.scalars(select(OTRSTicket).order_by(
                OTRSTicket.created_at.desc(), OTRSTicket.number.desc())).all() if otrs_manager and q else []
        matching_tickets = [ticket for ticket in tickets if q in " ".join((
            ticket.number, ticket.subject, ticket.queue, ticket.state, ticket.priority,
            ticket.owner, ticket.responsible or "", ticket.responsible_email or "")).lower()]
        return render_template("search.html", **common(repos, meta), q=q, issues=matching_issues[:100],
                               pulls=matching_pulls[:100], tickets=matching_tickets[:100],
                               otrs_url=otrs_config["url"].rstrip("/") + "/index.pl" if otrs_manager else None)

    def render_accounts(error=None, status=200):
        cards = []
        for provider, settings in integration_configs.items():
            sync_manager = integration_managers[provider]
            account, storage_error = None, None
            if sync_manager:
                try:
                    account = account_store.get(provider, settings["url"])
                except AccountError as exc:
                    storage_error = str(exc)
            cards.append({"id": provider, "name": "OTRS" if provider == "otrs" else "Zabbix",
                "enabled": sync_manager is not None, "url": settings["url"] if sync_manager else "",
                "user": account["user"] if account else "", "storage_error": storage_error,
                "busy": (sync_manager.running or (request.method == "GET" and sync_manager.lock.locked())) if sync_manager else False})
        repos, _, _, _, meta = snapshot()
        browser_session.setdefault("accounts_csrf", secrets.token_urlsafe(32))
        return render_template("accounts.html", **common(repos, meta), accounts=cards,
            csrf_token=browser_session["accounts_csrf"], account_form_error=error,
            account_saved=request.args.get("saved") == "1", account_removed=request.args.get("removed") == "1"), status

    @app.after_request
    def private_account_responses(response):
        if request.path.startswith("/settings/accounts"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/settings/accounts")
    def accounts_page():
        return render_accounts()

    def account_request(provider):
        if provider not in integration_managers or not integration_managers[provider]:
            abort(404)
        if urlsplit(request.host_url).hostname not in ("localhost", "127.0.0.1", "::1"):
            abort(403)
        try:
            if not ipaddress.ip_address(request.remote_addr or "").is_loopback:
                abort(403)
        except ValueError:
            abort(403)
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            abort(403)
        token = request.form.get("csrf_token", "")
        expected = browser_session.get("accounts_csrf", "")
        if not expected or not hmac.compare_digest(token, expected):
            abort(403)
        sync_manager = integration_managers[provider]
        if not sync_manager.lock.acquire(blocking=False):
            return None
        return sync_manager

    @app.post("/settings/accounts/<provider>")
    def save_account(provider):
        sync_manager = account_request(provider)
        if not sync_manager:
            return render_accounts("A sync is running. Wait for it to finish before changing this account.", 409)
        previous_next = sync_manager.next_sync_at
        if sync_manager.timer:
            sync_manager.timer.cancel()
        sync_manager.next_sync_at = None
        changed = False
        try:
            user, password = request.form.get("user", "").strip(), request.form.get("password", "")
            if not user or not password:
                return render_accounts("Enter both username and password.", 400)
            candidate = {**integration_configs[provider], "user": user, "password": password}
            if provider == "otrs":
                OTRSClient(candidate, include_responsible=sync_manager.include_responsible).fetch_all()
            else:
                ZabbixClient(candidate).fetch_all()
            account = account_store.set(provider, candidate["url"], user, password)
            clear_account_cache(provider, account_binding(provider, candidate["url"], account))
            sync_manager.credentials_available()
            changed = True
        except AccountError as exc:
            return render_accounts(str(exc), 503)
        except (OTRSError, ZabbixError):
            return render_accounts("Connection check failed. Check the account, server and permissions; the previous account was kept.", 400)
        except Exception:
            LOG.warning("Account connection check failed: %s", provider)
            return render_accounts("Connection check failed; the previous account was kept.", 503)
        finally:
            sync_manager.lock.release()
            if auto_sync and changed:
                sync_manager.enable_scheduler()
            elif sync_manager.scheduler_enabled and sync_manager.credentials_available():
                sync_manager.schedule_next(previous_next)
        return redirect(url_for("accounts_page", saved="1"), code=303)

    @app.post("/settings/accounts/<provider>/remove")
    def remove_account(provider):
        sync_manager = account_request(provider)
        if not sync_manager:
            return render_accounts("A sync is running. Wait for it to finish before removing this account.", 409)
        try:
            account_store.remove(provider, integration_configs[provider]["url"])
            if sync_manager.timer:
                sync_manager.timer.cancel()
                sync_manager.timer = None
            sync_manager.next_sync_at = None
            clear_account_cache(provider)
            sync_manager.credentials_available()
        except AccountError as exc:
            return render_accounts(str(exc), 503)
        finally:
            sync_manager.lock.release()
        return redirect(url_for("accounts_page", removed="1"), code=303)

    @app.post("/sync")
    def sync_now():
        started = manager.start()
        return jsonify({"started": started, "reason": None if started else
                        (manager.pause_reason() or "running"), **manager.status()})

    @app.get("/sync/status")
    def sync_status():
        with sessions() as session:
            meta = session.get(SyncMeta, "last_success")
            otrs_state = session.get(OTRSSyncState, "tickets") if otrs_manager else None
            zabbix_state = session.get(ZabbixSyncState, "monitoring") if zabbix_manager else None
            errors = [{"repository": r.name, "error": r.error} for r in session.scalars(select(Repository)).all() if r.error]
        return jsonify({**manager.status(), "otrs_running": otrs_manager.running if otrs_manager else False,
                        "sources": sync_sources(meta, otrs_state, zabbix_state, errors),
                        "server_time": datetime.now(timezone.utc).isoformat(),
                        "last_success": meta.value if meta else None,
                        "otrs_last_success": otrs_state.last_success.isoformat() if otrs_state and otrs_state.last_success else None,
                        "zabbix_revision": zabbix_state.last_attempt.isoformat() if zabbix_state and zabbix_state.last_attempt else "",
                        "zabbix_running": zabbix_manager.running if zabbix_manager else False,
                        "errors": errors})

    @app.get("/notifications")
    def notifications():
        def timestamp_param(name):
            value = request.args.get(name)
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                abort(400)
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

        since = timestamp_param("since")
        alert_since = timestamp_param("alert_since")
        before = request.args.get("before")
        source, _, identifier = before.partition(":") if before else ("", "", "")
        if before and before.isdecimal():
            source, identifier = "g", before
        if before and (source not in ("g", "o") or not identifier.isdecimal() or int(identifier) < 1):
            abort(400)
        as_of = datetime.now(timezone.utc)
        github_filters = (ObservedChange.repository_name.in_(configured),
                          ObservedChange.observed_at >= as_of - timedelta(days=30),
                          ObservedChange.observed_at <= as_of)
        otrs_filters = (OTRSObservedChange.queue_id.in_(otrs_config["attention_queue_ids"]),
                        OTRSObservedChange.observed_at >= as_of - timedelta(days=30),
                        OTRSObservedChange.observed_at <= as_of) if otrs_manager else None
        otrs_url_parts = urlsplit(otrs_config["url"]) if otrs_manager else None
        otrs_origin = f"{otrs_url_parts.scheme}://{otrs_url_parts.netloc}" if otrs_url_parts else None
        with sessions() as session:
            cursor = None
            if before:
                cursor = session.get(ObservedChange if source == "g" else OTRSObservedChange, int(identifier))
                if cursor is None:
                    abort(400)
            def page_query(model, filters, rank):
                query = select(model).where(*filters)
                if cursor:
                    tie = model.observed_at == cursor.observed_at
                    if rank < (1 if source == "g" else 0):
                        query = query.where(or_(model.observed_at < cursor.observed_at, tie))
                    elif rank == (1 if source == "g" else 0):
                        query = query.where(or_(model.observed_at < cursor.observed_at,
                                                and_(tie, model.id < cursor.id)))
                    else:
                        query = query.where(model.observed_at < cursor.observed_at)
                return session.scalars(query.order_by(model.observed_at.desc(), model.id.desc()).limit(21)).all()

            combined = [("g", event) for event in page_query(ObservedChange, github_filters, 1)]
            if otrs_filters:
                combined += [("o", event) for event in page_query(OTRSObservedChange, otrs_filters, 0)]
            combined.sort(key=lambda item: (item[1].observed_at.replace(tzinfo=timezone.utc),
                                            1 if item[0] == "g" else 0, item[1].id), reverse=True)
            shown = combined[:20]

            def count_since(model, filters, value):
                if value is None:
                    return 0
                return session.scalar(select(func.count()).select_from(model).where(
                    *filters, model.observed_at > value)) or 0

            def total_since(value):
                return count_since(ObservedChange, github_filters, value) + (
                    count_since(OTRSObservedChange, otrs_filters, value) if otrs_filters else 0)

            return jsonify({"as_of": as_of.isoformat(),
                            "unread_count": total_since(since), "new_count": total_since(alert_since),
                            "otrs_origin": otrs_origin,
                            "events": [{"id": f"{source}:{event.id}", "source": "github" if source == "g" else "otrs",
                                        "kind": event.kind, "repository": event.repository_name if source == "g" else event.queue,
                                        "number": event.number, "title": event.title, "url": event.url,
                                        "detail": event.detail, "observed_at": event.observed_at.replace(tzinfo=timezone.utc).isoformat()}
                                       for source, event in shown],
                            "next_before": f"{shown[-1][0]}:{shown[-1][1].id}" if len(combined) > 20 else None})

    if auto_sync:
        manager.enable_scheduler()
        if otrs_manager:
            otrs_manager.enable_scheduler()
        if zabbix_manager:
            zabbix_manager.enable_scheduler()
    return app
