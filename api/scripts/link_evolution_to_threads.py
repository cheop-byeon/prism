"""Link draft-evolution changes to the mailing list discussions behind them.

For every section change between draft revisions, this script uses the change
itself as a retrieval query against a mailarchive dataset (ideally imported
with thread grouping) and reports the best-matching discussions - answering
"which discussion caused this change?".

Runs inside the api container against the live stack:

    docker exec -w /app/api docker-api-1 python scripts/link_evolution_to_threads.py \
        --draft draft-ietf-ledbat-congestion \
        --dataset <mailarchive-dataset-id> \
        [--top-k 5] [--max-changes 0] [--report PATH] [--jsonl-out PATH]

Evolution records are generated in memory (prose format, minor edits
skipped); no evolution dataset needs to exist. Revision submission months
are fetched via the sidecar so matches can be flagged as inside/outside the
revision window - the discussion that motivated a change from rev N to N+1
almost always happens between the two submission dates.
"""

# ruff: noqa: T201 — stdout output is the point of a CLI tool

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app/api")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_THREAD_HEADER_RE = re.compile(
    r"^Thread: (?P<subject>.*?) \((?:[^,)]*, )?(?P<start>\d{4}-\d{2})-\d{2}(?: to (?P<end>\d{4}-\d{2})-\d{2})?\)",
)
_MESSAGE_URL_RE = re.compile(r"^--- Message \d+ \| .* \| .* \| (?P<url>\S+) ---$", re.MULTILINE)
# Prose-diff block openers whose following text represents the NEW state.
_NEW_TEXT_BLOCK_RE = re.compile(r"^(?:After \(rev [^)]*\):|Added in rev [^:]*:|New section added in rev [^:]*:)$")

_MAX_QUERY_CHARS = 1200


def _query_from_record(record: dict) -> str:
    """Build a retrieval query from a change record.

    Uses the section title plus the *new* text of the change (After/Added
    blocks) - the wording that discussion consensus produced. Falls back to
    the whole body for removed sections and unparseable bodies.
    """
    content = str(record.get("content") or "")
    lines = content.split("\n")
    body_lines = lines[2:] if len(lines) > 2 else lines  # drop header + blank

    selected: list[str] = []
    collecting = False
    for line in body_lines:
        if _NEW_TEXT_BLOCK_RE.match(line.strip()):
            collecting = True
            continue
        old_state_block = re.match(
            r"^(?:Before \(rev [^)]*\):|Removed in rev [^:]*:|Section removed after rev [^:]*:)$", line.strip()
        )
        if old_state_block:
            collecting = False
            continue
        if collecting and line.strip():
            selected.append(line.strip())

    text = " ".join(selected) if selected else " ".join(line.strip() for line in body_lines if line.strip())
    query = f"{record.get('section_title', '')} {text}".strip()
    return query[:_MAX_QUERY_CHARS]


def _month_range_of_thread(parent_content: str) -> tuple[str, str] | None:
    match = _THREAD_HEADER_RE.match(parent_content)
    if not match:
        return None
    start = match.group("start")
    return start, match.group("end") or start


def _windows_overlap(window: tuple[str, str] | None, thread_range: tuple[str, str] | None) -> bool | None:
    if window is None or thread_range is None:
        return None
    return thread_range[0] <= window[1] and thread_range[1] >= window[0]


def _revision_months(draft_name: str) -> dict[str, tuple[str | None, str | None]]:
    """Map revision string -> (since, until) months via the sidecar suggestions API."""
    from services.ietf_import_service import IetfImportService

    try:
        suggestions = IetfImportService.get_rfc_suggestions(query=draft_name, limit=5)
    except Exception as exc:
        print(f"revision dates unavailable ({exc}); window flags will be omitted")
        return {}
    for suggestion in suggestions:
        if suggestion.get("draft_base_name") != draft_name:
            continue
        months: dict[str, tuple[str | None, str | None]] = {}
        for version in suggestion.get("draft_versions") or []:
            rev = f"{int(version['version']):02d}"
            months[rev] = (version.get("since"), version.get("until"))
        return months
    return {}


def _change_window(record: dict, months: dict[str, tuple[str | None, str | None]]) -> tuple[str, str] | None:
    from_since = (months.get(str(record.get("from_rev"))) or (None, None))[0]
    to_until = (months.get(str(record.get("to_rev"))) or (None, None))[1]
    to_since = (months.get(str(record.get("to_rev"))) or (None, None))[0]
    start = from_since
    end = to_until or to_since
    if not start or not end:
        return None
    return start, end


def _retrieve(dataset_id: str, query: str, top_k: int) -> list[dict]:
    from core.rag.datasource.retrieval_service import RetrievalService
    from core.rag.retrieval.retrieval_methods import RetrievalMethod

    documents = RetrievalService.retrieve(
        retrieval_method=RetrievalMethod.HYBRID_SEARCH,
        dataset_id=dataset_id,
        query=query,
        top_k=top_k,
    )
    results = RetrievalService.format_retrieval_documents(documents)
    matches: list[dict] = []
    for result in results[:top_k]:
        segment = result["segment"] if isinstance(result, dict) else result.segment
        content = segment["content"] if isinstance(segment, dict) else segment.content
        score = result.get("score") if isinstance(result, dict) else result.score
        subject_match = _THREAD_HEADER_RE.match(content)
        url_match = _MESSAGE_URL_RE.search(content)
        matches.append(
            {
                "subject": subject_match.group("subject") if subject_match else content.splitlines()[0][:80],
                "url": url_match.group("url") if url_match else "",
                "score": round(float(score), 4) if score is not None else None,
                "thread_range": _month_range_of_thread(content),
            }
        )
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--draft", help="draft base name; evolution records are generated in memory")
    source.add_argument("--evolution-jsonl", type=Path, help="existing evolution JSONL file to link")
    parser.add_argument("--dataset", required=True, help="mailarchive dataset id to search")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-changes", type=int, default=0, help="0 = link all changes")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--jsonl-out", type=Path, default=None, help="write records enriched with related_threads")
    args = parser.parse_args()

    from app_factory import create_app

    app = create_app()
    with app.app_context():
        from services.ietf_import_service import IetfImportService

        if args.draft:
            sorted_revs, texts = IetfImportService._fetch_all_revisions(args.draft)
            parsed = {rev: IetfImportService._parse_sections(texts[rev]) for rev in sorted_revs}
            records: list[dict] = []
            for from_rev, to_rev in zip(sorted_revs, sorted_revs[1:]):
                records.extend(
                    IetfImportService._diff_version_pair(
                        parsed[from_rev], parsed[to_rev], from_rev, to_rev, args.draft, skip_minor_edits=True
                    )
                )
            draft_name = args.draft
        else:
            records = [json.loads(line) for line in args.evolution_jsonl.read_text().splitlines() if line.strip()]
            draft_name = str(records[0].get("draft_name", "draft")) if records else "draft"

        if args.max_changes:
            records = records[: args.max_changes]
        print(f"{len(records)} changes to link against dataset {args.dataset}")

        months = _revision_months(draft_name)

        lines = [f"# Discussions behind {draft_name} changes", ""]
        in_window_hits = 0
        linked = 0
        for index, record in enumerate(records, start=1):
            query = _query_from_record(record)
            window = _change_window(record, months)
            matches = _retrieve(args.dataset, query, args.top_k)
            for match in matches:
                match["in_window"] = _windows_overlap(window, match.pop("thread_range"))
            record["related_threads"] = matches
            if matches:
                linked += 1
                if any(m["in_window"] for m in matches):
                    in_window_hits += 1

            window_text = f"{window[0]} .. {window[1]}" if window else "unknown"
            lines.append(
                f"## {index}. Section {record.get('section_id')} ({record.get('change_type')}) "
                f"rev {record.get('from_rev')} -> {record.get('to_rev')}"
            )
            lines.append(f"*{record.get('section_title')}* — revision window: {window_text}")
            lines.append(f"query: `{query[:180]}`")
            for match in matches:
                flag = {True: "IN WINDOW", False: "outside window", None: "window unknown"}[match["in_window"]]
                lines.append(f"- ({match['score']}) [{flag}] {match['subject']}  {match['url']}")
            if not matches:
                lines.append("- no matches")
            lines.append("")
            print(f"[{index}/{len(records)}] {record.get('section_id')}: {len(matches)} matches")

        summary = (
            f"changes: {len(records)}, with matches: {linked}, "
            f"with an in-window match: {in_window_hits}"
        )
        lines.insert(2, summary)
        lines.insert(3, "")
        print(summary)

        report_path = args.report or Path(f"/app/api/storage/evolution-links-{draft_name}.md")
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"report: {report_path}")

        if args.jsonl_out:
            args.jsonl_out.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8"
            )
            print(f"enriched records: {args.jsonl_out}")


if __name__ == "__main__":
    main()
