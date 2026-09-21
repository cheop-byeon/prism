from types import SimpleNamespace

import pytest

from scripts.agentic_rag.ask_decision_agent import (
    _TOOL_DISPLAY_NAMES,
    _build_tools,
    _extract_message_links,
    _filter_primary_source_text,
    _format_evolution_hits,
    _format_hits,
    _iddiff_page_url,
    _parse_evolution_body,
    _resolve_draft_hint,
    _summarize_usage,
    tool_display_name,
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

    def test_start_offsets_numbering_for_a_later_search_call(self):
        # ask() passes start=len(numbered_evidence)+1 so a second search
        # call's hits keep counting up from an earlier one instead of both
        # restarting at [1] -- the model would otherwise write "[1]" in its
        # final answer meaning two different, ambiguous things.
        assert _format_hits(["third", "fourth"], start=3) == "[3] third\n\n[4] fourth"


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

    def test_start_offsets_numbering_for_a_later_search_call(self):
        text, _citations = _format_evolution_hits(["plain hit, no header"], start=5)
        assert text == "[5] plain hit, no header"


class TestIddiffPageUrl:
    def test_builds_author_tools_url_from_adjacent_revisions(self):
        url = _iddiff_page_url("draft-ietf-example-thing", "05", "06")

        assert url == (
            "https://author-tools.ietf.org/iddiff"
            "?url1=draft-ietf-example-thing-05&url2=draft-ietf-example-thing-06&difftype=--html"
        )


class TestResolveDraftHint:
    _catalog = [
        {"draft_name": "draft-ietf-quic-transport", "wg": "quic", "rfc_number": "9000"},
        {"draft_name": "draft-ietf-ledbat-congestion", "wg": "ledbat", "rfc_number": None},
    ]

    def test_matches_exact_draft_name(self):
        result = _resolve_draft_hint("draft-ietf-quic-transport", self._catalog)

        assert result is self._catalog[0]

    def test_matches_wg_case_insensitively(self):
        result = _resolve_draft_hint("QUIC", self._catalog)

        assert result is self._catalog[0]

    def test_matches_bare_rfc_number(self):
        assert _resolve_draft_hint("9000", self._catalog) is self._catalog[0]

    def test_matches_rfc_number_with_prefix_and_whitespace(self):
        # needle_rfc strips a leading "rfc" (plus any following whitespace)
        # before comparing against the catalog's bare number -- "RFC 9000"
        # and "rfc9000" must both resolve the same as "9000" alone.
        assert _resolve_draft_hint("RFC 9000", self._catalog) is self._catalog[0]
        assert _resolve_draft_hint("rfc9000", self._catalog) is self._catalog[0]

    def test_no_match_returns_none(self):
        assert _resolve_draft_hint("nonexistent", self._catalog) is None

    def test_ambiguous_match_returns_none_rather_than_guessing(self):
        # Two entries sharing the same WG acronym -- resolving to either
        # one silently would risk showing the wrong draft's history.
        ambiguous_catalog = [
            {"draft_name": "draft-a", "wg": "quic", "rfc_number": None},
            {"draft_name": "draft-b", "wg": "quic", "rfc_number": None},
        ]

        assert _resolve_draft_hint("quic", ambiguous_catalog) is None


class TestFilterPrimarySourceText:
    _text = "Para one about INIT_CWND.\n\nPara two about congestion.\n\nAnother INIT_CWND paragraph here."

    def test_no_query_returns_full_text_under_cap(self):
        result = _filter_primary_source_text(self._text, None, "draft-x-01", max_chars=6000)

        assert result == f"[draft-x-01, full text]\n\n{self._text}"

    def test_no_query_truncates_over_cap(self):
        result = _filter_primary_source_text("a" * 20, None, "draft-x-01", max_chars=10)

        assert result == (
            "[draft-x-01, showing first 10 of 20 chars -- "
            "pass a `query` to search for a specific phrase instead]\n\n" + "a" * 10
        )

    def test_query_matches_only_containing_paragraphs_case_insensitively(self):
        result = _filter_primary_source_text(self._text, "init_cwnd", "draft-x-01")

        assert result == (
            "[draft-x-01, 2 paragraph(s) matching 'init_cwnd']\n\n"
            "Para one about INIT_CWND.\n\n---\n\nAnother INIT_CWND paragraph here."
        )

    def test_query_no_match_says_so_explicitly(self):
        result = _filter_primary_source_text(self._text, "nomatch", "draft-x-01")

        assert result == "[draft-x-01] No paragraph containing 'nomatch' was found in this document."

    def test_query_match_truncates_over_cap(self):
        result = _filter_primary_source_text("X" * 20 + " INIT_CWND", "INIT_CWND", "draft-x-01", max_chars=10)

        assert result == "[draft-x-01, 1 paragraph(s) matching 'INIT_CWND']\n\n" + "X" * 10 + "\n\n[truncated]"


class TestBuildTools:
    """Guards against exactly the kind of drift the tool-name constants
    exist to prevent (see EVOLUTION_TOOL_NAME et al.'s own comment) --
    _build_tools() and _TOOL_DISPLAY_NAMES are two separate hand-maintained
    lists of the same tool names, so nothing stops them silently diverging
    when a tool is added, renamed, or removed in only one of them.
    """

    def test_every_tool_has_a_display_name_and_vice_versa(self):
        tool_names = {t.name for t in _build_tools()}

        assert tool_names == set(_TOOL_DISPLAY_NAMES.keys())

    def test_no_duplicate_tool_names(self):
        tools = _build_tools()

        assert len({t.name for t in tools}) == len(tools)

    def test_every_tool_has_a_non_empty_description(self):
        for t in _build_tools():
            assert t.description, f"{t.name} has no description"

    def test_required_parameters_are_a_subset_of_declared_properties(self):
        for t in _build_tools():
            properties = set(t.parameters.get("properties", {}).keys())
            required = set(t.parameters.get("required", []))
            assert required <= properties, f"{t.name} requires undeclared parameter(s)"


class TestToolDisplayName:
    def test_known_tool_returns_its_display_name(self):
        assert tool_display_name("search_ietf_evolution_dataset") == "Search decisions"

    def test_unknown_tool_falls_back_to_the_raw_name(self):
        assert tool_display_name("totally_unknown_tool") == "totally_unknown_tool"
