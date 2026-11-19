from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, WithJsonSchema


class ParentMode(StrEnum):
    FULL_DOC = "full-doc"
    PARAGRAPH = "paragraph"


PreProcessingRuleID = Annotated[
    str,
    WithJsonSchema(
        {
            "enum": ["remove_stopwords", "remove_extra_spaces", "remove_urls_emails"],
            "type": "string",
        }
    ),
]


class PreProcessingRule(BaseModel):
    id: PreProcessingRuleID = Field(description="Rule identifier.")
    enabled: bool = Field(description="Whether this preprocessing rule is enabled.")


class Segmentation(BaseModel):
    # Prism: separator and max_tokens are optional because boundary and
    # framework chunk strategies (see core.rag.splitter.chunk_strategy) don't
    # split by size.
    separator: str | None = Field(default=None, description="Custom separator for splitting text.")
    max_tokens: int | None = Field(default=None, description="Maximum token count per chunk.")
    chunk_overlap: int | None = Field(default=None, description="Token overlap between chunks.")
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
