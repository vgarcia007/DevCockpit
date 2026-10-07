"""Read-only security alert normalization and independent source snapshots."""
from datetime import datetime, timezone
from sqlalchemy import delete
from .github import RateLimitError
from .models import SecurityAlert, SecuritySyncState, SyncMeta

SOURCES = {"dependabot": "Dependabot", "code-scanning": "Code scanning", "secret-scanning": "Secret scanning"}
SEVERITIES = ["critical", "high", "medium", "low", "warning", "note", "error", "unknown"]


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def alert_data(item, repo, source):
    state = item["state"]
    group = "open" if state == "open" else "dismissed" if state in {"dismissed", "auto_dismissed"} else "fixed"
    if source == "secret-scanning" and item.get("resolution") in {"false_positive", "used_in_tests", "wont_fix"}:
        group = "dismissed"
    title, location, identifiers, fix, severity = "", None, None, None, "unknown"
    if source == "dependabot":
        advisory = item.get("security_advisory") or {}
        vulnerability = item.get("security_vulnerability") or {}
        dependency = item.get("dependency") or {}
        package = dependency.get("package") or vulnerability.get("package") or {}
        title = advisory.get("summary") or package.get("name") or "Dependency vulnerability"
        location = " · ".join(filter(None, [package.get("name"), package.get("ecosystem"), dependency.get("manifest_path")]))
        identifiers = " ".join(i.get("value", "") for i in advisory.get("identifiers") or [])
        fix = (vulnerability.get("first_patched_version") or {}).get("identifier")
        severity = vulnerability.get("severity") or advisory.get("severity") or "unknown"
    elif source == "code-scanning":
        rule = item.get("rule") or {}
        instance = item.get("most_recent_instance") or {}
        # Do not copy code snippets or arbitrary instance messages into the cache.
        title = rule.get("description") or rule.get("name") or rule.get("id") or "Code scanning alert"
        found = instance.get("location") or {}
        location = found.get("path")
        if location and found.get("start_line"):
            location += f":{found['start_line']}"
        identifiers = rule.get("id")
        severity = rule.get("security_severity_level") or rule.get("severity") or "unknown"
    else:
        title = item.get("secret_type_display_name") or item.get("secret_type") or "Exposed secret"
        identifiers = item.get("secret_type")
    updated = item.get("updated_at") or max(filter(None, [item.get("fixed_at"), item.get("dismissed_at"), item.get("resolved_at"), item.get("created_at")]), default=None)
    return dict(repository_name=repo, source=source, number=item["number"], title=title,
                url=item["html_url"], severity=severity if severity in SEVERITIES else "unknown",
                state=state, status_group=group, location=location, identifiers=identifiers,
                fix_version=fix, created_at=timestamp(item.get("created_at")), updated_at=timestamp(updated))


def sync_security(sessions, client, repo):
    for source in SOURCES:
        now = datetime.now(timezone.utc)
        error = None
        rows = None
        rate_limit = None
        try:
            rows = [alert_data(item, repo["name"], source) for item in
                    client.security_pages(f'/repos/{repo["full_name"]}/{source}/alerts')]
        except Exception as exc:
            if isinstance(exc, RateLimitError):
                rate_limit = exc
            # Secret scanning error bodies may contain sensitive values; keep only status.
            if source == "secret-scanning":
                status = getattr(exc, "status", None)
                error = f"Secret scanning could not be read (HTTP {status})" if status else "Secret scanning could not be read"
                if rate_limit:
                    rate_limit = RateLimitError(error, status, exc.retry_at)
            else:
                error = str(exc)
        with sessions() as session:
            state = session.get(SecuritySyncState, (repo["name"], source))
            if state is None:
                state = SecuritySyncState(repository_name=repo["name"], source=source)
                session.add(state)
            state.last_attempt, state.error = now, error
            if rows is not None:
                session.execute(delete(SecurityAlert).where(SecurityAlert.repository_name == repo["name"], SecurityAlert.source == source))
                session.add_all(SecurityAlert(**row) for row in rows)
                state.last_success = now
            session.merge(SyncMeta(key="security_revision", value=now.isoformat()))
            session.commit()
        if rate_limit:
            raise rate_limit
