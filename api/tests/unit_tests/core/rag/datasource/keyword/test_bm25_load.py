"""Regression tests for Bm25Keyword corpus loading.

The store previously read its blob through DatasetKeywordTable.keyword_table_dict,
whose Jieba-specific JSON decoder converts every list value to a set - fatal for
BM25's corpus_tokens (a list of lists raises "unhashable type: 'list'") and
silently order-destroying for doc_ids. The store must parse its own raw JSON.
"""

from unittest.mock import MagicMock

import orjson

from core.rag.datasource.keyword.bm25.bm25 import Bm25Keyword


def _dataset_with_blob(blob: dict) -> MagicMock:
    dataset = MagicMock()
    table = MagicMock()
    table.data_source_type = "database"
    table.keyword_table = orjson.dumps(blob).decode()
    dataset.dataset_keyword_table = table
    return dataset


class TestBm25CorpusLoading:
    def test_load_corpus_handles_nested_token_lists(self):
        blob = {
            "__type__": "bm25_index",
            "__data__": {
                "corpus_tokens": [["target", "delay"], ["late", "comers", "measure"]],
                "doc_ids": ["node-1", "node-2"],
            },
        }
        keyword = Bm25Keyword.__new__(Bm25Keyword)
        keyword.dataset = _dataset_with_blob(blob)

        corpus_tokens, doc_ids = keyword._load_corpus()

        assert corpus_tokens == [["target", "delay"], ["late", "comers", "measure"]]
        assert doc_ids == ["node-1", "node-2"]  # order preserved

    def test_load_corpus_returns_empty_for_foreign_blob(self):
        # A table written by the Jieba backend must not be misread as BM25.
        blob = {"__type__": "keyword_table", "__data__": {"kw": ["node-1"]}}
        keyword = Bm25Keyword.__new__(Bm25Keyword)
        keyword.dataset = _dataset_with_blob(blob)

        corpus_tokens, doc_ids = keyword._load_corpus()

        assert corpus_tokens == []
        assert doc_ids == []
