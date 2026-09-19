"""Unit tests for JobStatusService's disk-backed job status tracking."""

from services.job_status_service import JobStatusService


class TestJobStatusService:
    def test_get_status_returns_none_when_nothing_written(self, tmp_path, monkeypatch):
        monkeypatch.setattr("services.job_status_service._JOBS_DIR", tmp_path)

        assert JobStatusService.get_status("embed:draft-nonexistent") is None

    def test_set_status_then_get_status_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("services.job_status_service._JOBS_DIR", tmp_path)

        JobStatusService.set_status("embed:draft-a", "running", current=3, total=10, message="Embedding… (3/10)")
        status = JobStatusService.get_status("embed:draft-a")

        assert status is not None
        assert status["status"] == "running"
        assert status["current"] == 3
        assert status["total"] == 10
        assert status["message"] == "Embedding… (3/10)"
        assert "updated_at" in status

    def test_later_set_status_overwrites_earlier(self, tmp_path, monkeypatch):
        monkeypatch.setattr("services.job_status_service._JOBS_DIR", tmp_path)

        JobStatusService.set_status("embed:draft-b", "running", current=1, total=5)
        JobStatusService.set_status("embed:draft-b", "completed", current=5, total=5, message="Embedded 5 documents.")

        status = JobStatusService.get_status("embed:draft-b")
        assert status is not None
        assert status["status"] == "completed"
        assert status["current"] == 5

    def test_get_status_survives_a_corrupted_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("services.job_status_service._JOBS_DIR", tmp_path)
        (tmp_path / "embed:draft-c.json").write_text("{not valid json")

        assert JobStatusService.get_status("embed:draft-c") is None
