"""Personal Projects snapshots and a shared, deduplicated work view."""
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import delete, select

from .github import GitHubError, RateLimitError
from .models import Issue, ObservedChange, PersonalProject, PersonalProjectItem


PERSONAL_QUERY = """
query($owner:String!, $number:Int!, $cursor:String, $status:String!, $priority:String!) {
  user(login:$owner) { projectV2(number:$number) {
    title
    items(first:100, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id isArchived createdAt updatedAt
        status: fieldValueByName(name:$status) {
          ... on ProjectV2ItemFieldSingleSelectValue { name }
        }
        priority: fieldValueByName(name:$priority) {
          ... on ProjectV2ItemFieldSingleSelectValue { name }
          ... on ProjectV2ItemFieldTextValue { text }
        }
        content {
          __typename
          ... on DraftIssue {
            id title body createdAt updatedAt creator { login }
            assignees(first:100) { nodes { login } pageInfo { hasNextPage endCursor } }
          }
          ... on Issue {
            id number title body url state createdAt updatedAt closedAt
            author { login } repository { nameWithOwner }
            assignees(first:100) { nodes { login } pageInfo { hasNextPage endCursor } }
            labels(first:100) { nodes { name } pageInfo { hasNextPage endCursor } }
            milestone { title }
          }
        }
      }
    }
  } }
}
"""

ORGANIZATION_PERSONAL_QUERY = PERSONAL_QUERY.replace("user(login:$owner)", "organization(login:$owner)")


def as_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def personal_url(owner, number, owner_type="user"):
    segment = "orgs" if owner_type == "organization" else "users"
    return f"https://github.com/{segment}/{owner}/projects/{number}"


def project_reference(person, cfg):
    owner_type = person.get("project_owner_type", "user")
    owner = person.get("project_owner") or (cfg.get("github", {}).get("organization")
        if owner_type == "organization" else person["github"])
    return owner.lower(), owner_type, person.get("project_number")


def person_project_url(person, cfg):
    owner, owner_type, number = project_reference(person, cfg)
    return personal_url(owner, number, owner_type)


def saved_reference(state):
    # Existing snapshots were always user-owned and keyed by the member's login.
    return (state.owner or state.login).lower(), state.owner_type or "user", state.number


def fetch_project(client, login, number, cfg, owner_type="user"):
    # Reuse the existing field pagination without changing its public result shape.
    from .github import PROJECT_FIELDS_QUERY, USER_PROJECT_FIELDS_QUERY
    root = "organization" if owner_type == "organization" else "user"
    field_query = PROJECT_FIELDS_QUERY if owner_type == "organization" else USER_PROJECT_FIELDS_QUERY
    item_query = ORGANIZATION_PERSONAL_QUERY if owner_type == "organization" else PERSONAL_QUERY
    fields, field_cursor = set(), None
    while True:
        data = client.graphql(field_query, {"owner": login, "number": number, "cursor": field_cursor})
        project = (data.get(root) or {}).get("projectV2")
        if not project:
            raise GitHubError(f"Personal project #{number} is not accessible")
        connection = project["fields"]
        fields.update(field["name"] for field in connection["nodes"] if field)
        if not connection["pageInfo"]["hasNextPage"]:
            break
        next_cursor = connection["pageInfo"].get("endCursor")
        if not next_cursor or next_cursor == field_cursor:
            raise GitHubError("Repeated personal project field cursor")
        field_cursor = next_cursor
    rows, cursor, incomplete = {}, None, False
    while True:
        data = client.graphql(item_query, {"owner": login, "number": number, "cursor": cursor,
            "status": cfg["workflow"]["status_field"], "priority": cfg["priority"]["field"]})
        project = (data.get(root) or {}).get("projectV2")
        if not project:
            raise GitHubError(f"Personal project #{number} is not accessible")
        for item in project["items"]["nodes"]:
            if not item:
                incomplete = True
                continue
            content = item.get("content")
            if not content:
                incomplete = True
                continue
            if content["__typename"] not in ("DraftIssue", "Issue"):
                continue
            if any((content.get(key) or {}).get("pageInfo", {}).get("hasNextPage") for key in ("assignees", "labels")):
                raise GitHubError("Personal project item metadata is incomplete")
            item = dict(item, fields=sorted(fields))
            rows[item["id"]] = item
        info = project["items"]["pageInfo"]
        if not info["hasNextPage"]:
            if cfg["priority"]["source"] == "issue_field":
                from .sync import issue_field_priorities
                issue_items = [item for item in rows.values() if item["content"]["__typename"] == "Issue"]
                for offset in range(0, len(issue_items), 50):
                    batch = issue_items[offset:offset + 50]
                    values = issue_field_priorities(client, [{"node_id": item["content"]["id"]} for item in batch], cfg["priority"]["field"])
                    for item in batch:
                        item["issue_priority"] = values[item["content"]["id"]]
            return project["title"], rows, incomplete
        if not info.get("endCursor") or info["endCursor"] == cursor:
            raise GitHubError("Repeated personal project pagination cursor")
        cursor = info["endCursor"]


def sync_projects(sessions, client, cfg):
    successes = 0
    for person in cfg["team"]:
        number = person.get("project_number")
        if number is None:
            continue
        login = person["github"].lower()
        owner, owner_type, number = reference = project_reference(person, cfg)
        now = datetime.now(timezone.utc)
        try:
            title, items, incomplete = fetch_project(client, owner, number, cfg, owner_type)
        except RateLimitError:
            raise
        except Exception as exc:
            with sessions() as session:
                state = session.get(PersonalProject, login)
                if not state or saved_reference(state) != reference:
                    session.execute(delete(PersonalProjectItem).where(PersonalProjectItem.login == login))
                    state = state or PersonalProject(login=login, number=number)
                    state.number, state.title, state.last_success, state.incomplete = number, None, None, False
                state.owner, state.owner_type = owner, owner_type
                state.last_attempt, state.error = now, str(exc)
                session.add(state)
                session.commit()
            continue
        with sessions() as session:
            state = session.get(PersonalProject, login)
            previous = {item.item_id: item for item in session.scalars(select(PersonalProjectItem).where(
                PersonalProjectItem.login == login))} if state and saved_reference(state) == reference else {}
            replacements = []
            for item_id, payload in items.items():
                old = previous.get(item_id)
                view = item_view(login, number, person, payload, cfg)
                completed = old.completed_at if old else None
                if view.is_draft:
                    old_status = ((old.payload.get("status") or {}).get("name")) if old else None
                    if view.state == "open":
                        completed = None
                    elif old and old_status != cfg["workflow"]["values"]["done"]:
                        completed = now
                else:
                    completed = None
                replacements.append(PersonalProjectItem(login=login, item_id=item_id, payload=payload, completed_at=completed))
                if old and not payload["isArchived"]:
                    old_view = item_view(login, number, person, old.payload, cfg)
                    details = []
                    if view.workflow_state == old_view.workflow_state == "known" and view.workflow != old_view.workflow:
                        details.append(("workflow", f"Project status changed to {view.workflow}"))
                    if view.priority_state == "known" and view.priority != old_view.priority:
                        details.append(("urgent" if view.priority == "Urgent" else "priority", f"Project priority changed to {view.priority}"))
                    for kind, detail in details:
                        session.add(ObservedChange(repository_name=f"personal:{login}", kind=kind,
                            number=view.number, title=view.title, url=view.url, detail=detail, observed_at=now))
            session.execute(delete(PersonalProjectItem).where(PersonalProjectItem.login == login))
            session.add_all(replacements)
            state = state or PersonalProject(login=login, number=number)
            state.number, state.title = number, title
            state.owner, state.owner_type = owner, owner_type
            state.last_attempt = state.last_success = now
            required_fields = [cfg["workflow"]["status_field"]]
            if cfg["priority"]["source"] == "project" or any(item["content"]["__typename"] == "DraftIssue" for item in items.values()):
                required_fields.append(cfg["priority"]["field"])
            warnings = [f"Project field {name} is missing" for name in required_fields if name not in (next(iter(items.values()))["fields"] if items else [])]
            # Empty projects have no tasks whose field values could be unavailable.
            state.error = "; ".join(warnings) if items and warnings else None
            state.incomplete = incomplete
            session.add(state)
            session.commit()
        successes += 1
    return successes


def item_view(login, number, person, payload, cfg, completed_at=None):
    content = payload["content"]
    draft = content["__typename"] == "DraftIssue"
    fields = payload["fields"]
    workflow = (payload.get("status") or {}).get("name")
    priority = payload.get("priority") or {}
    priority = priority.get("name") or priority.get("text")
    priority_state = "unavailable" if cfg["priority"]["field"] not in fields else "known" if priority is not None else "no_priority"
    if not draft and cfg["priority"]["source"] == "issue_field":
        priority, priority_state = payload.get("issue_priority", (None, "unavailable"))
    assignees = [user["login"] for user in content.get("assignees", {}).get("nodes", []) if user]
    return SimpleNamespace(
        identity=content["id"], personal_owners=[login], source=f"personal:{login}", is_draft=draft,
        repository_name=f"Personal · {person.get('name', login)}", full_name=(content.get("repository") or {}).get("nameWithOwner"),
        number=content.get("number"), title=content["title"], body=content.get("body"),
        url=content.get("url") or person_project_url(person, cfg),
        state=("closed" if workflow == cfg["workflow"]["values"]["done"] else "open") if draft else content["state"].lower(),
        author=(content.get("creator" if draft else "author") or {}).get("login"),
        actual_assignees=assignees, assignees=list(dict.fromkeys([*assignees, login])),
        labels=[label["name"] for label in (content.get("labels") or {}).get("nodes", []) if label],
        milestone=(content.get("milestone") or {}).get("title"),
        created_at=as_date(content["createdAt"]), updated_at=max(as_date(content["updatedAt"]), as_date(payload["updatedAt"])),
        closed_at=completed_at if draft else as_date(content.get("closedAt")),
        workflow=workflow, workflow_state="unavailable" if cfg["workflow"]["status_field"] not in fields else "known" if workflow else "no_status",
        priority=priority, priority_state=priority_state,
        project_item_id=payload["id"], related_pulls=[],
    )


def project_states(session, cfg):
    configured = {p["github"].lower(): project_reference(p, cfg) for p in cfg["team"] if p.get("project_number") is not None}
    return [state for state in session.scalars(select(PersonalProject)) if configured.get(state.login) == saved_reference(state)]


def work_items(session, issues, repos, cfg):
    repo_by_name = {repo.name: repo for repo in repos}
    repo_config = {repo["name"]: repo for repo in cfg["repositories"]}
    rows, by_identity = [], {}
    for issue in issues:
        row = SimpleNamespace(**{column.name: getattr(issue, column.name) for column in Issue.__table__.columns})
        row.is_draft, row.personal_owners = False, []
        row.actual_assignees = list(row.assignees)
        row.source, row.identity = "repository", f"{repo_by_name[row.repository_name].full_name.lower()}#{row.number}"
        rows.append(row)
        by_identity[row.identity] = row
    people = {p["github"].lower(): p for p in cfg["team"] if p.get("project_number") is not None}
    states = {state.login: state for state in project_states(session, cfg)}
    items = session.scalars(select(PersonalProjectItem).order_by(PersonalProjectItem.login, PersonalProjectItem.item_id))
    for item in items:
        if item.login not in states or item.payload["isArchived"]:
            continue
        row = item_view(item.login, states[item.login].number, people[item.login], item.payload, cfg, item.completed_at)
        identity = row.identity if row.is_draft else f"{row.full_name.lower()}#{row.number}"
        existing = by_identity.get(identity)
        if existing:
            if existing.source == "repository" and repo_config.get(existing.repository_name, {}).get("project_number") is None and not existing.personal_owners:
                for name in ("workflow", "workflow_state", "priority", "priority_state"):
                    setattr(existing, name, getattr(row, name))
            existing.personal_owners.append(item.login)
            existing.assignees = list(dict.fromkeys([*existing.assignees, *row.assignees]))
        else:
            row.identity = identity
            by_identity[identity] = row
            rows.append(row)
    return rows
