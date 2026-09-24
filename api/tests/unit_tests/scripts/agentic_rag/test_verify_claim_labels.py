"""Unit tests for _parse_claim_labels -- extracts verify_answer's own
"CLAIM [N]: stated|inferred|unresolved" lines (see verify_skill.md) into
{citation_number: status}, the data the Evidence panel's per-citation pill
is built from.
"""

from scripts.agentic_rag.ask_decision_agent import _parse_claim_labels


class TestParseClaimLabels:
    def test_no_claim_lines_returns_empty_dict(self):
        response = "Everything checks out.\n\nVERDICT: no issues found"

        assert _parse_claim_labels(response) == {}

    def test_parses_well_formed_lines_in_any_order(self):
        response = (
            "Some critique text here.\n"
            "CLAIM [3]: stated\n"
            "CLAIM [1]: inferred\n"
            "CLAIM [2]: unresolved\n"
            "VERDICT: no issues found"
        )

        assert _parse_claim_labels(response) == {3: "stated", 1: "inferred", 2: "unresolved"}

    def test_case_insensitive_status(self):
        response = "CLAIM [1]: STATED\nCLAIM [2]: Inferred"

        assert _parse_claim_labels(response) == {1: "stated", 2: "inferred"}

    def test_mixed_with_surrounding_critique_prose(self):
        response = (
            '- "INIT_CWND was set to 4" [4]: found in the evolution diff, exact match.\n'
            "  No issues here.\n\n"
            "CLAIM [4]: stated\n"
            "CLAIM [7]: unresolved -- no evidence found for this claim.\n"
            "VERDICT: issues found"
        )

        # The trailing "-- no evidence found..." after [7]'s status breaks
        # the exact-match pattern (verify_skill.md asks for the status
        # alone on its own line) -- confirms a malformed line is simply
        # skipped, not partially parsed into a wrong value.
        assert _parse_claim_labels(response) == {4: "stated"}

    def test_line_missing_brackets_is_ignored(self):
        response = "CLAIM 3: stated\nCLAIM [4]: stated"

        assert _parse_claim_labels(response) == {4: "stated"}

    def test_unrecognized_status_word_is_ignored(self):
        response = "CLAIM [1]: maybe\nCLAIM [2]: stated"

        assert _parse_claim_labels(response) == {2: "stated"}

    def test_duplicate_claim_number_keeps_the_last_occurrence(self):
        # A model revising its own draft mid-response and restating a CLAIM
        # line shouldn't leave two conflicting entries -- last write wins,
        # same as claim_labels.update() at the ask() call site does across
        # multiple verify_answer calls in one turn.
        response = "CLAIM [1]: unresolved\nCLAIM [1]: stated"

        assert _parse_claim_labels(response) == {1: "stated"}
