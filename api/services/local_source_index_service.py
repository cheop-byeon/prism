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

Content (draft/RFC text) and mail are indexed as two separate on-disk
tiers, each independently buildable -- the Sources tab shows them as two
separate actions/statuses (a source can have its draft/RFC text indexed
with no mail yet pulled at all, and showing one combined "N passages
indexed" count silently hid that split: confirmed live, a source reading
"12 passages indexed" turned out to be 11 drafts + 1 RFC and zero mail,
which looked like a healthy index until actually checked). search() reads
both tiers and merges them, so callers that just want "search this
source" never need to know about the split; build_index/get_index_status
remain as thin combined wrappers over both tiers for callers that want
"fully indexed" in one call (the agent's own add_local_source flow, and
the bulk topic-pull flow) rather than the Sources tab's two buttons.
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
    # -- Paths ------------------------------------------------------------

    @staticmethod
    def _legacy_index_path(draft_name: str) -> Path:
        """The pre-split combined index file -- a source indexed before this
        content/mail split still has its documents here. Never written by
        new code, only read as a fallback (see _load_documents_for_search
        and get_index_status) so a source nobody's rebuilt since the split
        doesn't silently go dark for search/status.
        """
        return _INDEX_DIR / f"{draft_name}.json"

    @staticmethod
    def _content_index_path(draft_name: str) -> Path:
        return _INDEX_DIR / f"{draft_name}-content.json"

    @staticmethod
    def _mail_index_path(wg: str) -> Path:
        """Keyed by WORKING GROUP, not draft name -- unlike draft/RFC text,
        a WG's mail is the same content regardless of which of its drafts
        you're looking at it through. Keying this by draft_name (as the
        content tier correctly is) would mean every draft in a WG stores
        and indexes an identical copy of that WG's mail -- confirmed live,
        two MPLS drafts each had their own ~10,490-message copy of the
        exact same mpls mail. WG-keying means building it once, from any
        one draft, makes it "already built" for every other draft in that
        WG too.
        """
        return _INDEX_DIR / f"{wg}-mail.json"

    @staticmethod
    def _content_embeddings_matrix_path(draft_name: str) -> Path:
        return _INDEX_DIR / f"{draft_name}-content-embeddings.npy"

    @staticmethod
    def _content_embeddings_ids_path(draft_name: str) -> Path:
        return _INDEX_DIR / f"{draft_name}-content-embeddings-ids.json"

    @staticmethod
    def _mail_embeddings_matrix_path(wg: str) -> Path:
        return _INDEX_DIR / f"{wg}-mail-embeddings.npy"

    @staticmethod
    def _mail_embeddings_ids_path(wg: str) -> Path:
        return _INDEX_DIR / f"{wg}-mail-embeddings-ids.json"

    # -- Gathering ----------------------------------------------------------

    @staticmethod
    def _gather_content_documents(draft_name: str, rfc_number: str | None) -> list[dict]:
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
        return documents

    @staticmethod
    def _gather_mail_documents(wg: str | None) -> list[dict]:
        documents: list[dict] = []
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
                    if not isinstance(record, dict):
                        # A handful of lines in a few WGs' threaded output
                        # parse as a bare JSON scalar (e.g. a stray number)
                        # rather than an object -- confirmed live for
                        # mipshop, which crashed build_mail_index entirely
                        # on a single malformed line otherwise.
                        continue
                    text = f"{record.get('subject', '')}\n{record.get('body', '')}"
                    documents.append(
                        {
                            "kind": "mail_thread",
                            # record["id"] first, not url -- build_thread_records
                            # sets id to f"{thread_id}#part{N}" for a thread long
                            # enough to need splitting (see _split_into_parts),
                            # specifically to disambiguate that part from its
                            # siblings; url is just the first message's own url
                            # (see build_thread_records: "url": part[0]["url"]),
                            # which carries no part info at all. Preferring url
                            # happened to mostly work by accident (each part's
                            # first message usually has a distinct url of its
                            # own), but silently discarded the real
                            # disambiguator -- id -- whenever it was present.
                            "id": record.get("id") or record.get("url") or entry.stem,
                            "text": text,
                        }
                    )
        return documents

    # -- Sparse (BM25) index build -------------------------------------------

    @staticmethod
    def build_content_index(draft_name: str, rfc_number: str | None, on_event=None) -> dict:
        if on_event:
            on_event({"stage": "indexing", "message": "Gathering draft/RFC text…"})
        documents = LocalSourceIndexService._gather_content_documents(draft_name, rfc_number)
        if not documents:
            return {"error": "No draft/RFC text downloaded locally yet for this draft -- sync it first."}

        _INDEX_DIR.mkdir(parents=True, exist_ok=True)
        LocalSourceIndexService._content_index_path(draft_name).write_text(
            json.dumps({"draft_name": draft_name, "documents": documents})
        )
        counts: dict[str, int] = {}
        for doc in documents:
            counts[doc["kind"]] = counts.get(doc["kind"], 0) + 1
        if on_event:
            on_event({"stage": "indexing", "message": f"Indexed {len(documents)} documents"})
        return {"total_documents": len(documents), "by_kind": counts}

    @staticmethod
    def build_mail_index(wg: str | None, on_event=None) -> dict:
        if not wg:
            return {"error": "No working group resolved for this source."}
        if on_event:
            on_event({"stage": "indexing", "message": "Gathering WG mail…"})
        documents = LocalSourceIndexService._gather_mail_documents(wg)
        if not documents:
            return {"error": "No WG mail downloaded locally yet for this WG -- sync it first."}

        _INDEX_DIR.mkdir(parents=True, exist_ok=True)
        LocalSourceIndexService._mail_index_path(wg).write_text(json.dumps({"wg": wg, "documents": documents}))
        counts: dict[str, int] = {}
        for doc in documents:
            counts[doc["kind"]] = counts.get(doc["kind"], 0) + 1
        if on_event:
            on_event({"stage": "indexing", "message": f"Indexed {len(documents)} messages"})
        return {"total_documents": len(documents), "by_kind": counts}

    @staticmethod
    def build_index(draft_name: str, wg: str | None, rfc_number: str | None, on_event=None) -> dict:
        """Combined build over both tiers -- see this module's docstring for
        which callers want this vs. the two tiers separately.
        """
        content_result = LocalSourceIndexService.build_content_index(draft_name, rfc_number, on_event=on_event)
        mail_result = (
            LocalSourceIndexService.build_mail_index(wg, on_event=on_event)
            if wg
            else {"total_documents": 0, "by_kind": {}}
        )
        content_total = content_result.get("total_documents", 0)
        mail_total = mail_result.get("total_documents", 0)
        if content_total == 0 and mail_total == 0:
            return {"error": "Nothing downloaded locally yet for this draft -- sync first."}
        by_kind = {**content_result.get("by_kind", {}), **mail_result.get("by_kind", {})}
        return {"total_documents": content_total + mail_total, "by_kind": by_kind}

    # -- Dense (embeddings) index build ---------------------------------------

    @staticmethod
    def _build_embedding_index(
        documents: list[dict], matrix_path: Path, ids_path: Path, on_event=None, batch_size: int = 20
    ) -> dict:
        """One Ollama /api/embed call per document, not a batch of
        `batch_size` -- confirmed live on this deployment's CPU-only 7B
        model that a single real draft/mail document already takes
        20-30s+ (compute-bound, not request-overhead-bound), so the old
        behavior of sending `batch_size` (default 20) documents in ONE
        HTTP call could need 400s+ against a hardcoded 120s client
        timeout. `batch_size` now only controls how often on_event fires,
        not how many documents share one request.

        Resumable and crash-tolerant, unlike a from-scratch rebuild (the
        module docstring's "cheap enough to rebuild from scratch" applies
        to the sparse/BM25 tier, never true of this one at this model's
        real speed): an existing matrix/ids pair at these paths is treated
        as a checkpoint -- already-embedded ids are skipped, and progress
        is re-saved to the same paths every `_CHECKPOINT_EVERY` documents,
        so a timeout, an OOM, or this process simply being killed loses at
        most that many documents' work, not the whole run. One document's
        embedding call failing (confirmed live: happens) is logged and
        skipped rather than aborting every other document's already-done
        work.
        """
        if not documents:
            return {"error": "Nothing to embed yet -- sync and build the sparse index first."}

        _CHECKPOINT_EVERY = 20

        vectors_by_id: dict[str, list[float]] = {}
        if matrix_path.is_file() and ids_path.is_file():
            try:
                existing_ids = json.loads(ids_path.read_text())
                existing_matrix = np.load(matrix_path)
                for idx, doc_id in enumerate(existing_ids):
                    vectors_by_id[doc_id] = existing_matrix[idx].tolist()
            except (json.JSONDecodeError, OSError, ValueError, IndexError):
                vectors_by_id = {}

        # Later documents with the same id win -- matches how every other
        # tier in this file treats id collisions (dict keyed by id), and
        # means a source re-synced with revised content gets re-embedded
        # for that id rather than permanently keeping a stale checkpoint.
        remaining = [doc for doc in documents if doc["id"] not in vectors_by_id]
        already_done = len(documents) - len(remaining)

        def _save_checkpoint() -> None:
            out_ids = [doc["id"] for doc in documents if doc["id"] in vectors_by_id]
            out_matrix = np.array([vectors_by_id[i] for i in out_ids], dtype=np.float32)
            _INDEX_DIR.mkdir(parents=True, exist_ok=True)
            np.save(matrix_path, out_matrix)
            ids_path.write_text(json.dumps(out_ids))

        failed_ids: list[str] = []
        with httpx.Client(timeout=300.0) as client:
            for i, doc in enumerate(remaining, start=1):
                try:
                    resp = client.post(_OLLAMA_EMBED_URL, json={"model": _EMBED_MODEL, "input": [doc["text"]]})
                    resp.raise_for_status()
                    vectors_by_id[doc["id"]] = resp.json()["embeddings"][0]
                except Exception as exc:
                    failed_ids.append(doc["id"])
                    if on_event:
                        on_event(
                            {
                                "stage": "indexing",
                                "current": already_done + i,
                                "total": len(documents),
                                "message": f"Failed on {doc['id']}: {exc}",
                            }
                        )
                    continue
                done = already_done + i
                if on_event and i % batch_size == 0:
                    on_event(
                        {
                            "stage": "indexing",
                            "current": done,
                            "total": len(documents),
                            "message": f"Embedding… ({done}/{len(documents)})",
                        }
                    )
                if i % _CHECKPOINT_EVERY == 0:
                    _save_checkpoint()

        _save_checkpoint()
        dims = next(iter(vectors_by_id.values()), [])
        return {
            "total_documents": len(vectors_by_id),
            "failed": len(failed_ids),
            "failed_ids": failed_ids,
            "dims": len(dims),
        }

    @staticmethod
    def build_content_embedding_index(
        draft_name: str, rfc_number: str | None, on_event=None, batch_size: int = 20
    ) -> dict:
        """Meant to be run from a Celery task (see
        tasks/local_source_embedding_index_task.py), not called directly
        from a request handler -- see that module's docstring for why.
        """
        documents = LocalSourceIndexService._gather_content_documents(draft_name, rfc_number)
        return LocalSourceIndexService._build_embedding_index(
            documents,
            LocalSourceIndexService._content_embeddings_matrix_path(draft_name),
            LocalSourceIndexService._content_embeddings_ids_path(draft_name),
            on_event=on_event,
            batch_size=batch_size,
        )

    @staticmethod
    def build_mail_embedding_index(wg: str | None, on_event=None, batch_size: int = 20) -> dict:
        if not wg:
            return {"error": "No working group resolved for this source."}
        documents = LocalSourceIndexService._gather_mail_documents(wg)
        return LocalSourceIndexService._build_embedding_index(
            documents,
            LocalSourceIndexService._mail_embeddings_matrix_path(wg),
            LocalSourceIndexService._mail_embeddings_ids_path(wg),
            on_event=on_event,
            batch_size=batch_size,
        )

    # -- Status ------------------------------------------------------------

    @staticmethod
    def _index_status_from_path(path: Path) -> dict:
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

    _CONTENT_KINDS = ("draft", "rfc")
    _MAIL_KINDS = ("mail_thread",)

    @staticmethod
    def _legacy_documents_for_kinds(draft_name: str, kinds: tuple[str, ...]) -> list[dict]:
        """A source indexed before the content/mail split (and, for mail,
        before the later WG-keying) has one combined legacy file per draft,
        mixing both kinds together -- this pulls out just the requested
        kinds from it. Shared by the status and search fallbacks below so
        both agree on what a legacy file actually contains.
        """
        path = LocalSourceIndexService._legacy_index_path(draft_name)
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        return [doc for doc in payload.get("documents", []) if doc.get("kind") in kinds]

    @staticmethod
    def _legacy_index_status_for_kinds(draft_name: str, kinds: tuple[str, ...]) -> dict:
        path = LocalSourceIndexService._legacy_index_path(draft_name)
        if not path.is_file():
            return {"built": False}
        documents = LocalSourceIndexService._legacy_documents_for_kinds(draft_name, kinds)
        if not documents:
            return {"built": False}
        counts: dict[str, int] = {}
        for doc in documents:
            counts[doc.get("kind", "?")] = counts.get(doc.get("kind", "?"), 0) + 1
        return {
            "built": True,
            "total_documents": len(documents),
            "by_kind": counts,
            "built_at": path.stat().st_mtime,
        }

    @staticmethod
    def get_content_index_status(draft_name: str) -> dict:
        status = LocalSourceIndexService._index_status_from_path(
            LocalSourceIndexService._content_index_path(draft_name)
        )
        if status.get("built"):
            return status
        return LocalSourceIndexService._legacy_index_status_for_kinds(
            draft_name, LocalSourceIndexService._CONTENT_KINDS
        )

    @staticmethod
    def get_mail_index_status(wg: str | None, draft_name: str | None = None) -> dict:
        """WG-keyed (see _mail_index_path) -- draft_name is accepted only
        for the legacy pre-split/pre-WG-keying fallback, which was written
        per-draft before either change and has no WG-level file to fall
        back to instead.
        """
        if wg:
            status = LocalSourceIndexService._index_status_from_path(LocalSourceIndexService._mail_index_path(wg))
            if status.get("built"):
                return status
        if draft_name:
            return LocalSourceIndexService._legacy_index_status_for_kinds(
                draft_name, LocalSourceIndexService._MAIL_KINDS
            )
        return {"built": False}

    @staticmethod
    def get_index_status(draft_name: str, wg: str | None = None) -> dict:
        """Combined status over both tiers -- see this module's docstring
        for which callers want this vs. the two tiers separately.
        """
        content = LocalSourceIndexService.get_content_index_status(draft_name)
        mail = LocalSourceIndexService.get_mail_index_status(wg, draft_name)
        if not content.get("built") and not mail.get("built"):
            return {"built": False}
        total = content.get("total_documents", 0) + mail.get("total_documents", 0)
        by_kind = {**content.get("by_kind", {}), **mail.get("by_kind", {})}
        built_at = max((t for t in (content.get("built_at"), mail.get("built_at")) if t is not None), default=None)
        return {"built": True, "total_documents": total, "by_kind": by_kind, "built_at": built_at}

    # -- Search --------------------------------------------------------------

    @staticmethod
    def _load_documents_for_search(draft_name: str, wg: str | None, scope: str = "both") -> list[dict]:
        """Each tier falls back to the legacy file independently -- a
        source can easily have one tier rebuilt under the new split/WG-
        keyed files and the other still only in the old combined legacy
        file (e.g. content rebuilt, mail not yet), and an all-or-nothing
        fallback would silently drop whichever tier wasn't rebuilt instead
        of still finding it in the legacy file.

        scope: "both" (default, legacy single-tool behavior), "content"
        (draft/RFC text only), or "mail" (that WG's mail only) -- lets
        ask_decision_agent.py's two local-source search tools each query
        just their own tier, instead of every call always ranking against
        a corpus that mixes spec text and mailing-list discussion
        together (confirmed live: this was genuinely confusing -- a
        drafts/RFC-text query could surface mail hits and vice versa,
        with nothing about the call itself saying which tier actually
        answered it).
        """
        documents: list[dict] = []

        if scope in ("both", "content"):
            content_path = LocalSourceIndexService._content_index_path(draft_name)
            if content_path.is_file():
                try:
                    documents.extend(json.loads(content_path.read_text()).get("documents", []))
                except (json.JSONDecodeError, OSError):
                    documents.extend(
                        LocalSourceIndexService._legacy_documents_for_kinds(
                            draft_name, LocalSourceIndexService._CONTENT_KINDS
                        )
                    )
            else:
                documents.extend(
                    LocalSourceIndexService._legacy_documents_for_kinds(
                        draft_name, LocalSourceIndexService._CONTENT_KINDS
                    )
                )

        if scope in ("both", "mail"):
            mail_path = LocalSourceIndexService._mail_index_path(wg) if wg else None
            if mail_path and mail_path.is_file():
                try:
                    documents.extend(json.loads(mail_path.read_text()).get("documents", []))
                except (json.JSONDecodeError, OSError):
                    documents.extend(
                        LocalSourceIndexService._legacy_documents_for_kinds(
                            draft_name, LocalSourceIndexService._MAIL_KINDS
                        )
                    )
            else:
                documents.extend(
                    LocalSourceIndexService._legacy_documents_for_kinds(draft_name, LocalSourceIndexService._MAIL_KINDS)
                )

        return documents

    @staticmethod
    def search(draft_name: str, query: str, top_k: int = 5, wg: str | None = None, scope: str = "both") -> dict:
        if wg is None:
            from services.ietf_import_service import IetfImportService

            wg = IetfImportService.resolve_wg_for_draft(draft_name)
        documents = LocalSourceIndexService._load_documents_for_search(draft_name, wg, scope=scope)
        if not documents:
            no_index_msg = {
                "content": "No local drafts/RFC index built yet for this draft -- build it first.",
                "mail": "No local mail index built yet for this draft's WG -- build it first.",
            }.get(scope, "No local index built yet for this draft -- build it first.")
            return {"error": no_index_msg}

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
                    # 300 chars (the original size here) cuts a mail_thread
                    # document off before its first message's opening
                    # sentence finishes -- confirmed live: a real, highly
                    # relevant, 43-message WG thread ("Using different
                    # crypto", quic WG) scored top and was correctly found,
                    # but its snippet ended mid-word ("I like that people
                    # seem to forget eng"), so the model could see the
                    # thread existed without ever seeing why it mattered.
                    # mail_thread documents average ~7.2KB (up to ~48KB);
                    # 2000 chars typically covers one full message in a
                    # multi-message chunk -- enough to actually read the
                    # substance, not just confirm relevance, without
                    # ballooning a single search call's total observation
                    # size across top_k results.
                    "snippet": documents[i]["text"][:2000],
                }
            )
        return {"query": query, "results": results}
