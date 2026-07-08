"""Group preprocessed mailing list messages into conversation threads.

IETF consensus emerges across reply chains, not single messages, so for
"what was the objection / what was decided" retrieval the useful unit is the
thread. This module reconstructs threads from the headers preserved in mbox
exports and renders them as JSONL records suitable for Dify's parent-child
indexing: the thread record becomes the parent (LLM context), and the child
splitter produces the per-message retrieval targets.

Grouping is two-tier:

1. ``References``/``In-Reply-To`` message-ID chains via union-find. On real
   IETF archives ~99% of replies carry these headers.
2. Fallback for headerless replies only (subject starting ``Re:``/``Fwd:``):
   join the earliest earlier message with the same normalized subject within
   a time window. Plain subjects never merge, so two independent
   "Minutes" posts stay separate threads.

Because a thread parent is injected into the LLM context on every retrieval
hit, oversized threads are split into sequential parts (``part 2/3``) with
the subject and numbering carried across parts.

Records are dicts as produced by ``IetfImportService.preprocess_mbox_file``:
``id``, ``url``, ``subject``, ``from``, ``date``, ``body``, ``message_id``,
``references``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

# Reply/forward prefixes and mailing list tags, applied repeatedly:
# "Re: [quic] Re: Retry" -> "Retry".
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fwd?|aw)\s*(?:\[\d+\])?\s*:\s*|\[[^\]]{1,60}\]\s*)+", re.IGNORECASE)
_REPLY_SUBJECT_RE = re.compile(r"^\s*(?:\[[^\]]{1,60}\]\s*)*(?:re|fwd?|aw)\s*(?:\[\d+\])?\s*:", re.IGNORECASE)
_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")

# Subject-fallback window: a headerless "Re:" only joins a thread whose
# earliest message is within this many days.
_SUBJECT_FALLBACK_WINDOW_DAYS = 120

# Thread parents are LLM context on every retrieval hit; cap their size.
_DEFAULT_MAX_MESSAGES_PER_PART = 40
_DEFAULT_MAX_CHARS_PER_PART = 30_000


def normalize_subject(subject: str) -> str:
    """Strip reply/forward prefixes and list tags; collapse whitespace."""
    stripped = _SUBJECT_PREFIX_RE.sub("", subject or "")
    return re.sub(r"\s+", " ", stripped).strip()


def _is_reply_subject(subject: str) -> bool:
    return bool(_REPLY_SUBJECT_RE.match(subject or ""))


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _record_message_id(record: dict, index: int) -> str:
    for key in ("message_id", "id"):
        value = str(record.get(key) or "").strip()
        if value:
            return value if value.startswith("<") else f"<{value}>"
    return f"<synthetic-{index}@local>"


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        self._parent[self.find(a)] = self.find(b)


def group_into_threads(records: list[dict]) -> list[list[dict]]:
    """Group message records into threads, chronologically sorted.

    Returns threads ordered by their earliest message date; messages within a
    thread are ordered by date (undated messages keep input order, last).
    """
    # Defensive dedup: older mbox exports can contain the same message twice
    # (direct delivery + list relay); keep the first copy per Message-ID.
    seen_mids: set[str] = set()
    deduped: list[dict] = []
    for index, record in enumerate(records):
        mid = _record_message_id(record, index)
        if mid in seen_mids:
            continue
        seen_mids.add(mid)
        deduped.append(record)
    records = deduped

    union_find = _UnionFind()
    mids = [_record_message_id(record, index) for index, record in enumerate(records)]
    dates = [_parse_date(str(record.get("date") or "")) for record in records]

    linked: list[bool] = []
    for mid, record in zip(mids, records):
        references = _MESSAGE_ID_RE.findall(str(record.get("references") or ""))
        for reference in references:
            union_find.union(mid, reference)
        linked.append(bool(references))

    # Subject fallback for headerless replies: join the earliest message that
    # shares the normalized subject and lies within the time window.
    earliest_by_subject: dict[str, int] = {}
    for index, record in enumerate(records):
        key = normalize_subject(str(record.get("subject") or "")).casefold()
        if not key:
            continue
        current = earliest_by_subject.get(key)
        if current is None or _sort_key(dates[index], index) < _sort_key(dates[current], current):
            earliest_by_subject[key] = index

    for index, record in enumerate(records):
        if linked[index] or not _is_reply_subject(str(record.get("subject") or "")):
            continue
        key = normalize_subject(str(record.get("subject") or "")).casefold()
        anchor = earliest_by_subject.get(key)
        if anchor is None or anchor == index:
            continue
        anchor_date, reply_date = dates[anchor], dates[index]
        if anchor_date and reply_date:
            if abs((reply_date - anchor_date).days) > _SUBJECT_FALLBACK_WINDOW_DAYS:
                continue
        union_find.union(mids[index], mids[anchor])

    groups: dict[str, list[int]] = {}
    for index, mid in enumerate(mids):
        groups.setdefault(union_find.find(mid), []).append(index)

    threads: list[list[dict]] = []
    for indices in groups.values():
        ordered = sorted(indices, key=lambda i: _sort_key(dates[i], i))
        threads.append([records[i] for i in ordered])

    threads.sort(key=lambda thread: _sort_key(_parse_date(str(thread[0].get("date") or "")), 0))
    return threads


def _sort_key(date: datetime | None, index: int) -> tuple[int, float, int]:
    if date is None:
        return (1, 0.0, index)
    return (0, date.timestamp(), index)


def _display_date(record: dict) -> str:
    parsed = _parse_date(str(record.get("date") or ""))
    if parsed:
        return parsed.strftime("%Y-%m-%d")
    return str(record.get("date") or "unknown")[:32] or "unknown"


def build_thread_records(
    threads: list[list[dict]],
    max_messages: int = _DEFAULT_MAX_MESSAGES_PER_PART,
    max_chars: int = _DEFAULT_MAX_CHARS_PER_PART,
) -> list[dict]:
    """Render threads as upload-ready JSONL records (one per thread part).

    Each record keeps the ``id``/``url``/``subject``/``date``/``body`` shape
    the preprocessed-upload path validates; threading fields (``thread_id``,
    ``message_count``, ``part``, ``part_count``, ``participants``) ride along
    as chunk metadata. Author, date, and archive URL are embedded in each
    message marker line so citations survive child splitting.
    """
    records: list[dict] = []
    for thread in threads:
        if not thread:
            continue
        thread_id = _record_message_id(thread[0], 0).strip("<>")
        subject = normalize_subject(str(thread[0].get("subject") or "")) or "(no subject)"
        total = len(thread)
        date_start = _display_date(thread[0])
        date_end = _display_date(thread[-1])
        participants = ", ".join(dict.fromkeys(str(m.get("from") or "unknown") for m in thread))

        parts = _split_into_parts(thread, max_messages=max_messages, max_chars=max_chars)
        part_count = len(parts)
        message_number = 1
        for part_index, part in enumerate(parts, start=1):
            blocks: list[str] = []
            for message in part:
                marker = (
                    f"--- Message {message_number} | {message.get('from') or 'unknown'}"
                    f" | {_display_date(message)} | {message.get('url') or ''} ---"
                )
                blocks.append(f"{marker}\n{str(message.get('body') or '').strip()}")
                message_number += 1

            if part_count > 1:
                first_num = message_number - len(part)
                header = (
                    f"Thread: {subject} (part {part_index}/{part_count},"
                    f" messages {first_num}-{message_number - 1} of {total}, {date_start} to {date_end})"
                )
            elif total == 1:
                header = f"Thread: {subject} (1 message, {date_start})"
            else:
                header = f"Thread: {subject} ({total} messages, {date_start} to {date_end})"

            records.append(
                {
                    "id": thread_id if part_count == 1 else f"{thread_id}#part{part_index}",
                    "url": str(part[0].get("url") or ""),
                    "subject": subject,
                    "date": str(thread[0].get("date") or ""),
                    "body": header + "\n\n" + "\n\n".join(blocks),
                    "thread_id": thread_id,
                    "message_count": total,
                    "part": part_index,
                    "part_count": part_count,
                    "participants": participants,
                }
            )
    return records


def _split_into_parts(thread: list[dict], *, max_messages: int, max_chars: int) -> list[list[dict]]:
    parts: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for message in thread:
        message_chars = len(str(message.get("body") or ""))
        if current and (len(current) >= max_messages or current_chars + message_chars > max_chars):
            parts.append(current)
            current = []
            current_chars = 0
        current.append(message)
        current_chars += message_chars
    if current:
        parts.append(current)
    return parts
