"""Evaluate thread grouping against real mbox exports.

Reconstructs threads from local IETF mailarchive mbox files using the same
code path as the "group into threads" import option, then reports the thread
structure (count, size distribution, part splitting) and writes a markdown
report with a few fully rendered thread records for review.

Usage (from the api/ directory):

    uv run python scripts/eval_threading.py <mbox-file-or-dir> \
        [--quote-handling advanced] [--samples 5] [--report PATH]

When given a directory, all *.mbox files are used except "*-dup.mbox"
duplicates. --quote-handling runs bodies through the quote cleaner first,
matching what a combined import would produce.
"""

# ruff: noqa: T201 — stdout output is the point of a CLI eval tool

from __future__ import annotations

import argparse
import email as email_lib
import email.header as email_header_lib
import re
import statistics
import sys
from pathlib import Path

# Allow running as a plain script from the api/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.email_quote_cleaning import clean_email_body
from services.email_threading import build_thread_records, group_into_threads


def _decode_header(value: str) -> str:
    try:
        decoded = ""
        for text, charset in email_header_lib.decode_header(value):
            decoded += text.decode(charset or "utf-8", errors="replace") if isinstance(text, bytes) else text
        return decoded
    except Exception:
        return value


def _extract_text_body(msg: email_lib.message.Message) -> str | None:
    parts: list[str] = []
    candidates = msg.walk() if msg.is_multipart() else [msg]
    for part in candidates:
        if part.get_content_type() != "text/plain":
            continue
        if "attachment" in str(part.get("Content-Disposition", "")):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            try:
                parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            except LookupError:
                parts.append(payload.decode("utf-8", errors="replace"))
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    return text or None


def _parse_mbox(path: Path, quote_handling: str) -> list[dict]:
    records: list[dict] = []
    for raw in re.split(rb"(?m)^From ", path.read_bytes()):
        if not raw or raw.startswith(b"# "):
            continue
        try:
            msg = email_lib.message_from_bytes(b"From " + raw)
        except Exception:
            continue
        body = _extract_text_body(msg)
        message_id = (msg.get("Message-ID") or "").strip()
        if not body or not message_id:
            continue
        if quote_handling != "none":
            body = clean_email_body(body, mode=quote_handling)  # type: ignore[arg-type]
        archived = (msg.get("Archived-At") or "").strip().strip("<>")
        records.append(
            {
                "id": archived.rstrip("/").split("/")[-1] if archived else message_id.strip("<>"),
                "url": archived,
                "subject": _decode_header(msg.get("Subject", "")).strip(),
                "from": _decode_header(msg.get("From", "")).strip(),
                "date": msg.get("Date", "") or "",
                "message_id": message_id,
                "references": " ".join(
                    p for p in ((msg.get("References") or "").strip(), (msg.get("In-Reply-To") or "").strip()) if p
                ),
                "body": body.strip(),
            }
        )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="mbox file or directory containing *.mbox files")
    parser.add_argument(
        "--quote-handling", choices=("none", "naive", "advanced"), default="none", help="clean bodies first"
    )
    parser.add_argument("--samples", type=int, default=5, help="rendered threads in the report (default: 5)")
    parser.add_argument(
        "--report", type=Path, default=None, help="report path (default: threading-eval.md next to input)"
    )
    args = parser.parse_args()

    if args.target.is_file():
        mbox_files = [args.target]
    elif args.target.is_dir():
        mbox_files = sorted(p for p in args.target.glob("*.mbox") if not p.name.endswith("-dup.mbox"))
    else:
        sys.exit(f"No such file or directory: {args.target}")
    if not mbox_files:
        sys.exit(f"No .mbox files found under {args.target}")

    records: list[dict] = []
    for mbox_file in mbox_files:
        records.extend(_parse_mbox(mbox_file, args.quote_handling))
    if not records:
        sys.exit("No parseable messages found.")

    threads = group_into_threads(records)
    thread_records = build_thread_records(threads)

    sizes = sorted((len(t) for t in threads), reverse=True)
    multi = [s for s in sizes if s > 1]
    record_sizes = sorted((len(r["body"]) for r in thread_records), reverse=True)
    split_threads = {r["thread_id"] for r in thread_records if r["part_count"] > 1}

    singleton_count = len(sizes) - len(multi)
    print(f"messages:                    {len(records)} from {len(mbox_files)} file(s)")
    print(f"threads:                     {len(threads)} ({len(multi)} multi-message, {singleton_count} singletons)")
    print(f"messages in multi threads:   {sum(multi)} ({100 * sum(multi) / len(records):.1f}%)")
    print(f"thread sizes:                max {sizes[0]}, median {statistics.median(sizes):.0f}, top: {sizes[:8]}")
    print(f"thread records (parts):      {len(thread_records)}, from {len(split_threads)} threads split into parts")
    print(f"record body chars:           max {record_sizes[0]:,}, median {statistics.median(record_sizes):,.0f}")

    biggest = sorted(threads, key=len, reverse=True)[: args.samples]
    report_path = args.report or (mbox_files[0].parent / "threading-eval.md")
    lines = ["# Threading evaluation", ""]
    for thread in biggest:
        for record in build_thread_records([thread]):
            lines.append(f"## {record['subject']} — part {record['part']}/{record['part_count']}")
            lines.append(f"*{record['message_count']} messages, id {record['id']}, {len(record['body'])} chars*")
            lines.append("```text")
            lines.append(record["body"][:4000] + ("\n… [truncated]" if len(record["body"]) > 4000 else ""))
            lines.append("```")
        lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nRendered {args.samples} largest threads: {report_path}")


if __name__ == "__main__":
    main()
