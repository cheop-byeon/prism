"""Explicitly build one of the whole-corpus RFC search indices backing
search_rfc_corpus_bm25 / find_similar_rfcs_by_meaning (see
ask_decision_agent.py and services/rfc_corpus_index_service.py).

Deliberately a separate, manually-run script rather than something a tool
call triggers lazily: --what full_text and --what embeddings are each a
real one-time cost (tens of minutes of network fetching, or on the order
of an hour of local-model inference, respectively) that shouldn't
silently block an agent's tool call the first time it's used.

Run inside the ietf_decision_agent_ui (or api) container, e.g.:

    docker exec -w /app/api docker-ietf_decision_agent_ui-1 \\
        python scripts/agentic_rag/build_rfc_corpus_index.py --what title_abstract
    docker exec -w /app/api docker-ietf_decision_agent_ui-1 \\
        python scripts/agentic_rag/build_rfc_corpus_index.py --what full_text
    docker exec -w /app/api docker-ietf_decision_agent_ui-1 \\
        python scripts/agentic_rag/build_rfc_corpus_index.py --what embeddings

--what embeddings requires Ollama running on the host with rfc-dralign-q8
already registered (see ../../../dev/start-embedding-server) -- it embeds
every RFC's title+abstract, building on --what title_abstract if that
corpus isn't already cached.
"""

# ruff: noqa: T201 — stdout output is the point of a CLI tool

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, "/app/api")


def _print_progress(event: dict) -> None:
    print(f"\r{event.get('message', '')}", end="", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--what", required=True, choices=["title_abstract", "full_text", "embeddings"])
    parser.add_argument("--workers", type=int, default=10, help="concurrency for --what full_text")
    args = parser.parse_args()

    from services.rfc_corpus_index_service import RfcCorpusIndexService

    t0 = time.time()
    if args.what == "title_abstract":
        corpus = RfcCorpusIndexService.build_title_abstract_corpus(on_event=_print_progress, force=True)
        print(f"\nBuilt title+abstract corpus: {len(corpus)} RFCs in {time.time() - t0:.0f}s")
    elif args.what == "full_text":
        result = RfcCorpusIndexService.build_full_text_corpus(on_event=_print_progress, max_workers=args.workers)
        print(f"\nFull text: {result['total']} RFCs total, {result['fetched_this_run']} fetched this run, "
              f"{len(result['failed'])} failed, in {time.time() - t0:.0f}s")
        if result["failed"]:
            print("Failed:", ", ".join(result["failed"][:20]), "..." if len(result["failed"]) > 20 else "")
    else:
        result = RfcCorpusIndexService.build_embedding_index(on_event=_print_progress)
        print(f"\nBuilt embedding index: {result['count']} RFCs, {result['dims']} dims, in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
