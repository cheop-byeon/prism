"""One-off batch job: build the BM25 (sparse) index for every currently-
registered local source's draft/RFC text, and for every working group's
mail that's been pulled -- the exact same calls the Sources tab's own
"Build index" button makes, just batched. No embeddings here (that's a
separate, Ollama-backed step via build_*_embedding_index, not requested).

Purely local file I/O + tokenization (rank_bm25), no network calls -- much
faster than either prior bulk job. Resumable: skips any draft/WG whose
index file already exists.

Run detached, inside the api container:
    docker exec -d docker-api-1 python scripts/agentic_rag/bulk_build_bm25_index.py
"""

import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, "/app/api")

from app_factory import create_app

app = create_app()

with app.app_context():
    from extensions.ext_database import db
    from models.ietf_local_source import IetfLocalSource
    from services.ietf_import_service import IetfImportService
    from services.local_source_index_service import LocalSourceIndexService

    DRAFT_TO_WG_PATH = "/app/api/storage/draft_to_wg.json"
    INDEX_DIR = Path("/app/api/storage/ietf-downloads/index")

    draft_names = sorted(row[0] for row in db.session.query(IetfLocalSource.draft_name).all())
    draft_to_wg: dict[str, str | None] = json.load(open(DRAFT_TO_WG_PATH))

    print(f"[bulk-index] Resolving RFC numbers for {len(draft_names)} drafts from the cached index…", flush=True)
    rfc_numbers = IetfImportService.get_rfc_numbers_for_draft_names(draft_names)

    # -- Content tier: one per draft --------------------------------------
    content_todo = [d for d in draft_names if not (INDEX_DIR / f"{d}-content.json").is_file()]
    print(f"[bulk-index] Content: {len(draft_names) - len(content_todo)} already built, {len(content_todo)} to build.", flush=True)

    ok = fail = 0
    start = time.monotonic()
    for i, draft_name in enumerate(content_todo, start=1):
        try:
            result = LocalSourceIndexService.build_content_index(draft_name, rfc_numbers.get(draft_name))
            if "error" in result:
                fail += 1
                print(f"[bulk-index] content {i}/{len(content_todo)} FAILED {draft_name}: {result['error']}", flush=True)
            else:
                ok += 1
                if i % 500 == 0 or i == len(content_todo):
                    elapsed = time.monotonic() - start
                    rate = i / elapsed if elapsed > 0 else 0
                    eta_min = (len(content_todo) - i) / rate / 60 if rate > 0 else float("nan")
                    print(f"[bulk-index] content {i}/{len(content_todo)} ok={ok} fail={fail} ~{eta_min:.0f}m remaining", flush=True)
        except Exception:
            fail += 1
            print(f"[bulk-index] content {i}/{len(content_todo)} EXCEPTION {draft_name}:", flush=True)
            traceback.print_exc()
    print(f"[bulk-index] Content done: {ok} ok, {fail} failed.", flush=True)

    # -- Mail tier: one per unique WG -------------------------------------
    # --force-mail rebuilds every WG's mail index unconditionally -- for
    # re-running after the underlying mail content itself changed (e.g.
    # after fixing ietfdata_service's date-filtering bug and re-pulling),
    # where an existing -mail.json file reflects the OLD, wrong content,
    # not "already up to date".
    unique_wgs = sorted({w for w in draft_to_wg.values() if w})
    if "--force-mail" in sys.argv:
        mail_todo = unique_wgs
    else:
        mail_todo = [w for w in unique_wgs if not (INDEX_DIR / f"{w}-mail.json").is_file()]
    print(f"[bulk-index] Mail: {len(unique_wgs) - len(mail_todo)} already built, {len(mail_todo)} to build.", flush=True)

    ok = fail = 0
    start = time.monotonic()
    for i, wg in enumerate(mail_todo, start=1):
        try:
            result = LocalSourceIndexService.build_mail_index(wg)
            if "error" in result:
                fail += 1
                print(f"[bulk-index] mail {i}/{len(mail_todo)} FAILED {wg}: {result['error']}", flush=True)
            else:
                ok += 1
                if i % 100 == 0 or i == len(mail_todo):
                    elapsed = time.monotonic() - start
                    rate = i / elapsed if elapsed > 0 else 0
                    eta_min = (len(mail_todo) - i) / rate / 60 if rate > 0 else float("nan")
                    print(f"[bulk-index] mail {i}/{len(mail_todo)} ok={ok} fail={fail} ~{eta_min:.0f}m remaining", flush=True)
        except Exception:
            fail += 1
            print(f"[bulk-index] mail {i}/{len(mail_todo)} EXCEPTION {wg}:", flush=True)
            traceback.print_exc()
    print(f"[bulk-index] Mail done: {ok} ok, {fail} failed.", flush=True)
    print("[bulk-index] All done.", flush=True)
