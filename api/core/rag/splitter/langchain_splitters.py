"""Thin wrappers around LangChain text-splitter classes.

Each wrapper adapts LangChain splitters to the TextSplitter interface
expected by the RAG pipeline.  All imports are deferred so that the
module can be imported even when langchain is not installed; a clear
ImportError is raised at construction time instead.
"""

from __future__ import annotations

import re
from typing import Any

from core.rag.splitter.text_splitter import TextSplitter


def _require_langchain() -> None:
    try:
        import langchain  # noqa: F401
    except ImportError:
        raise ImportError(
            "langchain is not installed. "
            "Run `pip install langchain langchain-text-splitters` to use LangChain splitters."
        )


def _require_langchain_text_splitters() -> None:
    try:
        import langchain_text_splitters  # noqa: F401
    except ImportError:
        raise ImportError(
            "langchain-text-splitters is not installed. "
            "Run `pip install langchain-text-splitters` to use LangChain splitters."
        )


class _LangChainAdapter(TextSplitter):
    """Base class that adapts a LangChain splitter to the TextSplitter interface."""

    _lc_splitter: Any

    def split_text(self, text: str) -> list[str]:
        chunks = self._lc_splitter.split_text(text)
        return [c for c in chunks if c.strip()]


class LCRecursiveCharacterTextSplitter(_LangChainAdapter):
    """LangChain RecursiveCharacterTextSplitter."""

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200, **kwargs: Any) -> None:
        _require_langchain_text_splitters()
        super().__init__(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        self._lc_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )


class LCCharacterTextSplitter(_LangChainAdapter):
    """LangChain CharacterTextSplitter."""

    def __init__(
        self,
        separator: str = "\n\n",
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        **kwargs: Any,
    ) -> None:
        _require_langchain_text_splitters()
        super().__init__(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        from langchain_text_splitters import CharacterTextSplitter
        self._lc_splitter = CharacterTextSplitter(
            separator=separator, chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )


class LCTokenTextSplitter(_LangChainAdapter):
    """LangChain TokenTextSplitter."""

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 0, **kwargs: Any) -> None:
        _require_langchain_text_splitters()
        super().__init__(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        from langchain_text_splitters import TokenTextSplitter
        self._lc_splitter = TokenTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)


class LCMarkdownHeaderTextSplitter(TextSplitter):
    """LangChain MarkdownHeaderTextSplitter."""

    def __init__(self, **kwargs: Any) -> None:
        _require_langchain_text_splitters()
        super().__init__(chunk_size=10_000_000, chunk_overlap=0)
        from langchain_text_splitters import MarkdownHeaderTextSplitter
        self._splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "H1"), ("##", "H2"), ("###", "H3")]
        )

    def split_text(self, text: str) -> list[str]:
        docs = self._splitter.split_text(text)
        return [d.page_content for d in docs if d.page_content.strip()]


class LCHTMLHeaderTextSplitter(TextSplitter):
    """LangChain HTMLHeaderTextSplitter."""

    def __init__(self, **kwargs: Any) -> None:
        _require_langchain_text_splitters()
        super().__init__(chunk_size=10_000_000, chunk_overlap=0)
        from langchain_text_splitters import HTMLHeaderTextSplitter
        self._splitter = HTMLHeaderTextSplitter(
            headers_to_split_on=[("h1", "H1"), ("h2", "H2"), ("h3", "H3")]
        )

    def split_text(self, text: str) -> list[str]:
        docs = self._splitter.split_text(text)
        return [d.page_content for d in docs if d.page_content.strip()]


class LCSemanticChunker(TextSplitter):
    """LangChain SemanticChunker (requires langchain-experimental)."""

    BREAKPOINT_TYPES = ("percentile", "standard_deviation", "interquartile", "gradient")

    def __init__(
        self,
        breakpoint_threshold_type: str = "percentile",
        breakpoint_threshold_amount: float | None = None,
        embedding_model_instance=None,
        **kwargs: Any,
    ) -> None:
        try:
            import langchain_experimental  # noqa: F401
        except ImportError:
            raise ImportError(
                "langchain-experimental is not installed. "
                "Run `pip install langchain-experimental` to use SemanticChunker."
            )
        super().__init__(chunk_size=10_000_000, chunk_overlap=0)
        self._breakpoint_threshold_type = breakpoint_threshold_type
        self._breakpoint_threshold_amount = breakpoint_threshold_amount
        self._embedding_model_instance = embedding_model_instance

    def split_text(self, text: str) -> list[str]:
        try:
            from langchain.embeddings.base import Embeddings
            from langchain_experimental.text_splitter import SemanticChunker

            class _DifyEmbeddings(Embeddings):
                def __init__(self, model_instance):
                    self._model_instance = model_instance

                def embed_documents(self, texts: list[str]) -> list[list[float]]:
                    return self._model_instance.invoke_text_embedding(texts=texts).embeddings

                def embed_query(self, text: str) -> list[float]:
                    return self.embed_documents([text])[0]

            if self._embedding_model_instance is None:
                return [c.strip() for c in re.split(r"\n\n+", text) if c.strip()]

            embed = _DifyEmbeddings(self._embedding_model_instance)
            kwargs: dict[str, Any] = {"embeddings": embed, "breakpoint_threshold_type": self._breakpoint_threshold_type}
            if self._breakpoint_threshold_amount is not None:
                kwargs["breakpoint_threshold_amount"] = self._breakpoint_threshold_amount
            chunker = SemanticChunker(**kwargs)
            return [c for c in chunker.split_text(text) if c.strip()]
        except Exception:
            return [c.strip() for c in re.split(r"\n\n+", text) if c.strip()]


class LCPythonCodeTextSplitter(_LangChainAdapter):
    """LangChain PythonCodeTextSplitter."""

    def __init__(self, chunk_size: int = 1000, chunk_overlap: int = 200, **kwargs: Any) -> None:
        _require_langchain_text_splitters()
        super().__init__(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        from langchain_text_splitters import PythonCodeTextSplitter
        self._lc_splitter = PythonCodeTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
