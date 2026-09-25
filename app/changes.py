"""Technical observations of changes between completed GitHub syncs."""


def detect_changes(previous_issues, previous_pulls, previous_releases,
                   issues, pulls, releases, workflow_values):
    changes = []

    def add(kind, item, detail):
        changes.append({"kind": kind, "number": item.get("number"),
                        "title": item.get("title") or item.get("tag") or "Untitled",
                        "url": item["url"], "detail": detail})

    for item in issues:
        old = previous_issues.get(item["github_id"])
        if item["state"] != "open":
            if old and old.state == "open" and item["state"] == "closed":
                add("issue_closed", item, "Issue closed on GitHub")
            continue
        if old and old.workflow_state == item["workflow_state"] == "known" and old.workflow != item["workflow"]:
            for key, kind, detail in (("ready", "ready", "Moved to Ready in GitHub Project"),
                                      ("in_progress", "in_progress", "Moved to In Progress in GitHub Project"),
                                      ("in_review", "in_review", "Moved to In Review in GitHub Project")):
                if item["workflow"] == workflow_values.get(key):
                    add(kind, item, detail)
                    break
        if item["priority_state"] == "known" and item["priority"] == "Urgent":
            if old is None or (old.priority_state in ("known", "no_priority") and old.priority != "Urgent"):
                add("urgent", item, "Open issue gained GitHub priority Urgent")

    for item in pulls:
        old = previous_pulls.get(item["github_id"])
        if item["state"] == "merged":
            if old and old.state == "open":
                add("pr_merged", item, "Pull request merged on GitHub")
            continue
        if item["state"] != "open":
            continue
        if old is None:
            add("pr_opened", item, "Pull request opened on GitHub")
        elif old.state == "open":
            reviewers = set(item["requested_reviewers"]) - set(old.requested_reviewers or [])
            if reviewers:
                add("review_requested", item, "Review requested from " + ", ".join(sorted(reviewers)))
            if item["review_state"] == "Changes Requested" and old.review_state != "Changes Requested":
                add("changes_requested", item, "Changes requested in a GitHub review")
            if item["ci_state"] == "Failing" and old.ci_state in ("Passing", "Running"):
                add("ci_failing", item, "Checks changed to failing")

    for item in releases:
        old = previous_releases.get(item["github_id"])
        if not item["draft"] and item["published_at"] and (old is None or old.draft or not old.published_at):
            add("release", item, "Prerelease published" if item["prerelease"] else "Release published")

    return changes
