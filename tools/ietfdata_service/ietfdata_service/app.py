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
import shlex
import socket as _socket
from datetime import datetime, timezone
from email import message_from_bytes as _email_message_from_bytes
from email import policy as _email_policy
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


class FormatInfo(BaseModel):
    key: str       # "txt", "html", "pdf", "xml"
    label: str     # human-readable label
    mime_type: str

    model_config = ConfigDict(extra="forbid")


class RfcFormatEntry(BaseModel):
    rfc_id: str
    formats: list[FormatInfo]

    model_config = ConfigDict(extra="forbid")


class RfcFormatsResponse(BaseModel):
    entries: list[RfcFormatEntry]

    model_config = ConfigDict(extra="forbid")


class DraftFormatEntry(BaseModel):
    draft_name: str
    formats: list[FormatInfo]

    model_config = ConfigDict(extra="forbid")


class DraftFormatsResponse(BaseModel):
    entries: list[DraftFormatEntry]

    model_config = ConfigDict(extra="forbid")


class DraftImportRequest(BaseModel):
    draft_names: list[str] = Field(default_factory=list)
    format: str = Field(default="txt", description="Preferred download format: txt, html, xml")

    model_config = ConfigDict(extra="forbid")


class MailarchiveImportRequest(BaseModel):
    mailing_list: str = Field(..., min_length=1)
    sent_after: str = Field(default="1970-01-01T00:00:00")
    sent_before: str = Field(default="2038-01-19T03:14:07")
    from_addr: str | None = None
    subject: str | None = None
    search_in: list[Literal["subject", "body"]] = Field(default=["subject"])
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1)
    output_format: Literal["mbox_text", "jsonl"] = "jsonl"
    fetch_all: bool = False

    model_config = ConfigDict(extra="forbid")


class RfcImportRequest(BaseModel):
    rfc_ids: list[str] = Field(default_factory=list)
    since: str | None = None
    until: str | None = None
    stream: str | None = None
    wg: str | None = None
    status: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    format: str = Field(default="txt", description="Preferred download format: txt, html, pdf, xml")

    model_config = ConfigDict(extra="forbid")


class RfcFormatsRequest(BaseModel):
    rfc_ids: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class DraftFormatsRequest(BaseModel):
    draft_names: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


def _mailarchive() -> MailArchive:
    sqlite_file = os.getenv("IETFDATA_SQLITE_FILE", "ietfdata.sqlite")
    return MailArchive(sqlite_file=sqlite_file)


# In-process cache of known mailing list names (avoids IMAP connection on every request).
# Populated on first call to get_mailing_lists or import_mailarchive.
_mailing_list_names_cache: set[str] | None = None


def _get_mailing_list_names(archive: MailArchive, *, force_refresh: bool = False) -> set[str]:
    """Return known mailing list names, hitting IMAP only when the local DB is empty."""
    global _mailing_list_names_cache
    if force_refresh or _mailing_list_names_cache is None:
        # Read what is already in SQLite (fast, no IMAP).
        local_names = set(archive.mailing_list_names())
        if not local_names or force_refresh:
            # DB is empty or caller wants fresh data — sync once from IMAP.
            archive.update_mailing_list_names()
            local_names = set(archive.mailing_list_names())
        _mailing_list_names_cache = local_names
    return _mailing_list_names_cache


def _is_mailing_list_populated(archive: MailArchive, name: str) -> bool:
    """Return True if the list already has messages synced from IMAP (uidvalidity is set)."""
    try:
        return archive.mailing_list(name).uidvalidity() > 0
    except (TypeError, AttributeError, Exception):
        return False


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

# Maps user-facing format key → ietfdata content_url() format strings (tried in order).
_RFC_FORMAT_CONTENT_KEYS: dict[str, list[str]] = {
    "txt": ["ASCII", "TEXT"],
    "html": ["HTML"],
    "pdf": ["PDF"],
    "xml": ["XML"],
}

_FORMAT_LABELS: dict[str, str] = {
    "txt": "Plain Text",
    "html": "HTML",
    "pdf": "PDF",
    "xml": "XML",
}

_FORMAT_MIME_TYPES: dict[str, str] = {
    "txt": "text/plain",
    "html": "text/html",
    "pdf": "application/pdf",
    "xml": "application/xml",
}

_FORMAT_EXTENSIONS: dict[str, str] = {
    "txt": "txt",
    "html": "html",
    "pdf": "pdf",
    "xml": "xml",
}

# Draft formats we can attempt; txt is almost always present.
_DRAFT_FORMAT_ORDER = ["txt", "html", "xml"]

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


def _decode_bytes(data: bytes, charset: str | None) -> str:
    for enc in (charset or "utf-8", "utf-8", "latin-1"):
        try:
            return data.decode(enc, errors="replace")
        except (LookupError, TypeError):
            continue
    return data.decode("latin-1", errors="replace")


def _extract_message_text(message: Message) -> str:
    body_parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    body_parts.append(_decode_bytes(payload, part.get_content_charset()))
    else:
        payload = message.get_payload(decode=True)
        if isinstance(payload, bytes):
            body_parts.append(_decode_bytes(payload, message.get_content_charset()))

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


def _rfc_content_url_for_format(entry: object, fmt: str) -> str | None:
    """Return the first non-None content URL for the requested format key."""
    keys = _RFC_FORMAT_CONTENT_KEYS.get(fmt, ["ASCII", "TEXT"])
    for key in keys:
        try:
            url = entry.content_url(key)  # type: ignore[union-attr]
            if url:
                return str(url)
        except Exception:  # noqa: BLE001
            continue
    return None


def _available_rfc_formats(entry: object) -> list[FormatInfo]:
    formats: list[FormatInfo] = []
    for fmt in ("txt", "html", "pdf", "xml"):
        if _rfc_content_url_for_format(entry, fmt):
            formats.append(FormatInfo(
                key=fmt,
                label=_FORMAT_LABELS[fmt],
                mime_type=_FORMAT_MIME_TYPES[fmt],
            ))
    return formats


def _available_draft_formats(draft_name: str) -> list[FormatInfo]:
    """Check which formats exist for a draft by probing the IETF archive."""
    session = requests.Session()
    formats: list[FormatInfo] = []
    try:
        for fmt in _DRAFT_FORMAT_ORDER:
            ext = _FORMAT_EXTENSIONS[fmt]
            url = f"{IETF_DRAFT_BASE_URL}/{draft_name}.{ext}"
            try:
                resp = session.head(url, timeout=10, allow_redirects=True)
                if resp.status_code == 200:
                    formats.append(FormatInfo(
                        key=fmt,
                        label=_FORMAT_LABELS[fmt],
                        mime_type=_FORMAT_MIME_TYPES[fmt],
                    ))
            except Exception:  # noqa: BLE001
                continue
    finally:
        session.close()
    # Always include txt as a fallback even if HEAD failed.
    if not any(f.key == "txt" for f in formats):
        formats.insert(0, FormatInfo(key="txt", label="Plain Text", mime_type="text/plain"))
    return formats


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


def _query_message_headers(
    archive: MailArchive,
    ml_name: str,
    uidvalidity: int,
    rx_after: str,
    rx_before: str,
) -> list[tuple]:
    """Return (message_num, from_name, from_addr, subject, message_id, date) rows via a single
    JOIN query WITHOUT loading the message blob.

    Deduplicates by Message-ID: the IETF IMAP server often stores each email twice
    (direct delivery + list relay), so we keep only the lowest message_num per
    Message-ID, matching the deduplication that the mailarchive website applies.
    Messages with no Message-ID header are kept as-is (grouped by message_num).
    """
    sql = """
        SELECT MIN(m.message_num), h.from_name, h.from_addr, h.subject, h.message_id, h.date
        FROM   ietf_ma_msg  m
        LEFT JOIN ietf_ma_hdr h ON h.message_num = m.message_num
        WHERE  m.mailing_list  = ?
          AND  m.uidvalidity   = ?
          AND  m.date_received > ?
          AND  m.date_received < ?
        GROUP BY COALESCE(h.message_id, CAST(m.message_num AS TEXT))
    """
    return archive._db.cursor().execute(sql, (ml_name, uidvalidity, rx_after, rx_before)).fetchall()


def _fetch_message_raw(archive: MailArchive, message_num: int) -> bytes | None:
    """Fetch only the raw RFC822 bytes for a specific message_num (no other columns)."""
    res = archive._db.cursor().execute(
        "SELECT message FROM ietf_ma_msg WHERE message_num = ?;", (message_num,)
    ).fetchone()
    return bytes(res[0]) if res and res[0] is not None else None


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_boolean_query(query: str) -> list[list[str]]:
    """Parse a boolean query string into OR clauses of AND terms.

    Supports quoted phrases, OR and AND keywords (AND is also the default
    when no keyword separates two adjacent terms).

    Examples:
        "rfc8205"                                    → [["rfc8205"]]
        "rfc8205 OR draft-ietf"                      → [["rfc8205"], ["draft-ietf"]]
        "rfc8205 OR \"draft-ietf-sidr-bgpsec\""      → [["rfc8205"], ["draft-ietf-sidr-bgpsec"]]
        "rfc8205 AND security"                        → [["rfc8205", "security"]]
    """
    if not query.strip():
        return []
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()

    or_clauses: list[list[str]] = []
    current_clause: list[str] = []
    for token in tokens:
        upper = token.upper()
        if upper == "OR":
            if current_clause:
                or_clauses.append(current_clause)
            current_clause = []
        elif upper == "AND":
            pass  # AND is implicit between adjacent terms
        else:
            current_clause.append(token.lower())
    if current_clause:
        or_clauses.append(current_clause)
    return or_clauses


def _match_boolean_query(or_clauses: list[list[str]], text: str) -> bool:
    """Return True if text matches any OR clause (each clause requires all AND terms)."""
    if not or_clauses:
        return True
    text_lower = text.lower()
    return any(all(term in text_lower for term in clause) for clause in or_clauses)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/mailarchive/lists")
def get_mailing_lists() -> MailingListsResponse:
    """Fetch available mailing list names from IETF IMAP server."""
    archive = _mailarchive()
    names = _get_mailing_list_names(archive, force_refresh=True)
    return MailingListsResponse(lists=sorted(names))


class SyncStatusResponse(BaseModel):
    name: str
    status: Literal["ready", "syncing", "not_found"]
    message_count: int = 0

    model_config = ConfigDict(extra="forbid")


# Track which lists are currently syncing (set of list names)
_syncing: set[str] = set()


def _resolve_mailing_list(archive: MailArchive, list_name: str) -> bool:
    """Return True if list_name is a known IETF mailing list.

    Checks the local cache first (fast), then falls back to a force-refresh
    from IMAP if the name isn't found — so lists that haven't been synced yet
    are still discoverable without requiring a separate list-fetch step.
    """
    if list_name in _get_mailing_list_names(archive):
        return True
    # Not in cache — refresh from IMAP once and check again.
    refreshed = _get_mailing_list_names(archive, force_refresh=True)
    return list_name in refreshed


@app.get("/api/v1/mailarchive/{list_name}/status")
def get_mailarchive_status(list_name: str) -> SyncStatusResponse:
    """Return sync status for a specific mailing list."""
    archive = _mailarchive()
    if not _resolve_mailing_list(archive, list_name):
        raise HTTPException(status_code=404, detail=f"Unknown mailing list: {list_name}")
    if list_name in _syncing:
        return SyncStatusResponse(name=list_name, status="syncing")
    if not _is_mailing_list_populated(archive, list_name):
        return SyncStatusResponse(name=list_name, status="syncing" if list_name in _syncing else "not_found")
    count = archive._db.cursor().execute(
        "SELECT COUNT(*) FROM ietf_ma_msg WHERE mailing_list = ?;", (list_name,)
    ).fetchone()[0]
    return SyncStatusResponse(name=list_name, status="ready", message_count=count)


@app.post("/api/v1/mailarchive/{list_name}/sync")
def sync_mailing_list(list_name: str) -> SyncStatusResponse:
    """Start an IMAP sync for a specific mailing list in the background.

    Returns immediately.  Poll GET /api/v1/mailarchive/{name}/status to check
    when the sync is complete (status == "ready").
    """
    import threading

    archive = _mailarchive()
    if not _resolve_mailing_list(archive, list_name):
        raise HTTPException(status_code=404, detail=f"Unknown mailing list: {list_name}")

    if _is_mailing_list_populated(archive, list_name):
        count = archive._db.cursor().execute(
            "SELECT COUNT(*) FROM ietf_ma_msg WHERE mailing_list = ?;", (list_name,)
        ).fetchone()[0]
        return SyncStatusResponse(name=list_name, status="ready", message_count=count)

    if list_name in _syncing:
        return SyncStatusResponse(name=list_name, status="syncing")

    _syncing.add(list_name)

    def _do_sync() -> None:
        try:
            bg_archive = _mailarchive()
            bg_archive.update_mailing_list(list_name)
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).exception("Background sync failed for %s", list_name)
        finally:
            _syncing.discard(list_name)

    threading.Thread(target=_do_sync, daemon=True, name=f"sync-{list_name}").start()
    return SyncStatusResponse(name=list_name, status="syncing")


@app.get("/api/v1/rfcs/suggestions")
def get_rfc_suggestions(query: str = "", limit: int = 20) -> RfcSuggestionsResponse:
    normalized_limit = max(1, min(limit, 100))
    index = _rfc_index()
    suggestions = _search_rfc_suggestions(index=index, query=query.strip(), limit=normalized_limit)
    return RfcSuggestionsResponse(suggestions=suggestions)


@app.post("/api/v1/rfcs/formats")
def get_rfc_formats(payload: RfcFormatsRequest) -> RfcFormatsResponse:
    """Return available download formats for each requested RFC ID."""
    index = _rfc_index()
    entries: list[RfcFormatEntry] = []
    for rfc_id in payload.rfc_ids:
        normalized = _normalize_rfc_id(rfc_id)
        try:
            entry = index.rfc(normalized)
        except KeyError:
            entries.append(RfcFormatEntry(rfc_id=normalized, formats=[]))
            continue
        entries.append(RfcFormatEntry(rfc_id=normalized, formats=_available_rfc_formats(entry)))
    return RfcFormatsResponse(entries=entries)


@app.post("/api/v1/drafts/formats")
def get_draft_formats(payload: DraftFormatsRequest) -> DraftFormatsResponse:
    """Return available download formats for each requested Internet-Draft name."""
    entries: list[DraftFormatEntry] = []
    for draft_name in payload.draft_names:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "", draft_name.strip())
        if not safe:
            continue
        entries.append(DraftFormatEntry(draft_name=safe, formats=_available_draft_formats(safe)))
    return DraftFormatsResponse(entries=entries)


@app.post("/api/v1/import/drafts")
def import_drafts(payload: DraftImportRequest) -> SidecarFileResponse:
    """Download IETF internet-drafts by full name and return them as a dataset file."""
    if not payload.draft_names:
        raise HTTPException(status_code=400, detail="draft_names must not be empty")

    fmt = payload.format if payload.format in _FORMAT_EXTENSIONS else "txt"
    ext = _FORMAT_EXTENSIONS[fmt]
    mime_type = _FORMAT_MIME_TYPES[fmt]

    # Single draft: download raw bytes and return as-is.
    if len(payload.draft_names) == 1:
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "", payload.draft_names[0].strip())
        if not safe_name:
            raise HTTPException(status_code=400, detail="Invalid draft name")
        url = f"{IETF_DRAFT_BASE_URL}/{safe_name}.{ext}"
        try:
            resp = requests.get(url, timeout=30)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to fetch draft {safe_name}: {exc}") from exc
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Draft not found in {fmt} format: {safe_name}")
        if not resp.ok:
            raise HTTPException(status_code=502, detail=f"IETF returned {resp.status_code} for {safe_name}")
        return SidecarFileResponse(
            filename=f"{_sanitize_filename(safe_name)}.{ext}",
            mime_type=mime_type,
            content_base64=base64.b64encode(resp.content).decode("ascii"),
        )

    # Multiple drafts: concatenate raw bytes with plain separators.
    parts: list[bytes] = []
    for draft_name in payload.draft_names:
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "", draft_name.strip())
        if not safe_name:
            continue
        url = f"{IETF_DRAFT_BASE_URL}/{safe_name}.{ext}"
        try:
            resp = requests.get(url, timeout=30)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to fetch draft {safe_name}: {exc}") from exc
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Draft not found in {fmt} format: {safe_name}")
        if not resp.ok:
            raise HTTPException(status_code=502, detail=f"IETF returned {resp.status_code} for {safe_name}")
        separator = f"\n\n{'='*72}\n{safe_name}\n{'='*72}\n\n".encode()
        parts.append(separator + resp.content)

    if not parts:
        raise HTTPException(status_code=400, detail="No valid draft names provided")

    name_part = payload.draft_names[0] if len(payload.draft_names) == 1 else f"drafts-{len(payload.draft_names)}"
    return SidecarFileResponse(
        filename=f"{_sanitize_filename(name_part)}.{ext}",
        mime_type=mime_type,
        content_base64=base64.b64encode(b"".join(parts)).decode("ascii"),
    )


@app.post("/api/v1/import/mailarchive")
def import_mailarchive(payload: MailarchiveImportRequest) -> SidecarFileResponse:
    archive = _mailarchive()
    mailing_lists = _get_mailing_list_names(archive)
    if payload.mailing_list not in mailing_lists:
        raise HTTPException(status_code=404, detail=f"Unknown mailing list: {payload.mailing_list}")

    # If local data is not yet available, start a background sync and return 503
    # so the caller can poll /status and retry — avoids blocking for minutes.
    if not _is_mailing_list_populated(archive, payload.mailing_list):
        if payload.mailing_list not in _syncing:
            _syncing.add(payload.mailing_list)
            import threading as _threading
            def _bg() -> None:
                try:
                    _mailarchive().update_mailing_list(payload.mailing_list)
                except Exception:
                    import logging as _logging
                    _logging.getLogger(__name__).exception("Background sync failed for %s", payload.mailing_list)
                finally:
                    _syncing.discard(payload.mailing_list)
            _threading.Thread(target=_bg, daemon=True, name=f"sync-{payload.mailing_list}").start()
        raise HTTPException(
            status_code=503,
            detail=f"Mailing list '{payload.mailing_list}' is not yet synced. "
                   f"A background sync has been started — retry in a few minutes, "
                   f"or check GET /api/v1/mailarchive/{payload.mailing_list}/status.",
        )

    mailing_list = archive.mailing_list(payload.mailing_list)
    received_after = _parse_iso_datetime(payload.sent_after)
    received_before = _parse_iso_datetime(payload.sent_before)
    subject_query = _parse_boolean_query(payload.subject or "")
    search_in = set(payload.search_in) if payload.search_in else {"subject"}

    rows = _query_message_headers(
        archive, mailing_list.name(), mailing_list.uidvalidity(),
        received_after.isoformat(), received_before.isoformat(),
    )

    message_sections: list[str] = []
    jsonl_records: list[dict] = []
    matched_count = 0
    selected_count = 0
    skipped_count = 0
    for (message_num, from_name, from_addr, subject, message_id, date_str) in rows:
        from_value = f"{from_name} <{from_addr}>" if from_name else (from_addr or "")
        subject_value = subject or ""

        if payload.from_addr and payload.from_addr.lower() not in from_value.lower():
            continue

        # Query matching — check subject first (cheap), then body blob only if needed.
        prefetched_raw: bytes | None = None
        prefetched_message: Message | None = None
        if subject_query:
            matched = "subject" in search_in and _match_boolean_query(subject_query, subject_value)
            if not matched and "body" in search_in:
                prefetched_raw = _fetch_message_raw(archive, message_num)
                if prefetched_raw is not None:
                    prefetched_message = _email_message_from_bytes(prefetched_raw, policy=_email_policy.default)
                    matched = _match_boolean_query(subject_query, _extract_message_text(prefetched_message))
            if not matched:
                continue

        if matched_count < payload.offset:
            matched_count += 1
            continue
        matched_count += 1

        try:
            raw = prefetched_raw if prefetched_raw is not None else _fetch_message_raw(archive, message_num)
            if raw is None:
                skipped_count += 1
                continue
            message = prefetched_message if prefetched_message is not None else _email_message_from_bytes(raw, policy=_email_policy.default)
            if payload.output_format == "jsonl":
                body = _extract_message_text(message) or _extract_raw_message_text(message)
                if not body or not body.strip():
                    skipped_count += 1
                    continue
                archived_at = message.get("Archived-At", "").strip().strip("<>").strip()
                url = archived_at or ""
                msg_id = url.rstrip("/").split("/")[-1] if url else (message_id or "")
                jsonl_records.append(
                    {
                        "id": msg_id,
                        "url": url,
                        "subject": subject_value,
                        "from": from_value,
                        "date": date_str or "",
                        "body": body.strip(),
                    }
                )
            else:
                message_text = _extract_raw_message_text(message)
                message_sections.append("")
                message_sections.append(f"From ietfdata-export {date_str or 'unknown'}")
                message_sections.append(f"X-IETF-Mailing-List: {payload.mailing_list}")
                message_sections.append(f"X-IETF-Message-Id: {message_id or 'unknown'}")
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
    mailing_lists = _get_mailing_list_names(archive)
    if payload.mailing_list not in mailing_lists:
        raise HTTPException(status_code=404, detail=f"Unknown mailing list: {payload.mailing_list}")

    if not _is_mailing_list_populated(archive, payload.mailing_list):
        if payload.mailing_list not in _syncing:
            _syncing.add(payload.mailing_list)
            import threading as _threading
            def _bg2() -> None:
                try:
                    _mailarchive().update_mailing_list(payload.mailing_list)
                except Exception:
                    import logging as _logging
                    _logging.getLogger(__name__).exception("Background sync failed for %s", payload.mailing_list)
                finally:
                    _syncing.discard(payload.mailing_list)
            _threading.Thread(target=_bg2, daemon=True, name=f"sync-{payload.mailing_list}").start()
        raise HTTPException(
            status_code=503,
            detail=f"Mailing list '{payload.mailing_list}' is not yet synced. "
                   f"A background sync has been started — retry in a few minutes.",
        )
    mailing_list = archive.mailing_list(payload.mailing_list)
    received_after = _parse_iso_datetime(payload.sent_after)
    received_before = _parse_iso_datetime(payload.sent_before)
    subject_query = _parse_boolean_query(payload.subject or "")
    search_in = set(payload.search_in) if payload.search_in else {"subject"}

    rows = _query_message_headers(
        archive, mailing_list.name(), mailing_list.uidvalidity(),
        received_after.isoformat(), received_before.isoformat(),
    )

    mbox_lines: list[str] = []
    message_count = 0
    skipped_count = 0

    for (message_num, from_name, from_addr, subject, message_id, date_str) in rows:
        from_value = f"{from_name} <{from_addr}>" if from_name else (from_addr or "")
        subject_value = subject or ""

        if payload.from_addr and payload.from_addr.lower() not in from_value.lower():
            continue

        prefetched_raw: bytes | None = None
        prefetched_message: Message | None = None
        if subject_query:
            matched = "subject" in search_in and _match_boolean_query(subject_query, subject_value)
            if not matched and "body" in search_in:
                prefetched_raw = _fetch_message_raw(archive, message_num)
                if prefetched_raw is not None:
                    prefetched_message = _email_message_from_bytes(prefetched_raw, policy=_email_policy.default)
                    matched = _match_boolean_query(subject_query, _extract_message_text(prefetched_message))
            if not matched:
                continue

        try:
            raw = prefetched_raw if prefetched_raw is not None else _fetch_message_raw(archive, message_num)
            if raw is None:
                skipped_count += 1
                continue
            message = prefetched_message if prefetched_message is not None else _email_message_from_bytes(raw, policy=_email_policy.default)
            raw_message = _extract_raw_message_text(message)
            if not raw_message:
                skipped_count += 1
                continue

            # RFC4155 mbox format: "From " line (with space, not colon) as separator
            mbox_lines.append(f"From {from_value or 'unknown'} {date_str or 'unknown'}")
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
    fmt = payload.format if payload.format in _FORMAT_EXTENSIONS else "txt"

    rfc_entries = []
    if payload.rfc_ids:
        for rfc_id in payload.rfc_ids[: payload.limit]:
            normalized = _normalize_rfc_id(rfc_id)
            try:
                rfc_entries.append(index.rfc(normalized))
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
            rfc_entries.append(entry)
            if len(rfc_entries) >= payload.limit:
                break

    if not rfc_entries:
        raise HTTPException(status_code=404, detail="No RFCs matched the provided filters")

    ext = _FORMAT_EXTENSIONS[fmt]
    mime_type = _FORMAT_MIME_TYPES[fmt]

    # Single RFC: download raw bytes and return as-is.
    if len(rfc_entries) == 1:
        entry = rfc_entries[0]
        content_url = _rfc_content_url_for_format(entry, fmt)
        if not content_url:
            raise HTTPException(
                status_code=404,
                detail=f"{entry.doc_id} is not available in {_FORMAT_LABELS.get(fmt, fmt)} format",
            )
        session = requests.Session()
        try:
            response = session.get(content_url, timeout=30)
        finally:
            session.close()
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Failed to fetch {entry.doc_id} from RFC Editor")
        filename = f"ietf-{_sanitize_filename(entry.doc_id)}.{ext}"
        return SidecarFileResponse(
            filename=filename,
            mime_type=mime_type,
            content_base64=base64.b64encode(response.content).decode("ascii"),
        )

    # Multiple RFCs (filter-based query, txt only): concatenate with plain separators.
    parts: list[bytes] = []
    session = requests.Session()
    try:
        for entry in rfc_entries:
            content_url = _rfc_content_url_for_format(entry, fmt)
            if not content_url:
                parts.append(f"\n\n{'='*72}\n{entry.doc_id}: {entry.title}\n(not available in {fmt} format)\n".encode())
                continue
            response = session.get(content_url, timeout=30)
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Failed to fetch {entry.doc_id} from RFC Editor")
            separator = f"\n\n{'='*72}\n{entry.doc_id}: {entry.title}\n{'='*72}\n\n".encode()
            parts.append(separator + response.content)
    finally:
        session.close()

    combined = b"".join(parts)
    filename = f"ietf-rfcs.{ext}"
    return SidecarFileResponse(
        filename=filename,
        mime_type=mime_type,
        content_base64=base64.b64encode(combined).decode("ascii"),
    )