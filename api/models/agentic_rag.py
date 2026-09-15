import enum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, DefaultFieldsMixin
from .types import AdjustedJSON, EnumText, StringUUID


class AgenticRagTier(enum.StrEnum):
    """Which Research mode produced a turn -- Ask/Investigate are baselines,
    Deep Dive/Panorama are the tiers actually used day-to-day.
    """

    ASK = "ask"
    INVESTIGATE = "investigate"
    DEEPDIVE = "deepdive"
    PANORAMA = "panorama"


class AgenticRagSession(DefaultFieldsMixin, Base):
    """A saved conversation thread for the Prism agentic RAG tool
    (api/scripts/agentic_rag/). Deliberately not part of Dify's App/
    Conversation domain: this tool has no App/EndUser/tenant scoping, so
    sessions are a flat, unauthenticated list rather than per-user.
    """

    __tablename__ = "agentic_rag_sessions"
    __table_args__ = (sa.Index("agentic_rag_sessions_updated_at_idx", "updated_at"),)

    title: Mapped[str] = mapped_column(sa.String(200), nullable=False, default="")

    messages: Mapped[list["AgenticRagMessage"]] = relationship(
        "AgenticRagMessage",
        primaryjoin="AgenticRagSession.id == foreign(AgenticRagMessage.session_id)",
        uselist=True,
        back_populates="session",
        order_by="AgenticRagMessage.created_at",
        lazy="raise",
    )


class AgenticRagMessage(DefaultFieldsMixin, Base):
    """One turn (query + answer) within an AgenticRagSession. `trace` mirrors
    the tool-call trace already returned by ask_decision_agent.ask(), stored
    so a reopened session can re-render tool-call steps, not just prose.
    """

    __tablename__ = "agentic_rag_messages"
    __table_args__ = (sa.Index("agentic_rag_messages_session_id_idx", "session_id"),)

    session_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    tier: Mapped[AgenticRagTier] = mapped_column(EnumText(AgenticRagTier), nullable=False)
    query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    answer: Mapped[str] = mapped_column(sa.Text, nullable=False)
    trace: Mapped[list | None] = mapped_column(AdjustedJSON, nullable=True)

    session: Mapped[AgenticRagSession] = relationship(
        "AgenticRagSession",
        uselist=False,
        foreign_keys=[session_id],
        primaryjoin="AgenticRagMessage.session_id == AgenticRagSession.id",
        back_populates="messages",
        lazy="raise",
    )
