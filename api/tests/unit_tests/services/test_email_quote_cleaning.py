"""Unit tests for services.email_quote_cleaning.clean_email_body."""

from services.email_quote_cleaning import clean_email_body


class TestNoneMode:
    def test_returns_body_unchanged(self):
        body = "Hello\n> quoted line\n-- \nsig"

        result = clean_email_body(body, mode="none")

        assert result == body


class TestNaiveMode:
    def test_removes_quote_prefixed_lines(self):
        body = (
            "I disagree with this.\n"
            "> The server MUST close the connection.\n"
            "> This is required for safety.\n"
            "See section 4 for details."
        )

        result = clean_email_body(body, mode="naive")

        assert "MUST close" not in result
        assert "I disagree with this." in result
        assert "See section 4 for details." in result

    def test_removes_nested_quote_lines(self):
        body = "New text.\n>> deep quote\n> > spaced quote\nMore new text."

        result = clean_email_body(body, mode="naive")

        assert "deep quote" not in result
        assert "spaced quote" not in result
        assert "New text." in result
        assert "More new text." in result

    def test_falls_back_to_original_when_everything_is_quoted(self):
        body = "> line one\n> line two\n> line three of quoted material here"

        result = clean_email_body(body, mode="naive")

        assert result == body.strip()


class TestAdvancedMode:
    def test_drops_trailing_full_quote_with_attribution(self):
        body = (
            "I support this change, the security considerations are addressed "
            "by the mechanism described in section 7 of the draft document.\n"
            "\n"
            "On Mon, 3 Mar 2021 at 14:22, Martin Thomson wrote:\n"
            "> If the client sends 0-RTT data and the server rejects it,\n"
            "> the client MUST NOT assume anything about server state.\n"
            "> This also applies to retried packets.\n"
        )

        result = clean_email_body(body, mode="advanced")

        assert "I support this change" in result
        assert "MUST NOT assume" not in result
        assert "wrote:" not in result

    def test_compresses_inline_quote_to_stub(self):
        body = (
            "On Tue, 4 May 2021, Jana Iyengar wrote:\n"
            "> The congestion window should not grow during the\n"
            "> validation period because the path characteristics\n"
            "> are not yet confirmed by the peer endpoint.\n"
            "> We saw this in the interop testing last month.\n"
            "> It caused significant retransmission storms.\n"
            "\n"
            "That's too strict in my opinion. We only need this restriction "
            "for the case where transport parameters changed between sessions.\n"
        )

        result = clean_email_body(body, mode="advanced")

        assert "That's too strict in my opinion" in result
        # Stub keeps the beginning of the quote as context
        assert "congestion window" in result
        # But the bulk is elided
        assert "retransmission storms" not in result
        assert "[+3 more quoted lines]" in result
        # Attribution collapsed into a short marker
        assert "[quoting Jana Iyengar]" in result
        assert "On Tue, 4 May 2021" not in result

    def test_strips_rfc3676_signature(self):
        body = (
            "The new text looks good to me, please merge it as proposed "
            "and update the changelog section accordingly.\n"
            "\n"
            "-- \n"
            "John Doe\n"
            "Example Networks, Inc.\n"
            "+1 555 0100\n"
        )

        result = clean_email_body(body, mode="advanced")

        assert "looks good to me" in result
        assert "Example Networks" not in result
        assert "John Doe" not in result

    def test_strips_mailman_list_footer(self):
        body = (
            "Please review the updated pull request before Thursday's "
            "interim meeting so we can close this issue out properly.\n"
            "\n"
            "_______________________________________________\n"
            "QUIC mailing list\n"
            "quic@ietf.org\n"
            "https://www.ietf.org/mailman/listinfo/quic\n"
        )

        result = clean_email_body(body, mode="advanced")

        assert "review the updated pull request" in result
        assert "mailman" not in result
        assert "QUIC mailing list" not in result

    def test_strips_pgp_signature_block(self):
        body = (
            "Confirmed, the test vectors in appendix A.2 match my "
            "implementation output after the byte-order fix was applied.\n"
            "\n"
            "-----BEGIN PGP SIGNATURE-----\n"
            "iQIzBAEBCAAdFiEE\n"
            "aBcDeFgHiJkLmNoP\n"
            "-----END PGP SIGNATURE-----\n"
        )

        result = clean_email_body(body, mode="advanced")

        assert "test vectors in appendix A.2" in result
        assert "PGP SIGNATURE" not in result
        assert "iQIzBAEBCAAdFiEE" not in result

    def test_handles_attribution_wrapped_across_two_lines(self):
        body = (
            "Agreed on both points, this matches what we discussed during "
            "the hallway conversation at the last IETF meeting in-person.\n"
            "\n"
            "On Wed, 5 Jan 2022 at 09:15, Lucas Pardue\n"
            "<lucaspardue@example.com> wrote:\n"
            "> I think the header should be renamed before we go to\n"
            "> last call, otherwise we lock in the confusing name.\n"
        )

        result = clean_email_body(body, mode="advanced")

        assert "Agreed on both points" in result
        assert "header should be renamed" not in result
        assert "wrote:" not in result

    def test_falls_back_to_original_when_cleaning_removes_almost_everything(self):
        body = (
            "+1\n"
            "\n"
            "On Thu, 6 Oct 2022, Mark Nottingham wrote:\n"
            "> The proposal is to adopt the draft as a working group item\n"
            "> with the changes discussed in the issue tracker thread.\n"
        )

        result = clean_email_body(body, mode="advanced")

        # "+1" alone is below the safety threshold; keep the original so the
        # chunk still carries the context of what was endorsed.
        assert result == body.strip()

    def test_keeps_plain_message_untouched(self):
        body = (
            "Section 5.3 says the value -- when present -- must be greater\n"
            "than zero. The range 0 > x is therefore invalid input here.\n"
            "\n"
            "A horizontal rule follows:\n"
            "----\n"
            "And this text is still part of the message body."
        )

        result = clean_email_body(body, mode="advanced")

        assert result == body.strip()

    def test_drops_original_message_marker_block(self):
        body = (
            "Forwarding the relevant part of the earlier discussion for "
            "context, see my comments at the bottom of this message body.\n"
            "\n"
            "-----Original Message-----\n"
            "From: someone@example.com\n"
            "Subject: Re: draft adoption\n"
            "The original text of the forwarded message goes here.\n"
        )

        result = clean_email_body(body, mode="advanced")

        assert "Forwarding the relevant part" in result
        assert "Original Message" not in result
        assert "forwarded message goes here" not in result
