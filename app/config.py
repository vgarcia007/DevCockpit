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
    data.setdefault("sync", {}).setdefault("interval_seconds", 300)
    if int(data["sync"]["interval_seconds"]) < 10:
        raise ValueError("sync.interval_seconds must be at least 10")
    if "otrs" in data and data["otrs"] is not None:
        otrs = data["otrs"]
        if isinstance(otrs, list):
            if not all(isinstance(part, dict) for part in otrs):
                raise ValueError("otrs must contain user and password")
            combined = {}
            for part in otrs:
                if any(key in combined for key in part):
                    raise ValueError("Duplicate otrs setting")
                combined.update(part)
            otrs = combined
        if not isinstance(otrs, dict):
            raise ValueError("otrs must be a mapping")
        otrs.setdefault("enabled", True)
        if not isinstance(otrs["enabled"], bool):
            raise ValueError("otrs.enabled must be true or false")
        if otrs["enabled"]:
            if not otrs.get("user") or not otrs.get("password"):
                raise ValueError("otrs needs user and password when enabled")
            if not otrs.get("url"):
                raise ValueError("otrs.url is required when enabled")
            if not otrs.get("queue_ids"):
                raise ValueError("otrs.queue_ids is required when enabled")
            otrs.setdefault("interval_seconds", 900)
            otrs.setdefault("excluded_states", [])
            if not isinstance(otrs["queue_ids"], list) or not otrs["queue_ids"] or not all(isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in otrs["queue_ids"]):
                raise ValueError("otrs.queue_ids must be a list of positive IDs")
            otrs.setdefault("attention_queue_ids", [])
            otrs.setdefault("highlight_queue_ids", [])
            if not str(otrs["url"]).startswith("https://"):
                raise ValueError("otrs.url must use HTTPS")
            if int(otrs["interval_seconds"]) < 60:
                raise ValueError("otrs.interval_seconds must be at least 60")
            for key in ("attention_queue_ids", "highlight_queue_ids"):
                value = otrs[key]
                if not isinstance(value, list) or any(not isinstance(i, int) or isinstance(i, bool) or i not in otrs["queue_ids"] for i in value):
                    raise ValueError(f"otrs.{key} must contain configured queue IDs")
            if not isinstance(otrs["excluded_states"], list) or any(not isinstance(s, str) for s in otrs["excluded_states"]):
                raise ValueError("otrs.excluded_states must be a list of statuses")
        data["otrs"] = otrs
    unique = {}
    for person in data.get("team", []):
        unique[person["github"].lower()] = person
    data["team"] = list(unique.values())
    return data
