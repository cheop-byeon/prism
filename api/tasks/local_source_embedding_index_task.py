"""Runs LocalSourceIndexService.build_embedding_index in a Celery worker,
decoupled from the requesting page's own connection -- unlike serve_ui.py's
_sse_stream (a plain background thread tied to one Flask process), a
Celery task survives the requesting tab closing, the request's own worker
process being recycled, or a plain server restart, because the work item
itself lives in Redis and the worker consuming it is a separate,
long-running process from the one that enqueued it.

Progress is written to JobStatusService rather than streamed back over
this same request -- there is no request by the time this runs, since the
route that enqueues this task returns immediately. The frontend polls the
matching status route instead of holding a connection open.

Registered with the real Dify worker via extensions/ext_celery.py's
`imports` list -- without that, this module would only ever be imported by
serve_ui.py itself (to call .delay()), and the actual `worker` container
would never import it, so it would never learn how to run this task.
"""

import logging

from celery import shared_task

from services.job_status_service import JobStatusService
from services.local_source_index_service import LocalSourceIndexService

logger = logging.getLogger(__name__)


@shared_task(queue="dataset")
def local_source_embedding_index_task(
    job_key: str, draft_name: str, wg: str | None, rfc_number: str | None, target: str = "content"
) -> None:
    JobStatusService.set_status(job_key, "running", message="Starting…")

    def on_event(event: dict) -> None:
        JobStatusService.set_status(
            job_key,
            "running",
            current=event.get("current", 0),
            total=event.get("total", 0),
            message=event.get("message", ""),
        )

    try:
        if target == "mail":
            result = LocalSourceIndexService.build_mail_embedding_index(wg, on_event=on_event)
        else:
            result = LocalSourceIndexService.build_content_embedding_index(draft_name, rfc_number, on_event=on_event)
    except Exception as exc:
        logger.exception("local_source_embedding_index_task failed for draft %s", draft_name)
        JobStatusService.set_status(job_key, "error", message=f"{type(exc).__name__}: {exc}")
        return

    if result.get("error"):
        JobStatusService.set_status(job_key, "error", message=result["error"])
        return

    total = result.get("total_documents", 0)
    JobStatusService.set_status(
        job_key, "completed", current=total, total=total, message=f"Embedded {total} documents."
    )
