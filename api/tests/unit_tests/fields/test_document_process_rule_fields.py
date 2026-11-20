from fields.document_fields import DocumentProcessRuleResponse, DocumentSourceDetailResponse


def test_process_rule_reads_the_legacy_delimiter_as_separator() -> None:
    rule = DocumentProcessRuleResponse.model_validate(
        {
            "mode": "custom",
            "rules": {
                "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": True}],
                "segmentation": {"delimiter": "\n\n", "max_tokens": 500},
            },
        }
    )

    assert rule.model_dump(mode="json") == {
        "mode": "custom",
        "rules": {
            "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": True}],
            "segmentation": {
                "separator": "\n\n",
                "max_tokens": 500,
                "chunk_overlap": 0,
                "chunk_strategy": None,
                "strategy_params": None,
            },
            "parent_mode": None,
            "subchunk_segmentation": None,
        },
    }


def test_process_rule_keeps_prism_boundary_strategy_without_max_tokens() -> None:
    # Prism: boundary and framework chunk strategies are stored without a token budget.
    rule = DocumentProcessRuleResponse.model_validate(
        {
            "mode": "custom",
            "rules": {
                "pre_processing_rules": [],
                "segmentation": {"chunk_strategy": "lc_recursive", "strategy_params": {"chunk_size": 800}},
            },
        }
    )

    assert rule.model_dump(mode="json")["rules"]["segmentation"] == {
        "separator": "\n",
        "max_tokens": None,
        "chunk_overlap": 0,
        "chunk_strategy": "lc_recursive",
        "strategy_params": {"chunk_size": 800},
    }


def test_source_detail_keeps_notion_and_website_source_info() -> None:
    detail = DocumentSourceDetailResponse.model_validate({"url": "https://example.com", "title": "Example"})

    assert detail.model_dump(mode="json") == {"url": "https://example.com", "title": "Example"}
