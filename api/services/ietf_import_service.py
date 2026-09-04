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
import difflib
import email as email_lib
import email.header as email_header_lib
import json
import logging
import operator
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
from services.email_quote_cleaning import QuoteHandling, clean_email_body
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
        # Overwrite any previous download of the same list/date-range: the
        # fresh download supersedes it (filters and sidecar dedup fixes can
        # change content), and "-dup" copies just accumulated on every retry.
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
    def download_drafts_to_local(payload: IetfDraftImportPayload) -> tuple[dict, int]:
        """Download draft revision text into local mounted storage and return
        local file metadata -- mirrors download_mailarchive_as_mbox_to_local:
        no FileService/account dependency, so callers outside a logged-in
        Studio session (the standalone decision-agent UI's Corpora tab) can
        pull fresh raw text without also needing to attach/index it.
        """

        sidecar_payload = IetfImportService._request_sidecar("/api/v1/import/drafts", dict(payload))
        try:
            content = base64.b64decode(sidecar_payload.content_base64)
        except ValueError as exc:
            raise RemoteFileUploadError("IETF data service returned invalid file content") from exc

        local_dir = Path("/app/api/storage/ietf-downloads")
        local_dir.mkdir(parents=True, exist_ok=True)

        filename = sidecar_payload.filename
        destination = local_dir / filename
        destination.write_bytes(content)

        relative_hint = destination.as_posix().replace("/app/api/storage/", "docker/volumes/app/storage/")
        return (
            {
                "saved_path": destination.as_posix(),
                "host_path_hint": relative_hint,
                "filename": destination.name,
                "size": destination.stat().st_size,
            },
            201,
        )

    # Meeting-numbered material's own name embeds which meeting it's from
    # (e.g. "minutes-65-sidr", "slides-66-sidr-4") -- extracted here instead
    # of a second lookup, since it's also how download_wg_resource_to_local
    # builds the actual ietf.org/proceedings/<meeting>/... URL.
    _MEETING_MATERIAL_NAME_RE = re.compile(r"^[a-z]+-(\d+)-")

    @staticmethod
    def list_wg_resources(wg_acronym: str, doc_type: str) -> dict:
        """List a WG's documents of one Datatracker doctype (agenda, minutes,
        slides, charter, recording, etc.) via Datatracker's own document
        listing API, parameterized by type -- the same read-only call covers
        every non-draft/RFC/mailarchive resource a WG has, since Datatracker
        already exposes them all through one endpoint.
        """
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(
                    "https://datatracker.ietf.org/api/v1/doc/document/",
                    params={"group__acronym": wg_acronym, "type": doc_type, "format": "json", "limit": 250},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Datatracker document list failed for %s/%s: %s", wg_acronym, doc_type, exc)
            return {"count": 0, "documents": [], "error": str(exc)}

        data = response.json()
        documents = [
            {
                "name": obj.get("name", ""),
                "rev": obj.get("rev", ""),
                "title": obj.get("title", ""),
                "time": obj.get("time", ""),
                "uploaded_filename": obj.get("uploaded_filename", ""),
                "external_url": obj.get("external_url", ""),
            }
            for obj in data.get("objects", [])
        ]
        return {"count": data.get("meta", {}).get("total_count", len(documents)), "documents": documents}

    @staticmethod
    def download_wg_resource_to_local(doc_type: str, document: dict) -> dict:
        """Download one WG resource document (agenda/minutes/slides/charter/
        etc.) to local storage -- download only, same scope as every other
        Corpora-tab action: no FileService/account dependency, no dataset
        attach/index.

        URL construction genuinely differs per doctype family -- confirmed
        by spot-checking real documents rather than assumed:
          - charter: ietf.org/charter/<name>-<rev>.txt
          - conflrev: ietf.org/cr/<name>-<rev>.txt
          - recording: external_url is already a direct link (YouTube or an
            .mp3 on ietf.org) -- returned as a link, not downloaded, since a
            YouTube URL isn't a fetchable file and treating it as one would
            be misleading.
          - everything else (agenda, minutes, slides, bluesheets, chatlog,
            narrativeminutes, polls, procmaterials): meeting-numbered
            material served from
            ietf.org/proceedings/<meeting>/<type>/<uploaded_filename>.

        Raises RemoteFileUploadError with a specific reason when a document
        doesn't fit any of the above -- a real, current limitation for a
        handful of doctypes (e.g. BOF Request, Status Change) whose content
        lives in Datatracker's own database rather than a static file, not
        a bug to silently paper over with a guessed URL.
        """
        name = document.get("name", "")
        rev = document.get("rev", "")
        uploaded_filename = document.get("uploaded_filename", "")
        external_url = document.get("external_url", "")

        if doc_type == "recording":
            if not external_url:
                raise RemoteFileUploadError(f"{name}: no recording URL on file")
            return {"name": name, "kind": "link", "url": external_url}

        if doc_type == "charter":
            url = f"https://www.ietf.org/charter/{name}-{rev}.txt"
        elif doc_type == "conflrev":
            url = f"https://www.ietf.org/cr/{name}-{rev}.txt"
        else:
            if not uploaded_filename:
                raise RemoteFileUploadError(f"{name}: no known download location for this document type yet")
            match = IetfImportService._MEETING_MATERIAL_NAME_RE.match(name)
            if not match:
                raise RemoteFileUploadError(f"{name}: could not determine its meeting number")
            url = f"https://www.ietf.org/proceedings/{match.group(1)}/{doc_type}/{uploaded_filename}"

        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RemoteFileUploadError(f"{name}: failed to download from {url} ({exc})") from exc

        local_dir = Path("/app/api/storage/ietf-downloads/resources")
        local_dir.mkdir(parents=True, exist_ok=True)
        # uploaded_filename is only unique within one meeting -- many older
        # minutes/bluesheets across DIFFERENT meetings were all literally
        # uploaded as "sidr.txt", confirmed live (multiple downloads silently
        # overwrote each other before this fix). The document's own `name`
        # (e.g. "minutes-83-sidr") is unique across the whole WG, so it's
        # used for the local filename; the extension still comes from
        # uploaded_filename since format varies (.txt/.html/.pdf).
        extension = Path(uploaded_filename).suffix if uploaded_filename else ".txt"
        filename = f"{name}{extension}"
        destination = local_dir / filename
        destination.write_bytes(response.content)

        return {
            "name": name,
            "kind": "file",
            "saved_path": destination.as_posix(),
            "filename": destination.name,
            "size": destination.stat().st_size,
            "source_url": url,
        }

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
    def get_draft_relations(draft_name: str) -> dict:
        """Return relationship and date information for a draft via IETF Datatracker.

        replaces:       drafts this one supersedes (source=this, relationship=replaces)
        replaced_by:    drafts/RFCs that supersede this one (target=this, relationship=replaces)
        first_rev_date: ISO datetime of the earliest new_revision event (first submission)
        last_rev_date:  ISO datetime of the latest new_revision event (last published version)
        latest_rev:     current revision number (e.g. "10"), straight from the document
                        record's own "rev" field -- lets a caller build the exact
                        "<draft_name>-<rev>" identifier a fresh download needs without
                        a second lookup.

        All three queries run in parallel.
        """

        _RDOC_URL = "https://datatracker.ietf.org/api/v1/doc/relateddocument/"
        _EVENT_URL = "https://datatracker.ietf.org/api/v1/doc/docevent/"
        replaces: list[str] = []
        replaced_by: list[str] = []
        first_rev_date: str | None = None
        last_rev_date: str | None = None
        latest_rev: str | None = None

        def _fetch_related(client: httpx.Client, params: dict, uri_field: str) -> list[str]:
            try:
                resp = client.get(_RDOC_URL, params=params)
                resp.raise_for_status()
                names: list[str] = []
                for obj in resp.json().get("objects", []):
                    uri: str = obj.get(uri_field, "") or ""
                    name = uri.rstrip("/").split("/")[-1]
                    if name:
                        names.append(name)
                return names
            except Exception as exc:
                logger.warning("Datatracker relations sub-request failed: %s", exc)
                return []

        def _fetch_dates(client: httpx.Client) -> tuple[str | None, str | None, str | None]:
            first_date: str | None = None
            last_date: str | None = None
            rev: str | None = None
            try:
                # Last-modified date and current revision from the document record itself
                doc_resp = client.get(
                    f"https://datatracker.ietf.org/api/v1/doc/document/{draft_name}/",
                    params={"format": "json"},
                )
                doc_resp.raise_for_status()
                doc_json = doc_resp.json()
                last_date = doc_json.get("time")
                rev = doc_json.get("rev")
            except Exception as exc:
                logger.warning("Datatracker document fetch failed for '%s': %s", draft_name, exc)
            try:
                # First revision: docevent where desc contains "<name>-00"
                # (IETF drafts always start at revision 00; doc__name is not a
                # supported filter on this endpoint so we use desc__contains instead)
                # desc__contains with order_by returns 400 — the single rev-00
                # event is unique so ordering is not needed
                ev_resp = client.get(_EVENT_URL, params={
                    "type": "new_revision",
                    "desc__contains": f"{draft_name}-00",
                    "format": "json",
                    "limit": 1,
                })
                ev_resp.raise_for_status()
                objects = ev_resp.json().get("objects", [])
                if objects:
                    first_date = objects[0].get("time")
            except Exception as exc:
                logger.warning("Datatracker docevent fetch failed for '%s': %s", draft_name, exc)
            return first_date, last_date, rev

        try:
            with httpx.Client(timeout=10.0) as client:
                base_params = {"relationship__slug": "replaces", "format": "json", "limit": 50}
                with ThreadPoolExecutor(max_workers=3) as pool:
                    f_replaces = pool.submit(
                        _fetch_related, client, {**base_params, "source__name": draft_name}, "target"
                    )
                    f_replaced_by = pool.submit(
                        _fetch_related, client, {**base_params, "target__name": draft_name}, "source"
                    )
                    f_dates = pool.submit(_fetch_dates, client)
                    replaces = f_replaces.result()
                    replaced_by = f_replaced_by.result()
                    first_rev_date, last_rev_date, latest_rev = f_dates.result()
        except Exception as exc:
            logger.warning("Datatracker relations request failed for '%s': %s", draft_name, exc)

        return {
            "replaces": replaces,
            "replaced_by": replaced_by,
            "first_rev_date": first_rev_date,
            "last_rev_date": last_rev_date,
            "latest_rev": latest_rev,
        }

    @staticmethod
    def get_rfc_relations(rfc_number: str) -> dict:
        """Return obsoletes/obsoleted_by/updates/updated_by for an RFC via
        Datatracker's relateddocument API -- the same relationship graph
        get_draft_relations already queries for drafts (replaces/replaced_by),
        but the two edge types relevant to "is this actually explained
        somewhere else in the RFC's own family" (a later RFC obsoleting or
        updating it, or it obsoleting/updating an earlier one) rather than
        draft-to-draft supersession before publication.

        All four queries (two relationships x two directions) run in parallel.
        """
        number = re.sub(r"[^0-9]", "", rfc_number)
        if not number:
            return {"error": f"{rfc_number!r} doesn't look like an RFC number"}
        name = f"rfc{number}"

        def _fetch_related(client: httpx.Client, params: dict, uri_field: str) -> list[str]:
            try:
                resp = client.get("https://datatracker.ietf.org/api/v1/doc/relateddocument/", params=params)
                resp.raise_for_status()
                names: list[str] = []
                for obj in resp.json().get("objects", []):
                    uri: str = obj.get(uri_field, "") or ""
                    doc_name = uri.rstrip("/").split("/")[-1]
                    if not doc_name:
                        continue
                    names.append(doc_name.upper() if re.fullmatch(r"rfc\d+", doc_name) else doc_name)
                return names
            except Exception as exc:
                logger.warning("Datatracker relation fetch failed for '%s': %s", name, exc)
                return []

        obsoletes: list[str] = []
        obsoleted_by: list[str] = []
        updates: list[str] = []
        updated_by: list[str] = []
        try:
            with httpx.Client(timeout=10.0) as client:
                base_params = {"format": "json", "limit": 50}
                with ThreadPoolExecutor(max_workers=4) as pool:
                    f_obsoletes = pool.submit(
                        _fetch_related,
                        client,
                        {**base_params, "source__name": name, "relationship__slug": "obs"},
                        "target",
                    )
                    f_obsoleted_by = pool.submit(
                        _fetch_related,
                        client,
                        {**base_params, "target__name": name, "relationship__slug": "obs"},
                        "source",
                    )
                    f_updates = pool.submit(
                        _fetch_related,
                        client,
                        {**base_params, "source__name": name, "relationship__slug": "updates"},
                        "target",
                    )
                    f_updated_by = pool.submit(
                        _fetch_related,
                        client,
                        {**base_params, "target__name": name, "relationship__slug": "updates"},
                        "source",
                    )
                    obsoletes = f_obsoletes.result()
                    obsoleted_by = f_obsoleted_by.result()
                    updates = f_updates.result()
                    updated_by = f_updated_by.result()
        except Exception as exc:
            logger.warning("Datatracker relations request failed for '%s': %s", name, exc)
            return {"error": str(exc)}

        return {
            "rfc": name.upper(),
            "obsoletes": obsoletes,
            "obsoleted_by": obsoleted_by,
            "updates": updates,
            "updated_by": updated_by,
        }

    @staticmethod
    def get_wg_documents(wg_acronym: str) -> list[dict]:
        """Fetch Internet-Drafts for a WG from IETF Datatracker.

        Also fetches RFC documents for the same WG and links them to drafts by
        title (both the draft and its published RFC share an identical title).
        The resulting rfc_number field is null for unpublished drafts.
        """

        _DATATRACKER_URL = "https://datatracker.ietf.org/api/v1/doc/document/"
        documents: list[dict] = []

        try:
            with httpx.Client(timeout=15.0) as client:
                # Build title → rfc_number map from published RFC documents in this WG
                title_to_rfc: dict[str, int] = {}
                rfc_resp = client.get(_DATATRACKER_URL, params={
                    "group__acronym": wg_acronym,
                    "name__startswith": "rfc",
                    "format": "json",
                    "limit": 200,
                })
                rfc_resp.raise_for_status()
                for obj in rfc_resp.json().get("objects", []):
                    title: str = (obj.get("title") or "").strip()
                    rfc_num = obj.get("rfc_number")
                    if title and rfc_num:
                        title_to_rfc[title.lower()] = int(rfc_num)

                # Fetch all drafts with pagination
                params: dict = {
                    "group__acronym": wg_acronym,
                    "name__startswith": "draft-",
                    "format": "json",
                    "limit": 200,
                    "offset": 0,
                }
                while True:
                    response = client.get(_DATATRACKER_URL, params=params)
                    response.raise_for_status()
                    data = response.json()
                    for obj in data.get("objects", []):
                        name: str = obj.get("name", "")
                        if not name:
                            continue
                        doc_title: str = (obj.get("title") or "").strip()
                        rfc_number = title_to_rfc.get(doc_title.lower())
                        documents.append({"name": name, "title": doc_title, "rfc_number": rfc_number})
                    meta = data.get("meta", {})
                    if not meta.get("next"):
                        break
                    params["offset"] = int(params["offset"]) + int(params["limit"])
        except httpx.RequestError as exc:
            logger.warning("IETF Datatracker request failed for WG '%s': %s", wg_acronym, exc)
            raise RemoteFileUploadError(f"Failed to contact IETF Datatracker: {exc}") from exc
        except Exception as exc:
            logger.warning("IETF Datatracker response parse error for WG '%s': %s", wg_acronym, exc)
            raise RemoteFileUploadError("IETF Datatracker returned an unexpected response") from exc

        documents.sort(key=operator.itemgetter("name"))
        return documents

    # ------------------------------------------------------------------ #
    # Draft evolution (section-level diff chain across all revisions)      #
    # ------------------------------------------------------------------ #

    _SECTION_RE = re.compile(r'^(\d+(?:\.\d+)*|[A-Z](?:\.\d+)*)\.?\s{2,}(\S.*)$')
    # One page break = optional footer line ("Author  Expires ...  [Page N]"),
    # the form-feed line, at most ONE running-header line, then blank lines.
    # The old pattern ate up to three lines after the form feed blindly, which
    # could swallow the first body line of the next page; and it never removed
    # the footer, which then leaked into section bodies (and from there into
    # evolution diffs and retrieval queries built from them).
    _PAGE_BREAK_RE = re.compile(
        r'(?:^[^\n]*\[Page\s+\d+\][^\n]*\n)?'  # footer line before the break
        r'\x0c[^\n]*\n'                         # form-feed line
        r'(?:[^\n]*\S[^\n]*\n)?'                # one running-header line
        r'(?:[ \t]*\n)*',                       # trailing blank lines
        re.MULTILINE,
    )
    # Safety net for footers not adjacent to a form feed (flat text files).
    _PAGE_FOOTER_LINE_RE = re.compile(r'^[^\n]*\[Page\s+\d+\]\s*$')
    # Sections that are structural / bibliographic rather than normative content.
    # Changes to these sections are noise in an evolution diff.
    _SKIP_SECTION_RE = re.compile(
        r'^\s*(?:'
        r'table\s+of\s+contents?'
        r'|author(?:s)?(?:\'s?)?\s+(?:address(?:es)?|information|contact(?:s)?)'
        r'|(?:normative|informative|other)?\s*references?'
        r'|acknowledgm?ents?'
        r'|contributors?'
        r'|copyright\s+(?:notice|statement)'
        r'|full\s+copyright'
        r'|status\s+of\s+this\s+memo'
        r'|change\s+log'
        r'|changes?\s+(?:from|since)\b.*'
        r'|document\s+history'
        r'|revision\s+history'
        r'|open\s+issues'
        r'|editor\'?s?\s+notes?'
        r'|index'
        r')\s*$',
        re.IGNORECASE,
    )

    # RFC 2119 normative keywords: an edit touching one of these is always
    # significant no matter how small ("MAY" -> "MUST" is one word).
    _RFC2119_RE = re.compile(
        r'\b(?:MUST(?:\s+NOT)?|SHALL(?:\s+NOT)?|SHOULD(?:\s+NOT)?'
        r'|REQUIRED|RECOMMENDED|NOT\s+RECOMMENDED|MAY|OPTIONAL)\b'
    )

    # Word-level similarity at or above this marks a change as cosmetic
    # (roughly a couple of words per fifty). Deliberately loose: the
    # normative-keyword and digit guards, not the ratio, are what protect
    # significant-but-tiny edits from being dropped.
    _MINOR_EDIT_SIMILARITY = 0.95

    @staticmethod
    def _section_sort_key(sec_id: str) -> tuple:
        """Numeric sort key for section IDs like '1', '1.2', '10', 'A', 'A.1'."""
        parts = sec_id.split('.')
        key: list[tuple[int, int | str]] = []
        for part in parts:
            try:
                key.append((0, int(part)))
            except ValueError:
                key.append((1, part))
        return tuple(key)

    @staticmethod
    def _parse_sections(text: str) -> dict[str, tuple[str, str]]:
        """Split IETF plain-text into {section_id: (title, body)}.

        Headings are recognised by the pattern ``N.M.  Title`` at column 0.
        Page-break artifacts (form-feed + running header/footer) are stripped
        first so they do not appear as spurious section headings.
        """
        text = IetfImportService._PAGE_BREAK_RE.sub('\n', text)
        sections: dict[str, tuple[str, str]] = {}
        current_id: str | None = None
        current_title = ''
        current_lines: list[str] = []

        for line in text.splitlines():
            if IetfImportService._PAGE_FOOTER_LINE_RE.match(line):
                continue
            m = IetfImportService._SECTION_RE.match(line)
            if m:
                if current_id is not None:
                    sections[current_id] = (current_title, '\n'.join(current_lines).strip())
                current_id = m.group(1).rstrip('.')
                current_title = m.group(2).strip()
                current_lines = []
            elif current_id is not None:
                current_lines.append(line)

        if current_id is not None:
            sections[current_id] = (current_title, '\n'.join(current_lines).strip())
        return sections

    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        """Split section body into paragraphs on two or more consecutive blank lines."""
        return [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]

    @staticmethod
    def _prose_diff_blocks(old_text: str, new_text: str, from_rev: str, to_rev: str) -> list[str]:
        """Return each paragraph-level change as its own prose block.

        Uses SequenceMatcher on the paragraph list so each block describes one
        actual change in plain English rather than patch-format noise. Returns
        an empty list when no paragraph-level differences are found (caller
        should fall back to showing the full before/after texts).
        """
        old_paras = IetfImportService._split_paragraphs(old_text)
        new_paras = IetfImportService._split_paragraphs(new_text)
        matcher = difflib.SequenceMatcher(None, old_paras, new_paras, autojunk=False)
        blocks: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                continue
            old_block = old_paras[i1:i2]
            new_block = new_paras[j1:j2]
            if tag == 'replace':
                paired = min(len(old_block), len(new_block))
                for old_p, new_p in zip(old_block[:paired], new_block[:paired]):
                    blocks.append(f"Before (rev {from_rev}):\n{old_p}\n\nAfter (rev {to_rev}):\n{new_p}")
                for extra in old_block[paired:]:
                    blocks.append(f"Removed in rev {to_rev}:\n{extra}")
                for extra in new_block[paired:]:
                    blocks.append(f"Added in rev {to_rev}:\n{extra}")
            elif tag == 'delete':
                for p in old_block:
                    blocks.append(f"Removed in rev {to_rev}:\n{p}")
            elif tag == 'insert':
                for p in new_block:
                    blocks.append(f"Added in rev {to_rev}:\n{p}")
        return blocks

    @staticmethod
    def _prose_diff(old_text: str, new_text: str, from_rev: str, to_rev: str) -> str:
        """Return a human-readable paragraph-level diff as plain prose.

        Joins all of ``_prose_diff_blocks`` into a single body. Returns an
        empty string when no paragraph-level differences are found (caller
        should fall back to showing the full before/after texts).
        """
        return '\n\n'.join(IetfImportService._prose_diff_blocks(old_text, new_text, from_rev, to_rev))

    @staticmethod
    def _section_line_spans(text: str) -> dict[str, tuple[int, int]]:
        """Map section id -> (start_line, end_line), 1-based, in the ORIGINAL text.

        Computed against the raw downloaded revision (before page-break
        stripping) so the numbers match what a reader sees opening the .txt.
        ToC entries match the heading pattern too; the last occurrence wins,
        which is the real body heading. A span runs from the heading line to
        the line before the next heading (or end of file).
        """
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        headings: list[tuple[str, int]] = []
        for index, line in enumerate(lines, start=1):
            match = IetfImportService._SECTION_RE.match(line)
            if match:
                headings.append((match.group(1).rstrip("."), index))
        spans: dict[str, tuple[int, int]] = {}
        for position, (sec_id, start) in enumerate(headings):
            end = headings[position + 1][1] - 1 if position + 1 < len(headings) else len(lines)
            spans[sec_id] = (start, end)
        return spans

    @staticmethod
    def _classify_edit(old_text: str, new_text: str) -> tuple[float, bool]:
        """Return (word-level similarity, is_minor) for a modified section.

        An edit is *minor* (spelling/punctuation-level) only when similarity
        is at or above _MINOR_EDIT_SIMILARITY AND the changed words contain
        no RFC 2119 keyword and no digits - tiny edits like "MAY" -> "MUST"
        or "32" -> "64" are protocol-significant and must never be dropped.
        """
        old_words = old_text.split()
        new_words = new_text.split()
        matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
        similarity = matcher.ratio()

        changed_words: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                changed_words.extend(old_words[i1:i2])
                changed_words.extend(new_words[j1:j2])
        changed_blob = " ".join(changed_words)

        is_minor = (
            similarity >= IetfImportService._MINOR_EDIT_SIMILARITY
            and not IetfImportService._RFC2119_RE.search(changed_blob)
            and not any(character.isdigit() for character in changed_blob)
        )
        return similarity, is_minor

    @staticmethod
    def _patch_diff(old_text: str, new_text: str, from_rev: str, to_rev: str) -> str:
        """Unified diff of a section body; hunk offsets are section-relative.

        Absolute anchoring in the original revision text comes from the
        from_lines/to_lines record metadata, not the hunk headers.
        """
        diff_lines = difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"rev {from_rev}",
            tofile=f"rev {to_rev}",
            lineterm="",
            n=2,
        )
        return "\n".join(diff_lines)

    @staticmethod
    def _diff_version_pair(
        old: dict[str, tuple[str, str]],
        new: dict[str, tuple[str, str]],
        from_rev: str,
        to_rev: str,
        draft_name: str,
        *,
        diff_format: Literal["prose", "patch"] = "prose",
        old_spans: dict[str, tuple[int, int]] | None = None,
        new_spans: dict[str, tuple[int, int]] | None = None,
        skip_minor_edits: bool = False,
        split_modified_paragraphs: bool = False,
    ) -> list[dict]:
        """Return change records for every section that differs between two revisions.

        A "modified" section can bundle several unrelated paragraph-level
        changes (e.g. four different parameters all defined in one "Parameter
        Values" section) into what would otherwise be a single long, topically
        mixed chunk — which measurably hurts keyword retrieval for a query
        about just one of those changes (a real, DB-verified case: a genuine
        INIT_CWND value change was retrievable in the corpus but never ranked
        in the top 50 hits for "INIT_CWND" out of 328 segments, because it sat
        in a chunk that also talked about ALLOWED_INCREASE, GAIN, and
        CURRENT_FILTER). ``split_modified_paragraphs=True`` emits one record
        per paragraph-level change instead of one per section, so each
        retrievable chunk stays about one specific change. Off by default —
        this is opt-in so any existing evolution dataset built by the default
        path is unaffected by this change.
        """
        records: list[dict] = []
        sort_key = IetfImportService._section_sort_key
        for sec_id in sorted(set(old) | set(new), key=sort_key):
            old_entry = old.get(sec_id)
            new_entry = new.get(sec_id)
            old_text = old_entry[1] if old_entry else ''
            new_text = new_entry[1] if new_entry else ''
            similarity: float | None = None
            if old_entry is None and new_entry is not None:
                change_type = "added"
            elif old_entry is not None and new_entry is None:
                change_type = "removed"
            elif old_entry is not None and new_entry is not None:
                if ' '.join(old_text.split()) == ' '.join(new_text.split()):
                    continue  # identical text — skip
                similarity, is_minor = IetfImportService._classify_edit(old_text, new_text)
                if skip_minor_edits and is_minor:
                    continue  # cosmetic edit (spelling/punctuation) — skip
                change_type = "modified"
            else:
                continue
            section_title = (new_entry or old_entry)[0].strip()  # type: ignore[index]
            # Skip boilerplate/bibliographic sections — they're not normative content.
            if IetfImportService._SKIP_SECTION_RE.match(section_title):
                continue

            # Embed a parseable header as the first line so metadata is
            # recoverable from the DB content even without the vector store.
            # Must NOT start with punctuation/symbols — remove_leading_symbols()
            # in the indexing pipeline strips those characters from the start.
            header = (
                f"ietf-evolution draft:{draft_name} "
                f"from:{from_rev} to:{to_rev} "
                f"section:{sec_id} change:{change_type} "
                f"| {section_title}"
            )

            def _base_record(
                sec_id: str = sec_id,
                change_type: str = change_type,
                similarity: float | None = similarity,
                section_title: str = section_title,
            ) -> dict:
                # Default args bind these to *this* loop iteration's values at
                # def time, not whatever they are when the closure is later
                # called (it's always called within the same iteration here,
                # but binding explicitly avoids relying on that).
                record: dict = {
                    "draft_name": draft_name,
                    "from_rev": from_rev,
                    "to_rev": to_rev,
                    "section_id": sec_id,
                    "section_title": section_title,
                    "change_type": change_type,
                }
                # Line spans anchor the section in each ORIGINAL revision text.
                # Only stable within a revision (RFC text is reflowed between
                # revisions); section_id remains the cross-revision key.
                if old_spans and sec_id in old_spans:
                    record["from_lines"] = f"{old_spans[sec_id][0]}-{old_spans[sec_id][1]}"
                if new_spans and sec_id in new_spans:
                    record["to_lines"] = f"{new_spans[sec_id][0]}-{new_spans[sec_id][1]}"
                if similarity is not None:
                    record["similarity"] = f"{similarity:.3f}"
                return record

            if change_type == "modified" and diff_format == "prose" and split_modified_paragraphs:
                blocks = IetfImportService._prose_diff_blocks(old_text, new_text, from_rev, to_rev)
                if blocks:
                    for block in blocks:
                        record = _base_record()
                        record["content"] = f"{header}\n\n{block}"
                        records.append(record)
                    continue
                # No paragraph-level boundaries found — fall through to the
                # single-record full before/after body below.

            # Added/removed sections carry their full text in both formats;
            # only "modified" bodies differ by diff_format.
            if change_type == "added":
                body = f"New section added in rev {to_rev}:\n{new_text}"
            elif change_type == "removed":
                body = f"Section removed after rev {from_rev}:\n{old_text}"
            elif diff_format == "patch":
                body = IetfImportService._patch_diff(old_text, new_text, from_rev, to_rev)
                if not body:
                    body = f"Before (rev {from_rev}):\n{old_text}\n\nAfter (rev {to_rev}):\n{new_text}"
            else:
                # Human-readable prose body (embeds better than patch syntax).
                body = IetfImportService._prose_diff(old_text, new_text, from_rev, to_rev)
                if not body:
                    # No paragraph-level boundaries found — show full before/after.
                    body = f"Before (rev {from_rev}):\n{old_text}\n\nAfter (rev {to_rev}):\n{new_text}"

            record = _base_record()
            # `content` is promoted to page_content by JsonlExtractor; everything
            # else becomes chunk metadata (draft_name, from_rev, to_rev, …).
            record["content"] = f"{header}\n\n{body}"
            records.append(record)
        return records

    @staticmethod
    def _fetch_all_revisions(draft_name: str) -> tuple[list[str], dict[str, str]]:
        """Fetch all plain-text revisions of *draft_name* from the IETF archive.

        Returns ``(sorted_revs, version_texts)`` where *version_texts* maps each
        two-digit revision string to its plain-text content.
        """
        _ARCHIVE = "https://www.ietf.org/archive/id/"
        _DATATRACKER = "https://datatracker.ietf.org/api/v1/doc/document/"

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(f"{_DATATRACKER}{draft_name}/", params={"format": "json"})
                resp.raise_for_status()
                max_rev = int(resp.json().get("rev", "0"))
        except Exception as exc:
            raise RemoteFileUploadError(
                f"Could not retrieve revision info for '{draft_name}' from Datatracker"
            ) from exc

        revisions = [f"{i:02d}" for i in range(max_rev + 1)]

        def _fetch(rev: str) -> tuple[str, str | None]:
            url = f"{_ARCHIVE}{draft_name}-{rev}.txt"
            try:
                with httpx.Client(timeout=30.0) as c:
                    r = c.get(url)
                    if r.status_code == 200:
                        return rev, r.text
            except Exception as exc:
                logger.debug("Could not fetch %s: %s", url, exc)
            return rev, None

        version_texts: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            for rev, text in pool.map(_fetch, revisions):
                if text:
                    version_texts[rev] = text

        if len(version_texts) < 2:
            raise RemoteFileUploadError(
                f"Could not fetch enough revisions of '{draft_name}' to produce a diff"
            )

        return sorted(version_texts), version_texts

    @staticmethod
    def _compute_section_lineage(
        all_changes: list[dict],
        final_sections: dict[str, tuple[str, str]],
        first_rev: str,
    ) -> dict[str, dict]:
        """Derive revision lineage for each section present in the final version.

        Returns a mapping ``{section_id: {first_seen_rev, last_changed_rev,
        change_count}}`` using the ordered *all_changes* list built by
        ``_diff_version_pair``.
        """
        lineage: dict[str, dict] = {}
        for sec_id in final_sections:
            sec_changes = [c for c in all_changes if c["section_id"] == sec_id]
            if not sec_changes:
                lineage[sec_id] = {
                    "first_seen_rev": first_rev,
                    "last_changed_rev": first_rev,
                    "change_count": 0,
                }
            else:
                added_events = [c for c in sec_changes if c["change_type"] == "added"]
                first_seen = added_events[0]["to_rev"] if added_events else first_rev
                lineage[sec_id] = {
                    "first_seen_rev": first_seen,
                    "last_changed_rev": sec_changes[-1]["to_rev"],
                    "change_count": len(sec_changes),
                }
        return lineage

    @staticmethod
    def generate_draft_evolution(
        draft_name: str,
        diff_format: Literal["prose", "patch"] = "prose",
        skip_minor_edits: bool = False,
        split_modified_paragraphs: bool = False,
    ) -> tuple[dict, int]:
        """Fetch every plain-text revision of a draft, diff them section by
        section, and upload the resulting change-log as a JSONL dataset file.

        Each JSONL line becomes one chunk via JsonlExtractor.  Fields:
          content        — diff body (promoted to page_content): paragraph-level
                           prose (default, embeds better) or unified patch
          draft_name, from_rev, to_rev, section_id, section_title,
          change_type (added|removed|modified),
          from_lines/to_lines (section's line span in each original revision
          text; only stable within a revision)  — chunk metadata

        Boilerplate sections (ToC, References, Authors, Acknowledgements, …)
        are excluded; only normative content sections are diffed.

        ``split_modified_paragraphs`` (default off, see ``_diff_version_pair``)
        emits one chunk per paragraph-level change within a modified section
        instead of one chunk per section, so a query about one specific
        change doesn't have to compete for keyword rank against unrelated
        changes bundled into the same chunk.
        """
        sorted_revs, version_texts = IetfImportService._fetch_all_revisions(draft_name)
        parsed = {rev: IetfImportService._parse_sections(version_texts[rev]) for rev in sorted_revs}
        spans = {rev: IetfImportService._section_line_spans(version_texts[rev]) for rev in sorted_revs}

        all_changes: list[dict] = []
        for from_rev, to_rev in zip(sorted_revs, sorted_revs[1:]):
            all_changes.extend(
                IetfImportService._diff_version_pair(
                    parsed[from_rev],
                    parsed[to_rev],
                    from_rev,
                    to_rev,
                    draft_name,
                    diff_format=diff_format,
                    old_spans=spans[from_rev],
                    new_spans=spans[to_rev],
                    skip_minor_edits=skip_minor_edits,
                    split_modified_paragraphs=split_modified_paragraphs,
                )
            )

        jsonl_bytes = '\n'.join(json.dumps(r) for r in all_changes).encode()
        format_suffix = "-patch" if diff_format == "patch" else ""
        granularity_suffix = "-paragraph" if split_modified_paragraphs else ""
        filename = f"{draft_name}-evolution{format_suffix}{granularity_suffix}.jsonl"

        try:
            user, _ = current_account_with_tenant()
            upload_file = FileService(db.engine).upload_file(
                filename=filename,
                content=jsonl_bytes,
                mimetype="application/jsonlines",
                user=user,
                source="datasets",
                source_url=f"ietfdata://evolution/{draft_name}",
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

    @staticmethod
    def generate_draft_annotated_rfc(draft_name: str) -> tuple[dict, int]:
        """Fetch all revisions, compute per-section lineage, then upload a JSONL
        file containing the *final* version's sections annotated with:

          first_seen_rev   — revision that first introduced this section
          last_changed_rev — most recent revision that modified it
          change_count     — total number of revision pairs that touched it

        A section with change_count=0 has been stable since the first fetched
        revision (settled text); a high change_count signals a frequently
        renegotiated requirement.
        """
        sorted_revs, version_texts = IetfImportService._fetch_all_revisions(draft_name)
        parsed = {rev: IetfImportService._parse_sections(version_texts[rev]) for rev in sorted_revs}

        all_changes: list[dict] = []
        for from_rev, to_rev in zip(sorted_revs, sorted_revs[1:]):
            all_changes.extend(
                IetfImportService._diff_version_pair(
                    parsed[from_rev], parsed[to_rev], from_rev, to_rev, draft_name
                )
            )

        final_rev = sorted_revs[-1]
        final_sections = parsed[final_rev]
        lineage = IetfImportService._compute_section_lineage(all_changes, final_sections, sorted_revs[0])

        sort_key = IetfImportService._section_sort_key
        records: list[dict] = []
        for sec_id in sorted(final_sections, key=sort_key):
            title, body = final_sections[sec_id]
            if not body.strip():
                continue
            if IetfImportService._SKIP_SECTION_RE.match(title):
                continue
            lin = lineage.get(sec_id, {})
            first_seen_rev = str(lin.get("first_seen_rev", sorted_revs[0]))
            last_changed_rev = str(lin.get("last_changed_rev", sorted_revs[0]))
            change_count = str(lin.get("change_count", 0))
            # Embed parseable header (must start with a letter — see remove_leading_symbols).
            header = (
                f"ietf-annotated draft:{draft_name} "
                f"rev:{final_rev} "
                f"section:{sec_id} "
                f"first:{first_seen_rev} "
                f"last:{last_changed_rev} "
                f"changes:{change_count} "
                f"| {title}"
            )
            content = f"{header}\n\n{body}"
            records.append({
                "content": content,
                "draft_name": draft_name,
                "section_id": sec_id,
                "section_title": title,
                "final_rev": final_rev,
                "first_seen_rev": first_seen_rev,
                "last_changed_rev": last_changed_rev,
                "change_count": change_count,
            })

        jsonl_bytes = '\n'.join(json.dumps(r) for r in records).encode()
        filename = f"{draft_name}-annotated.jsonl"

        try:
            user, _ = current_account_with_tenant()
            upload_file = FileService(db.engine).upload_file(
                filename=filename,
                content=jsonl_bytes,
                mimetype="application/jsonlines",
                user=user,
                source="datasets",
                source_url=f"ietfdata://annotated/{draft_name}",
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
    def preprocess_mbox_file(
        saved_path: str, filter_emails: bool = True, quote_handling: QuoteHandling = "none"
    ) -> dict:
        """Parse a local mbox file and extract clean {url, subject, body} records.

        Returns a dict with keys:
            emails            – list of accepted {id, url, subject, body} records
            total_emails      – number of accepted emails
            discarded_emails  – number discarded (unparseable or non-natural body)
            preview_lines     – top 10 body lines of the first accepted email

        Messages without an Archived-At header (IETF list mail before ~2014)
        fall back to a mid:<message-id> identity instead of being discarded.
        When filter_emails is False, all emails with a parseable message and
        non-empty body are accepted regardless of body quality.

        quote_handling controls stripping of quoted reply chains / signatures from
        the extracted bodies — see services.email_quote_cleaning.clean_email_body.
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

            # ---- URL / ID from Archived-At, falling back to Message-ID ----
            # Archived-At only exists on IETF list mail from ~2014 onwards;
            # its absence says nothing about content quality, so older
            # messages fall back to a mid: identity instead of being
            # discarded by the quality filter.
            archived_at = msg.get("Archived-At", "").strip()
            url = archived_at.strip("<>").strip()
            if not url:
                raw_mid = msg.get("Message-ID", "").strip().strip("<>")
                message_id = raw_mid or str(len(accepted))
                url = f"mid:{message_id}"
            else:
                message_id = url.rstrip("/").split("/")[-1]

            # ---- Subject (RFC 2047 decoded) ----
            subject = IetfImportService._decode_header(msg.get("Subject", ""))

            # ---- Date ----
            date = IetfImportService._decode_header(msg.get("Date", ""))

            # ---- Threading headers (used by group_into_threads) ----
            from_header = IetfImportService._decode_header(msg.get("From", "")).strip()
            raw_message_id_header = (msg.get("Message-ID") or "").strip()
            references_header = " ".join(
                part
                for part in ((msg.get("References") or "").strip(), (msg.get("In-Reply-To") or "").strip())
                if part
            )

            # ---- Body (text/plain only) ----
            raw_body = IetfImportService._extract_text_body(msg)
            body = raw_body.strip() if raw_body else None
            if body and quote_handling != "none":
                body = clean_email_body(body, mode=quote_handling)
            if not body or (filter_emails and not IetfImportService._is_natural_text(body)):
                discarded += 1
                continue
            if filter_emails and IetfImportService._is_administrative_notice(subject):
                discarded += 1
                continue

            accepted.append(
                {
                    "id": message_id,
                    "url": url,
                    "subject": subject,
                    "body": body,
                    "date": date,
                    "from": from_header,
                    "message_id": raw_message_id_header,
                    "references": references_header,
                }
            )

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
        filter_emails: bool = True,
        quote_handling: QuoteHandling = "none",
        group_threads: bool = False,
    ) -> tuple[dict, int]:
        """Download one mbox per calendar year in [sent_after, sent_before] and preprocess each.

        Returns a list of per-year result dicts, each containing:
            year, saved_path, host_path_hint, processed_path, processed_host_path_hint,
            total_emails, discarded_emails, preview_lines (and total_threads in thread mode)

        When group_threads is True, messages are threaded across the *whole*
        selected range (threads cross year boundaries), then each thread is
        bucketed into the year of its earliest message so the per-year file
        and selection flow keep working. Each JSONL record is one thread
        (or thread part) — see services.email_threading.
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
            preprocess_result = IetfImportService.preprocess_mbox_file(
                download_result["saved_path"], filter_emails=filter_emails, quote_handling=quote_handling
            )
            if group_threads:
                # Defer writing: threading runs across all years once downloads finish.
                return {
                    "year": year,
                    "saved_path": download_result["saved_path"],
                    "host_path_hint": download_result["host_path_hint"],
                    "emails": preprocess_result["emails"],
                    "discarded_emails": preprocess_result["discarded_emails"],
                }
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

        if group_threads:
            year_results = IetfImportService._build_threaded_year_results(year_result_map)
        else:
            year_results = [year_result_map[y] for y in sorted(year_result_map)]
        return {"years": year_results}, 200

    @staticmethod
    def _build_threaded_year_results(year_result_map: dict[int, dict]) -> list[dict]:
        """Thread pooled messages across all years, bucket threads by start year, write per-year files."""
        from services.email_threading import build_thread_records, group_into_threads

        pooled: list[dict] = []
        for year in sorted(year_result_map):
            for email_record in year_result_map[year].get("emails") or []:
                email_record["_year"] = year
                pooled.append(email_record)

        threads_by_year: dict[int, list[list[dict]]] = {}
        for thread in group_into_threads(pooled):
            threads_by_year.setdefault(thread[0]["_year"], []).append(thread)

        year_results: list[dict] = []
        for year in sorted(year_result_map):
            source = year_result_map[year]
            year_threads = threads_by_year.get(year, [])
            records = build_thread_records(year_threads)

            processed_path: str | None = None
            processed_host_path_hint: str | None = None
            if records and source.get("saved_path"):
                thread_source = Path(source["saved_path"])
                thread_source = thread_source.with_name(f"{thread_source.stem}-threads{thread_source.suffix}")
                processed_path, processed_host_path_hint = IetfImportService._write_preprocessed_email_records(
                    source_path=thread_source.as_posix(),
                    emails=records,
                )

            year_results.append(
                {
                    "year": year,
                    "saved_path": source.get("saved_path"),
                    "host_path_hint": source.get("host_path_hint"),
                    "processed_path": processed_path,
                    "processed_host_path_hint": processed_host_path_hint,
                    "total_emails": sum(len(thread) for thread in year_threads),
                    "total_threads": len(year_threads),
                    "discarded_emails": source.get("discarded_emails", 0),
                    "preview_lines": records[0]["body"].splitlines()[:10] if records else [],
                }
            )
        return year_results

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

    # IETF Secretariat/datatracker bot notices, plus chair-issued procedural
    # announcements (Last Call, WG-adoption summaries) that follow an equally
    # fixed template -- no human writes these unprompted, so matching the
    # prefix carries no risk of dropping real discussion. Matching only the
    # prefix (not "Re: ...") means a reply in the same thread -- where actual
    # technical review comments live -- is never caught by this. Deliberately
    # NOT included: "New Version Notification" and "minutes" -- both are also
    # used by humans announcing real content (e.g. an author's own summary of
    # what changed and why), so a subject match alone can't safely tell those
    # apart from the bot's auto-notifications.
    _ADMINISTRATIVE_SUBJECT_RE = re.compile(
        r"^\s*(?:\[[^\]]{1,60}\]\s*)*(?:"
        r"WG Action:|Document Action:|I-D Action:|Call for Papers|CFP\b|"
        r"(?:\d+\s*weeks?\s*)?Last Call\b|"
        r"Summary:\s*Call for"
        r")",
        re.IGNORECASE,
    )

    @staticmethod
    def _is_administrative_notice(subject: str) -> bool:
        """Heuristic: IETF Secretariat/datatracker bot notices and conference
        CFPs carry no design rationale, but their broad, formal vocabulary
        makes them prone to false-positive matches in discussion retrieval."""
        return bool(IetfImportService._ADMINISTRATIVE_SUBJECT_RE.match(subject or ""))

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
        # Overwrite any previous run's output for the same source: the caller
        # always uploads the path returned by the current run, so timestamped
        # copies only accumulated as dead files.
        destination = processed_dir / f"{source.stem}.jsonl"

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