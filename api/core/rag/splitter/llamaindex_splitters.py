"""Thin wrappers around LlamaIndex text-splitter classes.

Each wrapper adapts LlamaIndex splitters to the TextSplitter interface
expected by the RAG pipeline.  All imports are deferred so that the
module can be imported even when llama-index is not installed; a clear
ImportError is raised at construction time instead.
"""

from __future__ import annotations

import re
from typing import Any

from core.rag.splitter.text_splitter import TextSplitter


def _require_llamaindex() -> None:
    try:
        import llama_index  # noqa: F401
    except ImportError:
        raise ImportError(
            "llama-index is not installed. "
            "Run `pip install llama-index` to use LlamaIndex splitters."
        )


class LISentenceSplitter(TextSplitter):
    """SentenceSplitter from llama-index."""

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 200, **kwargs: Any) -> None:
        _require_llamaindex()
        super().__init__(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        from llama_index.core.node_parser import SentenceSplitter
        self._splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    def split_text(self, text: str) -> list[str]:
        nodes = self._splitter.get_nodes_from_documents(
            [self._make_document(text)]
        )
        return [n.get_content() for n in nodes if n.get_content().strip()]

    @staticmethod
    def _make_document(text: str):
        from llama_index.core import Document
        return Document(text=text)


class LITokenTextSplitter(TextSplitter):
    """TokenTextSplitter from llama-index."""

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 20, **kwargs: Any) -> None:
        _require_llamaindex()
        super().__init__(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        from llama_index.core.node_parser import TokenTextSplitter
        self._splitter = TokenTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    def split_text(self, text: str) -> list[str]:
        nodes = self._splitter.get_nodes_from_documents(
            [LISentenceSplitter._make_document(text)]
        )
        return [n.get_content() for n in nodes if n.get_content().strip()]


class LIFixedTextSplitter(TextSplitter):
    """FixedTextSplitter from llama-index (sentence-window-like fixed chunking)."""

    def __init__(self, chunk_size: int = 1024, **kwargs: Any) -> None:
        _require_llamaindex()
        super().__init__(chunk_size=chunk_size, chunk_overlap=0)
        # Use SentenceWindowNodeParser as closest fixed-size equivalent
        self._chunk_size = chunk_size
        from llama_index.core.node_parser import SentenceSplitter
        self._splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=0)

    def split_text(self, text: str) -> list[str]:
        nodes = self._splitter.get_nodes_from_documents(
            [LISentenceSplitter._make_document(text)]
        )
        return [n.get_content() for n in nodes if n.get_content().strip()]


class LISemanticSplitter(TextSplitter):
    """SemanticSplitterNodeParser from llama-index."""

    def __init__(
        self,
        buffer_size: int = 1,
        breakpoint_percentile: int = 95,
        embedding_model_instance=None,
        **kwargs: Any,
    ) -> None:
        _require_llamaindex()
        super().__init__(chunk_size=10_000_000, chunk_overlap=0)
        self._buffer_size = buffer_size
        self._breakpoint_percentile = breakpoint_percentile
        self._embedding_model_instance = embedding_model_instance

    def split_text(self, text: str) -> list[str]:
        try:
            from llama_index.core.embeddings import BaseEmbedding
            from llama_index.core.node_parser import SemanticSplitterNodeParser

            class _DifyEmbedding(BaseEmbedding):
                def __init__(self, model_instance):
                    super().__init__()
                    self._model_instance = model_instance

                def _get_query_embedding(self, query: str) -> list[float]:
                    return self._model_instance.invoke_text_embedding(texts=[query]).embeddings[0]

                def _get_text_embedding(self, text: str) -> list[float]:
                    return self._model_instance.invoke_text_embedding(texts=[text]).embeddings[0]

                async def _aget_query_embedding(self, query: str) -> list[float]:
                    return self._get_query_embedding(query)

            if self._embedding_model_instance is None:
                return [c.strip() for c in re.split(r"\n\n+", text) if c.strip()]

            embed_model = _DifyEmbedding(self._embedding_model_instance)
            splitter = SemanticSplitterNodeParser(
                buffer_size=self._buffer_size,
                breakpoint_percentile_threshold=self._breakpoint_percentile,
                embed_model=embed_model,
            )
            nodes = splitter.get_nodes_from_documents([LISentenceSplitter._make_document(text)])
            return [n.get_content() for n in nodes if n.get_content().strip()]
        except Exception:
            return [c.strip() for c in re.split(r"\n\n+", text) if c.strip()]


class LIMarkdownNodeParser(TextSplitter):
    """MarkdownNodeParser from llama-index."""

    def __init__(self, **kwargs: Any) -> None:
        _require_llamaindex()
        super().__init__(chunk_size=10_000_000, chunk_overlap=0)
        from llama_index.core.node_parser import MarkdownNodeParser
        self._splitter = MarkdownNodeParser()

    def split_text(self, text: str) -> list[str]:
        nodes = self._splitter.get_nodes_from_documents([LISentenceSplitter._make_document(text)])
        return [n.get_content() for n in nodes if n.get_content().strip()]


class LIHTMLNodeParser(TextSplitter):
    """HTMLNodeParser from llama-index."""

    def __init__(self, **kwargs: Any) -> None:
        _require_llamaindex()
        super().__init__(chunk_size=10_000_000, chunk_overlap=0)
        from llama_index.core.node_parser import HTMLNodeParser
        self._splitter = HTMLNodeParser()

    def split_text(self, text: str) -> list[str]:
        nodes = self._splitter.get_nodes_from_documents([LISentenceSplitter._make_document(text)])
        return [n.get_content() for n in nodes if n.get_content().strip()]


class LICodeSplitter(TextSplitter):
    """CodeSplitter from llama-index."""

    SUPPORTED_LANGUAGES = [
        "python", "javascript", "typescript", "java", "c", "cpp",
        "go", "rust", "ruby", "php",
    ]

    def __init__(self, chunk_size: int = 1024, language: str = "python", **kwargs: Any) -> None:
        _require_llamaindex()
        super().__init__(chunk_size=chunk_size, chunk_overlap=0)
        from llama_index.core.node_parser import CodeSplitter
        self._splitter = CodeSplitter(language=language, chunk_lines=40, chunk_lines_overlap=5, max_chars=chunk_size)

    def split_text(self, text: str) -> list[str]:
        try:
            nodes = self._splitter.get_nodes_from_documents([LISentenceSplitter._make_document(text)])
            return [n.get_content() for n in nodes if n.get_content().strip()]
        except Exception:
            return [c.strip() for c in re.split(r"\n\n+", text) if c.strip()]
