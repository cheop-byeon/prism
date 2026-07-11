"""Unit tests for draft-evolution diff formats and line-span metadata."""

from services.ietf_import_service import IetfImportService

_OLD_TEXT = """draft-ietf-example-00

Table of Contents

1.  Introduction  . . . . . . . . . . . . . . . . . . . . . . . .  2
2.  Security Considerations  . . . . . . . . . . . . . . . . . .  3

1.  Introduction

   The old introduction paragraph about retry behavior.

2.  Security Considerations

   Unchanged security text.
"""

_NEW_TEXT = """draft-ietf-example-01

Table of Contents

1.  Introduction  . . . . . . . . . . . . . . . . . . . . . . . .  2
2.  Security Considerations  . . . . . . . . . . . . . . . . . .  3
3.  IANA Considerations . . . . . . . . . . . . . . . . . . . . .  4

1.  Introduction

   The new introduction paragraph about retry behavior.

2.  Security Considerations

   Unchanged security text.

3.  IANA Considerations

   Fresh registry text for the new section.
"""


def _diff(diff_format: str) -> list[dict]:
    old_sections = IetfImportService._parse_sections(_OLD_TEXT)
    new_sections = IetfImportService._parse_sections(_NEW_TEXT)
    return IetfImportService._diff_version_pair(
        old_sections,
        new_sections,
        "00",
        "01",
        "draft-ietf-example",
        diff_format=diff_format,  # type: ignore[arg-type]
        old_spans=IetfImportService._section_line_spans(_OLD_TEXT),
        new_spans=IetfImportService._section_line_spans(_NEW_TEXT),
    )


class TestSectionLineSpans:
    def test_real_headings_win_over_toc_entries(self):
        spans = IetfImportService._section_line_spans(_NEW_TEXT)

        # Section 1's real heading is on line 9 of _NEW_TEXT (1-based),
        # not the ToC entry on line 5.
        assert spans["1"][0] == 9
        assert spans["2"][0] == 13
        # Last section runs to end of file.
        assert spans["3"][1] == len(_NEW_TEXT.splitlines())

    def test_span_ends_before_next_heading(self):
        spans = IetfImportService._section_line_spans(_NEW_TEXT)

        assert spans["1"] == (9, 12)


class TestDiffFormats:
    def test_prose_records_carry_line_spans(self):
        records = _diff("prose")
        modified = next(r for r in records if r["change_type"] == "modified")
        added = next(r for r in records if r["change_type"] == "added")

        assert modified["section_id"] == "1"
        assert modified["from_lines"] == "8-11"
        assert modified["to_lines"] == "9-12"
        assert "Before (rev 00):" in modified["content"]
        # Added section exists only in the new revision.
        assert added["section_id"] == "3"
        assert "from_lines" not in added
        assert added["to_lines"] == "17-19"

    def test_patch_format_uses_unified_diff_for_modified_sections(self):
        records = _diff("patch")
        modified = next(r for r in records if r["change_type"] == "modified")

        assert "--- rev 00" in modified["content"]
        assert "+++ rev 01" in modified["content"]
        assert "@@" in modified["content"]
        assert "-The old introduction paragraph" in modified["content"]
        assert "+The new introduction paragraph" in modified["content"]
        # Metadata header line stays parseable regardless of format.
        assert modified["content"].startswith("ietf-evolution draft:draft-ietf-example")

    def test_patch_format_keeps_full_text_for_added_sections(self):
        records = _diff("patch")
        added = next(r for r in records if r["change_type"] == "added")

        assert "New section added in rev 01:" in added["content"]
        assert "Fresh registry text" in added["content"]

    def test_line_spans_omitted_when_spans_not_provided(self):
        old_sections = IetfImportService._parse_sections(_OLD_TEXT)
        new_sections = IetfImportService._parse_sections(_NEW_TEXT)

        records = IetfImportService._diff_version_pair(
            old_sections, new_sections, "00", "01", "draft-ietf-example"
        )

        assert all("from_lines" not in r and "to_lines" not in r for r in records)


_LONG_OLD_BODY = (
    "   The endpoint processes the request according to the behaviour described\n"
    "   in the previous section and records the outcome in its local state\n"
    "   table before sending the response back to the peer over the existing\n"
    "   connection so that ordering guarantees are preserved end to end.\n"
)


def _pair_with_body(old_body: str, new_body: str) -> list[dict]:
    old = {"1": ("Processing", old_body)}
    new = {"1": ("Processing", new_body)}
    return IetfImportService._diff_version_pair(
        old, new, "00", "01", "draft-ietf-example", skip_minor_edits=True
    )


class TestMinorEditFiltering:
    def test_spelling_fix_is_skipped_when_flag_on(self):
        new_body = _LONG_OLD_BODY.replace("behaviour", "behavior")

        assert _pair_with_body(_LONG_OLD_BODY, new_body) == []

    def test_normative_keyword_change_is_never_minor(self):
        # MAY -> MUST is a one-word edit with major protocol impact.
        old_body = _LONG_OLD_BODY.replace("processes", "MAY process")
        new_body = _LONG_OLD_BODY.replace("processes", "MUST process")

        records = _pair_with_body(old_body, new_body)

        assert len(records) == 1

    def test_numeric_change_is_never_minor(self):
        old_body = _LONG_OLD_BODY.replace("existing", "existing 32-octet")
        new_body = _LONG_OLD_BODY.replace("existing", "existing 64-octet")

        records = _pair_with_body(old_body, new_body)

        assert len(records) == 1

    def test_flag_off_keeps_minor_edits_with_similarity_metadata(self):
        new_body = _LONG_OLD_BODY.replace("behaviour", "behavior")
        records = IetfImportService._diff_version_pair(
            {"1": ("Processing", _LONG_OLD_BODY)},
            {"1": ("Processing", new_body)},
            "00",
            "01",
            "draft-ietf-example",
        )

        assert len(records) == 1
        assert float(records[0]["similarity"]) > 0.95


class TestSkipSections:
    def test_draft_meta_sections_are_excluded(self):
        for title in ("Changes since draft-ietf-example-04", "Document History", "Open Issues"):
            old = {"9": (title, "old text about edits")}
            new = {"9": (title, "new text about edits and more")}

            assert IetfImportService._diff_version_pair(old, new, "00", "01", "d") == []

    def test_substantive_sections_are_kept(self):
        old = {"5": ("IANA Considerations", "old registry policy text here")}
        new = {"5": ("IANA Considerations", "new registry policy text here indeed")}

        assert len(IetfImportService._diff_version_pair(old, new, "00", "01", "d")) == 1


class TestPageFurnitureStripping:
    def test_footer_and_header_are_stripped_from_section_bodies(self):
        text = (
            "1.  Introduction\n\n"
            "   First paragraph before the page break.\n\n"
            "Shalunov               Expires September 23, 2010               [Page 3]\n"
            "\x0c\n"
            "Internet-Draft              LEDBAT                            March 2010\n\n"
            "   Second paragraph after the page break.\n"
        )

        sections = IetfImportService._parse_sections(text)

        body = sections["1"][1]
        assert "First paragraph" in body
        assert "Second paragraph" in body  # body line after the break survives
        assert "[Page" not in body
        assert "Expires September" not in body
        assert "Internet-Draft" not in body

    def test_stray_footer_without_formfeed_is_stripped(self):
        text = (
            "1.  Introduction\n\n"
            "   Some body text here.\n"
            "Shalunov               Expires September 23, 2010               [Page 7]\n"
            "   More body text here.\n"
        )

        sections = IetfImportService._parse_sections(text)

        body = sections["1"][1]
        assert "Some body text" in body
        assert "More body text" in body
        assert "[Page" not in body

