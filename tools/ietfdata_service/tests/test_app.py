from __future__ import annotations

import base64
from email.message import EmailMessage

from fastapi.testclient import TestClient

from ietfdata_service import app as app_module


class FakeEnvelope:
    def __init__(self, subject: str, message_id: str) -> None:
        self._subject = subject
        self._message_id = message_id

    def subject(self) -> str:
        return self._subject

    def date(self) -> str:
        return "2026-04-19"

    def from_(self) -> str:
        return "author@example.com"

    def to(self) -> list[str]:
        return ["list@example.org"]

    def message_id(self) -> str:
        return self._message_id

    def contents(self) -> EmailMessage:
        msg = EmailMessage()
        msg.set_content("Test message body")
        return msg


class FakeMailArchive:
    def update_mailing_list_names(self) -> None:
        return None

    def update_mailing_list(self, _name: str) -> None:
        return None

    def mailing_list_names(self) -> list[str]:
        return ["httpapi"]

    def mailing_list(self, name: str) -> "FakeMailingList":
        assert name == "httpapi"
        return FakeMailingList()


class FakeMailingList:
    def messages(self, **_: object) -> list[FakeEnvelope]:
        return [FakeEnvelope("Subject A", "<a@example.org>")]


class FakeRfcEntry:
    def __init__(self, doc_id: str) -> None:
        self.doc_id = doc_id
        self.title = f"Title for {doc_id}"
        self.stream = "IETF"
        self.curr_status = "PROPOSED STANDARD"
        self.draft_names = ["draft-ietf-quic-transport"] if doc_id == "RFC9000" else []

    def content_url(self, fmt: str) -> str | None:
        if fmt == "ASCII":
            return f"https://example.com/{self.doc_id}.txt"
        return None

    def date(self) -> str:
        return "2026-01"


class FakeRfcIndex:
    def rfcs(self, **_: object) -> list[FakeRfcEntry]:
        return [FakeRfcEntry("RFC9000")]

    def rfc(self, doc_id: str) -> FakeRfcEntry:
        if doc_id != "RFC9000":
            raise KeyError(doc_id)
        return FakeRfcEntry(doc_id)


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeSession:
    def get(self, url: str, timeout: int = 30) -> FakeResponse:
        return FakeResponse(f"Fetched content from {url}")

    def close(self) -> None:
        return None


def test_mailarchive_import_returns_markdown_file(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_mailarchive", lambda: FakeMailArchive())
    client = TestClient(app_module.app)

    response = client.post(
        "/api/v1/import/mailarchive",
        json={
            "mailing_list": "httpapi",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"].endswith(".jsonl")
    assert payload["mime_type"] == "application/x-ndjson"
    assert payload["content_base64"]


def test_rfc_import_returns_markdown_file(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_rfc_index", lambda: FakeRfcIndex())
    monkeypatch.setattr(app_module.requests, "Session", FakeSession)
    client = TestClient(app_module.app)

    response = client.post(
        "/api/v1/import/rfcs",
        json={
            "rfc_ids": ["9000"],
            "limit": 1,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"].lower().startswith("ietf-")
    assert "9000" in payload["filename"].lower()
    assert payload["mime_type"] == "text/markdown"
    assert payload["content_base64"]


def test_rfc_suggestions_include_draft_names(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_rfc_index", lambda: FakeRfcIndex())
    client = TestClient(app_module.app)

    response = client.get("/api/v1/rfcs/suggestions", params={"query": "9000", "limit": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["suggestions"]
    assert payload["suggestions"][0]["doc_id"] == "RFC9000"
    assert "draft-ietf-quic-transport" in payload["suggestions"][0]["draft_names"]


def test_extract_technical_body_text_removes_non_technical_sections() -> None:
    raw = """RFC 9000                           QUIC                          May 2021
Table of Contents
1. Introduction..........................3
2. Protocol Overview.....................7

1. Introduction
This section contains the technical intro.

2. Protocol Overview
This section contains the protocol details.

10. References
[RFC2119]  Key words for use in RFCs.

11. Acknowledgments
Thanks to contributors.

12. Authors' Addresses
Alice Example
[Page 99]
"""

    processed = app_module._extract_technical_body_text(raw)

    assert "1. Introduction" in processed
    assert "2. Protocol Overview" in processed
    assert "Table of Contents" not in processed
    assert "10. References" not in processed
    assert "Acknowledgments" not in processed
    assert "Authors' Addresses" not in processed
    assert "[Page 99]" not in processed


def test_draft_import_applies_technical_preprocessing(monkeypatch) -> None:
    class FakeDraftResponse:
        status_code = 200
        ok = True
        text = """Internet-Draft
1. Introduction
Keep this body.
2. Mechanism
Keep this too.
9. References
Drop this.
"""

    monkeypatch.setattr(app_module.requests, "get", lambda *_args, **_kwargs: FakeDraftResponse())
    client = TestClient(app_module.app)

    response = client.post("/api/v1/import/drafts", json={"draft_names": ["draft-test-00"]})

    assert response.status_code == 200
    payload = response.json()
    content = base64.b64decode(payload["content_base64"]).decode("utf-8")
    assert "1. Introduction" in content
    assert "2. Mechanism" in content
    assert "9. References" not in content
