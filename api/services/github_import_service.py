"""Service for importing GitHub issue and pull-request comment threads into dataset uploads.

Three loader backends are supported:
  graphql   — direct GitHub GraphQL API (lean, minimal rate-limit cost)
  langchain — langchain_community.document_loaders.GitHubIssuesLoader (REST API)
  llamaindex — llama_index.readers.github.GitHubRepositoryIssuesReader (REST API)

Each issue or PR is exported as one JSONL record whose `body` field contains the
comment texts for RAG chunking; metadata fields (url, number, title, …) are stored
separately so they surface after retrieval.
"""

from __future__ import annotations

import json
import logging
import operator
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

OnProgress = Callable[[int, str, "dict[str, Any] | None"], None]

import httpx

import services
from controllers.common.errors import FileTooLargeError, RemoteFileUploadError, UnsupportedFileTypeError
from dify_graph.file import helpers as file_helpers
from extensions.ext_database import db
from fields.file_fields import FileWithSignedUrl
from libs.login import current_account_with_tenant
from services.file_service import FileService

logger = logging.getLogger(__name__)

_GITHUB_GRAPHQL_ENDPOINT = "https://api.github.com/graphql"
_PER_PAGE = 50
_MAX_RETRIES = 2


def _nonempty(val: Any) -> str:
    """Return a clean string, treating None and the literal string 'None' as empty."""
    s = str(val).strip() if val is not None else ""
    return "" if s == "None" else s


_REPO_QUERY = """
query(
  $owner: String!
  $repo: String!
  $issuePerPage: Int!
  $issueNextPageCursor: String
  $pullRequestPerPage: Int!
  $pullRequestNextPageCursor: String
  $issueStates: [IssueState!]
  $pullRequestStates: [PullRequestState!]
) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    issues(
      first: $issuePerPage
      after: $issueNextPageCursor
      filterBy: { states: $issueStates }
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      pageInfo { endCursor hasNextPage }
      nodes {
        id number url title state createdAt body
        author { login }
        comments(first: $issuePerPage) {
          pageInfo { endCursor hasNextPage }
          nodes { body }
        }
      }
    }
    pullRequests(
      first: $pullRequestPerPage
      after: $pullRequestNextPageCursor
      states: $pullRequestStates
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      pageInfo { endCursor hasNextPage }
      nodes {
        id number url title state createdAt body
        author { login }
        comments(first: $pullRequestPerPage) {
          pageInfo { endCursor hasNextPage }
          nodes { body }
        }
      }
    }
  }
}
"""

_NODE_COMMENT_QUERY = """
query($perPage: Int!, $id: ID!, $commentCursor: String!) {
  rateLimit { limit cost remaining resetAt }
  node(id: $id) {
    ... on Issue {
      comments(first: $perPage, after: $commentCursor) {
        pageInfo { endCursor hasNextPage }
        nodes { body }
      }
    }
    ... on PullRequest {
      comments(first: $perPage, after: $commentCursor) {
        pageInfo { endCursor hasNextPage }
        nodes { body }
      }
    }
  }
}
"""


class GithubImportService:

    @staticmethod
    def import_repo(
        *,
        repo: str,
        include_issues: bool,
        include_prs: bool,
        include_closed: bool,
        github_token: str,
        loader: Literal["graphql", "langchain", "llamaindex"] = "graphql",
    ) -> tuple[dict, int]:
        try:
            owner, repo_name = repo.strip().split("/", 1)
        except ValueError:
            raise RemoteFileUploadError("Repository must be in 'owner/repo' format")

        if loader == "langchain":
            records = GithubImportService._fetch_langchain(
                token=github_token,
                owner=owner,
                repo_name=repo_name,
                repo=repo,
                include_issues=include_issues,
                include_prs=include_prs,
                include_closed=include_closed,
            )
        elif loader == "llamaindex":
            records = GithubImportService._fetch_llamaindex(
                token=github_token,
                owner=owner,
                repo_name=repo_name,
                repo=repo,
                include_issues=include_issues,
                include_prs=include_prs,
                include_closed=include_closed,
            )
        else:
            nodes = GithubImportService._fetch_all_nodes(
                token=github_token,
                owner=owner,
                repo=repo_name,
                include_issues=include_issues,
                include_prs=include_prs,
                include_closed=include_closed,
            )
            if not nodes:
                raise RemoteFileUploadError("No issues or pull requests matched the selected filters")
            records = [r for n in nodes if (r := GithubImportService._node_to_record(n, repo=repo))["body"]]

        if not records:
            raise RemoteFileUploadError("No issues or pull requests matched the selected filters")

        content = ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode("utf-8")
        safe_repo = repo.replace("/", "-")
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
        filename = f"github-{safe_repo}-{loader}-{timestamp}.jsonl"

        try:
            user, _ = current_account_with_tenant()
            upload_file = FileService(db.engine).upload_file(
                filename=filename,
                content=content,
                mimetype="application/x-ndjson",
                user=user,
                source="datasets",
                source_url=f"github://{repo}",
            )
        except services.errors.file.FileTooLargeError as exc:
            raise FileTooLargeError(exc.description)
        except services.errors.file.UnsupportedFileTypeError:
            raise UnsupportedFileTypeError()

        return (
            FileWithSignedUrl(
                id=upload_file.id,
                name=upload_file.name,
                size=upload_file.size,
                extension=upload_file.extension,
                url=file_helpers.get_signed_file_url(upload_file_id=upload_file.id),
                mime_type=upload_file.mime_type,
                created_by=upload_file.created_by,
                created_at=int(upload_file.created_at.timestamp()),
            ).model_dump(mode="json"),
            201,
        )

    # ------------------------------------------------------------------
    # Streaming GraphQL fetcher — used by the background task for resume support
    # ------------------------------------------------------------------

    @staticmethod
    def stream_graphql_records(
        *,
        token: str,
        owner: str,
        repo: str,
        repo_full: str,
        include_issues: bool,
        include_prs: bool,
        include_closed: bool,
        on_record: Callable[[dict[str, Any]], None],
        on_checkpoint: Callable[[str | None, str | None, int], None],
        issue_cursor_start: str | None = None,
        pr_cursor_start: str | None = None,
        initial_count: int = 0,
    ) -> int:
        """
        Fetch issues/PRs one GraphQL page at a time.

        For each node: extra comments are fetched immediately, the node is converted to a
        record, and ``on_record`` is called.  After each page ``on_checkpoint`` is called
        with the current cursors and running total so the task can persist them.

        Returns the total number of records emitted.
        """

        variables: dict[str, Any] = {"owner": owner, "repo": repo}
        if include_issues:
            variables["issuePerPage"] = _PER_PAGE
            if not include_closed:
                variables["issueStates"] = ["OPEN"]
        else:
            variables["issuePerPage"] = 0

        if include_prs:
            variables["pullRequestPerPage"] = _PER_PAGE
            if not include_closed:
                variables["pullRequestStates"] = ["OPEN"]
        else:
            variables["pullRequestPerPage"] = 0

        issue_cursor: str | None = issue_cursor_start
        pr_cursor: str | None = pr_cursor_start
        has_issue_page = include_issues
        has_pr_page = include_prs
        total = initial_count

        while has_issue_page or has_pr_page:
            if issue_cursor:
                variables["issueNextPageCursor"] = issue_cursor
            if pr_cursor:
                variables["pullRequestNextPageCursor"] = pr_cursor

            data = GithubImportService._graphql(token, _REPO_QUERY, variables)
            repo_data = data.get("data", {}).get("repository", {})

            page_nodes: list[dict[str, Any]] = []

            issues_page = repo_data.get("issues", {})
            if include_issues and issues_page.get("nodes"):
                for node in issues_page["nodes"]:
                    node["_is_pr"] = False
                    page_nodes.append(node)
                issue_cursor = issues_page["pageInfo"]["endCursor"]
                has_issue_page = issues_page["pageInfo"]["hasNextPage"]
            else:
                has_issue_page = False

            prs_page = repo_data.get("pullRequests", {})
            if include_prs and prs_page.get("nodes"):
                for node in prs_page["nodes"]:
                    node["_is_pr"] = True
                    page_nodes.append(node)
                pr_cursor = prs_page["pageInfo"]["endCursor"]
                has_pr_page = prs_page["pageInfo"]["hasNextPage"]
            else:
                has_pr_page = False

            for node in page_nodes:
                GithubImportService._fetch_extra_comments(token, node)
                record = GithubImportService._node_to_record(node, repo=repo_full)
                if record["body"]:
                    on_record(record)
                    total += 1

            on_checkpoint(issue_cursor, pr_cursor, total)

        return total

    # ------------------------------------------------------------------
    # Background-task entry point (no file upload, returns records only)
    # ------------------------------------------------------------------

    @staticmethod
    def fetch_records(
        *,
        token: str,
        owner: str,
        repo_name: str,
        repo: str,
        include_issues: bool,
        include_prs: bool,
        include_closed: bool,
        loader: str,
        on_progress: OnProgress | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch issue/PR records without uploading. Used by the Celery background task."""
        if loader == "langchain":
            return GithubImportService._fetch_langchain(
                token=token, owner=owner, repo_name=repo_name, repo=repo,
                include_issues=include_issues, include_prs=include_prs,
                include_closed=include_closed, on_progress=on_progress,
            )
        if loader == "llamaindex":
            return GithubImportService._fetch_llamaindex(
                token=token, owner=owner, repo_name=repo_name, repo=repo,
                include_issues=include_issues, include_prs=include_prs,
                include_closed=include_closed, on_progress=on_progress,
            )
        # GraphQL (default)
        issue_cursor_start = (checkpoint or {}).get("issue_cursor")
        pr_cursor_start = (checkpoint or {}).get("pr_cursor")
        nodes = GithubImportService._fetch_all_nodes(
            token=token, owner=owner, repo=repo_name,
            include_issues=include_issues, include_prs=include_prs,
            include_closed=include_closed, on_progress=on_progress,
            issue_cursor_start=issue_cursor_start, pr_cursor_start=pr_cursor_start,
        )
        if not nodes:
            raise RemoteFileUploadError("No issues or pull requests matched the selected filters")
        return [r for n in nodes if (r := GithubImportService._node_to_record(n, repo=repo))["body"]]

    # ------------------------------------------------------------------
    # LangChain loader
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_langchain(
        *,
        token: str,
        owner: str,
        repo_name: str,
        repo: str,
        include_issues: bool,
        include_prs: bool,
        include_closed: bool,
        on_progress: OnProgress | None = None,
    ) -> list[dict[str, Any]]:
        try:
            from langchain_community.document_loaders import GitHubIssuesLoader
        except ImportError as exc:
            raise RemoteFileUploadError("langchain-community is not installed") from exc

        state = "all" if include_closed else "open"
        lc_loader = GitHubIssuesLoader(
            repo=f"{owner}/{repo_name}",
            access_token=token,
            include_prs=include_prs,
            state=state,
        )
        try:
            docs = lc_loader.load()
        except Exception as exc:
            msg = str(exc)
            if "403" in msg or "rate limit" in msg.lower() or "429" in msg:
                raise RemoteFileUploadError(
                    "GitHub rate limit exceeded during LangChain issue fetch. "
                    "Wait for the limit to reset and try again."
                ) from exc
            raise RemoteFileUploadError(f"LangChain GitHub loader failed: {exc}") from exc

        records: list[dict[str, Any]] = []
        for doc in docs:
            meta = doc.metadata
            is_pr = bool(meta.get("pull_request"))
            if is_pr and not include_prs:
                continue
            if not is_pr and not include_issues:
                continue
            number = meta.get("number", 0)
            opening = _nonempty(doc.page_content)
            comment_body = GithubImportService._fetch_comments_rest(
                token=token, owner=owner, repo_name=repo_name, number=number
            )
            body_parts = [p for p in [opening, comment_body] if p]
            if not body_parts:
                continue
            records.append({
                "id": f"github-{repo}#{number}",
                "url": meta.get("url") or "",
                "number": number,
                "title": meta.get("title") or "",
                "type": "pr" if is_pr else "issue",
                "state": (meta.get("state") or "").lower(),
                "author": meta.get("author") or "",
                "date": _nonempty(meta.get("created_at")),
                "body": "\n\n".join(body_parts),
            })
            if on_progress:
                on_progress(len(records), "fetching", None)

        records.sort(key=operator.itemgetter("number"), reverse=True)
        return records

    # ------------------------------------------------------------------
    # LlamaIndex loader
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_llamaindex(
        *,
        token: str,
        owner: str,
        repo_name: str,
        repo: str,
        include_issues: bool,
        include_prs: bool,
        include_closed: bool,
        on_progress: OnProgress | None = None,
    ) -> list[dict[str, Any]]:
        try:
            from llama_index.readers.github import GitHubRepositoryIssuesReader
            from llama_index.readers.github.issues.github_client import GitHubIssuesClient
        except ImportError as exc:
            raise RemoteFileUploadError(
                "llama-index-readers-github is not installed. "
                "Add it to pyproject.toml and rebuild the image."
            ) from exc

        client = GitHubIssuesClient(github_token=token)
        reader = GitHubRepositoryIssuesReader(
            github_client=client,
            owner=owner,
            repo=repo_name,
            verbose=False,
        )
        issue_state = (
            GitHubRepositoryIssuesReader.IssueState.ALL
            if include_closed
            else GitHubRepositoryIssuesReader.IssueState.OPEN
        )
        try:
            docs = reader.load_data(state=issue_state)
        except Exception as exc:
            msg = str(exc)
            if "403" in msg or "rate limit" in msg.lower() or "429" in msg:
                raise RemoteFileUploadError(
                    "GitHub rate limit exceeded during LlamaIndex issue fetch. "
                    "Wait for the limit to reset and try again."
                ) from exc
            raise RemoteFileUploadError(f"LlamaIndex GitHub reader failed: {exc}") from exc

        records: list[dict[str, Any]] = []
        for doc in docs:
            meta = doc.metadata or {}
            # LlamaIndex stores the issue number as doc_id (str), not in metadata
            number = int(doc.doc_id) if doc.doc_id and doc.doc_id.isdigit() else 0
            if number == 0:
                continue
            # LlamaIndex stores HTML URL as "source"; API URL as "url"
            html_url = meta.get("source", meta.get("url", ""))
            is_pr = "/pull/" in html_url
            if is_pr and not include_prs:
                continue
            if not is_pr and not include_issues:
                continue
            # LlamaIndex formats doc.text as "{title}\n{body}"; body may be None → "None"
            text_parts = (doc.text or "").split("\n", 1)
            title = text_parts[0]
            opening = _nonempty(text_parts[1] if len(text_parts) > 1 else "")
            comment_body = GithubImportService._fetch_comments_rest(
                token=token, owner=owner, repo_name=repo_name, number=number
            )
            body_parts = [p for p in [opening, comment_body] if p]
            if not body_parts:
                continue
            records.append({
                "id": f"github-{repo}#{number}",
                "url": html_url,
                "number": number,
                "title": title,
                "type": "pr" if is_pr else "issue",
                "state": (meta.get("state") or "").lower(),
                "author": "",
                "date": _nonempty(meta.get("created_at")),
                "body": "\n\n".join(body_parts),
            })
            if on_progress:
                on_progress(len(records), "fetching", None)

        records.sort(key=operator.itemgetter("number"), reverse=True)
        return records

    # ------------------------------------------------------------------
    # REST helpers (shared by LangChain and LlamaIndex paths)
    # ------------------------------------------------------------------

    @staticmethod
    def _rest_get(client: httpx.Client, url: str, headers: dict, params: dict) -> httpx.Response:
        """GET with automatic rate-limit back-off (primary + secondary) and retry."""
        for attempt in range(1, _MAX_RETRIES + 1):
            resp = client.get(url, headers=headers, params=params)

            if resp.status_code in (403, 429):
                # Secondary rate limit: Retry-After header (seconds)
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    delta = float(retry_after) + 5
                    logger.warning("GitHub secondary rate limit, sleeping %.0fs (Retry-After)", delta)
                    time.sleep(delta)
                    continue

                # Primary rate limit: X-RateLimit-Reset (unix timestamp)
                remaining = resp.headers.get("X-RateLimit-Remaining", "1")
                if remaining == "0":
                    reset_ts = resp.headers.get("X-RateLimit-Reset", "")
                    try:
                        delta = float(reset_ts) - time.time() + 30
                    except Exception:
                        delta = 60
                    logger.warning("GitHub primary rate limit, sleeping %.0fs", delta)
                    time.sleep(max(delta, 0))
                    continue

                # 401 — bad token, no point retrying
                if resp.status_code == 401:
                    raise RemoteFileUploadError("GitHub token is invalid or expired")

            if attempt < _MAX_RETRIES and resp.status_code >= 500:
                time.sleep(3)
                continue

            return resp

        return resp  # return last response after exhausting retries

    @staticmethod
    def _fetch_comments_rest(*, token: str, owner: str, repo_name: str, number: int) -> str:
        url = f"https://api.github.com/repos/{owner}/{repo_name}/issues/{number}/comments"
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        parts: list[str] = []
        page = 1
        with httpx.Client(timeout=30.0) as client:
            while True:
                resp = GithubImportService._rest_get(
                    client, url, headers, {"per_page": 100, "page": page}
                )
                if resp.status_code != 200:
                    logger.warning("REST comments fetch returned %d for #%d", resp.status_code, number)
                    break
                batch = resp.json()
                if not batch:
                    break
                for c in batch:
                    text = (c.get("body") or "").strip()
                    if text:
                        parts.append(text)
                if len(batch) < 100:
                    break
                page += 1
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # GraphQL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"token {token}"}
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=60.0) as client:
                    resp = client.post(
                        _GITHUB_GRAPHQL_ENDPOINT,
                        json={"query": query, "variables": variables},
                        headers=headers,
                    )
                resp.raise_for_status()
                data = resp.json()

                rl = data.get("data", {}).get("rateLimit") or {}
                if int(rl.get("remaining", 1)) <= 1:
                    reset_ts = rl.get("resetAt", "")
                    try:
                        reset_dt = datetime.fromisoformat(reset_ts)
                        delta = (reset_dt - datetime.now(tz=UTC)).total_seconds() + 30
                    except Exception:
                        delta = 60
                    logger.warning("GitHub rate limit hit, sleeping %.0fs", delta)
                    time.sleep(max(delta, 0))
                    return GithubImportService._graphql(token, query, variables)

                errors = data.get("errors")
                if errors:
                    raise RemoteFileUploadError(f"GitHub GraphQL error: {errors[0].get('message', errors)}")

                return data
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 401:
                    raise RemoteFileUploadError("GitHub token is invalid or expired") from exc
                if status in (403, 429):
                    retry_after = exc.response.headers.get("Retry-After")
                    if retry_after:
                        delta = float(retry_after) + 5
                        logger.warning("GitHub secondary rate limit (GraphQL), sleeping %.0fs", delta)
                        time.sleep(delta)
                        continue
                if attempt == _MAX_RETRIES:
                    raise RemoteFileUploadError(f"GitHub API returned {status}") from exc
                time.sleep(3)
            except RemoteFileUploadError:
                raise
            except Exception as exc:
                if attempt == _MAX_RETRIES:
                    raise RemoteFileUploadError(f"GitHub API request failed: {exc}") from exc
                time.sleep(3)
        return {}

    @staticmethod
    def _fetch_all_nodes(
        *,
        token: str,
        owner: str,
        repo: str,
        include_issues: bool,
        include_prs: bool,
        include_closed: bool,
        on_progress: OnProgress | None = None,
        issue_cursor_start: str | None = None,
        pr_cursor_start: str | None = None,
    ) -> list[dict[str, Any]]:
        variables: dict[str, Any] = {"owner": owner, "repo": repo}

        if include_issues:
            variables["issuePerPage"] = _PER_PAGE
            if not include_closed:
                variables["issueStates"] = ["OPEN"]
        else:
            variables["issuePerPage"] = 0

        if include_prs:
            variables["pullRequestPerPage"] = _PER_PAGE
            if not include_closed:
                variables["pullRequestStates"] = ["OPEN"]
        else:
            variables["pullRequestPerPage"] = 0

        issue_cursor: str | None = issue_cursor_start
        pr_cursor: str | None = pr_cursor_start
        # When resuming from a cursor, the fetching for that type is not yet done
        has_issue_page = include_issues
        has_pr_page = include_prs
        all_issues: list[dict] = []
        all_prs: list[dict] = []

        while has_issue_page or has_pr_page:
            if issue_cursor:
                variables["issueNextPageCursor"] = issue_cursor
            if pr_cursor:
                variables["pullRequestNextPageCursor"] = pr_cursor

            data = GithubImportService._graphql(token, _REPO_QUERY, variables)
            repo_data = data.get("data", {}).get("repository", {})

            issues_page = repo_data.get("issues", {})
            if include_issues and issues_page.get("nodes"):
                all_issues.extend(issues_page["nodes"])
                issue_cursor = issues_page["pageInfo"]["endCursor"]
                has_issue_page = issues_page["pageInfo"]["hasNextPage"]
            else:
                has_issue_page = False

            prs_page = repo_data.get("pullRequests", {})
            if include_prs and prs_page.get("nodes"):
                all_prs.extend(prs_page["nodes"])
                pr_cursor = prs_page["pageInfo"]["endCursor"]
                has_pr_page = prs_page["pageInfo"]["hasNextPage"]
            else:
                has_pr_page = False

            total_so_far = len(all_issues) + len(all_prs)
            logger.info(
                "Fetched GitHub page: issues=%d prs=%d has_more_issues=%s has_more_prs=%s",
                len(all_issues), len(all_prs), has_issue_page, has_pr_page,
            )
            if on_progress:
                on_progress(
                    total_so_far,
                    "fetching",
                    {"issue_cursor": issue_cursor, "pr_cursor": pr_cursor},
                )

        for i, node in enumerate(all_issues + all_prs):
            GithubImportService._fetch_extra_comments(token, node)
            if on_progress and (i + 1) % 10 == 0:
                on_progress(len(all_issues) + len(all_prs), "fetching_comments", None)

        for node in all_issues:
            node["_is_pr"] = False
        for node in all_prs:
            node["_is_pr"] = True

        return sorted(all_issues + all_prs, key=operator.itemgetter("number"), reverse=True)

    @staticmethod
    def _fetch_extra_comments(token: str, node: dict[str, Any]) -> None:
        comments = node.get("comments", {})
        if not comments.get("pageInfo", {}).get("hasNextPage"):
            return

        cursor = comments["pageInfo"]["endCursor"]
        while True:
            data = GithubImportService._graphql(token, _NODE_COMMENT_QUERY, {
                "id": node["id"],
                "perPage": _PER_PAGE,
                "commentCursor": cursor,
            })
            page = data.get("data", {}).get("node", {}).get("comments", {})
            if not page.get("nodes"):
                break
            node["comments"]["nodes"].extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]

    @staticmethod
    def _node_to_record(node: dict[str, Any], *, repo: str) -> dict[str, Any]:
        is_pr = node.get("_is_pr", False)
        author = node.get("author") or {}

        parts: list[str] = []
        opening = (node.get("body") or "").strip()
        if opening:
            parts.append(opening)
        for comment in node.get("comments", {}).get("nodes", []):
            text = (comment.get("body") or "").strip()
            if text:
                parts.append(text)

        return {
            "id": f"github-{repo}#{node['number']}",
            "url": node.get("url", ""),
            "number": node["number"],
            "title": node.get("title", ""),
            "type": "pr" if is_pr else "issue",
            "state": node.get("state", "").lower(),
            "author": author.get("login", ""),
            "date": node.get("createdAt", ""),
            "body": "\n\n".join(parts),
        }
