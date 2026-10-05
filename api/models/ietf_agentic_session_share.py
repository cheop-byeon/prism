import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, DefaultFieldsMixin
from .types import StringUUID


class IetfAgenticSessionShare(DefaultFieldsMixin, Base):
    """Sharing flag for one Prism investigation session (a Dify Conversation
    row under AgenticRagSessionService's fixed _PRISM_APP_ID). A separate
    side table rather than a new Conversation column -- Dify's Conversation
    model is core schema this project otherwise doesn't touch -- mirroring
    IetfLocalSource's existing owner/shared pattern for local sources.

    No owner_account_id column here: Conversation.from_account_id is
    already the real owner, and every write path re-derives/checks
    ownership through AgenticRagSessionService.get_session (Dify's own
    ownership-filtered lookup) before ever touching this table, so there's
    nothing to duplicate.

    A conversation with no row here is private -- the default, and
    deliberately so for every session that existed before this feature
    shipped (see this table's own migration: no backfill, unlike
    ietf_local_sources' legacy-registry backfill, since there's no prior
    "everything was public" state for sessions to preserve).
    """

    __tablename__ = "ietf_agentic_session_shares"
    __table_args__ = (sa.UniqueConstraint("conversation_id", name="ietf_agentic_session_shares_conversation_id_key"),)

    conversation_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    shared: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
