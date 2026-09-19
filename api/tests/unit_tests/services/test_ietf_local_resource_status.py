"""Unit tests for IetfImportService.get_local_resource_status."""

from services.ietf_import_service import IetfImportService


class TestGetLocalResourceStatus:
    def test_recording_never_counts_a_download(self, tmp_path, monkeypatch):
        monkeypatch.setattr(IetfImportService, "_RESOURCES_DIR", tmp_path)
        (tmp_path / "recording-83-sidr-1.txt").write_text("shouldn't matter")
        documents = [{"name": "recording-83-sidr-1", "uploaded_filename": ""}]

        result = IetfImportService.get_local_resource_status("recording", documents)

        assert result == {"downloaded_count": 0, "last_synced_at": None}

    def test_counts_only_documents_actually_present_on_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(IetfImportService, "_RESOURCES_DIR", tmp_path)
        (tmp_path / "minutes-83-sidr.txt").write_text("minutes text")
        documents = [
            {"name": "minutes-83-sidr", "uploaded_filename": "minutes-83-sidr.txt"},
            {"name": "minutes-84-sidr", "uploaded_filename": "minutes-84-sidr.txt"},
        ]

        result = IetfImportService.get_local_resource_status("minutes", documents)

        assert result["downloaded_count"] == 1
        assert result["last_synced_at"] is not None

    def test_no_uploaded_filename_falls_back_to_txt_extension(self, tmp_path, monkeypatch):
        monkeypatch.setattr(IetfImportService, "_RESOURCES_DIR", tmp_path)
        (tmp_path / "charter-ietf-sidr.txt").write_text("charter text")
        documents = [{"name": "charter-ietf-sidr", "uploaded_filename": ""}]

        result = IetfImportService.get_local_resource_status("charter", documents)

        assert result["downloaded_count"] == 1

    def test_nothing_downloaded_yet_returns_none_not_zero_timestamp(self, tmp_path, monkeypatch):
        monkeypatch.setattr(IetfImportService, "_RESOURCES_DIR", tmp_path)
        documents = [{"name": "slides-83-sidr", "uploaded_filename": "slides-83-sidr.pdf"}]

        result = IetfImportService.get_local_resource_status("slides", documents)

        assert result == {"downloaded_count": 0, "last_synced_at": None}
