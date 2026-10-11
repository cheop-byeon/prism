"""One-off batch job: pull mailing-list archives for every working group
that owns at least one currently-registered local source.

Reuses resolve_wgs_for_sources.py's own output (storage/draft_to_wg.json)
for the unique WG list, and IetfImportService.sync_local_source_mail for
the actual pull -- the exact same call the Sources tab's own per-source
"Sync" button makes with target=mail, just batched across every WG instead
of one at a time.

Forces a full re-pull of every WG unconditionally (pass --only-missing to
restore the old "skip WGs that already have a file" behavior) -- this
exists because of a real bug in tools/ietfdata_service's own date-range
query (see its _query_message_headers docstring: it was filtering on IMAP
INTERNALDATE, not the real parsed email date, so most WGs' first pull only
ever found whatever one import-timestamp window happened to line up with
the years this script tried). That's now fixed and every affected list's
headers have been reindexed, but the WGs already have a (likely wrong,
narrow) file on disk from the first run, so skip-if-present would leave
the bad data in place instead of replacing it with the real range.

Run detached, inside the api container:
    docker exec -d docker-api-1 python scripts/agentic_rag/bulk_pull_wg_mail.py
"""

import json
import sys
import time
import traceback

sys.path.insert(0, "/app/api")

from app_factory import create_app

app = create_app()

with app.app_context():
    from services.ietf_import_service import IetfImportService

    DRAFT_TO_WG_PATH = "/app/api/storage/draft_to_wg.json"

    draft_to_wg = json.load(open(DRAFT_TO_WG_PATH))
    unique_wgs = sorted({w for w in draft_to_wg.values() if w})

    if "--only-missing" in sys.argv:
        import os
        import re

        PREPROCESSED_DIR = "/app/api/storage/ietf-downloads/preprocessed"
        existing = set()
        for fn in os.listdir(PREPROCESSED_DIR):
            m = re.match(r"ietf-mailarchive-(.+?)-\d{8}-\d{8}-threads\.jsonl$", fn)
            if m:
                existing.add(m.group(1))
        todo = [wg for wg in unique_wgs if wg not in existing]
    else:
        todo = unique_wgs

    print(f"[bulk-mail] {len(unique_wgs)} unique WGs; {len(todo)} to (re-)pull.", flush=True)

    ok_count = 0
    fail_count = 0
    start = time.monotonic()

    for i, wg in enumerate(todo, start=1):
        try:
            result = IetfImportService.sync_local_source_mail(wg)
            elapsed = time.monotonic() - start
            rate = i / elapsed if elapsed > 0 else 0
            eta_min = (len(todo) - i) / rate / 60 if rate > 0 else float("nan")
            if result.get("ok"):
                ok_count += 1
                print(f"[bulk-mail] {i}/{len(todo)} OK {wg} -- {ok_count} ok, {fail_count} failed, ~{eta_min:.0f}m remaining", flush=True)
            else:
                fail_count += 1
                print(f"[bulk-mail] {i}/{len(todo)} FAILED {wg}: {result.get('error')}", flush=True)
        except Exception:
            fail_count += 1
            print(f"[bulk-mail] {i}/{len(todo)} EXCEPTION {wg}:", flush=True)
            traceback.print_exc()
        time.sleep(0.2)

    print(f"[bulk-mail] Done. {ok_count} pulled, {fail_count} failed, out of {len(todo)} attempted.", flush=True)
