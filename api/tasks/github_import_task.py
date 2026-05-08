"""Celery task for background GitHub repository import with progress tracking and resume."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from typing import Any

import click
from celery import shared_task

from extensions.ext_database import db
from extensions.ext_redis import redis_client

logger = logging.getLogger(__name__)

_JOB_TTL_SECONDS = 48 * 3600  # 48 hours
_TMP_DIR = tempfile.gettempdir()


def _redis_key(job_id: str) -> str:
    return f"github_import:{job_id}"


def _tmp_path(job_id: str) -> str:
    return os.path.join(_TMP_DIR, f"github_import_{job_id}.jsonl")


def get_job_state(job_id: str) -> dict[str, Any]:
    """Return job state dict from Redis, or {} if the key does not exist."""
    raw = redis_client.get(_redis_key(job_id))
    if not raw:
        return {}
    return json.loads(raw)  # type: ignore[no-any-return]


def _save_state(job_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    redis_client.setex(_redis_key(job_id), _JOB_TTL_SECONDS, json.dumps(state))


def create_pending_job(
    job_id: str,
    *,
    repo: str,
    loader: str,
    include_issues: bool = True,
    include_prs: bool = True,
    include_closed: bool = True,
    initial_fetched: int = 0,
) -> None:
    """Write the initial pending record before dispatching the Celery task."""
    _save_state(job_id, {
        "job_id": job_id,
        "status": "pending",
        "repo": repo,
        "loader": loader,
        "include_issues": include_issues,
        "include_prs": include_prs,
        "include_closed": include_closed,
        "fetched": initial_fetched,
        "phase": "pending",
        "elapsed_seconds": 0,
        "started_at": datetime.now(UTC).isoformat(),
        "resumable": False,
        "checkpoint": None,
        "file": None,
        "error": None,
    })


@shared_task(queue="dataset", acks_late=True, reject_on_worker_lost=True)
def github_import_task(
    *,
    job_id: str,
    repo: str,
    include_issues: bool,
    include_prs: bool,
    include_closed: bool,
    github_token: str,
    loader: str,
    account_id: str,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    """
    Import GitHub issues/PRs as a JSONL dataset file.

    For the GraphQL loader, records are written to a temp file after each page so that a
    resume can pick up exactly where it left off — both the progress count and the
    already-fetched records are preserved.

    Progress is stored in Redis under ``github_import:{job_id}``.
    """
    from controllers.common.errors import RemoteFileUploadError
    from core.db.session_factory import session_factory
    from dify_graph.file import helpers as file_helpers
    from fields.file_fields import FileWithSignedUrl
    from models import Account, Tenant
    from services.file_service import FileService
    from services.github_import_service import GithubImportService

    state = get_job_state(job_id)
    if not state:
        logger.warning("github_import_task: job_id=%s missing from Redis, aborting", job_id)
        return

    started_at_str: str = state.get("started_at", datetime.now(UTC).isoformat())
    started_at = datetime.fromisoformat(started_at_str)

    def _update(**kwargs: Any) -> None:
        current = get_job_state(job_id) or {}
        current.update(kwargs)
        _save_state(job_id, current)

    _update(status="running", phase="fetching")

    try:
        owner, repo_name = repo.strip().split("/", 1)
    except ValueError:
        _update(status="error", error="Repository must be in 'owner/repo' format")
        return

    # ------------------------------------------------------------------
    # GraphQL path: stream records page-by-page to a temp file so resume
    # starts with the correct count and includes all previously-saved records.
    # ------------------------------------------------------------------
    if loader == "graphql":
        # Determine the temp file to use.  On resume, reuse the previous job's
        # temp file if it still exists; otherwise start fresh.
        prev_tmp = (checkpoint or {}).get("tmp_path", "")
        if checkpoint and prev_tmp and os.path.exists(prev_tmp):
            cur_tmp = prev_tmp
            initial_count = checkpoint.get("fetched", 0)
        else:
            cur_tmp = _tmp_path(job_id)
            initial_count = 0

        issue_cursor_start = (checkpoint or {}).get("issue_cursor")
        pr_cursor_start = (checkpoint or {}).get("pr_cursor")

        def on_record(record: dict[str, Any]) -> None:
            with open(cur_tmp, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        def on_checkpoint_cb(
            issue_cursor: str | None,
            pr_cursor: str | None,
            total: int,
        ) -> None:
            elapsed = int((datetime.now(UTC) - started_at).total_seconds())
            _update(
                fetched=total,
                phase="fetching",
                elapsed_seconds=elapsed,
                checkpoint={
                    "issue_cursor": issue_cursor,
                    "pr_cursor": pr_cursor,
                    "tmp_path": cur_tmp,
                    "fetched": total,
                },
            )

        try:
            GithubImportService.stream_graphql_records(
                token=github_token,
                owner=owner,
                repo=repo_name,
                repo_full=repo,
                include_issues=include_issues,
                include_prs=include_prs,
                include_closed=include_closed,
                on_record=on_record,
                on_checkpoint=on_checkpoint_cb,
                issue_cursor_start=issue_cursor_start,
                pr_cursor_start=pr_cursor_start,
                initial_count=initial_count,
            )
        except RemoteFileUploadError as exc:
            current = get_job_state(job_id)
            has_checkpoint = bool((current or {}).get("checkpoint"))
            _update(
                status="error",
                error=str(exc),
                resumable=has_checkpoint,
            )
            logger.exception("github_import_task graphql fetch failed: job_id=%s", job_id)
            return
        except Exception as exc:
            current = get_job_state(job_id)
            has_checkpoint = bool((current or {}).get("checkpoint"))
            _update(status="error", error=f"Unexpected error: {exc}", resumable=has_checkpoint)
            logger.exception("github_import_task unexpected error: job_id=%s", job_id)
            return

        # Read all accumulated records from the temp file
        records: list[dict[str, Any]] = []
        if os.path.exists(cur_tmp):
            with open(cur_tmp, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

    # ------------------------------------------------------------------
    # LangChain / LlamaIndex paths: fetch all at once (no streaming resume)
    # ------------------------------------------------------------------
    else:
        def on_progress(fetched: int, phase: str = "fetching", cp: dict | None = None) -> None:
            elapsed = int((datetime.now(UTC) - started_at).total_seconds())
            _update(fetched=fetched, phase=phase, elapsed_seconds=elapsed)

        try:
            records = GithubImportService.fetch_records(
                token=github_token,
                owner=owner,
                repo_name=repo_name,
                repo=repo,
                include_issues=include_issues,
                include_prs=include_prs,
                include_closed=include_closed,
                loader=loader,
                on_progress=on_progress,
            )
        except RemoteFileUploadError as exc:
            _update(status="error", error=str(exc), resumable=False)
            logger.exception("github_import_task fetch failed: job_id=%s", job_id)
            return
        except Exception as exc:
            _update(status="error", error=f"Unexpected error: {exc}", resumable=False)
            logger.exception("github_import_task unexpected error: job_id=%s", job_id)
            return
        cur_tmp = None

    if not records:
        _update(status="error", error="No issues or pull requests matched the selected filters")
        return

    _update(phase="uploading", fetched=len(records))

    try:
        content = (
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
        ).encode("utf-8")
        safe_repo = repo.replace("/", "-")
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
        filename = f"github-{safe_repo}-{loader}-{timestamp}.jsonl"

        with session_factory.create_session() as session:
            from sqlalchemy import select as sa_select

            from models.account import TenantAccountJoin

            account = session.query(Account).where(Account.id == account_id).first()
            if not account:
                raise ValueError(f"Account not found: {account_id}")
            row = session.execute(
                sa_select(Tenant, TenantAccountJoin)
                .where(TenantAccountJoin.account_id == account_id)
                .where(TenantAccountJoin.current.is_(True))
                .where(Tenant.id == TenantAccountJoin.tenant_id)
            ).first()
            if not row:
                raise ValueError(f"No current tenant for account {account_id}")
            tenant, join = row
            account.role = join.role
            account.current_tenant = tenant

            upload_file = FileService(db.engine).upload_file(
                filename=filename,
                content=content,
                mimetype="application/x-ndjson",
                user=account,
                source="datasets",
                source_url=f"github://{repo}",
            )
            file_data = FileWithSignedUrl(
                id=upload_file.id,
                name=upload_file.name,
                size=upload_file.size,
                extension=upload_file.extension,
                url=file_helpers.get_signed_file_url(upload_file_id=upload_file.id),
                mime_type=upload_file.mime_type,
                created_by=upload_file.created_by,
                created_at=int(upload_file.created_at.timestamp()),
            ).model_dump(mode="json")

        elapsed = int((datetime.now(UTC) - started_at).total_seconds())
        _update(
            status="done",
            phase="done",
            file=file_data,
            checkpoint=None,
            fetched=len(records),
            elapsed_seconds=elapsed,
        )
        logger.info(
            click.style(
                f"github_import_task done: job_id={job_id} repo={repo}"
                f" records={len(records)} elapsed={elapsed}s",
                fg="green",
            )
        )

    except Exception as exc:
        _update(status="error", error=f"File upload failed: {exc}")
        logger.exception("github_import_task upload failed: job_id=%s", job_id)

    finally:
        if cur_tmp and os.path.exists(cur_tmp):
            try:
                os.remove(cur_tmp)
            except OSError:
                pass
