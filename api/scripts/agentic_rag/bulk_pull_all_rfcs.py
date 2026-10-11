"""One-off batch job: pull every published RFC that has an originating
Internet-Draft into local Sources (Sources are keyed on draft_name, so an
RFC with no originating draft -- mostly very old, pre-I-D-era ones -- can't
be registered this way at all; those are just skipped and counted).

Reuses the exact same service call the UI's "Add Source" button uses
(IetfImportService.download_all_draft_revisions_to_local) for every draft,
one at a time -- no new download/parsing logic here, just the bulk driver.

Resumable by construction, not by a separate state file: a draft already
registered as a local source is skipped up front (see already_registered
below), and download_all_draft_revisions_to_local itself skips any
revision file already on disk. Re-running this script after an interruption
just picks up wherever it left off. A draft that registered but had some
revisions fail mid-fetch isn't retried automatically after that -- the
Sources tab's own per-source "Sync" button covers filling in the rest
later.

Run detached, inside the api container (shares the same DB/storage as the
real app, via app_factory.create_app() same as every other script here):

    docker exec -d docker-api-1 python scripts/agentic_rag/bulk_pull_all_rfcs.py

Progress -> stdout, redirect to a log file from the caller if not already
run via -d with its own log capture.
"""

import sys
import time
import traceback

sys.path.insert(0, "/app/api")

from app_factory import create_app

app = create_app()

with app.app_context():
    from extensions.ext_database import db
    from models.account import Account
    from models.ietf_local_source import IetfLocalSource
    from services.ietf_import_service import IetfImportService

    OWNER_EMAIL = "jiebi@uio.no"

    owner = db.session.query(Account).filter_by(email=OWNER_EMAIL).first()
    if not owner:
        sys.exit(f"Owner account {OWNER_EMAIL!r} not found -- aborting.")
    owner_account_id = owner.id

    print(f"[bulk-pull] Loading full RFC index…", flush=True)
    cache = IetfImportService._load_rfc_index_cache()
    draftable = sorted({v["draft"] for v in cache.values() if v.get("draft")})
    no_draft_count = sum(1 for v in cache.values() if not v.get("draft"))

    already_registered = {
        row[0] for row in db.session.query(IetfLocalSource.draft_name).all()
    }

    todo = [d for d in draftable if d not in already_registered]

    print(
        f"[bulk-pull] {len(cache)} total RFCs; {len(draftable)} have an originating draft; "
        f"{no_draft_count} do not (skipped, can't be registered as a Source); "
        f"{len(already_registered)} drafts already pulled; {len(todo)} left to pull.",
        flush=True,
    )

    ok_count = 0
    fail_count = 0
    start = time.monotonic()

    for i, draft_name in enumerate(todo, start=1):
        try:
            result = IetfImportService.download_all_draft_revisions_to_local(
                draft_name, owner_account_id=owner_account_id
            )
            if "error" in result:
                fail_count += 1
                print(f"[bulk-pull] {i}/{len(todo)} FAILED {draft_name}: {result['error']}", flush=True)
            else:
                IetfImportService.set_local_source_shared(draft_name, owner_account_id, True)
                ok_count += 1
                n_revs = len(result.get("revisions", []))
                rfc = result.get("rfc_number")
                elapsed = time.monotonic() - start
                rate = i / elapsed if elapsed > 0 else 0
                eta_min = (len(todo) - i) / rate / 60 if rate > 0 else float("nan")
                print(
                    f"[bulk-pull] {i}/{len(todo)} OK {draft_name} ({n_revs} revisions"
                    f"{f', RFC{rfc}' if rfc else ''}) -- {ok_count} ok, {fail_count} failed, "
                    f"~{eta_min:.0f}m remaining",
                    flush=True,
                )
        except Exception:
            fail_count += 1
            print(f"[bulk-pull] {i}/{len(todo)} EXCEPTION {draft_name}:", flush=True)
            traceback.print_exc()
        # Courtesy pacing against rfc-editor.org / datatracker.ietf.org / the
        # IETF archive -- each draft already issues several sequential
        # requests of its own; this is just extra breathing room on top.
        time.sleep(0.3)

    print(
        f"[bulk-pull] Done. {ok_count} pulled, {fail_count} failed, out of {len(todo)} attempted "
        f"({len(already_registered)} were already present before this run).",
        flush=True,
    )
