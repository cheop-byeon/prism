"""BM25 keyword index backend.

Stores the full tokenised corpus per dataset so that BM25Okapi can apply
proper TF saturation and document-length normalisation at query time —
unlike the Jieba backend which only stores a top-N keyword → doc-id inverted
index and ranks by overlap count.
"""

from __future__ import annotations

import re
from typing import Any

import orjson

from core.rag.datasource.keyword.keyword_base import BaseKeyword
from core.rag.models.document import Document
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from extensions.ext_storage import storage
from models.dataset import Dataset, DatasetKeywordTable, DocumentSegment

# Minimal English stopword list — keeps common function words out of the
# BM25 corpus without pulling in NLTK or another NLP library.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "shall", "this", "that", "these",
    "those", "it", "its", "as", "into", "about", "up", "out", "if", "so",
    "not", "no", "can", "also", "than", "then", "i", "we", "you", "he",
    "she", "they", "their", "our", "your", "his", "her", "which", "who",
    "what", "when", "where", "how", "all", "each", "more", "such", "other",
})


def _tokenize(text: str) -> list[str]:
    """Lower-case, split on non-word boundaries, drop stopwords and short tokens."""
    return [
        token
        for token in re.findall(r"\b\w+\b", text.lower())
        if token not in _STOPWORDS and len(token) > 1
    ]


class Bm25Keyword(BaseKeyword):
    """BM25-based keyword index stored in the existing DatasetKeywordTable."""

    def __init__(self, dataset: Dataset) -> None:
        super().__init__(dataset)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create(self, texts: list[Document], **kwargs) -> Bm25Keyword:
        lock_name = f"keyword_indexing_lock_{self.dataset.id}"
        with redis_client.lock(lock_name, timeout=600):
            corpus_tokens, doc_ids = self._load_corpus()
            for text in texts:
                if text.metadata is None:
                    continue
                doc_id: str = text.metadata["doc_id"]
                if doc_id not in doc_ids:
                    corpus_tokens.append(_tokenize(text.page_content))
                    doc_ids.append(doc_id)
            self._save_corpus(corpus_tokens, doc_ids)
        return self

    def add_texts(self, texts: list[Document], **kwargs) -> None:
        lock_name = f"keyword_indexing_lock_{self.dataset.id}"
        with redis_client.lock(lock_name, timeout=600):
            corpus_tokens, doc_ids = self._load_corpus()
            for text in texts:
                if text.metadata is None:
                    continue
                doc_id: str = text.metadata["doc_id"]
                if doc_id not in doc_ids:
                    corpus_tokens.append(_tokenize(text.page_content))
                    doc_ids.append(doc_id)
            self._save_corpus(corpus_tokens, doc_ids)

    def delete_by_ids(self, ids: list[str]) -> None:
        lock_name = f"keyword_indexing_lock_{self.dataset.id}"
        with redis_client.lock(lock_name, timeout=600):
            corpus_tokens, doc_ids = self._load_corpus()
            id_set = set(ids)
            pairs = [(t, d) for t, d in zip(corpus_tokens, doc_ids) if d not in id_set]
            if pairs:
                new_tokens, new_ids = zip(*pairs)
                self._save_corpus(list(new_tokens), list(new_ids))
            else:
                self._save_corpus([], [])

    def delete(self) -> None:
        lock_name = f"keyword_indexing_lock_{self.dataset.id}"
        with redis_client.lock(lock_name, timeout=600):
            dataset_keyword_table = self.dataset.dataset_keyword_table
            if dataset_keyword_table:
                db.session.delete(dataset_keyword_table)
                db.session.commit()
                if dataset_keyword_table.data_source_type != "database":
                    file_key = "keyword_files/" + self.dataset.tenant_id + "/" + self.dataset.id + ".txt"
                    storage.delete(file_key)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def text_exists(self, id: str) -> bool:
        _, doc_ids = self._load_corpus()
        return id in doc_ids

    def search(self, query: str, **kwargs: Any) -> list[Document]:
        from rank_bm25 import BM25Plus

        k = kwargs.get("top_k", 4)
        document_ids_filter: list[str] | None = kwargs.get("document_ids_filter")

        corpus_tokens, doc_ids = self._load_corpus()
        if not doc_ids:
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        bm25 = BM25Plus(corpus_tokens)
        scores = bm25.get_scores(query_tokens)

        # Sort indices by score descending, apply filter, take top-k.
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        top_ids: list[str] = []
        for idx in ranked:
            if scores[idx] <= 0:
                break
            if len(top_ids) >= k:
                break
            top_ids.append(doc_ids[idx])

        if not top_ids:
            return []

        segment_query = db.session.query(DocumentSegment).where(
            DocumentSegment.dataset_id == self.dataset.id,
            DocumentSegment.index_node_id.in_(top_ids),
        )
        if document_ids_filter:
            segment_query = segment_query.where(
                DocumentSegment.document_id.in_(document_ids_filter)
            )
        segments = db.session.execute(segment_query).scalars().all()
        segment_map = {seg.index_node_id: seg for seg in segments}

        documents: list[Document] = []
        for node_id in top_ids:
            seg = segment_map.get(node_id)
            if seg:
                documents.append(Document(
                    page_content=seg.content,
                    metadata={
                        "doc_id": node_id,
                        "doc_hash": seg.index_node_hash,
                        "document_id": seg.document_id,
                        "dataset_id": seg.dataset_id,
                    },
                ))
        return documents

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    def _load_corpus(self) -> tuple[list[list[str]], list[str]]:
        """Return (corpus_tokens, doc_ids) from the stored index.

        Parses the raw JSON blob directly. DatasetKeywordTable.keyword_table_dict
        must NOT be used here: its Jieba-specific decoder converts every list
        value to a set, which raises on corpus_tokens (a list of lists is
        unhashable) and destroys doc_ids ordering.
        """
        dataset_keyword_table = self.dataset.dataset_keyword_table
        if dataset_keyword_table:
            raw_text: str | None = None
            if dataset_keyword_table.data_source_type == "database":
                raw_text = dataset_keyword_table.keyword_table
            else:
                file_key = "keyword_files/" + self.dataset.tenant_id + "/" + self.dataset.id + ".txt"
                if storage.exists(file_key):
                    raw_text = storage.load_once(file_key).decode("utf-8")
            if raw_text:
                try:
                    raw = orjson.loads(raw_text)
                except orjson.JSONDecodeError:
                    raw = None
                if isinstance(raw, dict) and raw.get("__type__") == "bm25_index":
                    data = raw.get("__data__", {})
                    return data.get("corpus_tokens", []), data.get("doc_ids", [])
        else:
            from configs import dify_config
            keyword_data_source_type = dify_config.KEYWORD_DATA_SOURCE_TYPE
            dataset_keyword_table = DatasetKeywordTable(
                dataset_id=self.dataset.id,
                keyword_table="",
                data_source_type=keyword_data_source_type,
            )
            if keyword_data_source_type == "database":
                dataset_keyword_table.keyword_table = self._empty_blob()
            db.session.add(dataset_keyword_table)
            db.session.commit()
        return [], []

    def _save_corpus(self, corpus_tokens: list[list[str]], doc_ids: list[str]) -> None:
        blob = orjson.dumps({
            "__type__": "bm25_index",
            "__data__": {"corpus_tokens": corpus_tokens, "doc_ids": doc_ids},
        }).decode("utf-8")

        dataset_keyword_table = self.dataset.dataset_keyword_table
        if dataset_keyword_table.data_source_type == "database":
            dataset_keyword_table.keyword_table = blob
            db.session.commit()
        else:
            file_key = "keyword_files/" + self.dataset.tenant_id + "/" + self.dataset.id + ".txt"
            if storage.exists(file_key):
                storage.delete(file_key)
            storage.save(file_key, blob.encode("utf-8"))

    @staticmethod
    def _empty_blob() -> str:
        return orjson.dumps(
            {"__type__": "bm25_index", "__data__": {"corpus_tokens": [], "doc_ids": []}}
        ).decode("utf-8")
