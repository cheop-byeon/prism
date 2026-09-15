from typing import TypedDict

from extensions.ext_database import db
from libs.datetime_utils import naive_utc_now
from models.agentic_rag import AgenticRagMessage, AgenticRagSession, AgenticRagTier

_TITLE_MAX_LEN = 80
# FIFO cap on turns fed back into a prompt as history -- mirrors the row-count
# half of Dify's own TokenBufferMemory; kept simple since Prism's turns run
# far longer than a normal chat message, so a token-precise budget would need
# a live model_instance this service doesn't have.
_HISTORY_TURN_LIMIT = 12


class AgenticRagTurn(TypedDict):
    tier: str
    query: str
    answer: str
    trace: list | None
    created_at: str


class AgenticRagSessionSummary(TypedDict):
    id: str
    title: str
    updated_at: str


class AgenticRagSessionService:
    """CRUD and history assembly for Prism's persisted conversation sessions.

    Deliberately separate from Dify's Conversation/Message + TokenBufferMemory
    machinery: this tool (api/scripts/agentic_rag/) has no App/EndUser/tenant
    scoping to hang those off of, so it keeps its own flat, unauthenticated
    session list instead.
    """

    @staticmethod
    def create_session(*, title_seed: str) -> AgenticRagSession:
        first_line = title_seed.strip().splitlines()[0] if title_seed.strip() else "Untitled"
        session = AgenticRagSession(title=first_line[:_TITLE_MAX_LEN])
        db.session.add(session)
        db.session.commit()
        return session

    @staticmethod
    def get_session(session_id: str) -> AgenticRagSession | None:
        return db.session.query(AgenticRagSession).filter(AgenticRagSession.id == session_id).first()

    @staticmethod
    def list_sessions(limit: int = 200) -> list[AgenticRagSessionSummary]:
        rows = db.session.query(AgenticRagSession).order_by(AgenticRagSession.updated_at.desc()).limit(limit).all()
        return [{"id": row.id, "title": row.title, "updated_at": row.updated_at.isoformat()} for row in rows]

    @staticmethod
    def delete_session(session_id: str) -> bool:
        session = AgenticRagSessionService.get_session(session_id)
        if session is None:
            return False
        db.session.query(AgenticRagMessage).filter(AgenticRagMessage.session_id == session_id).delete()
        db.session.delete(session)
        db.session.commit()
        return True

    @staticmethod
    def get_turns(session_id: str) -> list[AgenticRagTurn]:
        rows = (
            db.session.query(AgenticRagMessage)
            .filter(AgenticRagMessage.session_id == session_id)
            .order_by(AgenticRagMessage.created_at.asc())
            .all()
        )
        return [
            {
                "tier": row.tier.value,
                "query": row.query,
                "answer": row.answer,
                "trace": row.trace,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    @staticmethod
    def get_history_for_prompt(session_id: str, *, limit: int = _HISTORY_TURN_LIMIT) -> list[AgenticRagTurn]:
        return AgenticRagSessionService.get_turns(session_id)[-limit:]

    @staticmethod
    def append_turn(
        *,
        session_id: str,
        tier: AgenticRagTier,
        query: str,
        answer: str,
        trace: list | None,
    ) -> None:
        db.session.add(AgenticRagMessage(session_id=session_id, tier=tier, query=query, answer=answer, trace=trace))
        session = AgenticRagSessionService.get_session(session_id)
        if session is not None:
            # No column on `session` itself changes when a child message is
            # added, so onupdate=current_timestamp never fires on its own --
            # touch updated_at explicitly to keep sidebar recency ordering correct.
            session.updated_at = naive_utc_now()
        db.session.commit()
