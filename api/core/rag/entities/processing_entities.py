from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class ParentMode(StrEnum):
    FULL_DOC = "full-doc"
    PARAGRAPH = "paragraph"


class PreProcessingRule(BaseModel):
    id: str
    enabled: bool


class Segmentation(BaseModel):
    # Prism: separator and max_tokens are optional because boundary and
    # framework chunk strategies (see core.rag.splitter.chunk_strategy) don't
    # split by size.
    separator: str | None = None
    max_tokens: int | None = None
    chunk_overlap: int | None = None
    chunk_strategy: str = "recursive"
    strategy_params: dict | None = None


class Rule(BaseModel):
    pre_processing_rules: list[PreProcessingRule] | None = None
    segmentation: Segmentation | None = None
    parent_mode: Literal["full-doc", "paragraph"] | None = None
    subchunk_segmentation: Segmentation | None = None
