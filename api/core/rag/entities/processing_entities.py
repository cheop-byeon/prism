from enum import StrEnum
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field


class ParentMode(StrEnum):
    FULL_DOC = "full-doc"
    PARAGRAPH = "paragraph"


type PreProcessingRuleKey = Literal["remove_stopwords", "remove_extra_spaces", "remove_urls_emails"]


class PreProcessingRule(BaseModel):
    id: PreProcessingRuleKey = Field(description="Rule identifier.")
    enabled: bool = Field(description="Whether this preprocessing rule is enabled.")


class Segmentation(BaseModel):
    # Prism: separator and max_tokens are optional because boundary and
    # framework chunk strategies (see core.rag.splitter.chunk_strategy) don't
    # split by size.
    # TODO: there are internally mismatched / inconsistent naming
    # between `separator` and `delimiter` across the codebase.
    # Taking `separator` as the canonical.
    separator: str | None = Field(
        default=None,
        description="Custom separator for splitting text.",
        validation_alias=AliasChoices("separator", "delimiter"),
    )
    max_tokens: int | None = Field(default=None, description="Maximum token count per chunk.")
    chunk_overlap: int | None = Field(default=None, ge=0, description="Token overlap between chunks.")
    chunk_strategy: str = "recursive"
    strategy_params: dict | None = None


class Rule(BaseModel):
    pre_processing_rules: list[PreProcessingRule] | None = Field(
        default=None,
        description="Pre-processing rules to apply before segmentation.",
    )
    segmentation: Segmentation | None = Field(default=None, description="Parent chunk segmentation settings.")
    parent_mode: Literal["full-doc", "paragraph"] | None = Field(
        default=None,
        description="Parent-child segmentation mode.",
    )
    subchunk_segmentation: Segmentation | None = Field(
        default=None,
        description="Child chunk segmentation settings.",
    )
