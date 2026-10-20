"""One-off batch job: build the dense (embedding) index for a specific,
named list of working groups' mail -- the first increment of embedding
indexing, scoped to the DNSSEC family (dnsext, dnsop; dns-security has no
mail in the archive at all -- confirmed via get_mailarchive_sync_status,
"not_found" -- that WG predates centralized IMAP archiving) per explicit
direction, rather than the full 557-WG/264k-thread corpus, which measured
at ~20s/document on this CPU-only 7B model -- genuinely weeks for the
full mail corpus, months including content.

Reuses LocalSourceIndexService.build_mail_embedding_index as-is (no new
embedding logic) -- just the batch driver + progress logging, same
pattern as this directory's other bulk_*/embed_* scripts.

Run detached, inside the api container:
    docker exec -d docker-api-1 python scripts/agentic_rag/embed_mail_for_wgs.py
"""

import sys
import time

sys.path.insert(0, "/app/api")

from app_factory import create_app

app = create_app()

# Edit this list to embed a different scope next -- see this module's own
# docstring for why these two and not the full WG set (dns-security has
# no mail in the archive at all). "ace" (20 docs) already validated the
# resumable/crash-tolerant rewrite in local_source_index_service.py
# end-to-end with zero failures; this is the real DNSSEC-family run.
TARGET_WGS = ["dnsext", "dnsop"]

with app.app_context():
    from services.local_source_index_service import LocalSourceIndexService

    for wg in TARGET_WGS:
        print(f"[embed-mail] Starting {wg}…", flush=True)
        start = time.monotonic()

        def on_event(evt: dict) -> None:
            if evt.get("current") is not None and evt["current"] % 20 == 0:
                elapsed = time.monotonic() - start
                rate = evt["current"] / elapsed if elapsed > 0 else 0
                eta_min = (evt["total"] - evt["current"]) / rate / 60 if rate > 0 else float("nan")
                print(
                    f"[embed-mail] {wg}: {evt['current']}/{evt['total']} ~{eta_min:.0f}m remaining for this WG",
                    flush=True,
                )

        try:
            result = LocalSourceIndexService.build_mail_embedding_index(wg, on_event=on_event, batch_size=1)
            elapsed = time.monotonic() - start
            print(f"[embed-mail] {wg}: done in {elapsed/60:.1f}m -- {result}", flush=True)
        except Exception as exc:
            print(f"[embed-mail] {wg}: FAILED -- {exc}", flush=True)

    print("[embed-mail] All target WGs done.", flush=True)
