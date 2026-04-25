"""Functionality for splitting text."""

from __future__ import annotations

import codecs
import re
from typing import Any

from core.model_manager import ModelInstance
from core.rag.splitter.text_splitter import (
    TS,
    Collection,
    Literal,
    RecursiveCharacterTextSplitter,
    Set,
    TextSplitter,
    Union,
)
from dify_graph.model_runtime.model_providers.__base.tokenizers.gpt2_tokenizer import GPT2Tokenizer


class EnhanceRecursiveCharacterTextSplitter(RecursiveCharacterTextSplitter):
    """
    This class is used to implement from_gpt2_encoder, to prevent using of tiktoken
    """

    @classmethod
    def from_encoder(
        cls: type[TS],
        embedding_model_instance: ModelInstance | None,
        allowed_special: Union[Literal["all"], Set[str]] = set(),  # noqa: UP037
        disallowed_special: Union[Literal["all"], Collection[str]] = "all",  # noqa: UP037
        **kwargs: Any,
    ):
        def _token_encoder(texts: list[str]) -> list[int]:
            if not texts:
                return []

            if embedding_model_instance:
                return embedding_model_instance.get_text_embedding_num_tokens(texts=texts)
            else:
                return [GPT2Tokenizer.get_num_tokens(text) for text in texts]

        def _character_encoder(texts: list[str]) -> list[int]:
            if not texts:
                return []

            return [len(text) for text in texts]

        return cls(length_function=_character_encoder, **kwargs)


class FixedRecursiveCharacterTextSplitter(EnhanceRecursiveCharacterTextSplitter):
    def __init__(self, fixed_separator: str = "\n\n", separators: list[str] | None = None, **kwargs: Any):
        """Create a new TextSplitter."""
        super().__init__(**kwargs)
        self._fixed_separator = codecs.decode(fixed_separator, "unicode_escape")
        self._separators = separators or ["\n\n", "\n", "。", ". ", " ", ""]

    def split_text(self, text: str) -> list[str]:
        """Split incoming text and return chunks."""
        if self._fixed_separator:
            chunks = text.split(self._fixed_separator)
        else:
            chunks = [text]

        final_chunks = []
        chunks_lengths = self._length_function(chunks)
        for chunk, chunk_length in zip(chunks, chunks_lengths):
            if chunk_length > self._chunk_size:
                final_chunks.extend(self.recursive_split_text(chunk))
            else:
                final_chunks.append(chunk)

        return final_chunks

    def recursive_split_text(self, text: str) -> list[str]:
        """Split incoming text and return chunks."""

        final_chunks = []
        separator = self._separators[-1]
        new_separators = []

        for i, _s in enumerate(self._separators):
            if _s == "":
                separator = _s
                break
            if _s in text:
                separator = _s
                new_separators = self._separators[i + 1 :]
                break

        # Now that we have the separator, split the text
        if separator:
            if separator == " ":
                splits = re.split(r" +", text)
            else:
                splits = text.split(separator)
                if self._keep_separator:
                    splits = [s + separator for s in splits[:-1]] + splits[-1:]
        else:
            splits = list(text)
        if separator == "\n":
            splits = [s for s in splits if s != ""]
        else:
            splits = [s for s in splits if (s not in {"", "\n"})]
        _good_splits = []
        _good_splits_lengths = []  # cache the lengths of the splits
        _separator = "" if self._keep_separator else separator
        s_lens = self._length_function(splits)
        if separator != "":
            for s, s_len in zip(splits, s_lens):
                if s_len < self._chunk_size:
                    _good_splits.append(s)
                    _good_splits_lengths.append(s_len)
                else:
                    if _good_splits:
                        merged_text = self._merge_splits(_good_splits, _separator, _good_splits_lengths)
                        final_chunks.extend(merged_text)
                        _good_splits = []
                        _good_splits_lengths = []
                    if not new_separators:
                        final_chunks.append(s)
                    else:
                        other_info = self._split_text(s, new_separators)
                        final_chunks.extend(other_info)

            if _good_splits:
                merged_text = self._merge_splits(_good_splits, _separator, _good_splits_lengths)
                final_chunks.extend(merged_text)
        else:
            current_part = ""
            current_length = 0
            overlap_part = ""
            overlap_part_length = 0
            for s, s_len in zip(splits, s_lens):
                if current_length + s_len <= self._chunk_size - self._chunk_overlap:
                    current_part += s
                    current_length += s_len
                elif current_length + s_len <= self._chunk_size:
                    current_part += s
                    current_length += s_len
                    overlap_part += s
                    overlap_part_length += s_len
                else:
                    final_chunks.append(current_part)
                    current_part = overlap_part + s
                    current_length = s_len + overlap_part_length
                    overlap_part = ""
                    overlap_part_length = 0
            if current_part:
                final_chunks.append(current_part)

        return final_chunks


class SemanticTextSplitter(TextSplitter):
    """Groups sentences into chunks by embedding cosine-similarity.

    Adjacent sentences whose similarity exceeds *similarity_threshold* are
    merged into the same chunk.  When no embedding model is available the
    splitter falls back to paragraph boundaries.
    """

    def __init__(
        self,
        embedding_model_instance: ModelInstance | None,
        similarity_threshold: float = 0.5,
    ) -> None:
        super().__init__(chunk_size=10_000_000, chunk_overlap=0)
        self._embedding_model_instance = embedding_model_instance
        self._similarity_threshold = similarity_threshold

    # ------------------------------------------------------------------
    # TextSplitter interface
    # ------------------------------------------------------------------

    def split_text(self, text: str) -> list[str]:
        sentences = self._sentence_tokenize(text)
        if len(sentences) <= 1:
            return [s for s in sentences if s]

        if self._embedding_model_instance is None:
            # No model → fall back to paragraph splitting
            return [c.strip() for c in re.split(r"\n\n+", text) if c.strip()]

        embeddings = self._embedding_model_instance.invoke_text_embedding(texts=sentences).embeddings
        chunks: list[str] = []
        current: list[str] = [sentences[0]]

        for i in range(1, len(sentences)):
            sim = self._cosine_similarity(embeddings[i - 1], embeddings[i])
            if sim >= self._similarity_threshold:
                current.append(sentences[i])
            else:
                chunks.append(" ".join(current))
                current = [sentences[i]]

        if current:
            chunks.append(" ".join(current))

        return chunks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sentence_tokenize(text: str) -> list[str]:
        try:
            import nltk
            try:
                return nltk.sent_tokenize(text)
            except LookupError:
                nltk.download("punkt_tab", quiet=True)
                return nltk.sent_tokenize(text)
        except Exception:
            # Simple regex fallback when NLTK is unavailable
            return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        import math
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


class ParagraphSplitter(TextSplitter):
    """Splits on blank-line boundaries without enforcing a size limit.

    Each double-newline-separated paragraph becomes one chunk. Long paragraphs
    are kept intact rather than being sub-split, since the user explicitly chose
    a boundary-based strategy.
    """

    def __init__(self) -> None:
        # chunk_size must be > chunk_overlap; use a sentinel large value so the
        # base-class guard passes while we never actually enforce it.
        super().__init__(chunk_size=10_000_000, chunk_overlap=0)

    def split_text(self, text: str) -> list[str]:
        chunks = re.split(r"\n\n+", text)
        return [c.strip() for c in chunks if c.strip()]
