"""Strip quoted reply chains, signatures, and list footers from mailing list emails.

Mailing list messages (IETF lists in particular) carry large amounts of text
that is redundant for retrieval: quoted copies of parent messages, RFC 3676
signatures, mailman footers, and PGP blocks. Because every parent message is
imported as its own chunk, quoted material can be removed aggressively without
losing information — with one exception: inline replies need a short stub of
the quoted text to keep their referent.

Three modes are exposed to the user:

- ``none``     — body is returned unchanged
- ``naive``    — drop every line starting with ``>``
- ``advanced`` — stateful line classification + block-level policy:
    * signature (``-- `` delimiter), mailman footer, PGP and
      "-----Original Message-----" blocks are removed
    * a quote block with no new text after it (top-post trailing quote)
      is dropped entirely
    * a quote block followed by new text (inline reply) is compressed to a
      short attributed stub so the reply keeps its context

Both cleaning modes fall back to the original body when the cleaned result is
too short to stand alone as a chunk (e.g. a bare "+1" reply).
"""

from __future__ import annotations

import re
from typing import Literal

QuoteHandling = Literal["none", "naive", "advanced"]

# A cleaned body shorter than this cannot stand alone as a retrieval chunk;
# fall back to the original so replies like "+1" keep their quoted context.
_MIN_CLEANED_CHARS = 20

# Number of quoted lines kept as context stub for inline replies.
_INLINE_STUB_LINES = 2

_QUOTE_LINE_RE = re.compile(r"^\s{0,3}>")
# RFC 3676 signature delimiter: exactly two hyphens + optional trailing space
# (the trailing space is frequently stripped in transit).
_SIG_DELIMITER_RE = re.compile(r"^-- ?$")
_LIST_FOOTER_RULE_RE = re.compile(r"^_{20,}\s*$")
_LIST_FOOTER_HINT_RE = re.compile(r"mailing list|@ietf\.org", re.IGNORECASE)
_PGP_BEGIN_RE = re.compile(r"^-----BEGIN PGP (?:SIGNATURE|MESSAGE)-----")
_PGP_END_RE = re.compile(r"^-----END PGP (?:SIGNATURE|MESSAGE)-----")
_PGP_SIGNED_HEADER_RE = re.compile(r"^-----BEGIN PGP SIGNED MESSAGE-----")
# Trailing "From:..." on the marker line covers mobile clients that jam the
# forwarded headers onto the marker itself ("-------- Original message --------From: ...").
_ORIGINAL_MESSAGE_RE = re.compile(
    r"^\s*-{2,}\s*(?:original|forwarded) message\s*-{2,}\s*(?:From:.*)?$", re.IGNORECASE
)
_ATTRIBUTION_START_RE = re.compile(r"^\s*On \S")
_ATTRIBUTION_END_RE = re.compile(r"wrote:\s*$")
_ATTRIBUTION_NAME_RE = re.compile(r"(?:^|,)\s*([^,<>]{2,60}?)\s*(?:<[^>]*>)?\s*wrote:\s*$")
# How many extra lines a wrapped "On ... wrote:" attribution may span.
_ATTRIBUTION_MAX_WRAP = 2


def clean_email_body(body: str, mode: QuoteHandling = "none") -> str:
    """Return *body* cleaned according to *mode*.

    Falls back to the stripped original when cleaning leaves less than
    ``_MIN_CLEANED_CHARS`` characters.
    """
    if mode == "none":
        return body

    original = body.strip()
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    if mode == "naive":
        kept = [line for line in lines if not _QUOTE_LINE_RE.match(line)]
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    else:
        cleaned = _clean_advanced(lines).strip()

    if len(cleaned) < _MIN_CLEANED_CHARS:
        return original
    return cleaned


def _clean_advanced(lines: list[str]) -> str:
    lines = _truncate_at_signature(lines)
    lines = _truncate_at_list_footer(lines)
    lines = _truncate_at_original_message_marker(lines)
    lines = _remove_pgp_blocks(lines)
    segments = _segment_quote_blocks(lines)
    return _render_segments(segments)


def _truncate_at_signature(lines: list[str]) -> list[str]:
    for index, line in enumerate(lines):
        if _SIG_DELIMITER_RE.match(line):
            return lines[:index]
    return lines


def _truncate_at_list_footer(lines: list[str]) -> list[str]:
    for index, line in enumerate(lines):
        if _LIST_FOOTER_RULE_RE.match(line) and any(
            _LIST_FOOTER_HINT_RE.search(following) for following in lines[index + 1 : index + 4]
        ):
            return lines[:index]
    return lines


def _truncate_at_original_message_marker(lines: list[str]) -> list[str]:
    for index, line in enumerate(lines):
        if _ORIGINAL_MESSAGE_RE.match(line):
            return lines[:index]
    return lines


def _remove_pgp_blocks(lines: list[str]) -> list[str]:
    kept: list[str] = []
    in_pgp_block = False
    skip_next_hash_header = False
    for line in lines:
        if skip_next_hash_header:
            skip_next_hash_header = False
            if line.startswith("Hash:") or not line.strip():
                continue
        if _PGP_SIGNED_HEADER_RE.match(line):
            skip_next_hash_header = True
            continue
        if _PGP_BEGIN_RE.match(line):
            in_pgp_block = True
            continue
        if _PGP_END_RE.match(line):
            in_pgp_block = False
            continue
        if not in_pgp_block:
            kept.append(line)
    return kept


class _QuoteBlock:
    """A run of quoted lines with its (optional) collapsed attribution."""

    def __init__(self) -> None:
        self.author: str | None = None
        self.quote_lines: list[str] = []

    def __repr__(self) -> str:
        return f"_QuoteBlock(author={self.author!r}, lines={len(self.quote_lines)})"


_Segment = tuple[Literal["text"], list[str]] | tuple[Literal["quote"], _QuoteBlock]


def _match_attribution(lines: list[str], start: int) -> tuple[int, str | None] | None:
    """Detect an "On ... wrote:" attribution starting at *start*.

    Handles the line-wrapped form by joining up to ``_ATTRIBUTION_MAX_WRAP``
    continuation lines. Returns ``(end_index_exclusive, author_name)`` or
    ``None`` when *start* is not an attribution.
    """
    if not _ATTRIBUTION_START_RE.match(lines[start]):
        return None
    joined = lines[start]
    for offset in range(_ATTRIBUTION_MAX_WRAP + 1):
        index = start + offset
        if index >= len(lines):
            return None
        if offset > 0:
            if not lines[index].strip():
                return None
            joined = f"{joined} {lines[index]}"
        if _ATTRIBUTION_END_RE.search(lines[index]):
            name_match = _ATTRIBUTION_NAME_RE.search(joined.strip())
            author = name_match.group(1).strip() if name_match else None
            return index + 1, author
    return None


def _segment_quote_blocks(lines: list[str]) -> list[_Segment]:
    segments: list[_Segment] = []
    text_lines: list[str] = []
    index = 0

    def flush_text() -> None:
        nonlocal text_lines
        trimmed = _trim_blank_edges(text_lines)
        if trimmed:
            segments.append(("text", trimmed))
        text_lines = []

    while index < len(lines):
        attribution = _match_attribution(lines, index)
        is_quote_start = attribution is not None or _QUOTE_LINE_RE.match(lines[index])
        if not is_quote_start:
            text_lines.append(lines[index])
            index += 1
            continue

        flush_text()
        block = _QuoteBlock()
        if attribution is not None:
            index, block.author = attribution
        # Consume the quote run, tolerating blank lines that sit between
        # quoted lines (or between the attribution and the quote itself).
        while index < len(lines):
            if _QUOTE_LINE_RE.match(lines[index]):
                block.quote_lines.append(lines[index])
                index += 1
            elif not lines[index].strip():
                lookahead = index
                while lookahead < len(lines) and not lines[lookahead].strip():
                    lookahead += 1
                if lookahead < len(lines) and _QUOTE_LINE_RE.match(lines[lookahead]):
                    index = lookahead
                else:
                    break
            else:
                break
        segments.append(("quote", block))

    flush_text()
    return segments


def _trim_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _render_segments(segments: list[_Segment]) -> str:
    pieces: list[str] = []
    for position, segment in enumerate(segments):
        if segment[0] == "text":
            pieces.append("\n".join(segment[1]))
            continue

        block = segment[1]
        has_text_after = any(later[0] == "text" for later in segments[position + 1 :])
        if not has_text_after:
            # Trailing full quote: the parent message is its own chunk, drop it.
            continue

        # Inline reply: keep a short attributed stub so the reply keeps its
        # referent without duplicating the whole parent message.
        stub_source = [line for line in block.quote_lines if line.strip()]
        stub = stub_source[:_INLINE_STUB_LINES]
        elided = len(stub_source) - len(stub)
        marker = f"[quoting {block.author}]" if block.author else "[quoted text]"
        stub_lines = [marker, *stub]
        if elided > 0:
            stub_lines.append(f"[+{elided} more quoted lines]")
        pieces.append("\n".join(stub_lines))

    return "\n\n".join(piece for piece in pieces if piece)
