import contextlib
import json
from decimal import Decimal

from extensions.ext_database import db
from libs.datetime_utils import naive_utc_now
from models import Account, Tenant
from models.enums import ConversationFromSource
from models.model import App, AppMode, Conversation, Message
from services.conversation_service import ConversationService
from services.errors.conversation import ConversationNotExistsError

# Not a real `apps` row -- Conversation/Message.app_id has no DB-level FK
# constraint (confirmed in models/model.py), and ask_decision_agent.py's own
# module docstring explains why Prism can never be a normal Studio-configured
# app: Dify's real agent runner (core/agent/fc_agent_runner.py) has no
# tool_choice/forced-tool concept at all, which is exactly the reliability
# gap Prism's safety nets (forced mailarchive search, forced verify_answer,
# forced discovery) exist to close -- routing through that runner would
# silently reintroduce the fabricated-source bug this whole effort fixed.
# So this fixed id anchors every Prism Conversation/Message under one stable
# identity (for cost/token columns, real auth-scoped ownership, etc.)
# without pretending it's a real, Studio-visible App.
_PRISM_APP_ID = "8f21c3f0-6b0a-4b8e-9b2b-6f6a8f6e6c3a"
_PRISM_APP_NAME = "Prism"

# FIFO cap on turns fed back into a prompt as history -- mirrors the
# row-count half of Dify's own TokenBufferMemory; kept simple since Prism's
# turns run far longer than a normal chat message, so a token-precise budget
# would need a live model_instance this service doesn't have.
_HISTORY_TURN_LIMIT = 12


def _prism_app(tenant_id: str = "") -> App:
    """A transient (never persisted) App instance. ConversationService's
    methods only ever read .id/.name/.tenant_id off the app_model they're
    given -- never query the `apps` table through it -- so it doesn't need
    a real row; tenant_id only matters for the auto-naming LLM call.
    """
    return App(
        id=_PRISM_APP_ID,
        tenant_id=tenant_id,
        name=_PRISM_APP_NAME,
        mode=AppMode.AGENT_CHAT,
        enable_site=False,
        enable_api=False,
    )


class AgenticRagSessionService:
    """CRUD and history assembly for Prism's conversations -- backed by
    Dify's real Conversation/Message tables instead of a bespoke pair, so
    sessions inherit cost/token columns, real auth-scoped ownership, and
    whatever else already consumes Message rows. Prism's own agent loop
    (ask_decision_agent.ask()) is untouched; see _PRISM_APP_ID above for why
    this can't instead become a normal Studio-configured app.
    """

    @staticmethod
    def create_session(*, title_seed: str, account: Account) -> Conversation:
        first_line = title_seed.strip().splitlines()[0] if title_seed.strip() else "Untitled"
        conversation = Conversation(
            app_id=_PRISM_APP_ID,
            mode=AppMode.AGENT_CHAT.value,
            name=first_line[:200],
            inputs={},
            introduction="",
            system_instruction="",
            system_instruction_tokens=0,
            status="normal",
            from_source=ConversationFromSource.CONSOLE,
            from_end_user_id=None,
            from_account_id=account.id,
        )
        db.session.add(conversation)
        db.session.commit()
        return conversation

    @staticmethod
    def get_session(session_id: str, *, account: Account) -> Conversation | None:
        try:
            return ConversationService.get_conversation(_prism_app(), session_id, account)
        except ConversationNotExistsError:
            return None

    @staticmethod
    def list_sessions(*, account: Account, limit: int = 200) -> list[dict]:
        rows = (
            db.session.query(Conversation)
            .filter(
                Conversation.app_id == _PRISM_APP_ID,
                Conversation.from_source == ConversationFromSource.CONSOLE,
                Conversation.from_account_id == account.id,
                Conversation.is_deleted.is_(False),
            )
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .all()
        )
        return [{"id": row.id, "title": row.name, "updated_at": row.updated_at.isoformat()} for row in rows]

    @staticmethod
    def delete_session(session_id: str, *, account: Account) -> bool:
        try:
            ConversationService.delete(_prism_app(), session_id, account)
            return True
        except ConversationNotExistsError:
            return False

    @staticmethod
    def get_turns(session_id: str, *, account: Account) -> list[dict]:
        conversation = AgenticRagSessionService.get_session(session_id, account=account)
        if conversation is None:
            return []
        rows = (
            db.session.query(Message)
            .filter(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc())
            .all()
        )
        turns = []
        for row in rows:
            meta = json.loads(row.message_metadata) if row.message_metadata else {}
            turns.append(
                {
                    "tier": meta.get("tier", "ask"),
                    "query": row.query,
                    "answer": row.answer,
                    "trace": meta.get("trace"),
                    "created_at": row.created_at.isoformat(),
                }
            )
        return turns

    @staticmethod
    def get_history_for_prompt(session_id: str, *, account: Account, limit: int = _HISTORY_TURN_LIMIT) -> list[dict]:
        return AgenticRagSessionService.get_turns(session_id, account=account)[-limit:]

    @staticmethod
    def append_turn(
        *,
        session_id: str,
        account: Account,
        tier: str,
        query: str,
        answer: str,
        trace: list | None,
        usage: dict | None,
    ) -> None:
        usage = usage or {}
        message = Message(
            app_id=_PRISM_APP_ID,
            conversation_id=session_id,
            inputs={},
            query=query,
            message="",
            message_tokens=usage.get("prompt_tokens", 0),
            message_unit_price=0,
            message_price_unit=0,
            answer=answer,
            answer_tokens=usage.get("completion_tokens", 0),
            answer_unit_price=0,
            answer_price_unit=0,
            provider_response_latency=usage.get("latency", 0.0),
            total_price=usage.get("total_price", Decimal(0)),
            currency=usage.get("currency", "USD"),
            from_source=ConversationFromSource.CONSOLE,
            from_end_user_id=None,
            from_account_id=account.id,
            app_mode=AppMode.AGENT_CHAT,
            message_metadata=json.dumps({"tier": tier, "trace": trace}),
        )
        db.session.add(message)

        conversation = db.session.query(Conversation).filter(Conversation.id == session_id).first()
        is_first_turn = False
        if conversation is not None:
            conversation.updated_at = naive_utc_now()
            conversation.dialogue_count = (conversation.dialogue_count or 0) + 1
            is_first_turn = conversation.dialogue_count == 1
        db.session.commit()

        if is_first_turn and conversation is not None:
            # Reuses Dify's own conversation-naming LLM call -- the same one
            # a real chat app gets -- instead of hand-rolling a second title
            # generator now that this is a real Conversation row.
            tenant_id = db.session.query(Tenant).first().id
            with contextlib.suppress(Exception):
                ConversationService.auto_generate_name(_prism_app(tenant_id), conversation)
