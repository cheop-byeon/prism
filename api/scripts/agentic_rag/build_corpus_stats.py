"""One-off (re-runnable) job: compute aggregate statistics over the whole
locally-pulled corpus -- RFC/draft counts, revisions per draft, WG mail
coverage -- and cache them to a small JSON file so the Sources tab's new
stats panel can read them instantly instead of re-scanning every index
file (some mail index files are 100MB+) on every page load.

Re-run this any time the corpus changes meaningfully (another bulk pull,
more mail indexed, etc.) -- it's cheap relative to the bulk jobs that
produced the data (mostly local file reads, plus one cheap status call
per WG to the mail sidecar for the raw pre-threading message count --
~3 minutes total for 557 WGs), just not cheap enough to do live on every
request.

Run detached, inside the api container:
    docker exec -d docker-api-1 python scripts/agentic_rag/build_corpus_stats.py
"""

import json
import re
import sys
import time

sys.path.insert(0, "/app/api")

from app_factory import create_app

app = create_app()

with app.app_context():
    from extensions.ext_database import db
    from models.ietf_local_source import IetfLocalSource
    from services.ietf_import_service import IetfImportService

    DRAFT_TO_WG_PATH = "/app/api/storage/draft_to_wg.json"
    INDEX_DIR_STR = "/app/api/storage/ietf-downloads/index"
    OUT_PATH = f"{INDEX_DIR_STR}/_corpus_stats.json"

    start = time.monotonic()

    draft_names = sorted(row[0] for row in db.session.query(IetfLocalSource.draft_name).all())
    total_drafts = len(draft_names)
    print(f"[corpus-stats] {total_drafts} local sources.", flush=True)

    rfc_numbers = IetfImportService.get_rfc_numbers_for_draft_names(draft_names)
    rfc_drafts = [d for d in draft_names if rfc_numbers.get(d)]
    total_rfcs = len(rfc_drafts)
    print(f"[corpus-stats] {total_rfcs} have been published as an RFC.", flush=True)

    # -- Revisions per draft, from each draft's own content index file -----
    import os

    revision_counts: list[int] = []
    for d in draft_names:
        path = f"{INDEX_DIR_STR}/{d}-content.json"
        if not os.path.isfile(path):
            continue
        try:
            payload = json.loads(open(path).read())
        except (json.JSONDecodeError, OSError):
            continue
        n_revs = sum(1 for doc in payload.get("documents", []) if doc.get("kind") == "draft")
        revision_counts.append(n_revs)
    avg_revisions_per_draft = sum(revision_counts) / len(revision_counts) if revision_counts else 0.0
    print(f"[corpus-stats] Average revisions per draft: {avg_revisions_per_draft:.2f} (over {len(revision_counts)} drafts).", flush=True)

    # -- WGs + mail counts --------------------------------------------------
    draft_to_wg: dict[str, str | None] = json.load(open(DRAFT_TO_WG_PATH))
    unique_wgs = sorted({w for w in draft_to_wg.values() if w})
    total_wgs = len(unique_wgs)

    import re as _re

    PREPROCESSED_DIR = "/app/api/storage/ietf-downloads/preprocessed"
    preprocessed_files = os.listdir(PREPROCESSED_DIR) if os.path.isdir(PREPROCESSED_DIR) else []

    wg_email_counts: dict[str, int] = {}  # threads (post-processed), from the BM25 index
    wg_file_counts: dict[str, int] = {}  # one .jsonl per calendar year with any thread in it
    for wg in unique_wgs:
        wg_file_counts[wg] = sum(
            1 for fn in preprocessed_files if _re.match(rf"ietf-mailarchive-{_re.escape(wg)}-\d{{8}}-\d{{8}}-threads\.jsonl$", fn)
        )
        path = f"{INDEX_DIR_STR}/{wg}-mail.json"
        if not os.path.isfile(path):
            wg_email_counts[wg] = 0
            continue
        # Avoid a full json.loads() of files that can be 100MB+ -- counting
        # occurrences of the one fixed per-document marker every mail_thread
        # entry has is exact (build_mail_index always writes one key/value
        # pair with this literal text per document) and far cheaper than
        # parsing the whole structure just to len() it.
        count = 0
        with open(path, "r", errors="ignore") as f:
            for chunk in iter(lambda: f.read(1 << 20), ""):
                count += chunk.count('"kind": "mail_thread"')
        wg_email_counts[wg] = count

    total_mail_threads = sum(wg_email_counts.values())
    total_mail_files = sum(wg_file_counts.values())
    wgs_with_mail = sum(1 for c in wg_email_counts.values() if c > 0)
    print(
        f"[corpus-stats] {total_wgs} unique WGs, {wgs_with_mail} with real mail, "
        f"{total_mail_threads} total mail threads across {total_mail_files} yearly files.",
        flush=True,
    )

    # -- Raw (pre-threading) message counts, straight from the sidecar's own
    # per-list status -- one network call each, ~0.3s apiece, so ~3 minutes
    # for all 557 WGs; a thread bundles multiple replies into one record
    # (see build_thread_records), so this is always >= total_mail_threads
    # and is the real "how many individual emails is this" number.
    wg_raw_counts: dict[str, int] = {}
    for i, wg in enumerate(unique_wgs, start=1):
        try:
            status = IetfImportService.get_mailarchive_sync_status(wg)
            wg_raw_counts[wg] = int(status.get("message_count") or 0)
        except Exception:
            wg_raw_counts[wg] = 0
        if i % 100 == 0 or i == len(unique_wgs):
            print(f"[corpus-stats] raw message counts: {i}/{len(unique_wgs)} WGs queried.", flush=True)
    total_raw_emails = sum(wg_raw_counts.values())
    print(f"[corpus-stats] {total_raw_emails} total raw messages (pre-threading) across all WGs.", flush=True)

    # -- Per-RFC mail coverage ----------------------------------------------
    rfcs_with_wg_mail = sum(
        1 for d in rfc_drafts if draft_to_wg.get(d) and wg_email_counts.get(draft_to_wg.get(d), 0) > 0
    )
    ratio_rfcs_with_mail = rfcs_with_wg_mail / total_rfcs if total_rfcs else 0.0
    # Averaged over RFCs/WGs that actually HAVE mail, not the full
    # population -- dividing by every RFC (most of which have zero, see
    # ratio_rfcs_with_mail above) made the "average" look far lower than
    # what an RFC with mail actually has, which is the number that matters
    # when deciding whether a given source is worth reading its mail.
    avg_emails_per_rfc = total_mail_threads / rfcs_with_wg_mail if rfcs_with_wg_mail else 0.0
    avg_emails_per_wg = total_mail_threads / wgs_with_mail if wgs_with_mail else 0.0

    stats = {
        "total_sources": total_drafts,
        "total_rfcs": total_rfcs,
        "avg_revisions_per_draft": round(avg_revisions_per_draft, 2),
        "total_wgs": total_wgs,
        "wgs_with_mail": wgs_with_mail,
        # Three different counts of "how much mail" on purpose -- see
        # this script's own module docstring update below / the Sources
        # tab's stats panel, which shows all three rather than picking one:
        # raw_emails is individual messages (pre-threading, straight from
        # the sidecar's own per-list status); mail_threads is what's
        # actually indexed/searchable (one record per conversation, see
        # build_thread_records -- always <= total_raw_emails since a
        # thread bundles its replies into one record); mail_files is the
        # yearly .jsonl file count those threads are stored across.
        "total_raw_emails": total_raw_emails,
        "total_mail_threads": total_mail_threads,
        "total_mail_files": total_mail_files,
        "avg_emails_per_rfc": round(avg_emails_per_rfc, 2),
        "avg_emails_per_wg": round(avg_emails_per_wg, 2),
        "rfcs_with_wg_mail": rfcs_with_wg_mail,
        "ratio_rfcs_with_mail": round(ratio_rfcs_with_mail, 4),
        "computed_at": time.time(),
    }
    with open(OUT_PATH, "w") as f:
        json.dump(stats, f, indent=2)

    elapsed = time.monotonic() - start
    print(f"[corpus-stats] Wrote {OUT_PATH} in {elapsed:.1f}s: {json.dumps(stats)}", flush=True)

    # -- Per-draft status, for the Sources list's own badges + toggle
    # filters (has WG mail?, published as an RFC?) -- cached here (rather
    # than resolved live per row, which is what /api/corpora/freshness
    # already does for local-only entries and is explicitly documented as
    # the slow part of that endpoint) since it's just a lookup over data
    # this same script already computed above.
    per_draft_path = f"{INDEX_DIR_STR}/_draft_status.json"
    per_draft = {
        d: {
            "wg": draft_to_wg.get(d),
            "email_count": wg_email_counts.get(draft_to_wg.get(d), 0) if draft_to_wg.get(d) else 0,
            "has_wg_mail": bool(draft_to_wg.get(d) and wg_email_counts.get(draft_to_wg.get(d), 0) > 0),
            "rfc_number": rfc_numbers.get(d),
            "is_rfc": bool(rfc_numbers.get(d)),
        }
        for d in draft_names
    }
    with open(per_draft_path, "w") as f:
        json.dump(per_draft, f)
    print(f"[corpus-stats] Wrote {per_draft_path} ({len(per_draft)} drafts).", flush=True)
