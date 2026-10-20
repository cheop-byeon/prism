import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, DefaultFieldsMixin
from .types import StringUUID


class IetfRfcStructuralEdge(DefaultFieldsMixin, Base):
    """One obsoletes/updates relation between two published RFCs, corpus-
    wide -- the cheap tier of RFC-graph precomputation (see
    IetfImportService.get_global_structural_graph: ~1,543 obsoletes +
    ~2,157 updates relations total, fetched in two bulk Datatracker calls,
    ~2 minutes). Rebuilt wholesale on each refresh (see
    build_rfc_families.py --structural), not incrementally -- cheap enough
    that there's no resumability need the way citation harvesting has.
    """

    __tablename__ = "ietf_rfc_structural_edges"
    __table_args__ = (
        sa.UniqueConstraint("source", "target", "kind", name="ietf_rfc_structural_edges_source_target_kind_key"),
    )

    source: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # "obsoletes" | "updates"


class IetfRfcCitationFetchLog(DefaultFieldsMixin, Base):
    """Tracks which RFCs have already had find_related_rfc_family() run
    against them -- the expensive tier (per-RFC citation harvesting, real
    live Datatracker calls, ~9,930 RFCs corpus-wide). Exists purely for
    resumability: build_rfc_families.py --citations checks this table
    before each RFC, so an interrupted multi-hour run only ever redoes the
    one RFC it was mid-fetch on, never the whole corpus (same resumability
    reasoning as LocalSourceIndexService._build_embedding_index's
    checkpointing).
    """

    __tablename__ = "ietf_rfc_citation_fetch_log"
    __table_args__ = (sa.UniqueConstraint("rfc_number", name="ietf_rfc_citation_fetch_log_rfc_number_key"),)

    rfc_number: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # "ok" | "error"
    topic_keyword: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)


class IetfRfcCitationEdge(DefaultFieldsMixin, Base):
    """One citation-family relation found by find_related_rfc_family() --
    the real, if noisy, structural signal this project already uses for
    the per-RFC graph's "Citation Match" edges (RFC 9364's DNSSEC roadmap
    citing RFC 4033/4034/4035 is the motivating example throughout
    ietf_import_service.py). Deliberately NOT the whole-corpus BM25 "Topic
    Match" signal -- that stays live/per-request (cheap, local, no
    harvesting needed) and is excluded here on purpose: it's a weak
    "top-10 textually similar" signal that chains unrelated RFCs together
    transitively if used for clustering, so it's detail shown within a
    family, never what defines one.
    """

    __tablename__ = "ietf_rfc_citation_edges"
    __table_args__ = (sa.UniqueConstraint("source", "target", name="ietf_rfc_citation_edges_source_target_key"),)

    source: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(16), nullable=False)


class IetfRfcFamily(DefaultFieldsMixin, Base):
    """One connected component over (structural ∪ citation) edges --
    see build_rfc_families.py --cluster. `primary_wg` is a hint for the
    family's label (the most common Working Group among its members),
    never what defined its boundary -- a single topic routinely spans more
    than one WG over time (confirmed live: DNSSEC's own real family spans
    both dnsext and its successor dnsop), and WG is sometimes absent
    entirely (individual submissions, or RFCs older than Datatracker's own
    WG tracking). `label` defaults to `primary_wg` when there is one,
    falling back to the lowest-numbered member's own title otherwise (see
    build_rfc_families.py's _label_family).
    """

    __tablename__ = "ietf_rfc_families"

    label: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    primary_wg: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    member_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)


class IetfRfcFamilyMember(DefaultFieldsMixin, Base):
    """One RFC's membership in exactly one IetfRfcFamily, plus the node
    metadata (title/std_level/wg) the RFC Graph page needs to render it --
    stored here so a cache hit never needs a live Datatracker fetch at all,
    not even for display fields. `wg` is this RFC's OWN working group
    (the per-member hint IetfRfcFamily.primary_wg is aggregated from), kept
    here too since a family can legitimately mix WGs.
    """

    __tablename__ = "ietf_rfc_family_members"
    __table_args__ = (sa.UniqueConstraint("rfc_number", name="ietf_rfc_family_members_rfc_number_key"),)

    family_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    rfc_number: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    std_level: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="")
    wg: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
