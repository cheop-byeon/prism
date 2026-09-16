from types import SimpleNamespace

import pytest

from scripts.agentic_rag.ask_decision_agent import (
    _extract_message_links,
    _format_evolution_hits,
    _format_hits,
    _iddiff_page_url,
    _parse_evolution_body,
    _summarize_usage,
)


def _fake_usage(*, prompt_tokens, completion_tokens, total_tokens, total_price, latency, currency="USD"):
    """A duck-typed stand-in for dify_graph's LLMUsage -- _summarize_usage
    only ever reads these attributes, so a real LLMUsage (which needs
    Decimal fields and pydantic validation) isn't necessary here.
    """
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        total_price=total_price,
        latency=latency,
        currency=currency,
    )


class TestSummarizeUsage:
    def test_empty_log_returns_zeroed_totals(self):
        result = _summarize_usage([])

        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0
        assert result["total_tokens"] == 0
        assert result["total_price"] == 0
        assert result["latency"] == 0.0
        assert result["currency"] == "USD"

    def test_sums_every_llm_call_in_the_turn(self):
        # Mirrors a real turn: several tool-calling iterations plus one
        # verify_answer call, each its own LLMUsage appended via
        # _invoke_llm_with_retry's usage_sink.
        usage_log = [
            _fake_usage(prompt_tokens=100, completion_tokens=20, total_tokens=120, total_price=1, latency=0.5),
            _fake_usage(prompt_tokens=200, completion_tokens=30, total_tokens=230, total_price=2, latency=0.8),
            _fake_usage(prompt_tokens=50, completion_tokens=10, total_tokens=60, total_price=1, latency=0.3),
        ]

        result = _summarize_usage(usage_log)

        assert result["prompt_tokens"] == 350
        assert result["completion_tokens"] == 60
        assert result["total_tokens"] == 410
        assert result["total_price"] == 4
        assert result["latency"] == pytest.approx(1.6)

    def test_currency_takes_the_last_call_seen(self):
        # Every call in one ask() turn uses the same model_instance, so in
        # practice currency never actually varies mid-log -- this documents
        # the (arbitrary but harmless) tie-breaking behavior rather than
        # asserting a scenario that can't occur.
        usage_log = [
            _fake_usage(
                prompt_tokens=1, completion_tokens=1, total_tokens=2, total_price=0, latency=0.1, currency="USD"
            ),
            _fake_usage(
                prompt_tokens=1, completion_tokens=1, total_tokens=2, total_price=0, latency=0.1, currency="EUR"
            ),
        ]

        result = _summarize_usage(usage_log)

        assert result["currency"] == "EUR"


class TestExtractMessageLinks:
    def test_keeps_only_http_permalinks(self):
        # Pre-2014 mail has no Archived-At header -- just a non-fetchable
        # "mid:..." identity -- and that entry should be dropped rather
        # than surfaced as a dead link.
        text = (
            "Some preamble text.\n"
            "--- Message 1 | Jane Doe <jane@example.com> | 2020-01-15 | "
            "https://mailarchive.ietf.org/arch/msg/dnsop/abc123 ---\n"
            "Body of message 1.\n\n"
            "--- Message 2 | John Smith | 2013-05-01 | mid:abc@mail.gmail.com ---\n"
            "Body of message 2.\n"
        )

        links = _extract_message_links(text)

        assert links == [
            {
                "from": "Jane Doe <jane@example.com>",
                "date": "2020-01-15",
                "url": "https://mailarchive.ietf.org/arch/msg/dnsop/abc123",
            }
        ]

    def test_no_markers_returns_empty_list(self):
        assert _extract_message_links("just some plain body text, no markers at all") == []


class TestParseEvolutionBody:
    def test_modified_extracts_before_and_after(self):
        body = "Before (rev 01):\nOld text here.\n\nAfter (rev 02):\nNew text here."

        assert _parse_evolution_body(body) == ("Old text here.", "New text here.")

    def test_added_has_no_before(self):
        body = "New section added in rev 03:\nBrand new content."

        assert _parse_evolution_body(body) == (None, "Brand new content.")

    def test_removed_has_no_after(self):
        body = "Section removed after rev 04:\nOld content that got dropped."

        assert _parse_evolution_body(body) == ("Old content that got dropped.", None)

    def test_unrecognized_format_returns_both_none(self):
        assert _parse_evolution_body("nothing matches this format at all") == (None, None)


class TestFormatHits:
    def test_empty_hits_reports_no_results(self):
        assert _format_hits([]) == "(no results)"

    def test_numbers_hits_from_one(self):
        assert _format_hits(["first", "second"]) == "[1] first\n\n[2] second"


class TestFormatEvolutionHits:
    def test_empty_hits_reports_no_results_and_no_citations(self):
        assert _format_evolution_hits([]) == ("(no results)", [])

    def test_attaches_live_diff_link_only_to_headered_hits(self):
        # ask()'s _finalize_answer matches the final answer's own cited
        # revision pair against these citations rather than blindly trusting
        # rank [1] -- a real run cited a different (correct) pair than the
        # top-ranked evolution hit for that query, so the raw (header,
        # from_rev, to_rev, url) tuples have to be right, not just the text.
        hits = [
            "ietf-evolution draft:draft-ietf-example-thing from:05 to:06\nSection 3 changed wording.",
            "some unrelated plain hit with no header",
        ]

        text, citations = _format_evolution_hits(hits)

        expected_url = (
            "https://author-tools.ietf.org/iddiff"
            "?url1=draft-ietf-example-thing-05&url2=draft-ietf-example-thing-06&difftype=--html"
        )
        assert text == (
            "[1] ietf-evolution draft:draft-ietf-example-thing from:05 to:06\n"
            f"Section 3 changed wording.\nLive diff: {expected_url}\n\n"
            "[2] some unrelated plain hit with no header"
        )
        assert citations == [
            ("ietf-evolution draft:draft-ietf-example-thing from:05 to:06", "05", "06", expected_url)
        ]


class TestIddiffPageUrl:
    def test_builds_author_tools_url_from_adjacent_revisions(self):
        url = _iddiff_page_url("draft-ietf-example-thing", "05", "06")

        assert url == (
            "https://author-tools.ietf.org/iddiff"
            "?url1=draft-ietf-example-thing-05&url2=draft-ietf-example-thing-06&difftype=--html"
        )
