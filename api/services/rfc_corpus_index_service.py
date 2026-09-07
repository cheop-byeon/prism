"""Whole-corpus RFC search: BM25 (title+abstract or full text) and
embedding-based semantic search (title+abstract only, via the local
rfc-dralign-q8 model already used elsewhere in this project).

Standalone from ietf_import_service.py on purpose -- that module builds
per-RFC/whole-corpus *relationship graphs* (obsoletes/updates/citations);
this one builds whole-corpus *text search* indices, a different kind of
data (every RFC's own text, not the edges between RFCs) with a different
cost profile (network-bound corpus fetch once, then cheap repeated
queries) that doesn't belong mixed into the graph-building file.

Three cost tiers, deliberately not hidden behind one lazy "just works"
call:
  - title+abstract corpus (~9,930 RFCs, Datatracker's own abstract field,
    batched name__in lookups): a few minutes cold, fine to build lazily
    on first use.
  - full RFC text (one fetch per RFC from rfc-editor.org): tens of
    minutes and real bandwidth/disk (each RFC's plain text, all ~9,930 of
    them) -- requires an explicit build (see build_rfc_corpus_index.py),
    never triggered automatically from a search call.
  - embeddings over title+abstract (one local-model call per RFC): the
    slowest tier by far (measured ~160ms/doc even for short text, so on
    the order of an hour for the whole corpus) -- also requires an
    explicit build, never triggered automatically.
Everything is cached to disk under storage/rfc-corpus-index/ so each tier
is a true one-time cost, not something repeated per process restart.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TypedDict

import httpx
import numpy as np
from rank_bm25 import BM25Okapi

_CACHE_DIR = Path("/app/api/storage/rfc-corpus-index")
_TITLE_ABSTRACT_CACHE = _CACHE_DIR / "title_abstract_corpus.json"
_FULL_TEXT_DIR = _CACHE_DIR / "full_text"
_EMBEDDINGS_MATRIX = _CACHE_DIR / "embeddings_title_abstract.npy"
_EMBEDDINGS_IDS = _CACHE_DIR / "embeddings_title_abstract_ids.json"

_RFC_INDEX_URL = "https://www.rfc-editor.org/rfc-index.xml"
_RFC_INDEX_NS = {"idx": "https://www.rfc-editor.org/rfc-index"}
_RFC_EDITOR_TEXT_BASE = "https://www.rfc-editor.org/rfc/"

# Ollama is a host-machine service, not part of this repo's containers --
# host.docker.internal is how a container reaches it, same base_url the
# project's own Dify Ollama provider config uses (see dev/start-embedding-server).
_OLLAMA_EMBED_URL = "http://host.docker.internal:11434/api/embed"
_EMBED_MODEL = "rfc-dralign-q8"

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class RfcTextEntry(TypedDict):
    title: str
    abstract: str


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class RfcCorpusIndexService:
    # In-process caches on top of the on-disk ones -- rebuilding a BM25Okapi
    # object from already-fetched text is only a few seconds, but there is
    # no reason to redo even that on every single tool call within one
    # long-running process.
    _title_abstract_corpus: dict[str, RfcTextEntry] | None = None
    _bm25_title_abstract: tuple[BM25Okapi, list[str]] | None = None
    _bm25_full_text: tuple[BM25Okapi, list[str]] | None = None
    _embeddings: tuple[np.ndarray, list[str]] | None = None

    @staticmethod
    def is_title_abstract_corpus_built() -> bool:
        """Cheap on-disk/in-memory check, no build side effect -- callers
        that only want BM25 opportunistically (e.g. the RFC Graph's Topic
        Match edges, which shouldn't turn a normal graph fetch into a
        multi-minute cold corpus build) check this first and skip gracefully
        if it's False, rather than calling search_bm25 and triggering its
        lazy build.
        """
        return RfcCorpusIndexService._title_abstract_corpus is not None or _TITLE_ABSTRACT_CACHE.exists()

    @staticmethod
    def _list_all_rfc_names(on_event=None) -> list[str]:
        """Every published RFC name, from the RFC Editor's own index --
        the authoritative, complete list. Datatracker's document listing
        mixes in drafts and would need extra filtering to get the same
        result, so the index is fetched fresh here rather than reusing
        Datatracker for this specific lookup.
        """
        if on_event:
            on_event({"stage": "building", "message": "Fetching the RFC index…"})
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(_RFC_INDEX_URL)
            resp.raise_for_status()
        root = ET.fromstring(resp.content)
        names = [
            el.text.strip().upper()
            for el in root.findall(".//idx:rfc-entry/idx:doc-id", _RFC_INDEX_NS)
            if el.text
        ]
        return sorted(set(names), key=lambda n: int(re.sub(r"[^0-9]", "", n) or 0))

    @staticmethod
    def build_title_abstract_corpus(on_event=None, force: bool = False) -> dict[str, RfcTextEntry]:
        """Fetch {RFC_ID: {title, abstract}} for every published RFC via
        batched Datatracker lookups (the same name__in chunking pattern
        used throughout ietf_import_service.py), and cache to disk. This
        is the shared foundation both BM25's title_abstract scope and the
        embedding index build on -- one fetch, two consumers.
        """
        if not force and _TITLE_ABSTRACT_CACHE.exists():
            return json.loads(_TITLE_ABSTRACT_CACHE.read_text())

        names = RfcCorpusIndexService._list_all_rfc_names(on_event)
        corpus: dict[str, RfcTextEntry] = {}
        with httpx.Client(timeout=30.0) as client:
            for i in range(0, len(names), 50):
                chunk = names[i : i + 50]
                resp = client.get(
                    "https://datatracker.ietf.org/api/v1/doc/document/",
                    params={"name__in": ",".join(n.lower() for n in chunk), "format": "json", "limit": len(chunk)},
                )
                resp.raise_for_status()
                for obj in resp.json().get("objects", []):
                    corpus[obj["name"].upper()] = {
                        "title": obj.get("title") or "",
                        "abstract": obj.get("abstract") or "",
                    }
                if on_event:
                    on_event(
                        {
                            "stage": "building",
                            "message": f"Fetching title+abstract… ({min(i + 50, len(names))}/{len(names)})",
                        }
                    )

        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _TITLE_ABSTRACT_CACHE.write_text(json.dumps(corpus))
        RfcCorpusIndexService._title_abstract_corpus = corpus
        return corpus

    @staticmethod
    def build_full_text_corpus(on_event=None, max_workers: int = 10) -> dict:
        """Fetch every published RFC's plain-text body from rfc-editor.org,
        one file per RFC under storage/rfc-corpus-index/full_text/ --
        deliberately NOT one big JSON blob, so an interrupted build is
        resumable (already-downloaded files are skipped) and the whole
        corpus never has to sit in memory at once outside of an actual
        search/index-build call. Never called automatically from a search
        -- this is a real one-time cost (tens of minutes, real bandwidth
        for ~9,930 documents) meant to be run explicitly, see
        build_rfc_corpus_index.py.
        """
        names = RfcCorpusIndexService._list_all_rfc_names(on_event)
        _FULL_TEXT_DIR.mkdir(parents=True, exist_ok=True)
        to_fetch = [n for n in names if not (_FULL_TEXT_DIR / f"{n}.txt").exists()]
        done = len(names) - len(to_fetch)

        def _fetch_one(name: str) -> tuple[str, bool]:
            number = re.sub(r"[^0-9]", "", name)
            try:
                with httpx.Client(timeout=20.0) as client:
                    resp = client.get(f"{_RFC_EDITOR_TEXT_BASE}rfc{number}.txt")
                if resp.status_code != 200:
                    return name, False
                (_FULL_TEXT_DIR / f"{name}.txt").write_text(resp.text, errors="ignore")
                return name, True
            except httpx.HTTPError:
                return name, False

        failed = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_fetch_one, n): n for n in to_fetch}
            for future in as_completed(futures):
                name, ok = future.result()
                done += 1
                if not ok:
                    failed.append(name)
                if on_event and done % 25 == 0:
                    on_event({"stage": "building", "message": f"Fetching full text… ({done}/{len(names)})"})

        return {"total": len(names), "fetched_this_run": len(to_fetch) - len(failed), "failed": failed}

    @staticmethod
    def build_embedding_index(on_event=None, batch_size: int = 20) -> dict:
        """Embed every RFC's title+abstract via the local rfc-dralign-q8
        model (Ollama, host.docker.internal:11434) and cache the resulting
        matrix to disk. By far the slowest tier (~160ms/doc measured even
        for short text -- on the order of an hour for the whole corpus)
        -- never called automatically from a search, only via
        build_rfc_corpus_index.py.
        """
        corpus = RfcCorpusIndexService.build_title_abstract_corpus(on_event)
        ids = sorted(corpus.keys(), key=lambda n: int(re.sub(r"[^0-9]", "", n) or 0))
        texts = [f"{corpus[i]['title']} {corpus[i]['abstract']}".strip() for i in ids]

        vectors: list[list[float]] = []
        with httpx.Client(timeout=120.0) as client:
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                resp = client.post(_OLLAMA_EMBED_URL, json={"model": _EMBED_MODEL, "input": batch})
                resp.raise_for_status()
                vectors.extend(resp.json()["embeddings"])
                if on_event:
                    done = min(i + batch_size, len(texts))
                    on_event({"stage": "building", "message": f"Embedding title+abstract… ({done}/{len(texts)})"})

        matrix = np.array(vectors, dtype=np.float32)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.save(_EMBEDDINGS_MATRIX, matrix)
        _EMBEDDINGS_IDS.write_text(json.dumps(ids))
        RfcCorpusIndexService._embeddings = (matrix, ids)
        return {"count": len(ids), "dims": matrix.shape[1] if matrix.size else 0}

    @staticmethod
    def _get_bm25_title_abstract() -> tuple[BM25Okapi, list[str]]:
        if RfcCorpusIndexService._bm25_title_abstract is not None:
            return RfcCorpusIndexService._bm25_title_abstract
        corpus = RfcCorpusIndexService.build_title_abstract_corpus()  # lazy: a few minutes cold, cheap after
        ids = sorted(corpus.keys(), key=lambda n: int(re.sub(r"[^0-9]", "", n) or 0))
        tokenized = [_tokenize(f"{corpus[i]['title']} {corpus[i]['abstract']}") for i in ids]
        bm25 = BM25Okapi(tokenized)
        RfcCorpusIndexService._bm25_title_abstract = (bm25, ids)
        return bm25, ids

    @staticmethod
    def _get_bm25_full_text() -> tuple[BM25Okapi, list[str]] | None:
        if RfcCorpusIndexService._bm25_full_text is not None:
            return RfcCorpusIndexService._bm25_full_text
        if not _FULL_TEXT_DIR.exists():
            return None
        files = sorted(_FULL_TEXT_DIR.glob("RFC*.txt"))
        if not files:
            return None
        ids = [f.stem for f in files]
        tokenized = [_tokenize(f.read_text(errors="ignore")) for f in files]
        bm25 = BM25Okapi(tokenized)
        RfcCorpusIndexService._bm25_full_text = (bm25, ids)
        return bm25, ids

    @staticmethod
    def _resolve_query_text(query: str, corpus: dict[str, RfcTextEntry]) -> str:
        """A bare RFC number (or "RFC1234") as the query is resolved to
        that RFC's own title+abstract, so "find things like RFC6378"
        works the same as typing a topic phrase -- one interface for
        both "search by topic" and "search by example RFC" instead of
        two separate parameters.
        """
        stripped = query.strip().upper()
        if re.fullmatch(r"(?:RFC)?\d+", stripped):
            name = stripped if stripped.startswith("RFC") else f"RFC{stripped}"
            entry = corpus.get(name)
            if entry:
                return f"{entry['title']} {entry['abstract']}"
        return query

    @staticmethod
    def search_bm25(query: str, scope: str = "title_abstract", top_k: int = 10) -> dict:
        """Rank the whole RFC corpus against a free-text query (or an RFC
        number, resolved to that RFC's own text) via BM25. scope
        "title_abstract" builds/uses its index lazily (a few minutes cold,
        cached after); scope "full_text" requires build_full_text_corpus
        to have already been run and returns an error naming that script
        otherwise, since that corpus is too large to fetch inline.
        """
        if scope not in ("title_abstract", "full_text"):
            return {"error": f"scope must be 'title_abstract' or 'full_text', got {scope!r}"}

        corpus = RfcCorpusIndexService._title_abstract_corpus or RfcCorpusIndexService.build_title_abstract_corpus()
        RfcCorpusIndexService._title_abstract_corpus = corpus
        query_text = RfcCorpusIndexService._resolve_query_text(query, corpus)

        if scope == "full_text":
            index = RfcCorpusIndexService._get_bm25_full_text()
            if index is None:
                return {
                    "error": (
                        "The full-text index hasn't been built yet -- run "
                        "`python scripts/agentic_rag/build_rfc_corpus_index.py --what full_text` first "
                        "(fetches every RFC's plain text from rfc-editor.org, tens of minutes)."
                    )
                }
            bm25, ids = index
        else:
            bm25, ids = RfcCorpusIndexService._get_bm25_title_abstract()

        scores = bm25.get_scores(_tokenize(query_text))
        ranked = np.argsort(scores)[::-1][:top_k]
        return {
            "query": query,
            "scope": scope,
            "results": [
                {
                    "rfc": ids[i],
                    "score": round(float(scores[i]), 3),
                    "title": corpus.get(ids[i], {}).get("title", ""),
                }
                for i in ranked
                if scores[i] > 0
            ],
        }

    @staticmethod
    def search_semantic(query: str, top_k: int = 10) -> dict:
        """Rank the whole RFC corpus (title+abstract) against a free-text
        query (or an RFC number, resolved to that RFC's own text) by
        embedding cosine similarity. Requires build_embedding_index to
        have already been run -- that build takes on the order of an
        hour, so it's never triggered automatically from a search call.
        """
        if RfcCorpusIndexService._embeddings is None:
            if not (_EMBEDDINGS_MATRIX.exists() and _EMBEDDINGS_IDS.exists()):
                return {
                    "error": (
                        "The embedding index hasn't been built yet -- run "
                        "`python scripts/agentic_rag/build_rfc_corpus_index.py --what embeddings` first "
                        "(embeds every RFC's title+abstract via the local rfc-dralign-q8 model, ~an hour)."
                    )
                }
            matrix = np.load(_EMBEDDINGS_MATRIX)
            ids = json.loads(_EMBEDDINGS_IDS.read_text())
            RfcCorpusIndexService._embeddings = (matrix, ids)
        matrix, ids = RfcCorpusIndexService._embeddings

        corpus = RfcCorpusIndexService._title_abstract_corpus or RfcCorpusIndexService.build_title_abstract_corpus()
        RfcCorpusIndexService._title_abstract_corpus = corpus
        query_text = RfcCorpusIndexService._resolve_query_text(query, corpus)

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(_OLLAMA_EMBED_URL, json={"model": _EMBED_MODEL, "input": [query_text]})
                resp.raise_for_status()
                query_vec = np.array(resp.json()["embeddings"][0], dtype=np.float32)
        except httpx.HTTPError as exc:
            return {"error": f"Could not reach the local embedding model: {exc}"}

        norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_vec)
        norms[norms == 0] = 1e-9
        sims = (matrix @ query_vec) / norms
        ranked = np.argsort(sims)[::-1][:top_k]
        return {
            "query": query,
            "results": [
                {
                    "rfc": ids[i],
                    "similarity": round(float(sims[i]), 4),
                    "title": corpus.get(ids[i], {}).get("title", ""),
                }
                for i in ranked
            ],
        }
