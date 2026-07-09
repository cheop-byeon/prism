"""Unit tests for JsonlExtractor metadata handling."""

import json

from core.rag.extractor.jsonl_extractor import JsonlExtractor


def _write_jsonl(tmp_path, records):
    path = tmp_path / "records.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(path)


class TestJsonlExtractorMetadata:
    def test_body_promoted_to_page_content_and_excluded_from_metadata(self, tmp_path):
        # The promoted text field must not be duplicated into metadata:
        # document metadata is copied onto every child chunk, so a leaked
        # body field stores the full record text once per child chunk.
        path = _write_jsonl(tmp_path, [{"id": "a", "body": "the actual text", "subject": "s"}])

        documents = JsonlExtractor(path).extract()

        assert documents[0].page_content == "the actual text"
        assert "body" not in documents[0].metadata
        assert documents[0].metadata["id"] == "a"
        assert documents[0].metadata["subject"] == "s"

    def test_content_promoted_and_excluded_from_metadata(self, tmp_path):
        path = _write_jsonl(tmp_path, [{"id": "b", "content": "section text", "section_id": "1.2"}])

        documents = JsonlExtractor(path).extract()

        assert documents[0].page_content == "section text"
        assert "content" not in documents[0].metadata
        assert documents[0].metadata["section_id"] == "1.2"

    def test_record_without_text_field_serializes_whole_record(self, tmp_path):
        record = {"id": "c", "value": 42}
        path = _write_jsonl(tmp_path, [record])

        documents = JsonlExtractor(path).extract()

        assert json.loads(documents[0].page_content) == record
        assert documents[0].metadata["id"] == "c"
        assert documents[0].metadata["value"] == 42

    def test_one_document_per_line_with_line_numbers(self, tmp_path):
        path = _write_jsonl(
            tmp_path,
            [{"id": "1", "body": "first record body"}, {"id": "2", "body": "second record body"}],
        )

        documents = JsonlExtractor(path).extract()

        assert len(documents) == 2
        assert [d.metadata["line"] for d in documents] == [1, 2]
