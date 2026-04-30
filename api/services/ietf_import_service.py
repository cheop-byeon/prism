"""Service helpers for importing IETF mailarchive and RFC data into dataset uploads.

This module is the integration boundary between the Flask console API and the
external Python 3.13-only `ietfdata` sidecar. The sidecar performs data access
against the IETF mailarchive and RFC index, while this service is responsible
for:

- calling the sidecar with validated import parameters
- decoding the returned dataset file payload
- storing the generated file through the normal `FileService` upload path
- returning the same `FileWithSignedUrl` shape used by other import flows

The Dify API intentionally does not import the `ietfdata` package directly,
because the current backend runtime is Python 3.11/3.12 while `ietfdata`
declares Python 3.13+.
"""

from __future__ import annotations

import base64
import email as email_lib
import email.header as email_header_lib
import json
import logging
import re
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

import httpx
from pydantic import BaseModel, ConfigDict, Field
from yarl import URL

import services
from configs import dify_config
from controllers.common.errors import FileTooLargeError, RemoteFileUploadError, UnsupportedFileTypeError
from dify_graph.file import helpers as file_helpers
from extensions.ext_database import db
from fields.file_fields import FileWithSignedUrl
from libs.login import current_account_with_tenant
from services.file_service import FileService

logger = logging.getLogger(__name__)

ietfdata_service_url = URL(str(dify_config.IETFDATA_SERVICE_URL))


class IetfMailarchiveImportPayload(TypedDict):
    mailing_list: str
    sent_after: str
    sent_before: str
    from_addr: str | None
    subject: str | None
    search_in: list[str]
    offset: int
    limit: int
    output_format: Literal["mbox_text", "jsonl"]
    fetch_all: bool


class IetfRfcImportPayload(TypedDict):
    rfc_ids: list[str]
    since: str | None
    until: str | None
    stream: str | None
    wg: str | None
    status: str | None
    limit: int
    format: str


class IetfDraftImportPayload(TypedDict):
    draft_names: list[str]
    format: str


class IetfFormatInfo(TypedDict):
    key: str
    label: str
    mime_type: str


class IetfRfcSuggestionRecord(TypedDict, total=False):
    doc_id: str
    title: str
    draft_names: list[str]
    since: str | None
    stream: str | None
    wg: str | None
    area: str | None
    status: str | None
    draft_base_name: str | None
    draft_max_version: int | None
    draft_versions: list[dict[str, str | int | None]]


class IetfPreprocessedEmailRecord(TypedDict):
    id: str
    url: str
    subject: str
    body: str
    date: str  # RFC 2822 date string from the email Date header, may be empty


class IetfSidecarFileResponse(BaseModel):
    """Validated file payload returned by the IETF sidecar service."""

    filename: str = Field(..., min_length=1)
    mime_type: str = Field(..., min_length=1)
    content_base64: str = Field(..., min_length=1)
    message_count: int | None = Field(default=None, ge=0)

    model_config = ConfigDict(extra="forbid")


class IetfImportService:
    """Coordinate sidecar extraction and upload the returned content as a dataset file."""

    @staticmethod
    def import_mailarchive(payload: IetfMailarchiveImportPayload) -> tuple[dict, int]:
        """Import selected IETF mailarchive messages as a generated dataset file."""

        sidecar_payload = IetfImportService._request_sidecar("/api/v1/import/mailarchive", payload)
        source_url = (
            f"ietfdata://mailarchive/{payload['mailing_list']}?"
            f"sent_after={payload['sent_after']}&sent_before={payload['sent_before']}"
        )
        return IetfImportService._upload_sidecar_payload(sidecar_payload, source_url=source_url)

    @staticmethod
    def import_mailarchive_as_mbox(payload: IetfMailarchiveImportPayload) -> tuple[dict, int]:
        """Import entire IETF mailarchive as RFC4155 mbox format (no partitioning)."""

        sidecar_payload = IetfImportService._request_sidecar("/api/v1/import/mailarchive/mbox", payload)
        source_url = (
            f"ietfdata://mailarchive/{payload['mailing_list']}?"
            f"sent_after={payload['sent_after']}&sent_before={payload['sent_before']}&format=mbox"
        )
        return IetfImportService._upload_sidecar_payload(sidecar_payload, source_url=source_url)

    @staticmethod
    def download_mailarchive_as_mbox_to_local(payload: IetfMailarchiveImportPayload) -> tuple[dict, int]:
        """Download mbox export into local mounted storage and return local file metadata."""

        sidecar_payload = IetfImportService._request_sidecar("/api/v1/import/mailarchive/mbox", payload)
        try:
            content = base64.b64decode(sidecar_payload.content_base64)
        except ValueError as exc:
            raise RemoteFileUploadError("IETF data service returned invalid file content") from exc

        local_dir = Path("/app/api/storage/ietf-downloads")
        local_dir.mkdir(parents=True, exist_ok=True)

        filename = sidecar_payload.filename
        destination = local_dir / filename
        if destination.exists():
            stem = destination.stem
            suffix = destination.suffix
            destination = local_dir / f"{stem}-dup{suffix}"

        destination.write_bytes(content)

        message_count = sidecar_payload.message_count
        if message_count is None:
            message_count = IetfImportService._count_mbox_messages(content)

        relative_hint = destination.as_posix().replace("/app/api/storage/", "docker/volumes/app/storage/")
        return (
            {
                "saved_path": destination.as_posix(),
                "host_path_hint": relative_hint,
                "filename": destination.name,
                "size": destination.stat().st_size,
                "message_count": message_count,
            },
            201,
        )

    @staticmethod
    def import_rfcs(payload: IetfRfcImportPayload) -> tuple[dict, int]:
        """Import selected RFC documents as a markdown dataset file."""

        sidecar_payload = IetfImportService._request_sidecar("/api/v1/import/rfcs", payload)
        if payload["rfc_ids"]:
            source_hint = ",".join(payload["rfc_ids"])
        else:
            source_hint = "filtered-query"
        source_url = f"ietfdata://rfc/{source_hint}"
        return IetfImportService._upload_sidecar_payload(sidecar_payload, source_url=source_url)

    @staticmethod
    def import_drafts(payload: IetfDraftImportPayload) -> tuple[dict, int]:
        """Import IETF internet-draft documents by full draft name."""

        sidecar_payload = IetfImportService._request_sidecar("/api/v1/import/drafts", dict(payload))
        source_hint = ",".join(payload["draft_names"]) if payload["draft_names"] else "drafts"
        source_url = f"ietfdata://draft/{source_hint}"
        return IetfImportService._upload_sidecar_payload(sidecar_payload, source_url=source_url)

    @staticmethod
    def get_rfc_formats(*, rfc_id: str) -> dict:
        """Return available download formats for a single RFC from the sidecar."""
        url = str(ietfdata_service_url / "api/v1/rfcs/formats")
        try:
            with httpx.Client(timeout=dify_config.IETFDATA_SERVICE_TIMEOUT) as client:
                response = client.post(url, json={"rfc_ids": [rfc_id]})
        except httpx.RequestError as exc:
            logger.warning("ietfdata sidecar request failed for %s: %s", url, exc)
            raise RemoteFileUploadError(f"Failed to contact the IETF data service: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            logger.warning("ietfdata sidecar returned %s for %s: %s", response.status_code, url, response.text)
            raise RemoteFileUploadError(f"IETF data service error: {response.text}")

        data = response.json()
        entries = data.get("entries", [])
        formats: list[IetfFormatInfo] = []
        if entries:
            for fmt in entries[0].get("formats", []):
                formats.append({
                    "key": str(fmt.get("key", "")),
                    "label": str(fmt.get("label", "")),
                    "mime_type": str(fmt.get("mime_type", "")),
                })
        return {"rfc_id": rfc_id, "formats": formats}, 200

    @staticmethod
    def get_draft_formats(*, draft_name: str) -> dict:
        """Return available download formats for a single Internet-Draft from the sidecar."""
        url = str(ietfdata_service_url / "api/v1/drafts/formats")
        try:
            with httpx.Client(timeout=dify_config.IETFDATA_SERVICE_TIMEOUT) as client:
                response = client.post(url, json={"draft_names": [draft_name]})
        except httpx.RequestError as exc:
            logger.warning("ietfdata sidecar request failed for %s: %s", url, exc)
            raise RemoteFileUploadError(f"Failed to contact the IETF data service: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            logger.warning("ietfdata sidecar returned %s for %s: %s", response.status_code, url, response.text)
            raise RemoteFileUploadError(f"IETF data service error: {response.text}")

        data = response.json()
        entries = data.get("entries", [])
        formats: list[IetfFormatInfo] = []
        if entries:
            for fmt in entries[0].get("formats", []):
                formats.append({
                    "key": str(fmt.get("key", "")),
                    "label": str(fmt.get("label", "")),
                    "mime_type": str(fmt.get("mime_type", "")),
                })
        return {"draft_name": draft_name, "formats": formats}, 200

    @staticmethod
    def get_mailarchive_sync_status(list_name: str) -> dict:
        """Return sync status for a single mailing list from the sidecar."""
        url = str(ietfdata_service_url / f"api/v1/mailarchive/{list_name}/status")
        try:
            with httpx.Client(timeout=dify_config.IETFDATA_SERVICE_TIMEOUT) as client:
                response = client.get(url)
        except httpx.RequestError as exc:
            logger.warning("ietfdata sidecar request failed for %s: %s", url, exc)
            raise RemoteFileUploadError(f"Failed to contact the IETF data service: {exc}") from exc

        if response.status_code == 404:
            return {"name": list_name, "status": "not_found", "message_count": 0}
        if response.status_code != httpx.codes.OK:
            logger.warning("ietfdata sidecar returned %s for %s: %s", response.status_code, url, response.text)
            raise RemoteFileUploadError(f"IETF data service error: {response.text}")

        return response.json()

    @staticmethod
    def trigger_mailarchive_sync(list_name: str) -> dict:
        """Trigger a background IMAP sync for a mailing list via the sidecar."""
        url = str(ietfdata_service_url / f"api/v1/mailarchive/{list_name}/sync")
        try:
            with httpx.Client(timeout=dify_config.IETFDATA_SERVICE_TIMEOUT) as client:
                response = client.post(url)
        except httpx.RequestError as exc:
            logger.warning("ietfdata sidecar request failed for %s: %s", url, exc)
            raise RemoteFileUploadError(f"Failed to contact the IETF data service: {exc}") from exc

        if response.status_code == 404:
            raise RemoteFileUploadError(f"Unknown mailing list: {list_name}")
        if response.status_code != httpx.codes.OK:
            logger.warning("ietfdata sidecar returned %s for %s: %s", response.status_code, url, response.text)
            raise RemoteFileUploadError(f"IETF data service error: {response.text}")

        return response.json()

    @staticmethod
    def get_mailing_lists() -> list[str]:
        """Fetch the list of available IETF mailing lists from the sidecar."""

        url = str(ietfdata_service_url / "api/v1/mailarchive/lists")
        try:
            with httpx.Client(timeout=dify_config.IETFDATA_SERVICE_TIMEOUT) as client:
                response = client.get(url)
        except httpx.RequestError as exc:
            logger.warning("ietfdata sidecar request failed for %s: %s", url, exc)
            raise RemoteFileUploadError(f"Failed to contact the IETF data service: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            logger.warning("ietfdata sidecar returned %s for %s: %s", response.status_code, url, response.text)
            raise RemoteFileUploadError(f"IETF data service error: {response.text}")

        try:
            data = response.json()
            lists = data.get("lists", [])
            if not isinstance(lists, list):
                raise ValueError("'lists' must be an array")
            return [str(item) for item in lists]
        except Exception as exc:
            logger.warning("ietfdata sidecar returned invalid payload for %s: %s", url, exc)
            raise RemoteFileUploadError("IETF data service returned an invalid response") from exc

    @staticmethod
    def get_rfc_suggestions(*, query: str, limit: int) -> list[IetfRfcSuggestionRecord]:
        """Fetch RFC suggestions with related draft names from the sidecar."""

        url = str(ietfdata_service_url / "api/v1/rfcs/suggestions")
        try:
            with httpx.Client(timeout=dify_config.IETFDATA_SERVICE_TIMEOUT) as client:
                response = client.get(url, params={"query": query, "limit": limit})
        except httpx.RequestError as exc:
            logger.warning("ietfdata sidecar request failed for %s: %s", url, exc)
            raise RemoteFileUploadError(f"Failed to contact the IETF data service: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            logger.warning("ietfdata sidecar returned %s for %s: %s", response.status_code, url, response.text)
            raise RemoteFileUploadError(f"IETF data service error: {response.text}")

        try:
            data = response.json()
            suggestions = data.get("suggestions", [])
            if not isinstance(suggestions, list):
                raise ValueError("'suggestions' must be an array")

            parsed: list[IetfRfcSuggestionRecord] = []
            for item in suggestions:
                if not isinstance(item, dict):
                    continue
                doc_id = str(item.get("doc_id", "")).strip()
                if not doc_id:
                    continue
                title = str(item.get("title", "")).strip()
                raw_drafts = item.get("draft_names", [])
                draft_names = (
                    [str(d).strip() for d in raw_drafts if str(d).strip()]
                    if isinstance(raw_drafts, list)
                    else []
                )
                record: IetfRfcSuggestionRecord = {
                    "doc_id": doc_id,
                    "title": title,
                    "draft_names": draft_names,
                    "since": item.get("since"),
                    "stream": item.get("stream"),
                    "wg": item.get("wg"),
                    "area": item.get("area"),
                    "status": item.get("status"),
                    "draft_base_name": item.get("draft_base_name"),
                    "draft_max_version": item.get("draft_max_version"),
                    "draft_versions": (
                        item.get("draft_versions") if isinstance(item.get("draft_versions"), list) else []
                    ),
                }
                parsed.append(record)

            return parsed
        except Exception as exc:
            logger.warning("ietfdata sidecar returned invalid payload for %s: %s", url, exc)
            raise RemoteFileUploadError("IETF data service returned an invalid response") from exc

    @staticmethod
    def _request_sidecar(path: str, payload: dict) -> IetfSidecarFileResponse:
        """Call the sidecar and validate its file payload response."""

        url = str(ietfdata_service_url / path.lstrip("/"))
        try:
            with httpx.Client(timeout=dify_config.IETFDATA_SERVICE_TIMEOUT) as client:
                response = client.post(url, json=payload)
        except httpx.RequestError as exc:
            logger.warning("ietfdata sidecar request failed for %s: %s", url, exc)
            raise RemoteFileUploadError(f"Failed to contact the IETF data service: {exc}") from exc

        if response.status_code == 503:
            # Mailing list not yet synced — propagate the user-friendly message directly.
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise RemoteFileUploadError(detail)
        if response.status_code != httpx.codes.OK:
            logger.warning("ietfdata sidecar returned %s for %s: %s", response.status_code, url, response.text)
            raise RemoteFileUploadError(f"IETF data service error: {response.text}")

        try:
            return IetfSidecarFileResponse.model_validate(response.json())
        except Exception as exc:
            logger.warning("ietfdata sidecar returned invalid payload for %s: %s", url, exc)
            raise RemoteFileUploadError("IETF data service returned an invalid response") from exc

    @staticmethod
    def _upload_sidecar_payload(sidecar_payload: IetfSidecarFileResponse, *, source_url: str) -> tuple[dict, int]:
        """Decode the sidecar file payload and persist it through the normal upload service."""

        try:
            content = base64.b64decode(sidecar_payload.content_base64)
        except ValueError as exc:
            raise RemoteFileUploadError("IETF data service returned invalid file content") from exc

        try:
            user, _ = current_account_with_tenant()
            upload_file = FileService(db.engine).upload_file(
                filename=sidecar_payload.filename,
                content=content,
                mimetype=sidecar_payload.mime_type,
                user=user,
                source="datasets",
                source_url=source_url,
            )
        except services.errors.file.FileTooLargeError as file_too_large_error:
            raise FileTooLargeError(file_too_large_error.description)
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

    @staticmethod
    def _count_mbox_messages(content: bytes) -> int:
        text = content.decode("utf-8", errors="replace")
        return sum(1 for line in text.splitlines() if line.startswith("From "))

    # ------------------------------------------------------------------
    # Preprocessing (extract clean records from a local mbox file)
    # ------------------------------------------------------------------

    @staticmethod
    def preprocess_mbox_file(saved_path: str) -> dict:
        """Parse a local mbox file and extract clean {url, subject, body} records.

        Returns a dict with keys:
            emails            – list of accepted {id, url, subject, body} records
            total_emails      – number of accepted emails
            discarded_emails  – number discarded (no Archived-At, or non-natural body)
            preview_lines     – top 10 body lines of the first accepted email
        """
        path = Path(saved_path)
        if not path.exists():
            raise FileNotFoundError(f"mbox file not found: {saved_path}")

        content = Path(path).read_bytes()

        # Split on RFC 4155 "From " separator lines
        raw_parts = re.split(rb"(?m)^From ", content)
        # Skip any leading comment block (lines starting with #)
        raw_parts = [p for p in raw_parts if p and not p.startswith(b"# ")]

        accepted: list[dict] = []
        discarded = 0

        for raw in raw_parts:
            try:
                msg = email_lib.message_from_bytes(b"From " + raw)
            except Exception:
                discarded += 1
                continue

            # ---- URL / ID from Archived-At ----
            archived_at = msg.get("Archived-At", "").strip()
            url = archived_at.strip("<>").strip()
            if not url:
                discarded += 1
                continue
            message_id = url.rstrip("/").split("/")[-1]

            # ---- Subject (RFC 2047 decoded) ----
            subject = IetfImportService._decode_header(msg.get("Subject", ""))

            # ---- Date ----
            date = IetfImportService._decode_header(msg.get("Date", ""))

            # ---- Body (text/plain only) ----
            body = IetfImportService._extract_text_body(msg)
            if body is None or not IetfImportService._is_natural_text(body):
                discarded += 1
                continue

            accepted.append({"id": message_id, "url": url, "subject": subject, "body": body, "date": date})

        preview_lines: list[str] = []
        if accepted:
            preview_lines = accepted[0]["body"].splitlines()[:10]

        return {
            "emails": accepted,
            "total_emails": len(accepted),
            "discarded_emails": discarded,
            "preview_lines": preview_lines,
        }

    @staticmethod
    def download_and_preprocess_years(
        mailing_list: str,
        sent_after: str,
        sent_before: str,
        from_addr: str | None = None,
        subject: str | None = None,
        search_in: list[str] | None = None,
    ) -> tuple[dict, int]:
        """Download one mbox per calendar year in [sent_after, sent_before] and preprocess each.

        Returns a list of per-year result dicts, each containing:
            year, saved_path, host_path_hint, processed_path, processed_host_path_hint,
            total_emails, discarded_emails, preview_lines
        """
        try:
            start_year = datetime.fromisoformat(sent_after).year
            end_year = datetime.fromisoformat(sent_before).year
        except ValueError:
            start_year = 1970
            end_year = datetime.now(tz=UTC).year

        def _process_year(year: int) -> dict:
            year_after = sent_after if year == start_year else f"{year}-01-01T00:00:00"
            year_before = sent_before if year == end_year else f"{year}-12-31T23:59:59"
            payload: IetfMailarchiveImportPayload = {
                "mailing_list": mailing_list,
                "sent_after": year_after,
                "sent_before": year_before,
                "from_addr": from_addr,
                "subject": subject,
                "search_in": search_in if search_in is not None else ["subject"],
                "offset": 0,
                "limit": 100,
                "output_format": "mbox_text",
                "fetch_all": False,
            }
            try:
                download_result, _ = IetfImportService.download_mailarchive_as_mbox_to_local(payload)
            except RemoteFileUploadError as exc:
                if "No messages matched" in str(exc):
                    return {
                        "year": year,
                        "saved_path": None,
                        "host_path_hint": None,
                        "processed_path": None,
                        "processed_host_path_hint": None,
                        "total_emails": 0,
                        "discarded_emails": 0,
                        "preview_lines": [],
                    }
                raise
            preprocess_result = IetfImportService.preprocess_mbox_file(download_result["saved_path"])
            processed_path, processed_host_path_hint = IetfImportService._write_preprocessed_email_records(
                source_path=download_result["saved_path"],
                emails=preprocess_result["emails"],
            )
            return {
                "year": year,
                "saved_path": download_result["saved_path"],
                "host_path_hint": download_result["host_path_hint"],
                "processed_path": processed_path,
                "processed_host_path_hint": processed_host_path_hint,
                "total_emails": preprocess_result["total_emails"],
                "discarded_emails": preprocess_result["discarded_emails"],
                "preview_lines": preprocess_result["preview_lines"],
            }

        years = list(range(start_year, end_year + 1))
        max_workers = min(len(years), 8)
        year_result_map: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_year = {executor.submit(_process_year, y): y for y in years}
            for future in as_completed(future_to_year):
                result = future.result()  # re-raises any exception from the worker
                year_result_map[result["year"]] = result

        year_results = [year_result_map[y] for y in sorted(year_result_map)]
        return {"years": year_results}, 200

    @staticmethod
    def upload_preprocessed_file(processed_path: str) -> tuple[dict, int]:
        """Upload a preprocessed local file as a normal dataset upload file.

        For JSONL input, each line must be an object containing id/url/subject/body.
        Downstream extraction keeps each line as a separate document so email body
        content remains chunkable while message metadata stays out of the text.
        """

        path = Path(processed_path)
        if not path.exists() or not path.is_file():
            raise RemoteFileUploadError(f"Preprocessed file not found: {processed_path}")

        # Restrict reads to the managed IETF preprocessing directory.
        storage_root = Path("/app/api/storage/ietf-downloads").resolve()
        resolved = path.resolve()
        if storage_root not in resolved.parents:
            raise RemoteFileUploadError("Invalid preprocessed file path")

        try:
            raw_content = resolved.read_bytes()
        except OSError as exc:
            raise RemoteFileUploadError(f"Failed to read preprocessed file: {exc}") from exc

        upload_filename = resolved.name
        upload_mimetype = "application/x-ndjson"
        upload_content = raw_content

        if resolved.suffix.lower() == ".jsonl":
            IetfImportService._parse_preprocessed_jsonl(raw_content)

        try:
            user, _ = current_account_with_tenant()
            upload_file = FileService(db.engine).upload_file(
                filename=upload_filename,
                content=upload_content,
                mimetype=upload_mimetype,
                user=user,
                source="datasets",
                source_url=f"ietfdata://mailarchive/preprocessed/{resolved.name}",
            )
        except services.errors.file.FileTooLargeError as file_too_large_error:
            raise FileTooLargeError(file_too_large_error.description)
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
    # Private helpers for preprocessing
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_header(value: str) -> str:
        """Decode an RFC 2047-encoded header value to plain Unicode."""
        try:
            parts = email_header_lib.decode_header(value)
            decoded: list[str] = []
            for part, charset in parts:
                if isinstance(part, bytes):
                    decoded.append(part.decode(charset or "utf-8", errors="replace"))
                else:
                    decoded.append(part)
            return "".join(decoded)
        except Exception:
            return value

    @staticmethod
    def _extract_text_body(msg: email_lib.message.Message) -> str | None:
        """Return the decoded text/plain body, or None if not available."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")  # type: ignore[union-attr]
            return None

        if msg.get_content_type() == "text/plain":
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")  # type: ignore[union-attr]
        return None

    @staticmethod
    def _is_natural_text(text: str) -> bool:
        """Heuristic: return True only if text looks like human-readable prose."""
        stripped = text.strip()
        if len(stripped) < 20:
            return False
        # PGP/certificate blocks are not natural text
        if "-----BEGIN PGP" in stripped or "-----BEGIN CERTIFICATE" in stripped:
            return False
        printable_chars = set(string.printable)
        printable_ratio = sum(1 for c in stripped if c in printable_chars) / len(stripped)
        if printable_ratio < 0.70:
            return False
        # Must contain at least one word boundary (space or newline)
        if " " not in stripped and "\n" not in stripped:
            return False
        return True

    @staticmethod
    def _write_preprocessed_email_records(source_path: str, emails: list[dict]) -> tuple[str, str]:
        processed_dir = Path("/app/api/storage/ietf-downloads/preprocessed")
        processed_dir.mkdir(parents=True, exist_ok=True)

        source = Path(source_path)
        destination = processed_dir / f"{source.stem}.jsonl"
        if destination.exists():
            destination = processed_dir / f"{source.stem}-dup.jsonl"

        with destination.open("w", encoding="utf-8") as f:
            for email_record in emails:
                f.write(json.dumps(email_record, ensure_ascii=False) + "\n")

        relative_hint = destination.as_posix().replace("/app/api/storage/", "docker/volumes/app/storage/")
        return destination.as_posix(), relative_hint

    @staticmethod
    def _parse_preprocessed_jsonl(content: bytes) -> list[IetfPreprocessedEmailRecord]:
        records: list[IetfPreprocessedEmailRecord] = []
        text = content.decode("utf-8", errors="replace")

        for index, line in enumerate(text.splitlines(), start=1):
            raw = line.strip()
            if not raw:
                continue

            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RemoteFileUploadError(f"Invalid JSONL at line {index}: {exc}") from exc

            if not isinstance(obj, dict):
                raise RemoteFileUploadError(f"Invalid JSONL at line {index}: expected object")

            message_id = str(obj.get("id", "")).strip()
            url = str(obj.get("url", "")).strip()
            subject = str(obj.get("subject", "")).strip()
            body = str(obj.get("body", "")).strip()
            date = str(obj.get("date", "")).strip()

            if not message_id or not url or not body:
                raise RemoteFileUploadError(
                    f"Invalid JSONL at line {index}: id, url, and body are required"
                )

            records.append({"id": message_id, "url": url, "subject": subject, "body": body, "date": date})

        if not records:
            raise RemoteFileUploadError("No valid records found in preprocessed JSONL")

        return records