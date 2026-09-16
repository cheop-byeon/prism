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

import ast
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
from bs4 import BeautifulSoup
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

    @staticmethod
    def download_rfcs_to_local(payload: IetfRfcImportPayload) -> tuple[dict, int]:
        """Download RFC text into local mounted storage and return local file
        metadata -- mirrors download_drafts_to_local: no FileService/account
        dependency, so the standalone decision-agent UI's Sources tab can pull
        a brand-new RFC's raw text without also needing to attach/index it.
        """

        sidecar_payload = IetfImportService._request_sidecar("/api/v1/import/rfcs", dict(payload))
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

    @staticmethod
    def resolve_rfc_number_for_draft(draft_name: str) -> str | None:
        """Live lookup of the RFC number a draft was published as, if any.

        Datatracker's draft-keyed document record always has rfc/rfc_number set
        to null (confirmed directly, even for long-published drafts), so the
        only lookup that actually works is the plain doc page's redirect:
        https://datatracker.ietf.org/doc/<draft_name>/ 302s to
        https://datatracker.ietf.org/doc/rfcNNNN/ once a draft is published,
        and just stays on the draft's own page otherwise. Returns None if
        unpublished or on any lookup failure.
        """
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get(f"https://datatracker.ietf.org/doc/{draft_name}/")
                if resp.status_code != 200:
                    return None
                match = re.search(r"/doc/rfc(\d+)/", str(resp.url))
                return match.group(1) if match else None
        except httpx.HTTPError:
            return None

    @staticmethod
    def resolve_wg_for_draft(draft_name: str) -> str | None:
        """Live lookup of a draft's owning WG acronym via IETF Datatracker
        (draft -> group -> acronym) -- not cached, matching the pattern used
        by every other live-fetch tool. Returns None on any lookup failure.
        """
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                doc_resp = client.get(
                    f"https://datatracker.ietf.org/api/v1/doc/document/{draft_name}/", params={"format": "json"}
                )
                if doc_resp.status_code != 200:
                    return None
                group_url = doc_resp.json().get("group")
                if not group_url:
                    return None
                group_resp = client.get(f"https://datatracker.ietf.org{group_url}", params={"format": "json"})
                if group_resp.status_code != 200:
                    return None
                return group_resp.json().get("acronym")
        except httpx.HTTPError:
            return None

    _RFC_WAS_DRAFT_RE = re.compile(r'Was\s*<a href="/doc/([a-z0-9-]+)/\d+/">')

    @staticmethod
    def resolve_draft_name_for_rfc(rfc_number: str) -> str | None:
        """Reverse of resolve_rfc_number_for_draft: given a published RFC,
        find the draft name it was before publication -- needed for an
        agent that discovers RFCs by number (via find_related_rfc_family,
        search_rfc_corpus_bm25, ...) but ultimately has to act in terms of
        draft names, since that's this project's whole identity key
        (register_local_source, sync_local_source, the corpus catalog).

        No API field carries this directly (confirmed: the RFC's own
        Datatracker document record has no draft-name field at all, and the
        docalias API that historically exposed this has been removed). The
        RFC's own Datatracker doc *page* HTML does show it, in a "Was
        draft-..." line -- confirmed live against RFC 4035, which links
        directly to "draft-ietf-dnsext-dnssec-protocol". Returns None if
        that line isn't present (the RFC was never a separate I-D, or the
        page layout doesn't match) rather than guessing.
        """
        rfc_id = rfc_number.strip().lower()
        if not rfc_id.startswith("rfc"):
            rfc_id = f"rfc{rfc_id}"
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get(f"https://datatracker.ietf.org/doc/{rfc_id}/")
                if resp.status_code != 200:
                    return None
        except httpx.HTTPError:
            return None
        match = IetfImportService._RFC_WAS_DRAFT_RE.search(resp.text)
        return match.group(1) if match else None

    # A brand-new draft added from the Sources tab has no Dify dataset (so
    # _discover_evolution_corpora can't find it by content signature) --
    # this is the minimal local record of "the user chose to track this one",
    # just the draft name itself. Everything else about it (WG, RFC number,
    # mail sync status, latest revision) is resolved live the same way the
    # real, dataset-backed corpora already are -- keeping this registry as
    # thin as possible avoids a second, divergent source of truth for facts
    # that are already fetched fresh elsewhere.
    _LOCAL_SOURCES_PATH = Path("/app/api/storage/ietf-downloads/local_sources.json")

    @staticmethod
    def list_local_source_names() -> list[str]:
        if not IetfImportService._LOCAL_SOURCES_PATH.is_file():
            return []
        try:
            data = json.loads(IetfImportService._LOCAL_SOURCES_PATH.read_text())
            return [str(name) for name in data] if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def register_local_source(draft_name: str) -> None:
        names = IetfImportService.list_local_source_names()
        if draft_name in names:
            return
        names.append(draft_name)
        IetfImportService._LOCAL_SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
        IetfImportService._LOCAL_SOURCES_PATH.write_text(json.dumps(names, indent=2))

    @staticmethod
    def get_local_download_status(draft_name: str, wg: str | None, rfc_number: str | None) -> dict:
        """What's actually already on disk for one local source, as opposed
        to what Datatracker/the sidecar report is available -- a local
        source's facets need to show real download state (has this already
        been pulled, or not) rather than always presenting a generic action
        button that looks identical whether or not anything's been fetched.
        """
        local_dir = Path("/app/api/storage/ietf-downloads")
        if not local_dir.is_dir():
            return {
                "revisions_downloaded": 0,
                "latest_revision_downloaded": None,
                "rfc_downloaded": False,
                "mail_downloaded": False,
                "mail_thread_downloaded": False,
            }

        revision_re = re.compile(rf"^{re.escape(draft_name)}-(\d+)\.txt$")
        revisions = sorted(
            int(match.group(1))
            for entry in local_dir.iterdir()
            if entry.is_file() and (match := revision_re.match(entry.name))
        )
        rfc_downloaded = bool(rfc_number and any(local_dir.glob(f"ietf-RFC{rfc_number}.*")))
        mail_downloaded = bool(wg and any(local_dir.glob(f"ietf-mailarchive-{wg}-*.mbox")))
        thread_dir = local_dir / "preprocessed"
        mail_thread_downloaded = bool(
            wg and thread_dir.is_dir() and any(thread_dir.glob(f"ietf-mailarchive-{wg}-*-threads.jsonl"))
        )
        return {
            "revisions_downloaded": len(revisions),
            "latest_revision_downloaded": f"{revisions[-1]:02d}" if revisions else None,
            "rfc_downloaded": rfc_downloaded,
            "mail_downloaded": mail_downloaded,
            "mail_thread_downloaded": mail_thread_downloaded,
        }

    @staticmethod
    def download_all_draft_revisions_to_local(draft_name: str, on_event=None) -> dict:
        """Sync every existing revision of one draft's plain text to local
        storage, plus its published RFC (in whatever format the sidecar
        actually reports for it) if it has one -- the bulk counterpart to
        download_drafts_to_local's one-revision-at-a-time flow. Idempotent:
        a revision or the RFC already on disk is skipped rather than
        re-fetched, so this is cheap to call repeatedly (a first call pulls
        everything; a later call only pulls what's newly appeared -- a new
        revision, or the RFC once the draft finally gets published). Also
        registers the draft as a tracked local source (see
        register_local_source) so it shows up as its own unit in the
        Sources tab from now on, the same way an already dataset-backed
        draft does.

        Fetches revisions directly from the IETF archive (the same approach
        _fetch_all_revisions already uses for generate_draft_evolution)
        rather than one sidecar round-trip per revision -- N sequential sidecar
        calls for a 30+ revision draft would be far slower for no benefit,
        since the archive serves the exact same plain text the sidecar would
        relay anyway.
        """

        def _emit(message: str) -> None:
            if on_event:
                on_event({"stage": "downloading", "message": message})

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    f"https://datatracker.ietf.org/api/v1/doc/document/{draft_name}/", params={"format": "json"}
                )
                resp.raise_for_status()
                max_rev = int(resp.json().get("rev", "0"))
        except Exception as exc:
            return {"error": f"Could not retrieve revision info for '{draft_name}': {exc}"}

        IetfImportService.register_local_source(draft_name)

        local_dir = Path("/app/api/storage/ietf-downloads")
        local_dir.mkdir(parents=True, exist_ok=True)

        def _fetch_one(rev: str) -> dict:
            destination = local_dir / f"{draft_name}-{rev}.txt"
            if destination.exists():
                return {"rev": rev, "ok": True, "skipped": True, "filename": destination.name}
            url = f"https://www.ietf.org/archive/id/{draft_name}-{rev}.txt"
            try:
                with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                    resp = client.get(url)
                if resp.status_code != 200:
                    return {"rev": rev, "ok": False, "error": f"HTTP {resp.status_code}"}
                destination.write_bytes(resp.content)
                return {"rev": rev, "ok": True, "filename": destination.name, "size": destination.stat().st_size}
            except httpx.HTTPError as exc:
                return {"rev": rev, "ok": False, "error": str(exc)}

        revisions = [f"{i:02d}" for i in range(max_rev + 1)]
        results = []
        for i, rev in enumerate(revisions, start=1):
            _emit(f"{draft_name}-{rev} ({i}/{len(revisions)})")
            results.append(_fetch_one(rev))

        rfc_number = IetfImportService.resolve_rfc_number_for_draft(draft_name)
        rfc_result = None
        if rfc_number:
            rfc_id = f"RFC{rfc_number}"
            existing_rfc = next(iter(local_dir.glob(f"ietf-{rfc_id}.*")), None)
            if existing_rfc:
                rfc_result = {"ok": True, "skipped": True, "filename": existing_rfc.name}
            else:
                _emit(f"{rfc_id} (published RFC)")
                try:
                    formats, _status = IetfImportService.get_rfc_formats(rfc_id=rfc_id)
                    available = {f["key"] for f in formats.get("formats", [])}
                    if not available:
                        rfc_result = {"ok": False, "error": "no downloadable formats reported"}
                    else:
                        fmt = next((f for f in ("txt", "html") if f in available), next(iter(available)))
                        file_result, _status = IetfImportService.download_rfcs_to_local(
                            {
                                "rfc_ids": [rfc_id],
                                "since": None,
                                "until": None,
                                "stream": None,
                                "wg": None,
                                "status": None,
                                "limit": 20,
                                "format": fmt,
                            }
                        )
                        rfc_result = {"ok": True, **file_result}
                except Exception as exc:
                    rfc_result = {"ok": False, "error": str(exc)}

        return {"draft_name": draft_name, "revisions": results, "rfc_number": rfc_number, "rfc": rfc_result}

    @staticmethod
    def sync_local_source(draft_name: str, on_event=None) -> dict:
        """The single "Sync" action for a locally-tracked source: everything
        raw it could have -- draft revisions, its RFC once published, and
        its WG's mail (both raw and threaded) -- pulled current in one call.
        Idempotent/incremental on the draft+RFC side (see
        download_all_draft_revisions_to_local); the mail side re-runs the
        full mbox+threading pass each time (already fast in practice -- a
        few seconds to under a minute even for a large, decades-old list --
        so a from-scratch incremental-fetch/merge mechanism isn't worth the
        complexity it would add on top of that).
        """
        from datetime import UTC, datetime

        def _emit(message: str) -> None:
            if on_event:
                on_event({"stage": "syncing", "message": message})

        draft_result = IetfImportService.download_all_draft_revisions_to_local(draft_name, on_event=on_event)
        wg = IetfImportService.resolve_wg_for_draft(draft_name)

        mail_result = None
        if wg:
            _emit(f"Syncing mail for {wg}…")
            current_year = datetime.now(tz=UTC).year
            try:
                thread_result, _status = IetfImportService.download_and_preprocess_years(
                    mailing_list=wg,
                    sent_after="1985-01-01T00:00:00",
                    sent_before=f"{current_year}-12-31T23:59:59",
                    group_threads=True,
                )
                mail_result = {"ok": True, **thread_result}
            except Exception as exc:
                mail_result = {"ok": False, "error": str(exc)}

        return {**draft_result, "wg": wg, "mail": mail_result}

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

    _RFC_NAME_RE = re.compile(r"^rfc\d+$")

    @staticmethod
    def find_related_rfc_family(rfc_number: str) -> dict:
        """Find the topically-related "family" of RFCs around one RFC --
        beyond structural obsoletes/updates relations, some topics (DNSSEC
        is the motivating example: RFC 9364 is a roadmap tying together a
        couple dozen RFCs that neither obsolete nor update each other) are
        actually explained across a cluster of RFCs connected only by
        citation. Datatracker has no "topic cluster" or "roadmap" concept
        to query directly, so this is a heuristic pipeline over real
        citation and keyword data, not a curated taxonomy.

        Bidirectional, because a one-directional (inbound-only) version was
        tried first and found wanting live: RFC 9364 -- a roadmap published
        in 2026 -- had barely been cited by anything yet, so "who cites
        this RFC" returned almost nothing, even though RFC 9364's own 37
        outbound references (to RFC 4033/4034/4035 and the rest of the
        DNSSEC family) ARE the family. The two directions need different
        filters, not the same one:

          - Inbound (who cites this RFC): noisy and large for an old,
            foundational RFC (513 for RFC 4035) -- kept only if the citing
            document is itself an RFC, BCP/Informational-track (an
            overview/survey, not a Standards-track spec that merely builds
            on this one), and its title contains this RFC's own most
            specific keyword.
          - Outbound (what this RFC cites): a document's own reference list
            is already curated by its authors, so no std_level filter is
            applied here (RFC 9364 normatively cites RFC 4033/4034/4035,
            all Standards-track -- filtering those out would gut the exact
            case this method exists for). Still filtered by the topic
            keyword, to drop ubiquitous boilerplate references every RFC
            makes (RFC 2119's "MUST/SHOULD" keywords, base RFC numbers,
            etc.) that have nothing to do with this RFC's actual topic.

        The topic keyword itself is picked more carefully than "shortest
        tag": a keyword that is a substring of another of this RFC's own
        keywords is treated as the generic parent term and excluded first
        (RFC 9364 is tagged "DNS", "DNSSEC", "DNS Security Extensions" --
        "DNS" is a substring of both others and would match nearly any
        DNS-related RFC if used directly), then the shortest of what's left
        is used (correctly resolving to "DNSSEC", not "DNS").
        """
        number = re.sub(r"[^0-9]", "", rfc_number)
        if not number:
            return {"error": f"{rfc_number!r} doesn't look like an RFC number"}
        name = f"rfc{number}"

        try:
            with httpx.Client(timeout=15.0) as client:
                origin_resp = client.get(
                    f"https://datatracker.ietf.org/api/v1/doc/document/{name}/", params={"format": "json"}
                )
                origin_resp.raise_for_status()
                origin = origin_resp.json()
        except httpx.HTTPError as exc:
            return {"error": f"Could not fetch {name}: {exc}"}

        # Datatracker serializes this list field as the str() of a Python
        # list (confirmed live: "['DNSSEC', 'DNS Security Extensions', 'DNS']"
        # as an actual JSON string value, not a JSON array) -- literal_eval
        # is the correct, safe parse for that, not a string-replace hack.
        try:
            keywords = [k for k in ast.literal_eval(origin.get("keywords") or "[]") if k]
        except (ValueError, SyntaxError):
            keywords = []
        specific_keywords = [k for k in keywords if not any(k != other and k in other for other in keywords)]
        topic_keyword = min(specific_keywords or keywords, key=len, default=None)

        # No keyword means no reliable topic signal -- returning early here
        # (before any citation fetch) both avoids the wasted requests and
        # fixes a real bug: _keyword_matches used to treat "no keyword" as
        # "match everything", so an RFC with no Datatracker keywords (RFC
        # 5435, confirmed live) got an unfiltered dump of every citation as
        # its "family" while well-keyworded RFCs got a properly narrow
        # result -- an inconsistency a user caught by comparing RFCs side
        # by side, not something that showed up as an error.
        if not topic_keyword:
            return {"rfc": name.upper(), "topic_keyword": topic_keyword, "family": []}

        # Capped, not unbounded: RFC 2119 ("MUST/SHOULD/MAY") is cited by
        # tens of thousands of documents -- confirmed live that fetching
        # its full citation list hangs this call for minutes with zero
        # progress feedback (this endpoint isn't SSE-streamed). Any RFC
        # that's this broadly cited is a poor "family" candidate anyway --
        # the signal we actually want (a tight topical cluster) is exactly
        # what's absent when a single RFC is cited by half the corpus.
        # 8 pages (800 citations) per direction/relationship-slug, up to 4
        # combinations -- worst case ~15-25s instead of the ~58s a 20-page
        # cap measured live for RFC 2119. This endpoint isn't SSE-streamed
        # (unlike the global graph), so whatever this takes is a blank
        # "Loading..." with no progress -- the priority is a bounded wait,
        # not a bigger sample for the rare RFC that even hits the cap.
        _MAX_RELATED_PAGES = 8

        def _fetch_related_names(client: httpx.Client, field: str, relationship_slug: str) -> tuple[list[str], bool]:
            names: list[str] = []
            other_field = "target" if field == "source__name" else "source"
            url = "https://datatracker.ietf.org/api/v1/doc/relateddocument/"
            params: dict | None = {
                field: name,
                "relationship__slug": relationship_slug,
                "format": "json",
                "limit": 100,
            }
            pages = 0
            truncated = False
            while url:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                for obj in data.get("objects", []):
                    uri: str = obj.get(other_field, "") or ""
                    doc_name = uri.rstrip("/").split("/")[-1]
                    if doc_name:
                        names.append(doc_name)
                pages += 1
                next_page = data.get("meta", {}).get("next")
                if next_page and pages >= _MAX_RELATED_PAGES:
                    truncated = True
                    break
                url = f"https://datatracker.ietf.org{next_page}" if next_page else None
                params = None
            return names, truncated

        try:
            with httpx.Client(timeout=15.0) as client:
                cited_by_1, trunc_1 = _fetch_related_names(client, "target__name", "refinfo")
                cited_by_2, trunc_2 = _fetch_related_names(client, "target__name", "refnorm")
                cites_1, trunc_3 = _fetch_related_names(client, "source__name", "refinfo")
                cites_2, trunc_4 = _fetch_related_names(client, "source__name", "refnorm")
                cited_by = set(cited_by_1) | set(cited_by_2)
                cites = set(cites_1) | set(cites_2)
                truncated = trunc_1 or trunc_2 or trunc_3 or trunc_4
        except httpx.HTTPError as exc:
            return {"error": f"Could not fetch citing documents for {name}: {exc}"}

        cited_by_candidates = sorted(n for n in cited_by if IetfImportService._RFC_NAME_RE.fullmatch(n))
        cites_candidates = sorted(n for n in cites if IetfImportService._RFC_NAME_RE.fullmatch(n))
        all_candidates = sorted(set(cited_by_candidates) | set(cites_candidates))
        if not all_candidates:
            return {"rfc": name.upper(), "topic_keyword": topic_keyword, "family": []}

        metas: dict[str, dict] = {}
        try:
            with httpx.Client(timeout=20.0) as client:
                for i in range(0, len(all_candidates), 50):
                    chunk = all_candidates[i : i + 50]
                    resp = client.get(
                        "https://datatracker.ietf.org/api/v1/doc/document/",
                        params={"name__in": ",".join(chunk), "format": "json", "limit": len(chunk)},
                    )
                    resp.raise_for_status()
                    for obj in resp.json().get("objects", []):
                        metas[obj["name"]] = obj
        except httpx.HTTPError as exc:
            return {"error": f"Could not fetch candidate metadata: {exc}"}

        def _keyword_matches(meta: dict, title: str) -> bool:
            # Title-substring alone missed the exact case this method exists
            # for: RFC 9364's topic_keyword resolves to "DNSSEC", but RFC
            # 4033/4034/4035's titles spell it out as "DNS Security
            # (Introduction/Extensions)" -- no literal "DNSSEC" substring --
            # so all three got silently dropped from RFC 9364's own family
            # (confirmed live before this fix). Datatracker's per-document
            # keywords are a curated tag list, not prose, and do carry the
            # abbreviated form (confirmed live: all three list "dnssec"),
            # so they're checked first; the title check stays as a fallback
            # for candidates with no usable keywords, purely additive to
            # what already matched -- this can only gain true positives,
            # never lose one that matched by title before.
            try:
                candidate_keywords = [k for k in ast.literal_eval(meta.get("keywords") or "[]") if k]
            except (ValueError, SyntaxError):
                candidate_keywords = []
            topic_lower = topic_keyword.lower()
            if any(topic_lower in kw.lower() for kw in candidate_keywords):
                return True
            return topic_lower in title.lower()

        family: dict[str, dict] = {}
        for candidate in cited_by_candidates:
            meta = metas.get(candidate, {})
            level = (meta.get("std_level") or "").rstrip("/").split("/")[-1]
            title = meta.get("title") or ""
            if level in ("bcp", "inf") and _keyword_matches(meta, title):
                family[candidate] = {
                    "rfc": candidate.upper(),
                    "title": title,
                    "std_level": level,
                    "direction": "cited_by",
                }
        for candidate in cites_candidates:
            if candidate in family:
                continue
            meta = metas.get(candidate, {})
            title = meta.get("title") or ""
            if _keyword_matches(meta, title):
                family[candidate] = {
                    "rfc": candidate.upper(),
                    "title": title,
                    "std_level": (meta.get("std_level") or "").rstrip("/").split("/")[-1],
                    "direction": "cites",
                }

        result = {"rfc": name.upper(), "topic_keyword": topic_keyword, "family": list(family.values())}
        if truncated:
            result["note"] = (
                "This RFC is cited by an unusually large number of documents; "
                "family is based on a partial (capped) sample of its citations."
            )
        return result

    @staticmethod
    def get_iesg_ballot_comments(draft_name: str) -> dict:
        """Fetch IESG DISCUSS/comment text left on a draft during its
        standards-track review -- a real, distinct venue from the mailing
        list and meeting minutes where a decision can get argued out (an AD
        raising a specific technical objection, the author's response, the
        objection being cleared), tied to the exact revision it applied to.

        Datatracker's plain docevent listing only shows that a position
        changed ("New position, Discuss, has been recorded for X"), not the
        DISCUSS/comment text itself -- that lives on the ballotpositiondocevent
        sub-resource (Django's polymorphic event model exposed as its own
        Tastypie resource), which is what this actually queries.

        Only returns entries with real discuss/comment text -- a pure
        "No Objection, no comment" position record is a vote, not evidence.
        """
        try:
            with httpx.Client(timeout=15.0) as client:
                events: list[dict] = []
                url = "https://datatracker.ietf.org/api/v1/doc/ballotpositiondocevent/"
                params: dict | None = {"doc__name": draft_name, "format": "json", "limit": 100}
                while url:
                    resp = client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    events.extend(data.get("objects", []))
                    next_page = data.get("meta", {}).get("next")
                    url = f"https://datatracker.ietf.org{next_page}" if next_page else None
                    params = None

                with_text = [e for e in events if (e.get("discuss") or "").strip() or (e.get("comment") or "").strip()]
                balloter_ids = sorted(
                    {e["balloter"].rstrip("/").split("/")[-1] for e in with_text if e.get("balloter")}
                )
                names: dict[str, str] = {}
                if balloter_ids:
                    person_resp = client.get(
                        "https://datatracker.ietf.org/api/v1/person/person/",
                        params={"id__in": ",".join(balloter_ids), "format": "json", "limit": len(balloter_ids)},
                    )
                    person_resp.raise_for_status()
                    names = {str(p["id"]): p["name"] for p in person_resp.json().get("objects", [])}
        except httpx.HTTPError as exc:
            return {"error": str(exc)}

        comments = []
        for event in with_text:
            balloter_id = event["balloter"].rstrip("/").split("/")[-1] if event.get("balloter") else None
            comments.append(
                {
                    "balloter": names.get(balloter_id, "unknown"),
                    "position": (event.get("pos") or "").rstrip("/").split("/")[-1],
                    "revision": event.get("rev", ""),
                    "time": event.get("time", ""),
                    "discuss": (event.get("discuss") or "").strip(),
                    "comment": (event.get("comment") or "").strip(),
                }
            )
        comments.sort(key=operator.itemgetter("time"))
        return {"draft_name": draft_name, "comments": comments}

    @staticmethod
    def get_rfc_errata(rfc_number: str) -> list[dict]:
        """Fetch published errata for an RFC -- corrections/clarifications
        that sometimes explain exactly why later text reads oddly, or flag
        that the original wording was outright wrong. Node detail for one
        RFC, not a relationship between two, so this isn't part of
        build_rfc_relationship_graph's edges -- it's meant to be fetched on
        demand when a graph node is clicked.

        RFC Editor's errata.json API ignores its own rfc/doc-id query
        params entirely and always returns the full, 10MB+, unfiltered
        database (confirmed live) -- there's no working filtered JSON
        endpoint. The classic errata_search.php page (redirects to
        errata.rfc-editor.org) IS server-rendered per-RFC HTML though, so
        that's what gets scraped here instead.
        """
        number = re.sub(r"[^0-9]", "", rfc_number)
        if not number:
            return []
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get("https://www.rfc-editor.org/errata_search.php", params={"rfc": number})
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("RFC errata fetch failed for RFC%s: %s", number, exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        errata: list[dict] = []
        for heading in soup.find_all("h4"):
            if "Errata-ID" not in heading.get_text():
                continue
            link = heading.find("a")
            card = heading.find_parent("div", class_="card")
            if not card:
                continue
            body = card.find("div", class_="card-body", recursive=False)
            if not body:
                continue
            dl = body.find("dl")
            fields = {}
            if dl:
                for dt in dl.find_all("dt"):
                    key = dt.get_text(strip=True).rstrip(":")
                    dd = dt.find_next_sibling("dd")
                    fields[key] = dd.get_text(strip=True) if dd else ""
            texts = {}
            for para in body.find_all("p"):
                label = para.get_text(strip=True).rstrip(":")
                pre = para.find_next_sibling("pre")
                if pre:
                    texts[label] = pre.get_text().strip()
            errata.append(
                {
                    "id": link.get_text(strip=True) if link else "",
                    "status": fields.get("Status", ""),
                    "type": fields.get("Type", ""),
                    "reported_by": fields.get("Reported By", ""),
                    "date_reported": fields.get("Date Reported", ""),
                    "texts": texts,
                }
            )
        return errata

    @staticmethod
    def _structural_neighbors(rfc_number: str) -> dict:
        """Obsoletes/updates neighbors only for one RFC -- no citation
        lookup. This is the cheap half of build_rfc_relationship_graph,
        factored out so build_focus_neighborhood can use it as a fast
        expansion primitive for hop 2+ (see there for why: running the
        citation-based family lookup for every node in a multi-hop BFS,
        not just the focus node itself, is what made hop-2/3 focus
        requests take minutes in practice, confirmed live).
        """
        relations = IetfImportService.get_rfc_relations(rfc_number)
        if relations.get("error"):
            return {"error": relations["error"]}
        center = relations["rfc"]

        edges = []
        for target in relations["obsoletes"]:
            edges.append({"source": center, "target": target, "kind": "obsoletes"})
        for source in relations["obsoleted_by"]:
            edges.append({"source": source, "target": center, "kind": "obsoletes"})
        for target in relations["updates"]:
            edges.append({"source": center, "target": target, "kind": "updates"})
        for source in relations["updated_by"]:
            edges.append({"source": source, "target": center, "kind": "updates"})

        # obsoletes/updates can (rarely) point at a not-yet-published draft
        # name rather than an RFC -- this graph only ever shows published
        # RFCs, so anything that isn't "RFC<digits>" is dropped here.
        node_names = sorted({name for e in edges for name in (e["source"], e["target"])} | {center}, key=str)
        node_names = [n for n in node_names if IetfImportService._RFC_NAME_RE.fullmatch(n.lower())]

        try:
            nodes = IetfImportService._batch_fetch_rfc_nodes(node_names)
        except httpx.HTTPError as exc:
            return {"error": f"Could not fetch node metadata: {exc}"}

        valid_ids = {n["id"] for n in nodes}
        edges = IetfImportService._dedupe_edges(edges, valid_ids)
        return {"center": center, "nodes": nodes, "edges": edges}

    @staticmethod
    def build_rfc_relationship_graph(rfc_number: str) -> dict:
        """Assemble one consolidated {nodes, edges} graph for an RFC -- the
        data foundation for the RFC Graph tab's table and interactive graph.

        Combines _structural_neighbors (obsoletes/updates), find_related_rfc_family
        (citation graph + exact keyword match -- "Citation Match" in the UI),
        and, when the whole-corpus BM25 index is already built, a "Topic
        Match" lookup (RfcCorpusIndexService.search_bm25) into a single
        deduplicated node/edge list, since a graph UI needs one coherent
        structure, not several separately-shaped tool outputs. Topic Match
        exists because Citation Match has a real, confirmed-live blind spot:
        it requires this RFC's own topic keyword to appear verbatim in a
        candidate's title, which fails whenever that keyword is a narrow
        internal acronym (RFC 6378's "PSC" never appears in its real
        relatives' titles, which all say "Linear Protection" instead) --
        BM25's term overlap over the full title+abstract text catches
        exactly those. Skipped (not lazily built) if the BM25 index isn't
        already there, since building it is a multi-minute one-time cost
        that shouldn't silently happen inside an ordinary graph fetch -- see
        build_rfc_corpus_index.py. Each node carries its resolved
        title/std_level/wg (wg is for visual grouping only -- drawing an
        edge between every same-WG pair would just be a dense,
        low-information clique, so it's a node attribute instead). Errata is
        deliberately NOT part of this graph: it's node detail (one RFC's own
        history), not a relationship between two RFCs -- fetched separately,
        on demand, when a node is clicked.
        """
        structural = IetfImportService._structural_neighbors(rfc_number)
        if structural.get("error"):
            return structural
        family = IetfImportService.find_related_rfc_family(rfc_number)
        center = structural["center"]

        edges = list(structural["edges"])
        for entry in family.get("family", []):
            if entry.get("direction") == "cited_by":
                edges.append({"source": entry["rfc"], "target": center, "kind": "family"})
            else:
                edges.append({"source": center, "target": entry["rfc"], "kind": "family"})

        from services.rfc_corpus_index_service import RfcCorpusIndexService

        if RfcCorpusIndexService.is_title_abstract_corpus_built():
            topic_matches = RfcCorpusIndexService.search_bm25(center, scope="title_abstract", top_k=10)
            for r in topic_matches.get("results", []):
                if r["rfc"] != center:
                    edges.append({"source": center, "target": r["rfc"], "kind": "topic_bm25"})

        node_names = sorted(
            {name for e in edges for name in (e["source"], e["target"])} | {center},
            key=str,
        )
        node_names = [n for n in node_names if IetfImportService._RFC_NAME_RE.fullmatch(n.lower())]

        try:
            nodes = IetfImportService._batch_fetch_rfc_nodes(node_names)
        except httpx.HTTPError as exc:
            return {"error": f"Could not fetch node metadata: {exc}"}
        for node in nodes:
            node["is_center"] = node["id"] == center

        valid_ids = {n["id"] for n in nodes}
        edges = IetfImportService._dedupe_edges(edges, valid_ids)

        return {"center": center, "nodes": nodes, "edges": edges}

    @staticmethod
    def build_focus_neighborhood(rfc_number: str, max_hops: int = 2, on_event=None) -> dict:
        """BFS outward from one RFC by obsoletes/updates/Citation-Match/
        Topic-Match edges, up to max_hops rings out -- the data behind the
        RFC Graph tab's Focus mode (as opposed to Overview mode's whole-
        corpus structural graph, which deliberately excludes both since
        neither can be fetched corpus-wide).

        Only the focus RFC itself is expanded with the full build_rfc_relationship_graph
        (obsoletes/updates + Citation Match + Topic Match) -- every node
        beyond that uses the cheap _structural_neighbors (obsoletes/updates
        only). Confirmed live that using the full lookup at every hop made a
        2-hop expansion take 2+ minutes and still not finish:
        find_related_rfc_family's citation fetch is the expensive part, and
        running it for every node in a BFS multiplies that cost by the
        frontier size at each hop. Neither Citation Match nor Topic Match is
        well-defined relative to a neighbor's neighbor anyway -- both mean
        "shares this RFC's topic", which only makes sense relative to the
        one RFC the user actually asked about.

        Capped at a total expansion count (not just per hop) as a second
        safety net against fan-out at hop 3. Every node gets tagged with
        the hop distance it was first reached at (0 = the focus RFC itself),
        which is what lets the UI size/color nodes by proximity instead of
        the flat "expanded or not" look the old per-node click-to-expand had.
        """
        # 80, not the original 40 -- measured live that a structural-only
        # (hop 2+) expansion is cheap, ~1s each, so this mostly costs time
        # only for the single full (citation-aware) hop-1 expansion of the
        # center. A well-connected hub RFC (RFC 4035: 35 hop-1 + 46 hop-2
        # neighbors) can still exhaust this within 2 hops -- that's a
        # legitimate truncation of an unusually dense neighborhood, not a
        # bug, and is reported via the "truncated" flag.
        _MAX_EXPANSIONS = 80

        def _emit(message: str) -> None:
            if on_event:
                on_event({"stage": "focusing", "message": message})

        digits = re.sub(r"[^0-9]", "", rfc_number)
        if not digits:
            return {"error": f"{rfc_number!r} doesn't look like an RFC number"}
        center = f"RFC{digits}"

        all_nodes: dict[str, dict] = {}
        all_edges: dict[tuple[str, str, str], dict] = {}
        hop_of: dict[str, int] = {center: 0}
        frontier = [center]
        expansions = 0
        truncated = False

        for hop in range(1, max_hops + 1):
            next_frontier: list[str] = []
            for node_id in frontier:
                if expansions >= _MAX_EXPANSIONS:
                    truncated = True
                    break
                expansions += 1
                _emit(f"Hop {hop}: expanding {node_id}… ({expansions}/{_MAX_EXPANSIONS})")
                if node_id == center:
                    result = IetfImportService.build_rfc_relationship_graph(node_id)
                else:
                    result = IetfImportService._structural_neighbors(node_id)
                if result.get("error"):
                    continue  # one bad node shouldn't fail the whole neighborhood
                for n in result.get("nodes", []):
                    all_nodes.setdefault(n["id"], n)
                    if n["id"] not in hop_of:
                        hop_of[n["id"]] = hop
                        next_frontier.append(n["id"])
                for e in result.get("edges", []):
                    all_edges[(e["source"], e["target"], e["kind"])] = e
            if truncated:
                break
            frontier = next_frontier
            if not frontier:
                break

        if center not in all_nodes:
            return {"error": f"Could not resolve {center}"}

        nodes = [{**n, "hop": hop_of.get(n["id"], max_hops + 1)} for n in all_nodes.values()]
        valid_ids = set(all_nodes)
        edges = IetfImportService._dedupe_edges(list(all_edges.values()), valid_ids)

        result = {"center": center, "nodes": nodes, "edges": edges}
        if truncated:
            result["truncated"] = True
        return result

    @staticmethod
    def _batch_fetch_rfc_nodes(names: list[str], on_event=None) -> list[dict]:
        """Batch-fetch {id, title, std_level, wg} for a list of RFC names,
        via name__in chunks (not one lookup per name) -- shared by every
        RFC-graph builder in this file (single-RFC neighborhood and the
        whole-corpus structural graph alike) so the metadata/WG-resolution
        logic exists in exactly one place. on_event is optional since the
        single-RFC callers finish in a couple of chunks; the whole-corpus
        caller (~80 chunks) is the one that actually needs progress.
        """
        metas: dict[str, dict] = {}
        with httpx.Client(timeout=30.0) as client:
            lookup = [n.lower() for n in names]
            for i in range(0, len(lookup), 50):
                chunk = lookup[i : i + 50]
                resp = client.get(
                    "https://datatracker.ietf.org/api/v1/doc/document/",
                    params={"name__in": ",".join(chunk), "format": "json", "limit": len(chunk)},
                )
                resp.raise_for_status()
                for obj in resp.json().get("objects", []):
                    metas[obj["name"].upper()] = obj
                if on_event:
                    on_event(
                        {
                            "stage": "building",
                            "message": f"Resolving RFC metadata… ({min(i + 50, len(lookup))}/{len(lookup)})",
                        }
                    )

            wg_ids = sorted({meta["group"].rstrip("/").split("/")[-1] for meta in metas.values() if meta.get("group")})
            wg_names: dict[str, str] = {}
            if wg_ids:
                for i in range(0, len(wg_ids), 50):
                    chunk = wg_ids[i : i + 50]
                    group_resp = client.get(
                        "https://datatracker.ietf.org/api/v1/group/group/",
                        params={"id__in": ",".join(chunk), "format": "json", "limit": len(chunk)},
                    )
                    group_resp.raise_for_status()
                    # Datatracker uses the literal group acronym "none" for
                    # individual submissions / no-WG documents -- normalized
                    # to empty here so the UI doesn't render it as a real WG.
                    wg_names.update(
                        {
                            str(g["id"]): (g["acronym"] if g["acronym"] != "none" else "")
                            for g in group_resp.json().get("objects", [])
                        }
                    )

        nodes = []
        for name in sorted(names, key=str.upper):
            meta = metas.get(name.upper(), {})
            wg_id = meta["group"].rstrip("/").split("/")[-1] if meta.get("group") else None
            nodes.append(
                {
                    "id": name.upper(),
                    "title": meta.get("title", ""),
                    "std_level": (meta.get("std_level") or "").rstrip("/").split("/")[-1],
                    "wg": wg_names.get(wg_id, "") if wg_id else "",
                }
            )
        return nodes

    @staticmethod
    def _dedupe_edges(edges: list[dict], valid_ids: set[str]) -> list[dict]:
        seen: set[tuple[str, str, str]] = set()
        deduped = []
        for edge in edges:
            if edge["source"] not in valid_ids or edge["target"] not in valid_ids:
                continue
            key = (edge["source"], edge["target"], edge["kind"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(edge)
        return deduped

    @staticmethod
    def get_global_structural_graph(on_event=None) -> dict:
        """Build the whole-corpus obsoletes/updates graph -- every RFC that
        obsoletes or updates (or is obsoleted/updated by) another one, in
        one graph. Unlike the citation graph (513 citations for RFC 4035
        ALONE, tens of thousands corpus-wide -- not fetchable in bulk),
        obsoletes/updates relations are rare enough to fetch completely:
        1,543 obsoletes + 2,157 updates relations exist in the entire
        system (confirmed live) -- but resolving metadata for the ~3,900
        RFCs those relations touch takes ~80 more batched requests, and the
        whole build measured at ~2 minutes live. Definitely not a per-request
        cost -- see serve_ui.py's module-level cache -- and on_event (same
        {"stage", "message"} shape as ask()/decision_thread()) lets a
        caller stream progress instead of leaving the UI looking hung for
        two minutes.

        This is the real "overall graph" -- deliberately not the citation
        family (still available per-node, on demand, as an enrichment),
        since that genuinely can't be fetched whole. RFCs with no
        obsoletes/updates relation at all are excluded rather than added
        as disconnected dots; they'd add clutter without adding structure.
        """

        def _emit(message: str) -> None:
            if on_event:
                on_event({"stage": "building", "message": message})

        def _fetch_all(client: httpx.Client, relationship_slug: str, label: str) -> list[tuple[str, str]]:
            pairs: list[tuple[str, str]] = []
            url = "https://datatracker.ietf.org/api/v1/doc/relateddocument/"
            params: dict | None = {"relationship__slug": relationship_slug, "format": "json", "limit": 100}
            while url:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                for obj in data.get("objects", []):
                    source = (obj.get("source") or "").rstrip("/").split("/")[-1]
                    target = (obj.get("target") or "").rstrip("/").split("/")[-1]
                    if source and target:
                        pairs.append((source, target))
                total = data.get("meta", {}).get("total_count", len(pairs))
                _emit(f"Fetching {label} relations… ({len(pairs)}/{total})")
                next_page = data.get("meta", {}).get("next")
                url = f"https://datatracker.ietf.org{next_page}" if next_page else None
                params = None
            return pairs

        try:
            with httpx.Client(timeout=30.0) as client:
                obs_pairs = _fetch_all(client, "obs", "obsoletes")
                updates_pairs = _fetch_all(client, "updates", "updates")
        except httpx.HTTPError as exc:
            return {"error": f"Could not fetch global relations: {exc}"}

        edges = [{"source": s, "target": t, "kind": "obsoletes"} for s, t in obs_pairs]
        edges += [{"source": s, "target": t, "kind": "updates"} for s, t in updates_pairs]
        # Same rule as the single-RFC graph: only published RFCs, no drafts.
        edges = [
            e
            for e in edges
            if IetfImportService._RFC_NAME_RE.fullmatch(e["source"].lower())
            and IetfImportService._RFC_NAME_RE.fullmatch(e["target"].lower())
        ]

        node_names = sorted({n for e in edges for n in (e["source"], e["target"])}, key=str.upper)
        if not node_names:
            return {"nodes": [], "edges": []}

        _emit(f"Resolving metadata for {len(node_names)} RFCs…")
        try:
            nodes = IetfImportService._batch_fetch_rfc_nodes(node_names, on_event=on_event)
        except httpx.HTTPError as exc:
            return {"error": f"Could not fetch node metadata: {exc}"}

        valid_ids = {n["id"] for n in nodes}
        edges = [{"source": e["source"].upper(), "target": e["target"].upper(), "kind": e["kind"]} for e in edges]
        edges = IetfImportService._dedupe_edges(edges, valid_ids)

        return {"nodes": nodes, "edges": edges}

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
            archived_at = IetfImportService._decode_header(msg.get("Archived-At", "")).strip()
            url = archived_at.strip("<>").strip()
            if not url:
                raw_mid = IetfImportService._decode_header(msg.get("Message-ID", "")).strip().strip("<>")
                message_id = raw_mid or str(len(accepted))
                url = f"mid:{message_id}"
            else:
                message_id = url.rstrip("/").split("/")[-1]

            # ---- Subject (RFC 2047 decoded) ----
            subject = IetfImportService._decode_header(msg.get("Subject", ""))

            # ---- Date ----
            date = IetfImportService._decode_header(msg.get("Date", ""))

            # ---- Threading headers (used by group_into_threads) ----
            # Every header read here goes through _decode_header first, not just
            # .strip() -- email.policy's header factory can hand back a legacy
            # email.header.Header object (not a str) for a header it can't parse
            # into its own structured type, and Header has no .strip() (confirmed
            # live against a real dnsop mail archive message). _decode_header
            # already calls email.header.decode_header(), which explicitly
            # accepts a Header instance as well as a plain string.
            from_header = IetfImportService._decode_header(msg.get("From", "")).strip()
            raw_message_id_header = IetfImportService._decode_header(msg.get("Message-ID") or "").strip()
            references_header = " ".join(
                part
                for part in (
                    IetfImportService._decode_header(msg.get("References") or "").strip(),
                    IetfImportService._decode_header(msg.get("In-Reply-To") or "").strip(),
                )
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
        """Decode an RFC 2047-encoded header value to plain Unicode.

        `value` isn't always already a plain str: Message.get() can hand
        back a legacy email.header.Header instance for a header it couldn't
        parse cleanly (confirmed live against a real dnsop mail archive
        message), and Header has no .strip() -- every caller here chains
        .strip() straight onto this return value, so returning anything
        else on the exception path just moves the crash one line later.
        """
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
            return str(value)

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