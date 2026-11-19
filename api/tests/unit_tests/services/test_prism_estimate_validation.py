"""Prism's chunk strategies through DocumentService.estimate_args_validate."""

import pytest

from services.dataset_service import DocumentService


def _custom_args(segmentation: dict) -> dict:
    return {
        "info_list": {"data_source_type": "upload_file"},
        "process_rule": {
            "mode": "custom",
            "rules": {
                "pre_processing_rules": [{"id": "remove_stopwords", "enabled": True}],
                "segmentation": segmentation,
            },
        },
    }


def test_boundary_strategy_needs_no_separator_or_max_tokens():
    args = _custom_args({"chunk_strategy": "paragraph"})

    DocumentService.estimate_args_validate(args)

    assert args["process_rule"]["rules"]["segmentation"] == {"chunk_strategy": "paragraph"}


def test_framework_strategy_keeps_its_params():
    args = _custom_args({"chunk_strategy": "lc_recursive", "strategy_params": {"chunk_size": 800}})

    DocumentService.estimate_args_validate(args)

    assert args["process_rule"]["rules"]["segmentation"] == {
        "chunk_strategy": "lc_recursive",
        "strategy_params": {"chunk_size": 800},
    }


def test_size_based_strategy_keeps_overlap():
    args = _custom_args({"chunk_strategy": "token", "separator": "\n", "max_tokens": 256, "chunk_overlap": 32})

    DocumentService.estimate_args_validate(args)

    assert args["process_rule"]["rules"]["segmentation"] == {
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

    with pytest.raises(ValueError, match="Process rule segmentation separator is required"):
        DocumentService.estimate_args_validate(_custom_args(segmentation))


def test_hierarchical_child_keeps_its_own_strategy():
    args = {
        "info_list": {"data_source_type": "upload_file"},
        "process_rule": {
            "mode": "hierarchical",
            "rules": {
                "pre_processing_rules": [],
                "segmentation": {"chunk_strategy": "paragraph"},
                "subchunk_segmentation": {"chunk_strategy": "sentence", "separator": "\n", "max_tokens": 128},
            },
        },
    }

    DocumentService.estimate_args_validate(args)

    rules = args["process_rule"]["rules"]
    assert rules["parent_mode"] == "paragraph"
    assert rules["segmentation"] == {"chunk_strategy": "paragraph"}
    assert rules["subchunk_segmentation"] == {"chunk_strategy": "sentence", "separator": "\n", "max_tokens": 128}
