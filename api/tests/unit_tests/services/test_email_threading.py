"""Unit tests for services.email_threading."""

from services.email_threading import (
    build_thread_records,
    group_into_threads,
    normalize_subject,
)


def _record(
    message_id: str,
    subject: str,
    date: str,
    references: str = "",
    body: str = "body text long enough to be useful",
    from_: str = "Alice <alice@example.com>",
    url: str = "",
) -> dict:
    return {
        "id": message_id.strip("<>"),
        "message_id": message_id,
        "url": url or f"https://mailarchive.ietf.org/arch/msg/quic/{message_id.strip('<>')}",
        "subject": subject,
        "from": from_,
        "date": date,
        "references": references,
        "body": body,
    }


class TestNormalizeSubject:
    def test_strips_reply_prefixes_and_list_tags(self):
        assert normalize_subject("Re: [quic] Re: Retry and version negotiation") == "Retry and version negotiation"

    def test_strips_fwd_and_mixed_case_prefixes(self):
        assert normalize_subject("FWD: RE: [tls] Deprecating obsolete key exchange") == (
            "Deprecating obsolete key exchange"
        )

    def test_plain_subject_unchanged(self):
        assert normalize_subject("Working group last call") == "Working group last call"


class TestGroupIntoThreads:
    def test_groups_by_references_chain(self):
        a = _record("<a@x>", "Retry design", "Mon, 01 Mar 2021 10:00:00 +0000")
        b = _record("<b@x>", "Re: Retry design", "Mon, 01 Mar 2021 12:00:00 +0000", references="<a@x>")
        c = _record("<c@x>", "Re: Retry design", "Tue, 02 Mar 2021 09:00:00 +0000", references="<a@x> <b@x>")
        d = _record("<d@x>", "Unrelated announcement", "Mon, 01 Mar 2021 11:00:00 +0000")

        threads = group_into_threads([a, b, c, d])

        assert len(threads) == 2
        sizes = sorted(len(t) for t in threads)
        assert sizes == [1, 3]

    def test_reply_without_headers_joins_by_normalized_subject(self):
        root = _record("<root@x>", "Congestion control", "Mon, 01 Mar 2021 10:00:00 +0000")
        reply = _record("<orphan@x>", "Re: [quic] Congestion control", "Mon, 01 Mar 2021 14:00:00 +0000")

        threads = group_into_threads([root, reply])

        assert len(threads) == 1
        assert len(threads[0]) == 2

    def test_two_roots_with_same_subject_stay_separate(self):
        first = _record("<m1@x>", "Minutes", "Mon, 01 Mar 2021 10:00:00 +0000")
        second = _record("<m2@x>", "Minutes", "Mon, 05 Apr 2021 10:00:00 +0000")

        threads = group_into_threads([first, second])

        assert len(threads) == 2

    def test_duplicate_message_ids_are_collapsed(self):
        # Older mbox exports can contain the same message twice (direct
        # delivery + list relay); only the first copy should survive.
        a = _record("<a@x>", "Dup thread", "Mon, 01 Mar 2021 10:00:00 +0000")
        a_dup = _record("<a@x>", "Dup thread", "Mon, 01 Mar 2021 10:00:00 +0000")
        b = _record("<b@x>", "Re: Dup thread", "Tue, 02 Mar 2021 10:00:00 +0000", references="<a@x>")

        threads = group_into_threads([a, a_dup, b])

        assert len(threads) == 1
        assert len(threads[0]) == 2

    def test_messages_sorted_chronologically_within_thread(self):
        a = _record("<a@x>", "Ordering", "Mon, 01 Mar 2021 10:00:00 +0000")
        b = _record("<b@x>", "Re: Ordering", "Tue, 02 Mar 2021 10:00:00 +0000", references="<a@x>")
        c = _record("<c@x>", "Re: Ordering", "Wed, 03 Mar 2021 10:00:00 +0000", references="<b@x>")

        threads = group_into_threads([c, a, b])

        assert len(threads) == 1
        assert [m["message_id"] for m in threads[0]] == ["<a@x>", "<b@x>", "<c@x>"]


class TestBuildThreadRecords:
    def test_single_thread_becomes_one_record(self):
        a = _record("<a@x>", "Retry design", "Mon, 01 Mar 2021 10:00:00 +0000", body="Initial proposal text here.")
        b = _record(
            "<b@x>",
            "Re: Retry design",
            "Tue, 02 Mar 2021 10:00:00 +0000",
            references="<a@x>",
            body="I object to the third paragraph.",
            from_="Bob <bob@example.com>",
        )

        records = build_thread_records([[a, b]])

        assert len(records) == 1
        record = records[0]
        assert record["id"] == "a@x"
        assert record["subject"] == "Retry design"
        assert record["message_count"] == "2"
        assert "Alice" in record["participants"]
        assert "Bob" in record["participants"]
        # Thread header + per-message markers with author, date, and URL
        assert record["parent_unit"] == "record"
        assert record["body"].startswith("Thread: Retry design (2 messages")
        assert "--- Message 1 | Alice <alice@example.com> | 2021-03-01 |" in record["body"]
        assert "--- Message 2 | Bob <bob@example.com> | 2021-03-02 |" in record["body"]
        assert "Initial proposal text here." in record["body"]
        assert "I object to the third paragraph." in record["body"]
        assert "https://mailarchive.ietf.org/arch/msg/quic/a@x" in record["body"]

    def test_oversized_thread_splits_into_parts(self):
        messages = [
            _record(
                f"<m{i}@x>",
                "Big thread" if i == 0 else "Re: Big thread",
                f"Mon, 0{i + 1} Mar 2021 10:00:00 +0000",
                references="<m0@x>" if i else "",
                body=f"message {i} " + "x" * 400,
            )
            for i in range(6)
        ]

        records = build_thread_records([messages], max_chars=1000)

        assert len(records) > 1
        assert all(r["thread_id"] == "m0@x" for r in records)
        # Metadata values are strings (chunk metadata / vector store properties
        # are string-typed by convention — see the annotated-RFC records).
        assert [r["part"] for r in records] == [str(i) for i in range(1, len(records) + 1)]
        assert all(r["part_count"] == str(len(records)) for r in records)
        # Distinct ids per part, all derived from the thread id
        assert len({r["id"] for r in records}) == len(records)
        assert all(r["id"].startswith("m0@x") for r in records)
        # Message numbering continues across parts
        assert "--- Message 1 |" in records[0]["body"]
        assert "--- Message 1 |" not in records[1]["body"]
        # Every part header names the thread and its part position
        assert all(f"part {r['part']}/{r['part_count']}" in r["body"] for r in records)

    def test_default_part_cap_fits_dify_parent_chunk_limit(self):
        # Dify's parent-chunk splitter is hard-capped at 4,000 tokens
        # (~16K chars); the default part cap must stay safely below it so a
        # thread record maps to one parent without being re-split.
        messages = [
            _record(
                f"<m{i}@x>",
                "Long thread" if i == 0 else "Re: Long thread",
                f"Mon, 0{i + 1} Mar 2021 10:00:00 +0000",
                references="<m0@x>" if i else "",
                body="y" * 3000,
            )
            for i in range(5)
        ]

        records = build_thread_records([messages])  # default caps

        assert len(records) > 1  # 15K chars of bodies exceeds the default part cap
        assert all(len(r["body"]) < 16_000 for r in records)

    def test_singleton_thread_still_renders_thread_format(self):
        only = _record("<solo@x>", "Announcement", "Mon, 01 Mar 2021 10:00:00 +0000", body="One-off message body.")

        records = build_thread_records([[only]])

        assert len(records) == 1
        assert records[0]["message_count"] == "1"
        assert records[0]["body"].startswith("Thread: Announcement (1 message")
        assert "One-off message body." in records[0]["body"]

    def test_unparseable_dates_are_tolerated(self):
        a = _record("<a@x>", "No dates", "not-a-date", body="First message body content.")
        b = _record("<b@x>", "Re: No dates", "", references="<a@x>", body="Second message body content.")

        records = build_thread_records(group_into_threads([a, b]))

        assert len(records) == 1
        assert records[0]["message_count"] == "2"
