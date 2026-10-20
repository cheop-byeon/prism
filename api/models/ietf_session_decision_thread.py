import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, DefaultFieldsMixin
from .types import StringUUID


class IetfSessionDecisionThread(DefaultFieldsMixin, Base):
    """The most recent "Track phrase" (Decision history / Within-a-document)
    result for one investigation session -- one row per Conversation,
    overwritten on every new Track phrase run. decision_thread() has no
    append_turn()-style persistence of its own (unlike the main Ask flow),
    so without this, reopening a past session always showed Decision
    history as empty, even though Track phrase can cost real, multi-lane
    LLM calls to produce (see _judge_lane_discussion in
    ask_decision_agent.py).

    Deliberately one slot per session, not a history of every phrase ever
    tracked in it -- matches the UI, which only ever shows one tracked
    result on screen at a time (a new Track phrase run replaces the last
    one, it doesn't append to it).
    """

    __tablename__ = "ietf_session_decision_threads"
    __table_args__ = (
        sa.UniqueConstraint("conversation_id", name="ietf_session_decision_threads_conversation_id_key"),
    )

    conversation_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    draft_hint: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_provider: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    # JSON-serialized decision_thread() result -- {draft_name, rfc_number,
    # query, lanes}, the exact shape renderThread() already expects, so
    # restoring it on reopen is just renderThread(json.loads(row.data)).
    data: Mapped[str] = mapped_column(sa.Text, nullable=False)
