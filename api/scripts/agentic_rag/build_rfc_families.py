"""Precomputes RFC "families" (topically/structurally related clusters) so
the RFC Graph tab serves instantly from the database instead of rebuilding
a per-RFC graph live on every click (confirmed live: a single RFC's graph
build -- structural neighbors + citation-family harvest -- can take 9+
minutes of real Datatracker calls).

Three stages, run as separate subcommands since they have very different
cost profiles -- see IetfImportService's own docstrings for the full
reasoning on each:

  --structural   The cheap tier: two bulk Datatracker calls (obsoletes/
                 updates relations corpus-wide), ~2 minutes. Always safe
                 to rerun -- wholesale replace, not incremental.

  --citations    The expensive tier: one find_related_rfc_family() call
                 per RFC (~9,930 RFCs total), each involving several live,
                 paginated Datatracker citation lookups -- genuinely many
                 hours for the whole corpus. Resumable: ietf_rfc_citation_
                 fetch_log records every RFC already attempted, so an
                 interrupted run only ever redoes the one RFC it was
                 mid-fetch on when stopped. Run this detached, the same
                 way embed_mail_for_wgs.py runs.

  --cluster      Cheap, local, no network: union-find over whatever
                 structural + citation edges exist so far, written as
                 IetfRfcFamily/IetfRfcFamilyMember rows. Safe to rerun at
                 any point as --citations accumulates more edges --
                 families only get more complete over time, never wrong
                 in the interim (an RFC not yet clustered just falls back
                 to the old live build, same as before this existed).

Typical sequence for a fresh build:
    docker exec -d docker-api-1 python scripts/agentic_rag/build_rfc_families.py --structural
    docker exec -d docker-api-1 python scripts/agentic_rag/build_rfc_families.py --citations
    # (the above runs for hours; --cluster can be rerun periodically
    #  against its partial progress, or once at the end)
    docker exec -d docker-api-1 python scripts/agentic_rag/build_rfc_families.py --cluster

--citations accepts --limit N to only process the next N not-yet-attempted
RFCs -- useful for a quick validation run before committing to the full
corpus (matching embed_mail_for_wgs.py's own earlier "ace" (20 docs)
dry run before the real WG-scoped run).
"""

import argparse
import sys
import time

sys.path.insert(0, "/app/api")

from app_factory import create_app

app = create_app()


def _print_progress(event: dict) -> None:
    print(event.get("message", ""), flush=True)


def run_structural() -> None:
    from services.ietf_import_service import IetfImportService

    print("[rfc-families] Building structural (obsoletes/updates) edge cache…", flush=True)
    start = time.monotonic()
    result = IetfImportService.rebuild_structural_edges_cache(on_event=_print_progress)
    if result.get("error"):
        print(f"[rfc-families] FAILED -- {result['error']}", flush=True)
        return
    print(f"[rfc-families] Structural edges: {result['count']} in {time.monotonic() - start:.0f}s", flush=True)


def run_citations(limit: int | None) -> None:
    from extensions.ext_database import db
    from models.ietf_rfc_family import IetfRfcCitationFetchLog
    from services.ietf_import_service import IetfImportService
    from services.rfc_corpus_index_service import RfcCorpusIndexService

    print("[rfc-families] Listing every published RFC (RFC Editor's own index)…", flush=True)
    all_names = RfcCorpusIndexService._list_all_rfc_names()
    already_done = {row[0] for row in db.session.query(IetfRfcCitationFetchLog.rfc_number).all()}
    todo = [n for n in all_names if n not in already_done]
    if limit is not None:
        todo = todo[:limit]
    print(
        f"[rfc-families] {len(all_names)} RFCs total, {len(already_done)} already fetched, "
        f"{len(todo)} to go this run.",
        flush=True,
    )

    start = time.monotonic()
    failures = 0
    for i, rfc_name in enumerate(todo, start=1):
        result = IetfImportService.fetch_citation_edges_for_rfc(rfc_name)
        if result.get("error"):
            failures += 1
        if i % 20 == 0 or i == len(todo):
            elapsed = time.monotonic() - start
            rate = elapsed / i
            remaining_min = rate * (len(todo) - i) / 60
            print(
                f"[rfc-families] citations: {i}/{len(todo)} ({failures} failed) "
                f"~{remaining_min:.0f}m remaining",
                flush=True,
            )
    print(f"[rfc-families] Citation harvest run complete -- {len(todo)} processed, {failures} failed.", flush=True)


def run_cluster() -> None:
    from services.ietf_import_service import IetfImportService

    print("[rfc-families] Clustering families from current edges…", flush=True)
    start = time.monotonic()
    result = IetfImportService.rebuild_families(on_event=_print_progress)
    if result.get("error"):
        print(f"[rfc-families] FAILED -- {result['error']}", flush=True)
        return
    print(
        f"[rfc-families] {result['families']} families, {result['members']} RFCs clustered "
        f"in {time.monotonic() - start:.0f}s",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--structural", action="store_true", help="Rebuild the structural (obsoletes/updates) cache")
    parser.add_argument("--citations", action="store_true", help="Harvest citation-family edges (long-running)")
    parser.add_argument("--cluster", action="store_true", help="Recompute families from current edges")
    parser.add_argument("--limit", type=int, default=None, help="With --citations, only process N not-yet-done RFCs")
    args = parser.parse_args()

    if not (args.structural or args.citations or args.cluster):
        parser.error("Pass at least one of --structural, --citations, --cluster")

    with app.app_context():
        if args.structural:
            run_structural()
        if args.citations:
            run_citations(args.limit)
        if args.cluster:
            run_cluster()


if __name__ == "__main__":
    main()
