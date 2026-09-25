import json
import os
import re
import subprocess

class GitHubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GitHubCliClient:
    def __init__(self, runner=None):
        self.runner = runner or subprocess.run

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def _run(self, args, input_text=None):
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}}
        try:
            result = self.runner(args, input=input_text, capture_output=True, text=True,
                                 check=True, timeout=300, env=environment)
        except FileNotFoundError as exc:
            raise GitHubError("GitHub CLI (gh) is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError("GitHub CLI request timed out") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip().splitlines()
            message = detail[-1] if detail else "GitHub CLI request failed"
            match = re.search(r"HTTP (\d{3})", message)
            status = int(match.group(1)) if match else None
            if "not logged" in message.lower() or "authenticate" in message.lower():
                message = "GitHub CLI is not authenticated; run gh auth login"
            raise GitHubError(message, status) from None
        return result.stdout

    def _api(self, path, params=None, paginate=False):
        args = ["gh", "api", path.lstrip("/"), "--method", "GET",
                "-H", "X-GitHub-Api-Version:2026-03-10"]
        for key, value in (params or {}).items():
            if value is not None:
                args.extend(["-f", f"{key}={value}"])
        if paginate:
            args.extend(["--paginate", "--slurp"])
        output = self._run(args)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub CLI returned invalid JSON") from exc

    def get(self, path, **params):
        return self._api(path, params)

    def pages(self, path, **params):
        pages = self._api(path, {**params, "per_page": 100}, paginate=True)
        if not isinstance(pages, list):
            raise GitHubError("Unexpected GitHub pagination response")
        for result in pages:
            if not isinstance(result, list):
                raise GitHubError("Unexpected GitHub pagination response")
            yield from result

    def graphql(self, query, variables):
        output = self._run(["gh", "api", "graphql", "--input", "-"],
                           json.dumps({"query": query, "variables": variables}))
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub CLI returned invalid GraphQL JSON") from exc
        if data.get("errors"):
            messages = [e.get("message", "GraphQL error") for e in data["errors"]]
            detail = "; ".join(messages[:3])
            if "scope" in detail.lower() and "project" in detail.lower():
                detail += " (run gh auth refresh -s read:project)"
            raise GitHubError(detail)
        return data.get("data") or {}


PROJECT_QUERY = """
query($owner:String!, $number:Int!, $cursor:String, $status:String!, $priority:String!) {
  organization(login:$owner) {
    projectV2(number:$number) {
      id title
      fields(first:100) { pageInfo { hasNextPage endCursor } nodes { ... on ProjectV2FieldCommon { name } } }
      items(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content { ... on Issue { id number repository { nameWithOwner } } }
          status: fieldValueByName(name:$status) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          priority: fieldValueByName(name:$priority) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
            ... on ProjectV2ItemFieldTextValue { text }
            ... on ProjectV2ItemIssueFieldValue {
              issueFieldValue {
                ... on IssueFieldSingleSelectValue { name }
                ... on IssueFieldTextValue { value }
              }
            }
          }
        }
      }
    }
  }
}
"""

USER_PROJECT_QUERY = PROJECT_QUERY.replace("organization(login:$owner)", "user(login:$owner)")

PROJECT_FIELDS_QUERY = """
query($owner:String!, $number:Int!, $cursor:String) {
  organization(login:$owner) {
    projectV2(number:$number) {
      fields(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on ProjectV2FieldCommon { name } }
      }
    }
  }
}
"""
USER_PROJECT_FIELDS_QUERY = PROJECT_FIELDS_QUERY.replace("organization(login:$owner)", "user(login:$owner)")

RELATED_QUERY = """
query($ids:[ID!]!) {
  nodes(ids:$ids) {
    ... on PullRequest {
      id number
      closingIssuesReferences(first:100) {
        nodes { number repository { nameWithOwner } }
        pageInfo { hasNextPage }
      }
    }
  }
}
"""

RELATED_PAGE_QUERY = """
query($id:ID!, $cursor:String) {
  node(id:$id) {
    ... on PullRequest {
      closingIssuesReferences(first:100, after:$cursor) {
        nodes { number repository { nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def project_items(client, owner, number, status_field, priority_field, owner_type="organization"):
    query = PROJECT_QUERY if owner_type == "organization" else USER_PROJECT_QUERY
    cursor = None
    items = {}
    fields = set()
    while True:
        data = client.graphql(query, {"owner": owner, "number": int(number), "cursor": cursor,
                                      "status": status_field, "priority": priority_field})
        project = (data.get("organization") or data.get("user") or {}).get("projectV2")
        if not project:
            raise GitHubError(f"Project #{number} is not accessible")
        fields.update(n.get("name") for n in project["fields"]["nodes"] if n)
        field_info = project["fields"]["pageInfo"]
        field_cursor = field_info["endCursor"]
        while field_info["hasNextPage"]:
            field_query = PROJECT_FIELDS_QUERY if owner_type == "organization" else USER_PROJECT_FIELDS_QUERY
            field_data = client.graphql(field_query, {"owner": owner, "number": int(number), "cursor": field_cursor})
            field_project = (field_data.get("organization") or field_data.get("user") or {}).get("projectV2")
            if not field_project:
                raise GitHubError(f"Project #{number} fields are not accessible")
            connection_fields = field_project["fields"]
            fields.update(n.get("name") for n in connection_fields["nodes"] if n)
            field_info = connection_fields["pageInfo"]
            field_cursor = field_info["endCursor"]
        connection = project["items"]
        for node in connection["nodes"]:
            content = node.get("content") or {}
            repo = (content.get("repository") or {}).get("nameWithOwner")
            if repo and content.get("number"):
                priority_value = node.get("priority") or {}
                issue_field_value = priority_value.get("issueFieldValue") or {}
                items[(repo.lower(), content["number"])] = {
                    "id": node["id"],
                    "status": (node.get("status") or {}).get("name"),
                    "priority": priority_value.get("name") or priority_value.get("text")
                                or issue_field_value.get("name") or issue_field_value.get("value"),
                }
        if not connection["pageInfo"]["hasNextPage"]:
            return items, fields
        cursor = connection["pageInfo"]["endCursor"]


def related_issues(client, pulls):
    result = {}
    for offset in range(0, len(pulls), 50):
        chunk = pulls[offset:offset + 50]
        data = client.graphql(RELATED_QUERY, {"ids": [p["node_id"] for p in chunk]})
        for node in data.get("nodes") or []:
            if not node:
                continue
            refs = node["closingIssuesReferences"]
            found = list(refs["nodes"])
            while refs["pageInfo"]["hasNextPage"]:
                page = client.graphql(RELATED_PAGE_QUERY, {"id": node["id"], "cursor": refs["pageInfo"]["endCursor"]})
                refs = page["node"]["closingIssuesReferences"]
                found.extend(refs["nodes"])
            result[node["number"]] = [
                {"repository": n["repository"]["nameWithOwner"], "number": n["number"]}
                for n in found if n and n.get("repository")
            ]
    return result
