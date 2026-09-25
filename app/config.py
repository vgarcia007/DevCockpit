from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path=None):
    path = Path(path or ROOT / "config.yml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data.get("repositories"), list) or not data["repositories"]:
        raise ValueError("config.yml needs a non-empty repositories list")
    names = set()
    for repo in data["repositories"]:
        if not repo.get("name") or not repo.get("url"):
            raise ValueError("Each repository needs name and url")
        parts = repo["url"].strip("/").split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"Invalid repository URL for {repo['name']}")
        repo["full_name"] = "/".join(parts)
        if repo["name"] in names:
            raise ValueError(f"Duplicate repository name: {repo['name']}")
        names.add(repo["name"])
    data.setdefault("workflow", {})
    data["workflow"].setdefault("status_field", "Status")
    data["workflow"].setdefault("values", {
        "backlog": "Backlog", "ready": "Ready", "in_progress": "In Progress",
        "in_review": "In Review", "done": "Done",
    })
    data.setdefault("priority", {})
    data["priority"].setdefault("source", "project")
    data["priority"].setdefault("field", "Priority")
    if data["priority"]["source"] not in ("project", "issue_field"):
        raise ValueError("priority.source must be project or issue_field")
    data.setdefault("sync", {}).setdefault("interval_seconds", 60)
    if int(data["sync"]["interval_seconds"]) < 10:
        raise ValueError("sync.interval_seconds must be at least 10")
    unique = {}
    for person in data.get("team", []):
        unique[person["github"].lower()] = person
    data["team"] = list(unique.values())
    return data
