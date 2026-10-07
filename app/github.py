import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit
from sqlalchemy import delete
from .models import ApiCache

API_VERSION = "2026-03-10"

class GitHubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class RateLimitError(GitHubError):
    def __init__(self, message, status=None, retry_at=None):
        super().__init__(message, status)
        self.retry_at = retry_at


class GitHubCliClient:
    def __init__(self, runner=None, cache_sessions=None):
        self.runner = runner or subprocess.run
        self.cache_sessions = cache_sessions
        self.cache_identity = None

    def __enter__(self):
        if self.cache_sessions:
            with self.cache_sessions() as session:
                session.execute(delete(ApiCache).where(ApiCache.last_used < datetime.now(timezone.utc) - timedelta(days=30)))
                session.commit()
            try:
                _, _, body = self._request(["gh", "api", "user", "--method", "GET", "-i",
                                           "-H", f"X-GitHub-Api-Version:{API_VERSION}"])
                self.cache_identity = json.loads(body)["login"].lower()
            except RateLimitError:
                raise
            except (GitHubError, KeyError, ValueError):
                # Without a verified account, reuse of private cached data is unsafe.
                self.cache_identity = None
        return self

    def __exit__(self, *_):
        return False

    def _run(self, args, input_text=None):
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}}
        try:
            result = self.runner(args, input=input_text, capture_output=True, text=True,
                                 check=False, timeout=300, env=environment)
        except FileNotFoundError as exc:
            raise GitHubError("GitHub CLI (gh) is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError("GitHub CLI request timed out") from exc
        except subprocess.CalledProcessError as exc:
            # Some injected runners still use check=True.
            result = subprocess.CompletedProcess(args, exc.returncode, exc.stdout or "", exc.stderr or "")
        return result

    def _request(self, args, input_text=None):
        result = self._run(args, input_text)
        output = result.stdout or ""
        if output.startswith("HTTP/"):
            separator = "\r\n\r\n" if "\r\n\r\n" in output else "\n\n"
            head, _, body = output.partition(separator)
            match = re.match(r"HTTP/\S+\s+(\d+)", head)
            status = int(match.group(1)) if match else 0
            headers = {key.lower(): value.strip() for line in head.splitlines() if ":" in line
                       for key, value in [line.split(":", 1)]}
        else:
            status, headers, body = (200 if result.returncode == 0 else 0), {}, output
        if status == 304:
            return status, headers, body
        if (result.returncode and status != 200) or status >= 400:
            try:
                message = json.loads(body).get("message", "")
            except (ValueError, AttributeError):
                message = ""
            detail = (result.stderr or "").strip().splitlines()
            message = message or (detail[-1] if detail else "GitHub CLI request failed")
            if "not logged" in message.lower() or "authenticate" in message.lower():
                message = "GitHub CLI is not authenticated; run gh auth login"
            if not status:
                match = re.search(r"HTTP (\d{3})", message)
                status = int(match.group(1)) if match else None
            rate_limited = status == 429 or (status == 403 and (
                headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0" or
                "rate limit" in message.lower() or "abuse detection" in message.lower()))
            if rate_limited:
                retry_at = None
                retry_after = headers.get("retry-after")
                if retry_after:
                    try:
                        retry_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(retry_after)))
                    except ValueError:
                        try:
                            retry_at = parsedate_to_datetime(retry_after).astimezone(timezone.utc)
                        except (TypeError, ValueError):
                            pass
                if retry_at is None and headers.get("x-ratelimit-remaining") == "0":
                    try:
                        retry_at = datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), timezone.utc) + timedelta(seconds=5)
                    except (KeyError, ValueError, OverflowError):
                        pass
                raise RateLimitError(message, status, retry_at)
            raise GitHubError(message, status)
        return status, headers, body

    def _rest_page(self, path, params=None, *, cache=True):
        params = {key: value for key, value in (params or {}).items() if value is not None}
        cache_key = None
        cached = None
        if cache and self.cache_sessions and self.cache_identity:
            cache_key = json.dumps([os.getenv("GH_HOST", "github.com"), self.cache_identity, API_VERSION,
                                    path.lstrip("/"), sorted(params.items())], separators=(",", ":"))
            with self.cache_sessions() as session:
                cached = session.get(ApiCache, cache_key)
                if cached:
                    cached = (cached.etag, cached.body, cached.link)
        args = ["gh", "api", path.lstrip("/"), "--method", "GET", "-i",
                "-H", f"X-GitHub-Api-Version:{API_VERSION}"]
        for key, value in (params or {}).items():
            if value is not None:
                args.extend(["-f", f"{key}={value}"])
        if cached:
            args.extend(["-H", f"If-None-Match: {cached[0]}"])
        status, headers, body = self._request(args)
        if status == 304:
            try:
                data = json.loads(cached[1]) if cached else None
            except json.JSONDecodeError:
                data = None
            if data is not None:
                link = cached[2]
                with self.cache_sessions() as session:
                    row = session.get(ApiCache, cache_key)
                    if row:
                        row.last_used = datetime.now(timezone.utc)
                        session.commit()
                return data, link
            status, headers, body = self._request(args[:-2] if cached else args)
            if status == 304:
                raise GitHubError("GitHub returned 304 without a usable cached response")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub CLI returned invalid JSON") from exc
        link = headers.get("link")
        if cache_key:
            with self.cache_sessions() as session:
                if headers.get("etag"):
                    session.merge(ApiCache(key=cache_key, etag=headers["etag"], body=body,
                                           link=link, last_used=datetime.now(timezone.utc)))
                else:
                    old = session.get(ApiCache, cache_key)
                    if old:
                        session.delete(old)
                session.commit()
        return data, link

    def get(self, path, **params):
        return self._rest_page(path, params)[0]

    def pages(self, path, **params):
        page = 1
        while True:
            result, link = self._rest_page(path, {**params, "per_page": 100, "page": page})
            if not isinstance(result, list):
                raise GitHubError("Unexpected GitHub pagination response")
            yield from result
            if not link or not re.search(r'<[^>]+>;\s*rel="next"', link):
                break
            page += 1

    def security_pages(self, path):
        """Follow server pagination; never cache raw security response bodies."""
        params = {"per_page": 100}
        seen = set()
        while True:
            key = (path, tuple(sorted(params.items())))
            if key in seen:
                raise GitHubError("Repeated security pagination cursor")
            seen.add(key)
            result, link = self._rest_page(path, params, cache=False)
            if not isinstance(result, list):
                raise GitHubError("Unexpected GitHub security response")
            yield from result
            match = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
            if not match:
                return
            next_url = urlsplit(match[1])
            if next_url.path.rstrip("/") != path.split("?", 1)[0].rstrip("/"):
                raise GitHubError("Unexpected security pagination path")
            params = dict(parse_qsl(next_url.query))

    def graphql(self, query, variables):
        _, headers, output = self._request(["gh", "api", "graphql", "--input", "-", "-i"],
                                           json.dumps({"query": query, "variables": variables}))
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub CLI returned invalid GraphQL JSON") from exc
        if data.get("errors"):
            messages = [e.get("message", "GraphQL error") for e in data["errors"]]
            detail = "; ".join(messages[:3])
            if "rate limit" in detail.lower() or headers.get("x-ratelimit-remaining") == "0":
                retry_at = None
                try:
                    retry_at = datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), timezone.utc) + timedelta(seconds=5)
                except (KeyError, ValueError, OverflowError):
                    pass
                raise RateLimitError(detail, retry_at=retry_at)
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
