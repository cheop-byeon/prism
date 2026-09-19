"""Unit tests for LocalSourceIndexService.build_embedding_index."""

import json

from services import local_source_index_service
from services.local_source_index_service import LocalSourceIndexService


class _FakeResponse:
    def __init__(self, embeddings):
        self._embeddings = embeddings

    def raise_for_status(self):
        pass

    def json(self):
        return {"embeddings": self._embeddings}


class _FakeClient:
    """Records every batch it's asked to embed and returns one fixed-length
    vector per input text, so tests can assert on both the call shape and
    the resulting matrix without a real embedding model running.
    """

    def __init__(self, calls, dims=4, **kwargs):
        self._calls = calls
        self._dims = dims

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json):
        self._calls.append((url, json))
        return _FakeResponse([[0.1] * self._dims for _ in json["input"]])


class TestBuildEmbeddingIndex:
    def test_errors_when_nothing_downloaded_locally(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_source_index_service, "_DOWNLOADS_DIR", tmp_path)
        monkeypatch.setattr(local_source_index_service, "_PREPROCESSED_DIR", tmp_path / "preprocessed")

        result = LocalSourceIndexService.build_embedding_index("draft-ietf-missing", wg=None, rfc_number=None)

        assert result == {"error": "Nothing downloaded locally yet for this draft -- sync first."}

    def test_embeds_every_gathered_document_and_caches_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_source_index_service, "_DOWNLOADS_DIR", tmp_path)
        monkeypatch.setattr(local_source_index_service, "_PREPROCESSED_DIR", tmp_path / "preprocessed")
        monkeypatch.setattr(local_source_index_service, "_INDEX_DIR", tmp_path / "index")

        (tmp_path / "draft-ietf-quic-transport-05.txt").write_text("Transport parameters are negotiated...")
        (tmp_path / "draft-ietf-quic-transport-06.txt").write_text("Revision six changes flow control...")

        calls: list[tuple] = []
        monkeypatch.setattr(local_source_index_service.httpx, "Client", lambda **kwargs: _FakeClient(calls, **kwargs))

        events: list[dict] = []
        result = LocalSourceIndexService.build_embedding_index(
            "draft-ietf-quic-transport", wg=None, rfc_number=None, on_event=events.append, batch_size=1
        )

        assert result["total_documents"] == 2
        assert result["dims"] == 4
        # batch_size=1 with 2 documents -> 2 separate embedding calls
        assert len(calls) == 2

        ids_path = tmp_path / "index" / "draft-ietf-quic-transport-embeddings-ids.json"
        matrix_path = tmp_path / "index" / "draft-ietf-quic-transport-embeddings.npy"
        assert ids_path.is_file()
        assert matrix_path.is_file()
        assert len(json.loads(ids_path.read_text())) == 2

        # Progress events report a growing "current" out of a fixed "total"
        # once the actual embedding batches start (the initial "gathering"
        # event has total=0, filtered out here).
        assert [e["current"] for e in events if e["total"]] == [1, 2]
