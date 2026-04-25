"""Chunk strategy presets shared by indexing processors."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

ChunkStrategy = Literal[
    # Custom separator-based strategies (our implementations)
    "recursive",
    "paragraph",
    "sentence",
    "markdown",
    "html",
    "json",
    "code",
    "latex",
    "token",
    "semantic",
    # LlamaIndex strategies (require llama-index package)
    "li_sentence",
    "li_token",
    "li_fixed",
    "li_semantic",
    "li_markdown",
    "li_html",
    "li_code",
    # LangChain strategies (require langchain package)
    "lc_recursive",
    "lc_character",
    "lc_token",
    "lc_markdown_header",
    "lc_html_header",
    "lc_semantic",
    "lc_python",
]

DEFAULT_CHUNK_STRATEGY: ChunkStrategy = "recursive"

StrategyProviderType = Literal["custom", "llamaindex", "langchain"]


class StrategyCategory(StrEnum):
    """Determines which sizing inputs are meaningful for a strategy."""

    CHARACTER = "character"  # delimiter + char max + char overlap
    TOKEN = "token"          # token max + token overlap, no delimiter
    BOUNDARY = "boundary"    # natural boundaries only, no size inputs
    FRAMEWORK = "framework"  # framework-specific params in strategy_params dict


_STRATEGY_CATEGORIES: dict[str, StrategyCategory] = {
    # Custom
    "recursive": StrategyCategory.CHARACTER,
    "sentence":  StrategyCategory.CHARACTER,
    "markdown":  StrategyCategory.CHARACTER,
    "html":      StrategyCategory.CHARACTER,
    "json":      StrategyCategory.CHARACTER,
    "code":      StrategyCategory.CHARACTER,
    "latex":     StrategyCategory.CHARACTER,
    "token":     StrategyCategory.TOKEN,
    "paragraph": StrategyCategory.BOUNDARY,
    "semantic":  StrategyCategory.BOUNDARY,
    # LlamaIndex
    "li_sentence":       StrategyCategory.FRAMEWORK,
    "li_token":          StrategyCategory.FRAMEWORK,
    "li_fixed":          StrategyCategory.FRAMEWORK,
    "li_semantic":       StrategyCategory.FRAMEWORK,
    "li_markdown":       StrategyCategory.FRAMEWORK,
    "li_html":           StrategyCategory.FRAMEWORK,
    "li_code":           StrategyCategory.FRAMEWORK,
    # LangChain
    "lc_recursive":      StrategyCategory.FRAMEWORK,
    "lc_character":      StrategyCategory.FRAMEWORK,
    "lc_token":          StrategyCategory.FRAMEWORK,
    "lc_markdown_header": StrategyCategory.FRAMEWORK,
    "lc_html_header":    StrategyCategory.FRAMEWORK,
    "lc_semantic":       StrategyCategory.FRAMEWORK,
    "lc_python":         StrategyCategory.FRAMEWORK,
}

_STRATEGY_PROVIDERS: dict[str, StrategyProviderType] = {
    # Custom
    "recursive": "custom", "paragraph": "custom", "sentence": "custom",
    "markdown": "custom", "html": "custom", "json": "custom",
    "code": "custom", "latex": "custom", "token": "custom", "semantic": "custom",
    # LlamaIndex
    "li_sentence": "llamaindex", "li_token": "llamaindex", "li_fixed": "llamaindex",
    "li_semantic": "llamaindex", "li_markdown": "llamaindex", "li_html": "llamaindex",
    "li_code": "llamaindex",
    # LangChain
    "lc_recursive": "langchain", "lc_character": "langchain", "lc_token": "langchain",
    "lc_markdown_header": "langchain", "lc_html_header": "langchain",
    "lc_semantic": "langchain", "lc_python": "langchain",
}


def get_strategy_category(strategy: str) -> StrategyCategory:
    return _STRATEGY_CATEGORIES.get(strategy, StrategyCategory.CHARACTER)


def get_strategy_provider(strategy: str) -> StrategyProviderType:
    return _STRATEGY_PROVIDERS.get(strategy, "custom")


def get_strategy_separators(strategy: str) -> list[str]:
    """Return ordered separator presets for character-based strategies."""
    presets: dict[str, list[str]] = {
        "recursive": ["\n\n", "。", ". ", " ", ""],
        "sentence":  ["。", ". ", "! ", "? ", "\n", " ", ""],
        "markdown":  ["\n## ", "\n### ", "\n#### ", "\n\n", "\n", " ", ""],
        "html":      ["</article>", "</section>", "</div>", "</p>", "<br>", "\n", " ", ""],
        "json":      ["\n},", "\n],", "},", "],", "\n", " ", ""],
        "code":      ["\nclass ", "\ndef ", "\n\n", "\n", " ", ""],
        "latex":     ["\n\\section{", "\n\\subsection{", "\n\\paragraph{", "\n\n", "\n", " ", ""],
    }
    return presets.get(strategy, presets[DEFAULT_CHUNK_STRATEGY])
