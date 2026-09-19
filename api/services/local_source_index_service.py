"""Per-local-source BM25 search over draft/RFC text and threaded mail --
everything IetfImportService's download-to-local methods have already put
on disk for one locally-tracked source (see register_local_source).

Deliberately separate from RfcCorpusIndexService: that one indexes the
whole published-RFC corpus's title+abstract or full text, never mail, and
is built once for the entire corpus. This one is scoped to a single
draft's own local files (drafts, its RFC, its WG's threaded mail) and is
cheap enough -- a handful of small text files, not ~9,930 RFCs -- to
rebuild from scratch on every "Build index" click rather than needing any
incremental-rebuild machinery.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import numpy as np
from rank_bm25 import BM25Okapi

_DOWNLOADS_DIR = Path("/app/api/storage/ietf-downloads")
_PREPROCESSED_DIR = _DOWNLOADS_DIR / "preprocessed"
_INDEX_DIR = _DOWNLOADS_DIR / "index"

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Same local model/endpoint as RfcCorpusIndexService.build_embedding_index --
# duplicated rather than shared for now since these are each's only caller
# and the two services intentionally don't depend on each other (see this
# file's own docstring on why); worth a shared helper if a third caller
# shows up.
_OLLAMA_EMBED_URL = "http://host.docker.internal:11434/api/embed"
_EMBED_MODEL = "rfc-dralign-q8"


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class LocalSourceIndexService:
    @staticmethod
    def _index_path(draft_name: str) -> Path:
        return _INDEX_DIR / f"{draft_name}.json"

    @staticmethod
    def _gather_documents(draft_name: str, wg: str | None, rfc_number: str | None) -> list[dict]:
        documents: list[dict] = []

        revision_re = re.compile(rf"^{re.escape(draft_name)}-(\d+)\.txt$")
        for entry in sorted(_DOWNLOADS_DIR.glob(f"{draft_name}-*.txt")):
            if not revision_re.match(entry.name):
                continue
            documents.append({"kind": "draft", "id": entry.stem, "text": entry.read_text(errors="ignore")})

        # Every downloaded RFC (any draft's) lands in the same shared
        # directory with no draft-name prefix -- glob only this draft's own
        # RFC number, not every "ietf-RFC*" file that happens to be there
        # from downloading something else entirely.
        if rfc_number:
            for entry in sorted(_DOWNLOADS_DIR.glob(f"ietf-RFC{rfc_number}.*")):
                if entry.is_file():
                    documents.append(
                        {
                            "kind": "rfc",
                            "id": entry.stem.removeprefix("ietf-"),
                            "text": entry.read_text(errors="ignore"),
                        }
                    )

        if wg and _PREPROCESSED_DIR.is_dir():
            for entry in sorted(_PREPROCESSED_DIR.glob(f"ietf-mailarchive-{wg}-*-threads.jsonl")):
                for line in entry.read_text(errors="ignore").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = f"{record.get('subject', '')}\n{record.get('body', '')}"
                    documents.append(
                        {"kind": "mail_thread", "id": record.get("url") or record.get("id") or entry.stem, "text": text}
                    )

        return documents

    @staticmethod
    def build_index(draft_name: str, wg: str | None, rfc_number: str | None, on_event=None) -> dict:
        if on_event:
            on_event({"stage": "indexing", "message": "Gathering local content…"})
        documents = LocalSourceIndexService._gather_documents(draft_name, wg, rfc_number)
        if not documents:
            return {"error": "Nothing downloaded locally yet for this draft -- sync first."}

        _INDEX_DIR.mkdir(parents=True, exist_ok=True)
        LocalSourceIndexService._index_path(draft_name).write_text(
            json.dumps({"draft_name": draft_name, "wg": wg, "documents": documents})
        )

        counts: dict[str, int] = {}
        for doc in documents:
            counts[doc["kind"]] = counts.get(doc["kind"], 0) + 1
        if on_event:
            on_event({"stage": "indexing", "message": f"Indexed {len(documents)} documents"})
        return {"total_documents": len(documents), "by_kind": counts}

    @staticmethod
    def _embeddings_matrix_path(draft_name: str) -> Path:
        return _INDEX_DIR / f"{draft_name}-embeddings.npy"

    @staticmethod
    def _embeddings_ids_path(draft_name: str) -> Path:
        return _INDEX_DIR / f"{draft_name}-embeddings-ids.json"

    @staticmethod
    def build_embedding_index(
        draft_name: str, wg: str | None, rfc_number: str | None, on_event=None, batch_size: int = 20
    ) -> dict:
        """Same document set as build_index (the BM25 tier above), embedded
        via the same local rfc-dralign-q8 model RfcCorpusIndexService uses
        for its own whole-corpus embedding tier -- see that module's
        docstring for why this calls the embedding model directly rather
        than going through Dify's own Dataset/vector-store pipeline.

        Meant to be run from a Celery task (see
        tasks/local_source_embedding_index_task.py), not called directly
        from a request handler -- a source's worth of mail/draft/RFC text
        is small compared to the whole corpus, but still slow enough
        (measured ~160ms/doc for RfcCorpusIndexService's short
        title+abstract texts; these are full documents) that it must
        survive the requesting page closing, unlike build_index's BM25
        tier which is cheap enough to run inline in the request/SSE.
        """
        if on_event:
            on_event({"stage": "indexing", "current": 0, "total": 0, "message": "Gathering local content…"})
        documents = LocalSourceIndexService._gather_documents(draft_name, wg, rfc_number)
        if not documents:
            return {"error": "Nothing downloaded locally yet for this draft -- sync first."}

        ids = [doc["id"] for doc in documents]
        texts = [doc["text"] for doc in documents]

        vectors: list[list[float]] = []
        with httpx.Client(timeout=120.0) as client:
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                resp = client.post(_OLLAMA_EMBED_URL, json={"model": _EMBED_MODEL, "input": batch})
                resp.raise_for_status()
                vectors.extend(resp.json()["embeddings"])
                if on_event:
                    done = min(i + batch_size, len(texts))
                    on_event(
                        {
                            "stage": "indexing",
                            "current": done,
                            "total": len(texts),
                            "message": f"Embedding… ({done}/{len(texts)})",
                        }
                    )

        matrix = np.array(vectors, dtype=np.float32)
        _INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.save(LocalSourceIndexService._embeddings_matrix_path(draft_name), matrix)
        LocalSourceIndexService._embeddings_ids_path(draft_name).write_text(json.dumps(ids))
        return {"total_documents": len(ids), "dims": matrix.shape[1] if matrix.size else 0}

    @staticmethod
    def get_index_status(draft_name: str) -> dict:
        path = LocalSourceIndexService._index_path(draft_name)
        if not path.is_file():
            return {"built": False}
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"built": False}
        documents = payload.get("documents", [])
        counts: dict[str, int] = {}
        for doc in documents:
            counts[doc.get("kind", "?")] = counts.get(doc.get("kind", "?"), 0) + 1
        # Same free-from-disk approach as IetfImportService.
        # get_local_download_status's last_synced_at -- the index file's own
        # mtime IS "when this was last built", no separate timestamp to keep
        # in sync with reality.
        return {
            "built": True,
            "total_documents": len(documents),
            "by_kind": counts,
            "built_at": path.stat().st_mtime,
        }

    @staticmethod
    def search(draft_name: str, query: str, top_k: int = 5) -> dict:
        path = LocalSourceIndexService._index_path(draft_name)
        if not path.is_file():
            return {"error": "No local index built yet for this draft -- build it first."}
        documents = json.loads(path.read_text()).get("documents", [])
        if not documents:
            return {"error": "Local index is empty."}

        tokenized = [_tokenize(doc["text"]) for doc in documents]
        bm25 = BM25Okapi(tokenized)
        query_tokens = set(_tokenize(query))
        scores = bm25.get_scores(list(query_tokens))

        # A local source's corpus is small and narrow -- often just one
        # draft's own revision history, where every document shares the
        # same core vocabulary (confirmed live: searching a DNSSEC draft's
        # own index for "DNSSEC" itself gave every document a *negative*
        # score, since rank_bm25's IDF term goes negative for a word that
        # appears in every document -- unlike RfcCorpusIndexService's
        # whole-corpus index, where that's rare because the corpus spans
        # thousands of unrelated topics). So score sign/magnitude alone
        # isn't trustworthy here in either direction: a plain `score > 0`
        # filter silently hid genuinely on-topic results (negative scores),
        # while relying on ranking alone let a completely unrelated query
        # through too (confirmed live: a random rare word appearing once,
        # by coincidence, in one old revision scored higher than 0 even
        # though the query had nothing to do with this corpus). The one
        # check that holds in both directions: only keep a result if at
        # least one actual query token appears in *that* document's own
        # text -- real term overlap, independent of BM25's score sign.
        ranked = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
        results = []
        for i in ranked:
            if len(results) >= top_k:
                break
            if not query_tokens.intersection(tokenized[i]):
                continue
            results.append(
                {
                    "kind": documents[i]["kind"],
                    "id": documents[i]["id"],
                    "score": round(float(scores[i]), 3),
                    "snippet": documents[i]["text"][:300],
                }
            )
        return {"query": query, "results": results}
