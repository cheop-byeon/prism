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

from rank_bm25 import BM25Okapi

_DOWNLOADS_DIR = Path("/app/api/storage/ietf-downloads")
_PREPROCESSED_DIR = _DOWNLOADS_DIR / "preprocessed"
_INDEX_DIR = _DOWNLOADS_DIR / "index"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


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
        return {"built": True, "total_documents": len(documents), "by_kind": counts}

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
