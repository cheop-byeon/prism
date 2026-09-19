"""Disk-backed status tracking for background jobs that must survive the
requesting page/tab closing -- e.g. a Celery task's progress, polled by the
frontend on an interval instead of over a connection tied to the browser
(unlike _sse_stream in serve_ui.py, which dies the moment the tab closes,
even though the work itself would keep running either way).

A flat JSON file per job, matching the same disk-cache convention
LocalSourceIndexService/RfcCorpusIndexService already use for their own
index artifacts, rather than a new Postgres model -- this project's
local-source system has never used the database for its own state, and a
job's status is exactly the same kind of small, locally-rebuildable data
those indices already are.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal, TypedDict

_JOBS_DIR = Path("/app/api/storage/ietf-downloads/jobs")

JobStatus = Literal["queued", "running", "completed", "error"]


class JobStatusPayload(TypedDict):
    status: JobStatus
    current: int
    total: int
    message: str
    updated_at: float


class JobStatusService:
    @staticmethod
    def _path(job_key: str) -> Path:
        return _JOBS_DIR / f"{job_key}.json"

    @staticmethod
    def set_status(job_key: str, status: JobStatus, current: int = 0, total: int = 0, message: str = "") -> None:
        _JOBS_DIR.mkdir(parents=True, exist_ok=True)
        payload: JobStatusPayload = {
            "status": status,
            "current": current,
            "total": total,
            "message": message,
            "updated_at": time.time(),
        }
        # Same-directory rename is atomic on a local filesystem -- a poller
        # reading mid-write would otherwise risk a truncated/partial JSON
        # file, since this is written many times over a long-running job
        # (once per progress event), not just once at the end.
        tmp_path = JobStatusService._path(job_key).with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload))
        tmp_path.replace(JobStatusService._path(job_key))

    @staticmethod
    def get_status(job_key: str) -> JobStatusPayload | None:
        path = JobStatusService._path(job_key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
