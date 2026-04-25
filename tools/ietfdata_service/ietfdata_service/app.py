"""FastAPI sidecar that exposes a small subset of the `ietfdata` library.

The main Dify API cannot depend on `ietfdata` directly because `ietfdata`
requires Python 3.13+, while the API runtime currently targets Python 3.11/3.12.
This sidecar isolates that dependency and returns generated dataset files that
the main API stores through the normal upload pipeline.

Mailarchive exports support two output modes:

- `markdown`: normalized, reader-friendly summaries for immediate ingestion
- `mbox_text`: plain-text raw-message batches that preserve the original RFC822
    source as closely as possible within the existing upload constraints
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket as _socket
from datetime import datetime, timezone
from email.message import Message
from typing import Literal

# Docker containers often lack IPv6 routing even when DNS returns AAAA records.
# Force IPv4-only to avoid [Errno 99] Cannot assign requested address on IMAP.
_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    results = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4 = [r for r in results if r[0] == _socket.AF_INET]
    return ipv4 if ipv4 else results


_socket.getaddrinfo = _ipv4_only_getaddrinfo

import requests
from fastapi import FastAPI, HTTPException
from ietfdata.datatracker import DataTracker
from ietfdata.mailarchive3 import MailArchive
from ietfdata.rfcindex import RFCIndex
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(title="ietfdata-service", version="0.1.0")


class SidecarFileResponse(BaseModel):
    filename: str
    mime_type: str
    content_base64: str
    message_count: int | None = None

    model_config = ConfigDict(extra="forbid")


class MailingListsResponse(BaseModel):
    lists: list[str]

    model_config = ConfigDict(extra="forbid")


class RfcSuggestion(BaseModel):
    doc_id: str
    title: str
    draft_names: list[str] = Field(default_factory=list)
    since: str | None = None  # "YYYY-MM" publication date
    stream: str | None = None
    wg: str | None = None
    area: str | None = None
    status: str | None = None
    draft_base_name: str | None = None  # e.g. "draft-ietf-quic-transport"
    draft_max_version: int | None = None  # e.g. 34
    draft_versions: list[dict[str, str | int | None]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class RfcSuggestionsResponse(BaseModel):
    suggestions: list[RfcSuggestion]

    model_config = ConfigDict(extra="forbid")


class DraftImportRequest(BaseModel):
    draft_names: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class MailarchiveImportRequest(BaseModel):
    mailing_list: str = Field(..., min_length=1)
    sent_after: str = Field(default="1970-01-01T00:00:00")
    sent_before: str = Field(default="2038-01-19T03:14:07")
    from_addr: str | None = None
    subject: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1)
    output_format: Literal["mbox_text", "jsonl"] = "jsonl"
    fetch_all: bool = False
    refresh: bool = False

    model_config = ConfigDict(extra="forbid")


class RfcImportRequest(BaseModel):
    rfc_ids: list[str] = Field(default_factory=list)
    since: str | None = None
    until: str | None = None
    stream: str | None = None
    wg: str | None = None
    status: str | None = None
    limit: int = Field(default=20, ge=1, le=100)

    model_config = ConfigDict(extra="forbid")


def _mailarchive() -> MailArchive:
    sqlite_file = os.getenv("IETFDATA_SQLITE_FILE", "ietfdata.sqlite")
    return MailArchive(sqlite_file=sqlite_file)


def _rfc_index() -> RFCIndex:
    cache_dir = os.getenv("IETFDATA_RFC_CACHE_DIR")
    return RFCIndex(cache_dir=cache_dir)


def _datatracker() -> DataTracker:
    return DataTracker()


MONTH_NAME_TO_NUM: dict[str, int] = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

IETF_DRAFT_BASE_URL = "https://www.ietf.org/archive/id"

_SECTION_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(?P<title>.+?)\s*$")
_NON_TECHNICAL_SECTION_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)*)\.\s+"
    r"(references|normative references|informative references|"
    r"acknowledg(e)?ments?|contributors?|author'?s addresses?)\s*$",
    re.IGNORECASE,
)
_START_INTRO_RE = re.compile(r"^\s*1\.?\s+introduction\s*$", re.IGNORECASE)
_BACK_MATTER_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(references|normative references|informative references|"
    r"acknowledg(e)?ments?|contributors?|author'?s addresses?)\s*$",
    re.IGNORECASE,
)


_FRONT_BLOCK_HDR_RE = re.compile(
    r"^\s*(status of this memo|copyright notice|table of contents)\s*$",
    re.IGNORECASE,
)
_NUMBERED_SECTION_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+\S")
# Body-level section heading: must start at column 0.
# In RFC plaintext, body headings are unindented; TOC entries are always indented.
_BODY_SECTION_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+\S")
# TOC entry: older RFC format where entries end with dots/spaces + page number.
_TOC_ENTRY_RE = re.compile(r"[.\s]{3,}\d+\s*$")


def _extract_technical_body_text(text: str) -> str:
    """Keep RFC/I-D technical content and remove common non-technical boilerplate.

    Flat RFC/I-D plaintext (no form feeds, the common case):
    - Strip BOM and normalise line endings.
    - Find the real body start: the first numbered section heading that is NOT a
      TOC navigation entry (i.e. does NOT end with dots/spaces + page number) and
      is followed by actual prose.
    - Strip everything before that heading (title page, Abstract, Status of This
      Memo, Copyright Notice, Table of Contents).
    - Strip back matter from the first References/Acknowledgments/Authors' heading.

    Form-feed RFC/I-D plaintext (older format):
    - Same body-start and back-matter logic.
    - Additionally remove per-page running headers/footers (isolated lines
      surrounded by blank lines that contain "[Page N]" or multi-column layout).
    """
    if not text.strip():
        return text

    # Strip UTF-8 BOM and normalise line endings.
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    has_ff = "\f" in text
    text = text.replace("\f", "\n")
    raw_lines = text.split("\n")

    # ------------------------------------------------------------------ #
    # Find real body start                                                 #
    # A TOC entry is a numbered-section line that ends with dots/spaces + #
    # a page number.  A real heading does not.                            #
    # ------------------------------------------------------------------ #
    body_start: int | None = None
    for idx, line in enumerate(raw_lines):
        # Require column-0: body headings are unindented, TOC entries are indented.
        if not _BODY_SECTION_RE.match(line):
            continue
        stripped = line.strip()
        if _TOC_ENTRY_RE.search(stripped):
            continue  # older-format TOC navigation line with trailing page number
        # Look ahead: a non-blank line that is itself NOT a column-0 body heading
        # confirms that this heading has prose below it.
        for j in range(idx + 1, min(idx + 12, len(raw_lines))):
            next_line = raw_lines[j]
            if not next_line.strip():
                continue
            if not _BODY_SECTION_RE.match(next_line):
                body_start = idx
                break
        if body_start is not None:
            break

    lines = raw_lines[body_start:] if body_start is not None else raw_lines

    # ------------------------------------------------------------------ #
    # Find back-matter start and trim                                      #
    # ------------------------------------------------------------------ #
    end_index = len(lines)
    for i, line in enumerate(lines):
        if _BACK_MATTER_RE.match(line.strip()):
            end_index = i
            break
    lines = lines[:end_index]

    # ------------------------------------------------------------------ #
    # Remove page furniture                                                #
    # For form-feed files: strip running headers/footers (isolated single  #
    # lines surrounded by blanks that look like RFC page headers).        #
    # For flat files: only strip stray [Page N] markers.                  #
    # ------------------------------------------------------------------ #
    if has_ff:
        cleaned: list[str] = []
        prev_blank = True
        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.search(r"\[Page\s+\d+\]", stripped, re.IGNORECASE):
                continue
            next_blank = (i + 1 >= len(lines)) or not lines[i + 1].strip()
            if prev_blank and next_blank and stripped:
                # Running header heuristic: two or more wide whitespace gaps.
                if len(re.findall(r" {3,}", line)) >= 2:
                    continue
            cleaned.append(line)
            prev_blank = not stripped
        lines = cleaned
    else:
        lines = [ln for ln in lines if not re.search(r"\[Page\s+\d+\]", ln, re.IGNORECASE)]

    # Collapse repeated blank lines.
    compacted: list[str] = []
    prev_blank2 = False
    for line in lines:
        blank = not line.strip()
        if blank and prev_blank2:
            continue
        compacted.append(line.rstrip())
        prev_blank2 = blank

    processed = "\n".join(compacted).strip()

    # If heuristics over-trim, fall back to the normalised original.
    if len(processed) < 20:
        return text.strip()

    return processed


def _parse_draft_version(draft_str: str) -> tuple[str | None, int | None]:
    """Parse 'draft-ietf-quic-transport-34' -> ('draft-ietf-quic-transport', 34)."""
    if not draft_str:
        return None, None
    m = re.match(r"^(draft-.+?)-(\d{2,3})$", draft_str)
    if m:
        return m.group(1), int(m.group(2))
    return draft_str, None


def _format_date_for_month(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    text = _safe_string(value, "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    if re.match(r"^\d{4}-\d{2}", text):
        return text[:7]
    return None


def _build_draft_version_metadata(draft_base_name: str | None) -> list[dict[str, str | int | None]]:
    if not draft_base_name:
        return []

    try:
        document = _datatracker().document_from_draft(draft_base_name)
    except Exception:
        return []
    if not document:
        return []

    versions: list[tuple[int, str]] = []
    for submission_uri in getattr(document, "submissions", []) or []:
        try:
            submission = _datatracker().submission(submission_uri)
        except Exception:
            continue
        if not submission:
            continue
        version_str = _safe_string(getattr(submission, "rev", None), "").strip()
        if not version_str.isdigit():
            continue
        versions.append((int(version_str), _format_date_for_month(getattr(submission, "submission_date", None)) or ""))

    versions.sort(key=lambda item: item[0])

    metadata: list[dict[str, str | int | None]] = []
    for index, (version, since) in enumerate(versions):
        next_since = versions[index + 1][1] if index + 1 < len(versions) else None
        metadata.append({
            "name": f"{draft_base_name}-{version:02d}",
            "version": version,
            "since": since or None,
            "until": next_since or None,
        })

    return metadata


def _build_rfc_suggestion(entry: object) -> RfcSuggestion:
    """Build a full RfcSuggestion from an ietfdata RfcEntry."""
    doc_id = _safe_string(getattr(entry, "doc_id", ""), "")
    title = _safe_string(getattr(entry, "title", ""), "")
    draft_names = _extract_draft_names(entry)

    year_raw = getattr(entry, "year", None)
    month_raw = _safe_string(getattr(entry, "month", ""), "")
    since: str | None = None
    if year_raw:
        month_num = MONTH_NAME_TO_NUM.get(month_raw)
        if month_num:
            since = f"{year_raw}-{month_num:02d}"
        else:
            since = str(year_raw)

    stream = _safe_string(getattr(entry, "stream", ""), "") or None
    wg = _safe_string(getattr(entry, "wg", ""), "") or None
    area = _safe_string(getattr(entry, "area", ""), "") or None
    status = _safe_string(getattr(entry, "curr_status", ""), "") or None

    draft_base_name: str | None = None
    draft_max_version: int | None = None
    draft_versions: list[dict[str, str | int | None]] = []
    if draft_names:
        base, version = _parse_draft_version(draft_names[0])
        draft_base_name = base
        draft_max_version = version
        draft_versions = _build_draft_version_metadata(draft_base_name)
        if draft_versions:
            draft_max_version = int(draft_versions[-1]["version"])

    return RfcSuggestion(
        doc_id=doc_id,
        title=title,
        draft_names=draft_names,
        since=since,
        stream=stream,
        wg=wg,
        area=area,
        status=status,
        draft_base_name=draft_base_name,
        draft_max_version=draft_max_version,
        draft_versions=draft_versions,
    )


def _normalize_rfc_id(value: str) -> str:
    normalized = _safe_string(value, "").strip().upper()
    if not normalized:
        return ""

    if normalized.isdigit():
        return f"RFC{normalized}"

    if normalized.startswith("RFC") and normalized[3:].isdigit():
        return normalized

    # Keep non-standard values (for example, draft-* search keywords) unchanged.
    return normalized


def _extract_draft_names(entry: object) -> list[str]:
    draft_names: list[str] = []
    # Support multiple potential attribute names from ietfdata RFC entry models.
    candidate_fields = (
        "draft",
        "drafts",
        "draft_name",
        "draft_names",
        "source_draft",
        "source_drafts",
        "draft_references",
    )

    for field_name in candidate_fields:
        value = getattr(entry, field_name, None)
        if not value:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                draft_names.append(text)
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                text = _safe_string(item, "").strip()
                if text:
                    draft_names.append(text)

    # Keep order stable while removing duplicates.
    deduped: list[str] = []
    seen: set[str] = set()
    for name in draft_names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(name)

    return deduped


def _match_rfc_entry(entry: object, query: str, normalized_query: str) -> bool:
    if not query:
        return True

    doc_id = _safe_string(getattr(entry, "doc_id", ""), "")
    title = _safe_string(getattr(entry, "title", ""), "")
    draft_names = _extract_draft_names(entry)

    query_lower = query.lower()
    normalized_lower = normalized_query.lower()

    if normalized_lower and normalized_lower in doc_id.lower():
        return True
    if query_lower in doc_id.lower() or query_lower in title.lower():
        return True
    return any(query_lower in draft.lower() for draft in draft_names)


def _search_rfc_suggestions(index: RFCIndex, query: str, limit: int) -> list[RfcSuggestion]:
    normalized_query = _normalize_rfc_id(query)

    # Prioritize exact RFC lookups for numeric/RFC-style queries.
    suggestions: list[RfcSuggestion] = []
    seen_doc_ids: set[str] = set()
    if normalized_query.startswith("RFC") and normalized_query[3:].isdigit():
        try:
            entry = index.rfc(normalized_query)
            suggestion = _build_rfc_suggestion(entry)
            suggestions.append(suggestion)
            seen_doc_ids.add(normalized_query.lower())
        except KeyError:
            pass

    for entry in index.rfcs(since="1969-01", until="2038-01"):
        if len(suggestions) >= limit:
            break
        if not _match_rfc_entry(entry, query=query, normalized_query=normalized_query):
            continue

        doc_id = _safe_string(getattr(entry, "doc_id", ""), "")
        if not doc_id:
            continue
        key = doc_id.lower()
        if key in seen_doc_ids:
            continue
        seen_doc_ids.add(key)

        suggestions.append(_build_rfc_suggestion(entry))

    return suggestions[:limit]


def _extract_message_text(message: Message) -> str:
    body_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    body_parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = message.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))

    return "\n\n".join(part.strip() for part in body_parts if part and part.strip())


def _extract_raw_message_text(message: Message) -> str:
    try:
        return message.as_string()
    except Exception:  # noqa: BLE001
        return ""


def _safe_string(value: object, fallback: str = "") -> str:
    if value is None:
        return fallback
    try:
        text = str(value)
    except Exception:  # noqa: BLE001
        return fallback
    return text if text else fallback


def _sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "ietf"


def _compact_date_for_filename(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)[:8] or "date"


def _as_sidecar_file(filename: str, markdown: str) -> SidecarFileResponse:
    content_base64 = base64.b64encode(markdown.encode("utf-8")).decode("ascii")
    return SidecarFileResponse(filename=filename, mime_type="text/markdown", content_base64=content_base64)


def _as_jsonl_sidecar_file(filename: str, records: list[dict]) -> SidecarFileResponse:
    """Serialise a list of dicts as newline-delimited JSON and return a SidecarFileResponse."""
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    content = "\n".join(lines)
    content_base64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return SidecarFileResponse(
        filename=filename,
        mime_type="application/x-ndjson",
        content_base64=content_base64,
        message_count=len(records),
    )


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/mailarchive/lists")
def get_mailing_lists() -> MailingListsResponse:
    """Fetch available mailing list names from IETF IMAP server."""
    archive = _mailarchive()
    archive.update_mailing_list_names()
    lists = sorted(archive.mailing_list_names())
    return MailingListsResponse(lists=lists)


@app.get("/api/v1/rfcs/suggestions")
def get_rfc_suggestions(query: str = "", limit: int = 20) -> RfcSuggestionsResponse:
    normalized_limit = max(1, min(limit, 100))
    index = _rfc_index()
    suggestions = _search_rfc_suggestions(index=index, query=query.strip(), limit=normalized_limit)
    return RfcSuggestionsResponse(suggestions=suggestions)


@app.post("/api/v1/import/drafts")
def import_drafts(payload: DraftImportRequest) -> SidecarFileResponse:
    """Download IETF internet-drafts by full name and return them as a markdown file."""
    if not payload.draft_names:
        raise HTTPException(status_code=400, detail="draft_names must not be empty")

    sections: list[str] = []
    for draft_name in payload.draft_names:
        # Sanitize: only allow alphanumeric, dots, hyphens in draft names.
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "", draft_name.strip())
        if not safe_name:
            continue
        url = f"{IETF_DRAFT_BASE_URL}/{safe_name}.txt"
        try:
            resp = requests.get(url, timeout=30)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to fetch draft {safe_name}: {exc}") from exc
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Draft not found: {safe_name}")
        if not resp.ok:
            raise HTTPException(status_code=502, detail=f"IETF returned {resp.status_code} for {safe_name}")
        header = f"# {safe_name}\n\nSource: {url}\n\n"
        sections.append(header + _extract_technical_body_text(resp.text))

    if not sections:
        raise HTTPException(status_code=400, detail="No valid draft names provided")

    combined = "\n\n---\n\n".join(sections)
    name_part = payload.draft_names[0] if len(payload.draft_names) == 1 else f"drafts-{len(payload.draft_names)}"
    filename = f"{_sanitize_filename(name_part)}.md"
    return _as_sidecar_file(filename, combined)


@app.post("/api/v1/import/mailarchive")
def import_mailarchive(payload: MailarchiveImportRequest) -> SidecarFileResponse:
    archive = _mailarchive()
    archive.update_mailing_list_names()
    mailing_lists = set(archive.mailing_list_names())
    if payload.mailing_list not in mailing_lists:
        raise HTTPException(status_code=404, detail=f"Unknown mailing list: {payload.mailing_list}")

    # Always update the mailing list to populate uidvalidity from IMAP.
    # This is required even on first import; payload.refresh can force a full re-fetch.
    archive.update_mailing_list(payload.mailing_list)

    mailing_list = archive.mailing_list(payload.mailing_list)
    received_after = _parse_iso_datetime(payload.sent_after)
    received_before = _parse_iso_datetime(payload.sent_before)

    message_sections: list[str] = []
    jsonl_records: list[dict] = []
    matched_count = 0
    selected_count = 0
    skipped_count = 0
    for envelope in mailing_list.messages(received_after=received_after, received_before=received_before):
        from_value = _safe_string(envelope.from_(), "")
        subject_value = _safe_string(envelope.subject(), "")

        if payload.from_addr and payload.from_addr.lower() not in from_value.lower():
            continue
        if payload.subject and payload.subject.lower() not in subject_value.lower():
            continue

        if matched_count < payload.offset:
            matched_count += 1
            continue
        matched_count += 1

        try:
            message = envelope.contents()
            if payload.output_format == "jsonl":
                body = _extract_message_text(message) or _extract_raw_message_text(message)
                if not body or not body.strip():
                    skipped_count += 1
                    continue
                archived_at = message.get("Archived-At", "").strip().strip("<>").strip()
                url = archived_at or ""
                msg_id = url.rstrip("/").split("/")[-1] if url else _safe_string(envelope.message_id(), "")
                jsonl_records.append(
                    {
                        "id": msg_id,
                        "url": url,
                        "subject": subject_value,
                        "from": from_value,
                        "date": _safe_string(envelope.date(), ""),
                        "body": body.strip(),
                    }
                )
            else:
                message_text = _extract_raw_message_text(message)
                message_sections.append("")
                message_sections.append(f"From ietfdata-export {_safe_string(envelope.date(), 'unknown')}")
                message_sections.append(f"X-IETF-Mailing-List: {payload.mailing_list}")
                message_sections.append(f"X-IETF-Message-Id: {_safe_string(envelope.message_id(), 'unknown')}")
                message_sections.append(message_text or "(raw message unavailable)")
            selected_count += 1
        except Exception:  # noqa: BLE001
            skipped_count += 1
            continue

        if not payload.fetch_all and selected_count >= payload.limit:
            break

    if selected_count == 0:
        raise HTTPException(status_code=404, detail="No messages matched the provided filters")

    if payload.output_format == "jsonl":
        filename = (
            f"ietf-mailarchive-{_sanitize_filename(payload.mailing_list)}"
            f"-{_compact_date_for_filename(payload.sent_after)}"
            f"-{_compact_date_for_filename(payload.sent_before)}"
            f"-offset-{payload.offset}.jsonl"
        )
        return _as_jsonl_sidecar_file(filename, jsonl_records)

    sections = ["IETF Mailarchive Raw Export", f"- Mailing list: {payload.mailing_list}"]
    sections.append(f"- Sent after: {payload.sent_after}")
    sections.append(f"- Sent before: {payload.sent_before}")
    sections.append(f"- Message count: {selected_count}")
    sections.append(f"- Offset: {payload.offset}")
    sections.append(f"- Output format: {payload.output_format}")
    if skipped_count:
        sections.append(f"- Skipped malformed messages: {skipped_count}")
    sections.extend(message_sections)

    extension = "txt"
    format_suffix = "raw"
    filename = (
        f"ietf-mailarchive-{_sanitize_filename(payload.mailing_list)}"
        f"-{format_suffix}"
        f"-{_compact_date_for_filename(payload.sent_after)}"
        f"-{_compact_date_for_filename(payload.sent_before)}"
        f"-offset-{payload.offset}.{extension}"
    )
    response = _as_sidecar_file(filename, "\n".join(sections))
    return response.model_copy(update={"mime_type": "text/plain"})


@app.post("/api/v1/import/mailarchive/mbox")
def import_mailarchive_as_mbox(payload: MailarchiveImportRequest) -> SidecarFileResponse:
    """Export entire mailarchive as RFC4155 mbox format (no partitioning, single file)."""
    archive = _mailarchive()
    archive.update_mailing_list_names()
    mailing_lists = set(archive.mailing_list_names())
    if payload.mailing_list not in mailing_lists:
        raise HTTPException(status_code=404, detail=f"Unknown mailing list: {payload.mailing_list}")

    archive.update_mailing_list(payload.mailing_list)
    mailing_list = archive.mailing_list(payload.mailing_list)
    received_after = _parse_iso_datetime(payload.sent_after)
    received_before = _parse_iso_datetime(payload.sent_before)

    mbox_lines: list[str] = []
    message_count = 0
    skipped_count = 0

    for envelope in mailing_list.messages(received_after=received_after, received_before=received_before):
        from_value = _safe_string(envelope.from_(), "")
        subject_value = _safe_string(envelope.subject(), "")

        if payload.from_addr and payload.from_addr.lower() not in from_value.lower():
            continue
        if payload.subject and payload.subject.lower() not in subject_value.lower():
            continue

        try:
            message = envelope.contents()
            raw_message = _extract_raw_message_text(message)
            if not raw_message:
                skipped_count += 1
                continue

            # RFC4155 mbox format: "From " line (with space, not colon) as separator
            mbox_lines.append(f"From {from_value or 'unknown'} {_safe_string(envelope.date(), 'unknown')}")
            mbox_lines.append(raw_message)
            mbox_lines.append("")  # blank line between messages
            message_count += 1
        except Exception:  # noqa: BLE001
            skipped_count += 1
            continue

    if message_count == 0:
        raise HTTPException(status_code=404, detail="No messages matched the provided filters")

    # Add metadata header
    header_lines = [
        f"# IETF Mailarchive Export - {payload.mailing_list}",
        f"# Messages: {message_count}",
        f"# Sent after: {payload.sent_after}",
        f"# Sent before: {payload.sent_before}",
        "# Format: RFC4155 mbox",
        "",
    ]
    if skipped_count:
        header_lines.append(f"# Skipped malformed messages: {skipped_count}")
        header_lines.append("")

    mbox_content = "\n".join(header_lines) + "\n".join(mbox_lines)
    filename = (
        f"ietf-mailarchive-{_sanitize_filename(payload.mailing_list)}"
        f"-{_compact_date_for_filename(payload.sent_after)}"
        f"-{_compact_date_for_filename(payload.sent_before)}.mbox"
    )
    response = SidecarFileResponse(
        filename=filename,
        mime_type="application/mbox",
        content_base64=base64.b64encode(mbox_content.encode("utf-8")).decode("ascii"),
        message_count=message_count,
    )
    return response


@app.post("/api/v1/import/rfcs")
def import_rfcs(payload: RfcImportRequest) -> SidecarFileResponse:
    index = _rfc_index()

    entries = []
    if payload.rfc_ids:
        for rfc_id in payload.rfc_ids[: payload.limit]:
            normalized = _normalize_rfc_id(rfc_id)
            try:
                entries.append(index.rfc(normalized))
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=f"Unknown RFC: {normalized}") from exc
    else:
        for entry in index.rfcs(
            since=payload.since or "1969-01",
            until=payload.until or "2038-01",
            stream=payload.stream,
            wg=payload.wg,
            status=payload.status,
        ):
            entries.append(entry)
            if len(entries) >= payload.limit:
                break

    if not entries:
        raise HTTPException(status_code=404, detail="No RFCs matched the provided filters")

    sections = ["# IETF RFC Export", f"- RFC count: {len(entries)}"]
    session = requests.Session()
    try:
        for entry in entries:
            content_url = entry.content_url("ASCII") or entry.content_url("TEXT")
            if not content_url:
                sections.append("\n---\n")
                sections.append(f"## {entry.doc_id}: {entry.title}")
                sections.append("No downloadable ASCII/TEXT representation is available.")
                continue

            response = session.get(content_url, timeout=30)
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Failed to fetch {entry.doc_id} from RFC Editor")

            sections.append("\n---\n")
            sections.append(f"## {entry.doc_id}: {entry.title}")
            sections.append(f"- Stream: {entry.stream}")
            sections.append(f"- Status: {entry.curr_status}")
            sections.append(f"- Date: {entry.date()}")
            sections.append(f"- Source: {content_url}")
            sections.append("")
            sections.append(_extract_technical_body_text(response.text))
    finally:
        session.close()

    filename = "ietf-rfcs.md" if not payload.rfc_ids else f"ietf-{'-'.join(map(_sanitize_filename, payload.rfc_ids[:3]))}.md"
    return _as_sidecar_file(filename, "\n".join(sections))