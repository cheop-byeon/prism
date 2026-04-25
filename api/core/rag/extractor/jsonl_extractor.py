"""Extract structured JSONL records into individual text documents.

This extractor is intended for newline-delimited JSON sources where each line is
an object. If a record contains a `body` field, that field is treated as the
chunkable text and the remaining fields stay in document metadata. Otherwise,
the record is serialized back to JSON text.
"""

from __future__ import annotations

import json

from core.rag.extractor.extractor_base import BaseExtractor
from core.rag.models.document import Document


class JsonlExtractor(BaseExtractor):
    """Load JSONL files as one document per line."""

    def __init__(self, file_path: str, encoding: str = "utf-8") -> None:
        self._file_path = file_path
        self._encoding = encoding

    def extract(self) -> list[Document]:
        """Parse each non-empty JSONL line into a separate document."""
        try:
            with open(self._file_path, encoding=self._encoding) as file:
                return self._read_from_file(file)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSONL in {self._file_path}: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Error loading {self._file_path}") from exc

    def _read_from_file(self, file) -> list[Document]:
        documents: list[Document] = []

        for line_number, line in enumerate(file, start=1):
            raw = line.strip()
            if not raw:
                continue

            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")

            documents.append(
                Document(
                    page_content=self._extract_page_content(record),
                    metadata={
                        **record,
                        "source": self._file_path,
                        "line": line_number,
                    },
                )
            )

        if not documents:
            raise ValueError("JSONL file is empty")

        return documents

    @staticmethod
    def _extract_page_content(record: dict) -> str:
        body = record.get("body")
        if isinstance(body, str) and body.strip():
            return body.strip()

        content = record.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

        return json.dumps(record, ensure_ascii=False)