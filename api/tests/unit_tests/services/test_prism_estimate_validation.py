"""Prism's chunk strategies through the indexing-estimate process-rule validation."""

import pytest

from services.knowledge.entities.indexing_estimate import EstimateValidationError, normalize_process_rule


def _custom_rule(segmentation: dict) -> dict:
    return {
        "mode": "custom",
        "rules": {
            "pre_processing_rules": [{"id": "remove_stopwords", "enabled": True}],
            "segmentation": segmentation,
        },
    }


def test_boundary_strategy_needs_no_separator_or_max_tokens():
    normalized = normalize_process_rule(_custom_rule({"chunk_strategy": "paragraph"}))

    assert normalized["rules"]["segmentation"] == {"chunk_strategy": "paragraph"}


def test_framework_strategy_keeps_its_params():
    normalized = normalize_process_rule(
        _custom_rule({"chunk_strategy": "lc_recursive", "strategy_params": {"chunk_size": 800}})
    )

    assert normalized["rules"]["segmentation"] == {
        "chunk_strategy": "lc_recursive",
        "strategy_params": {"chunk_size": 800},
    }


def test_size_based_strategy_keeps_overlap():
    normalized = normalize_process_rule(
        _custom_rule({"chunk_strategy": "token", "separator": "\n", "max_tokens": 256, "chunk_overlap": 32})
    )

    assert normalized["rules"]["segmentation"] == {
        "chunk_strategy": "token",
        "separator": "\n",
        "max_tokens": 256,
        "chunk_overlap": 32,
    }


@pytest.mark.parametrize("strategy", [None, "recursive", "token"])
def test_size_based_strategy_still_requires_separator(strategy):
    segmentation = {"max_tokens": 256}
    if strategy:
        segmentation["chunk_strategy"] = strategy

    with pytest.raises(EstimateValidationError, match="Process rule segmentation separator is required"):
        normalize_process_rule(_custom_rule(segmentation))


def test_hierarchical_child_keeps_its_own_strategy():
    normalized = normalize_process_rule(
        {
            "mode": "hierarchical",
            "rules": {
                "pre_processing_rules": [],
                "segmentation": {"chunk_strategy": "paragraph"},
                "subchunk_segmentation": {"chunk_strategy": "sentence", "separator": "\n", "max_tokens": 128},
            },
        }
    )

    rules = normalized["rules"]
    assert rules["parent_mode"] == "paragraph"
    assert rules["segmentation"] == {"chunk_strategy": "paragraph"}
    assert rules["subchunk_segmentation"] == {"chunk_strategy": "sentence", "separator": "\n", "max_tokens": 128}
