"""Unit tests for IetfImportService.preprocess_mbox_file header handling."""

import pytest

from services.ietf_import_service import IetfImportService


def _mbox_message(message_id: str, body: str, archived_at: str | None = None) -> str:
    headers = [
        "From sender@example.com Mon Mar 01 10:00:00 2010",
        "From: Alice <alice@example.com>",
        "To: ledbat@ietf.org",
        f"Subject: Congestion control question {message_id}",
        "Date: Mon, 01 Mar 2010 10:00:00 +0000",
        f"Message-ID: <{message_id}>",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if archived_at:
        headers.insert(6, f"Archived-At: <{archived_at}>")
    return "\n".join(headers) + "\n\n" + body + "\n"


class TestPreprocessMboxArchivedAtFallback:
    def test_message_without_archived_at_is_kept_with_filter_on(self, tmp_path):
        # Pre-2014 IETF list mail has no Archived-At header; the quality
        # filter must not discard it — the header only affects the citation
        # URL, not content quality.
        body = "The target delay should stay at 100ms because interop testing showed queue buildup."
        mbox = tmp_path / "old-list.mbox"
        mbox.write_text(_mbox_message("old1@x", body))

        result = IetfImportService.preprocess_mbox_file(str(mbox), filter_emails=True)

        assert result["total_emails"] == 1
        assert result["discarded_emails"] == 0
        record = result["emails"][0]
        assert record["id"] == "old1@x"
        assert record["url"] == "mid:old1@x"
        assert record["message_id"] == "<old1@x>"

    def test_archived_at_still_preferred_when_present(self, tmp_path):
        body = "Please review the updated draft text before the interim meeting next Thursday."
        url = "https://mailarchive.ietf.org/arch/msg/ledbat/AbCdEf123"
        mbox = tmp_path / "new-list.mbox"
        mbox.write_text(_mbox_message("new1@x", body, archived_at=url))

        result = IetfImportService.preprocess_mbox_file(str(mbox), filter_emails=True)

        assert result["total_emails"] == 1
        assert result["emails"][0]["url"] == url
        assert result["emails"][0]["id"] == "AbCdEf123"

    def test_quality_filter_still_discards_short_bodies(self, tmp_path):
        mbox = tmp_path / "short.mbox"
        mbox.write_text(_mbox_message("short1@x", "+1"))

        result = IetfImportService.preprocess_mbox_file(str(mbox), filter_emails=True)

        assert result["total_emails"] == 0
        assert result["discarded_emails"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
