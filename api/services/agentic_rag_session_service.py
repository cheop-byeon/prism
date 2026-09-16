import contextlib
import json
from decimal import Decimal

from sqlalchemy import or_

from core.app.entities.app_invoke_entities import InvokeFrom
from extensions.ext_database import db
from libs.datetime_utils import naive_utc_now
from libs.infinite_scroll_pagination import InfiniteScrollPagination
from models import Account, Tenant
from models.enums import ConversationFromSource, CreatorUserRole, FeedbackRating
from models.model import App, AppMode, Conversation, Message, MessageAgentThought, MessageFeedback
from services.conversation_service import ConversationService
from services.errors.conversation import ConversationNotExistsError
from services.message_service import MessageService

# Prism has no real per-invocation surface of its own (WEB_APP/SERVICE_API
# don't apply -- it's neither Dify's web app nor an API caller); DEBUGGER is
# the closest fit for "an internal console tool", and since every Prism
# Conversation leaves invoke_from unset (None), pagination_by_last_id's own
# `Conversation.invoke_from.is_(None) OR == invoke_from` filter matches
# regardless of which value is passed here -- this is mostly documentation.
_INVOKE_FROM = InvokeFrom.DEBUGGER

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
    def list_sessions(*, account: Account, last_id: str | None = None, limit: int = 30) -> dict:
        """Real cursor pagination via ConversationService.pagination_by_last_id
        (the same method a normal Dify app's conversation list uses) instead
        of a flat limit=200 query that silently dropped anything older.
        """
        pagination: InfiniteScrollPagination = ConversationService.pagination_by_last_id(
            session=db.session,
            app_model=_prism_app(),
            user=account,
            last_id=last_id,
            limit=limit,
            invoke_from=_INVOKE_FROM,
        )
        return {
            "sessions": [
                {
                    "id": row.id,
                    "title": row.name,
                    "updated_at": row.updated_at.isoformat(),
                    "model_provider": row.model_provider,
                    "model_id": row.model_id,
                }
                for row in pagination.data
            ],
            "has_more": pagination.has_more,
        }

    @staticmethod
    def search_sessions(*, account: Account, query: str, limit: int = 30) -> list[dict]:
        """Searches session titles AND the actual query/answer text of every
        turn within them -- not just titles, since the point of searching
        past sessions is finding a conversation by what was actually
        discussed (a phrase from an answer, a draft name mentioned mid-turn),
        not just whatever auto-generated title it happened to get. Scoped to
        this account's own sessions only, same as list_sessions. Not cursor-
        paginated (unlike list_sessions) -- a capped, most-recent-first list
        is the right shape for a search result, not something you page
        through indefinitely.
        """
        term = query.strip()
        if not term:
            return []
        like_term = f"%{term}%"

        # Match on conversation id only (not the full Conversation.distinct(),
        # which Postgres rejects -- it has no equality operator for the
        # json-typed inputs/override_model_configs columns), then fetch the
        # full rows in a second pass.
        matching_ids = (
            db.session.query(Conversation.id)
            .outerjoin(Message, Message.conversation_id == Conversation.id)
            .filter(
                Conversation.app_id == _PRISM_APP_ID,
                Conversation.from_source == ConversationFromSource.CONSOLE,
                Conversation.from_account_id == account.id,
                Conversation.is_deleted.is_(False),
                or_(
                    Conversation.name.ilike(like_term),
                    Message.query.ilike(like_term),
                    Message.answer.ilike(like_term),
                ),
            )
            .distinct()
        )
        rows = (
            db.session.query(Conversation)
            .filter(Conversation.id.in_(matching_ids))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": row.id,
                "title": row.name,
                "updated_at": row.updated_at.isoformat(),
                "model_provider": row.model_provider,
                "model_id": row.model_id,
            }
            for row in rows
        ]

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
            # Not Message.admin_feedback -- that property matches on
            # from_source alone (at most one "admin" in a normal Dify app),
            # which would show one Prism user another's feedback on a
            # shared message. Scoped to this account explicitly instead.
            feedback_row = (
                db.session.query(MessageFeedback)
                .filter(MessageFeedback.message_id == row.id, MessageFeedback.from_account_id == account.id)
                .first()
            )
            thought_rows = (
                db.session.query(MessageAgentThought)
                .filter(MessageAgentThought.message_id == row.id)
                .order_by(MessageAgentThought.position.asc())
                .all()
            )
            trace = [
                {
                    "tool": thought.tool,
                    "args": json.loads(thought.tool_input) if thought.tool_input else {},
                    "iteration": thought.position,
                    "forced": json.loads(thought.tool_meta_str or "{}").get("forced", False),
                    "observation_preview": thought.observation,
                }
                for thought in thought_rows
            ]
            turns.append(
                {
                    "message_id": row.id,
                    "tier": meta.get("tier", "ask"),
                    "query": row.query,
                    "answer": row.answer,
                    "trace": trace,
                    "created_at": row.created_at.isoformat(),
                    "feedback": feedback_row.rating.value if feedback_row else None,
                    "usage": {
                        "prompt_tokens": row.message_tokens,
                        "completion_tokens": row.answer_tokens,
                        "total_tokens": row.message_tokens + row.answer_tokens,
                        "total_price": float(row.total_price) if row.total_price is not None else 0.0,
                        "currency": row.currency,
                        "latency": row.provider_response_latency,
                    },
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
        model_provider: str | None = None,
        model_name: str | None = None,
    ) -> str:
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
            message_metadata=json.dumps({"tier": tier}),
        )
        db.session.add(message)
        db.session.flush()  # populate message.id before the thoughts below reference it

        # One real MessageAgentThought row per tool call, not a JSON blob in
        # message_metadata -- tool/tool_input/observation map directly onto
        # what ask_decision_agent.py's trace entries already carry, and this
        # is the same table/shape Dify's own message_fields.py already knows
        # how to serialize for agent-thought rendering elsewhere.
        for position, entry in enumerate(trace or []):
            db.session.add(
                MessageAgentThought(
                    message_id=message.id,
                    position=position,
                    created_by_role=CreatorUserRole.ACCOUNT,
                    created_by=account.id,
                    tool=entry.get("tool"),
                    tool_input=json.dumps(entry.get("args") or {}),
                    observation=entry.get("observation_preview"),
                    tool_meta_str=json.dumps({"forced": entry.get("forced", False)}),
                )
            )

        conversation = db.session.query(Conversation).filter(Conversation.id == session_id).first()
        is_first_turn = False
        if conversation is not None:
            conversation.updated_at = naive_utc_now()
            conversation.dialogue_count = (conversation.dialogue_count or 0) + 1
            is_first_turn = conversation.dialogue_count == 1
            # Conversation.model_provider/model_id are real, otherwise-unused
            # columns on this same row -- remembers whichever model a
            # session last actually used, so reopening it can pre-select the
            # same one instead of every session silently reverting to
            # ask()'s hardcoded default.
            if model_provider and model_name:
                conversation.model_provider = model_provider
                conversation.model_id = model_name
        db.session.commit()

        if is_first_turn and conversation is not None:
            # Reuses Dify's own conversation-naming LLM call -- the same one
            # a real chat app gets -- instead of hand-rolling a second title
            # generator now that this is a real Conversation row.
            tenant_id = db.session.query(Tenant).first().id
            with contextlib.suppress(Exception):
                ConversationService.auto_generate_name(_prism_app(tenant_id), conversation)

        return message.id

    @staticmethod
    def set_feedback(*, message_id: str, account: Account, rating: str | None, content: str | None = None) -> dict:
        """Thumbs up/down on one answer -- reuses MessageService.create_feedback
        as-is (it toggles/clears/updates a MessageFeedback row by itself);
        rating=None clears existing feedback, matching that method's own
        contract. Real per-answer quality signal to query later, where Prism
        previously had none at all.
        """
        parsed_rating = FeedbackRating(rating) if rating else None
        feedback = MessageService.create_feedback(
            app_model=_prism_app(),
            message_id=message_id,
            user=account,
            rating=parsed_rating,
            content=content,
        )
        if parsed_rating is None or feedback is None:
            return {"rating": None, "content": None}
        return {"rating": feedback.rating.value, "content": feedback.content}

    @staticmethod
    def rename_session(session_id: str, *, account: Account, name: str) -> Conversation:
        """Reuses ConversationService.rename as-is -- it already handles
        looking the conversation up scoped to this account (raises
        ConversationNotExistsError otherwise) and committing the change.
        """
        return ConversationService.rename(_prism_app(), session_id, account, name, auto_generate=False)
