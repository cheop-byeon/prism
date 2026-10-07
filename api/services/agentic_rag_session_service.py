import contextlib
import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy import text as sa_text

from core.app.entities.app_invoke_entities import InvokeFrom
from extensions.ext_database import db
from libs.datetime_utils import naive_utc_now
from libs.infinite_scroll_pagination import InfiniteScrollPagination
from models import Account, Tenant
from models.account import TenantAccountJoin, TenantAccountRole
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


def get_owned_tenant_id(account: Account) -> str | None:
    """The tenant this account itself owns, if any -- their real, personal
    Dify workspace (every self-service sign-up gets exactly one, as the
    owner: see RegisterService.register -> TenantService.create_tenant).
    None for an account that only ever joined other tenants (e.g. invited
    into the shared admin tenant as a normal member without ever
    self-registering their own) -- there's no personal workspace to point
    bring-your-own-key at in that case.

    Deliberately filters on role == owner, not "any tenant this account
    belongs to" -- an account can belong to several (their own, plus any
    they were invited into), and only the one they own is truly theirs to
    configure model credentials for. The admin account's own tenant (the
    one with all the real IETF data) is not special-cased here: it comes
    back the same way, since the admin owns it too, and using their own
    tenant for BYOK is exactly correct for them as well.
    """
    join = (
        db.session.query(TenantAccountJoin)
        .filter(TenantAccountJoin.account_id == account.id, TenantAccountJoin.role == TenantAccountRole.OWNER)
        .first()
    )
    return join.tenant_id if join else None


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
    def get_session_for_view(session_id: str, *, account: Account) -> Conversation | None:
        """Like get_session, but also returns a session this account does
        NOT own if its owner has explicitly marked it shared -- read-only:
        only used for viewing (get_turns), never by rename/delete/share
        itself, which all stay on get_session's strict ownership check.
        """
        owned = AgenticRagSessionService.get_session(session_id, account=account)
        if owned is not None:
            return owned
        if not AgenticRagSessionService.get_session_shared(session_id):
            return None
        return (
            db.session.query(Conversation)
            .filter(
                Conversation.id == session_id,
                Conversation.app_id == _PRISM_APP_ID,
                Conversation.from_source == ConversationFromSource.CONSOLE,
                Conversation.is_deleted.is_(False),
            )
            .first()
        )

    @staticmethod
    def get_session_shared(session_id: str) -> bool:
        from models.ietf_agentic_session_share import IetfAgenticSessionShare

        row = db.session.query(IetfAgenticSessionShare).filter_by(conversation_id=session_id).first()
        return bool(row and row.shared)

    @staticmethod
    def _shared_map(conversation_ids: list[str]) -> dict[str, bool]:
        from models.ietf_agentic_session_share import IetfAgenticSessionShare

        if not conversation_ids:
            return {}
        rows = (
            db.session.query(IetfAgenticSessionShare.conversation_id, IetfAgenticSessionShare.shared)
            .filter(IetfAgenticSessionShare.conversation_id.in_(conversation_ids))
            .all()
        )
        return {row[0]: row[1] for row in rows}

    @staticmethod
    def set_session_shared(session_id: str, *, account: Account, shared: bool) -> dict:
        """Only the owning account may flip this -- same convention as
        IetfImportService.set_local_source_shared: an {"error": ...} dict
        for a caller-facing failure rather than raising, so the route can
        jsonify it directly.
        """
        from models.ietf_agentic_session_share import IetfAgenticSessionShare

        conversation = AgenticRagSessionService.get_session(session_id, account=account)
        if conversation is None:
            return {"error": "session not found"}
        row = db.session.query(IetfAgenticSessionShare).filter_by(conversation_id=session_id).first()
        if row is None:
            row = IetfAgenticSessionShare(conversation_id=session_id, shared=shared)
            db.session.add(row)
        else:
            row.shared = shared
        db.session.commit()
        return {"session_id": session_id, "shared": shared}

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
        shared_map = AgenticRagSessionService._shared_map([row.id for row in pagination.data])
        return {
            "sessions": [
                {
                    "id": row.id,
                    "title": row.name,
                    # +"Z": row.updated_at is a naive datetime (no tzinfo)
                    # that's actually always UTC -- without an explicit
                    # marker, JS's `new Date(...)` parses a timezone-less
                    # ISO string as LOCAL time instead, confirmed live to
                    # show "2h ago" for a session updated seconds earlier
                    # on a UTC+2 browser (Oslo, October/CEST).
                    "updated_at": row.updated_at.isoformat() + "Z",
                    "model_provider": row.model_provider,
                    "model_id": row.model_id,
                    "is_owner": True,
                    "shared": shared_map.get(row.id, False),
                }
                for row in pagination.data
            ],
            "has_more": pagination.has_more,
        }

    @staticmethod
    def list_shared_sessions(*, account: Account, limit: int = 30) -> list[dict]:
        """Sessions someone ELSE has shared, visible to any account --
        the Investigations-rail counterpart to IetfImportService.
        list_local_source_names's own-pulls-plus-shared merge, kept as a
        separate list here (rather than merged into list_sessions) since
        list_sessions' cursor pagination comes straight from Dify's own
        ConversationService.pagination_by_last_id, which is hard-scoped to
        one owning account by design -- not worth forking to inject a
        second account's rows into the same cursor.
        """
        from models.ietf_agentic_session_share import IetfAgenticSessionShare

        rows = (
            db.session.query(Conversation, Account.email)
            .join(IetfAgenticSessionShare, IetfAgenticSessionShare.conversation_id == Conversation.id)
            .join(Account, Account.id == Conversation.from_account_id)
            .filter(
                IetfAgenticSessionShare.shared.is_(True),
                Conversation.app_id == _PRISM_APP_ID,
                Conversation.from_source == ConversationFromSource.CONSOLE,
                Conversation.is_deleted.is_(False),
                Conversation.from_account_id != account.id,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": conversation.id,
                "title": conversation.name,
                "updated_at": conversation.updated_at.isoformat() + "Z",  # see list_sessions' own note
                "model_provider": conversation.model_provider,
                "model_id": conversation.model_id,
                "is_owner": False,
                "shared": True,
                "owner_email": owner_email,
            }
            for conversation, owner_email in rows
        ]

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
        shared_map = AgenticRagSessionService._shared_map([row.id for row in rows])
        return [
            {
                "id": row.id,
                "title": row.name,
                "updated_at": row.updated_at.isoformat() + "Z",  # see list_sessions' own note
                "model_provider": row.model_provider,
                "model_id": row.model_id,
                "is_owner": True,
                "shared": shared_map.get(row.id, False),
            }
            for row in rows
        ]

    @staticmethod
    def delete_session(session_id: str, *, account: Account) -> bool:
        from models.ietf_agentic_session_share import IetfAgenticSessionShare

        try:
            ConversationService.delete(_prism_app(), session_id, account)
        except ConversationNotExistsError:
            return False
        # IetfAgenticSessionShare is this project's own table, not part of
        # Dify's core delete_conversation_related_data cleanup -- deleted
        # synchronously here (a single row by its unique conversation_id,
        # not worth a background task) rather than left behind pointing at
        # a conversation_id that no longer exists.
        db.session.query(IetfAgenticSessionShare).filter_by(conversation_id=session_id).delete()
        db.session.commit()
        return True

    @staticmethod
    def get_turns(session_id: str, *, account: Account) -> list[dict]:
        # get_session_for_view, not get_session -- viewing (unlike rename/
        # delete/share) is allowed for a session someone else owns if
        # they've shared it.
        conversation = AgenticRagSessionService.get_session_for_view(session_id, account=account)
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
                    "claim_labels": meta.get("claim_labels", {}),
                    "evidence": meta.get("evidence", []),
                    "created_at": row.created_at.isoformat() + "Z",  # see list_sessions' own note
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
        claim_labels: dict | None = None,
        evidence: list | None = None,
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
            # claim_labels/evidence: same rationale as tier above -- a JSON
            # blob here rather than real columns/tables, since (unlike
            # trace's tool calls) these aren't queried or joined on, just
            # replayed back to the frontend as-is when a session reopens.
            # JSON object keys are always strings, so claim_labels'
            # {citation_number: status} round-trips as string keys after
            # this -- already true of the live (non-reloaded) SSE path too,
            # via jsonify, so both paths hand the frontend the same shape.
            message_metadata=json.dumps({"tier": tier, "claim_labels": claim_labels or {}, "evidence": evidence or []}),
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

    @staticmethod
    def get_observability_stats(*, days: int = 30) -> dict:
        """Org-wide usage/cost/quality trends across every Prism account --
        not one account's own sessions, so this is for the data-admin gate,
        not every logged-in account.

        Adapted from controllers/console/app/statistic.py's own daily-
        messages/token-costs/average-response-time/user-satisfaction-rate
        queries (same tables, same idea: bucket by day, cheap SQL, no
        external tracing service) rather than reusing those routes
        directly -- they're wired to a real, persisted App row via
        @get_app_model, and _PRISM_APP_ID is deliberately a transient one
        (see _prism_app's docstring). Two deliberate simplifications since
        Prism has exactly one deployment, not many tenants: dates are
        grouped in UTC (Dify's version converts to each viewing account's
        own timezone) and there's no invoke_from filter (Prism never sets
        it -- there's no "debugger" concept here to exclude).

        Tool-call frequency has no Dify equivalent to adapt -- Dify's own
        stats don't know Prism's agent makes tool calls at all -- so it's
        built from scratch from MessageAgentThought, the same table
        append_turn already writes one row per tool call into.
        """
        since = naive_utc_now() - timedelta(days=days)

        daily_metrics = db.session.execute(
            sa_text("""
                SELECT
                    DATE(created_at) AS date,
                    COUNT(*) AS turns,
                    COALESCE(SUM(message_tokens) + SUM(answer_tokens), 0) AS tokens,
                    COALESCE(SUM(total_price), 0) AS cost,
                    COALESCE(AVG(provider_response_latency), 0) AS avg_latency
                FROM messages
                WHERE app_id = :app_id AND created_at >= :since
                GROUP BY date
                ORDER BY date
            """),
            {"app_id": _PRISM_APP_ID, "since": since},
        ).mappings().all()

        daily_satisfaction = db.session.execute(
            sa_text("""
                SELECT
                    DATE(m.created_at) AS date,
                    COUNT(m.id) AS message_count,
                    COUNT(mf.id) AS like_count
                FROM messages m
                LEFT JOIN message_feedbacks mf ON mf.message_id = m.id AND mf.rating = 'like'
                WHERE m.app_id = :app_id AND m.created_at >= :since
                GROUP BY date
                ORDER BY date
            """),
            {"app_id": _PRISM_APP_ID, "since": since},
        ).mappings().all()
        satisfaction_by_date = {
            row["date"]: (row["like_count"] / row["message_count"] * 100) if row["message_count"] else 0.0
            for row in daily_satisfaction
        }

        daily = [
            {
                "date": str(row["date"]),
                "turns": row["turns"],
                "tokens": row["tokens"],
                "cost": float(row["cost"]),
                "avg_latency": round(float(row["avg_latency"]), 2),
                "satisfaction_pct": round(satisfaction_by_date.get(row["date"], 0.0), 1),
            }
            for row in daily_metrics
        ]

        tool_usage_rows = db.session.execute(
            sa_text("""
                SELECT mat.tool AS tool, COUNT(*) AS calls
                FROM message_agent_thoughts mat
                JOIN messages m ON m.id = mat.message_id
                WHERE m.app_id = :app_id AND m.created_at >= :since AND mat.tool IS NOT NULL AND mat.tool != ''
                GROUP BY mat.tool
                ORDER BY calls DESC
            """),
            {"app_id": _PRISM_APP_ID, "since": since},
        ).mappings().all()
        tool_usage = [{"tool": row["tool"], "calls": row["calls"]} for row in tool_usage_rows]

        totals_row = db.session.execute(
            sa_text("""
                SELECT
                    COUNT(*) AS turns,
                    COALESCE(SUM(message_tokens) + SUM(answer_tokens), 0) AS tokens,
                    COALESCE(SUM(total_price), 0) AS cost,
                    COALESCE(AVG(provider_response_latency), 0) AS avg_latency,
                    COUNT(DISTINCT conversation_id) AS sessions,
                    COUNT(DISTINCT from_account_id) AS accounts
                FROM messages
                WHERE app_id = :app_id AND created_at >= :since
            """),
            {"app_id": _PRISM_APP_ID, "since": since},
        ).mappings().one()
        totals_satisfaction_row = db.session.execute(
            sa_text("""
                SELECT COUNT(m.id) AS message_count, COUNT(mf.id) AS like_count
                FROM messages m
                LEFT JOIN message_feedbacks mf ON mf.message_id = m.id AND mf.rating = 'like'
                WHERE m.app_id = :app_id AND m.created_at >= :since
            """),
            {"app_id": _PRISM_APP_ID, "since": since},
        ).mappings().one()
        totals_satisfaction_pct = (
            (totals_satisfaction_row["like_count"] / totals_satisfaction_row["message_count"] * 100)
            if totals_satisfaction_row["message_count"]
            else 0.0
        )

        return {
            "days": days,
            "daily": daily,
            "tool_usage": tool_usage,
            "totals": {
                "turns": totals_row["turns"],
                "tokens": totals_row["tokens"],
                "cost": float(totals_row["cost"]),
                "avg_latency": round(float(totals_row["avg_latency"]), 2),
                "sessions": totals_row["sessions"],
                "accounts": totals_row["accounts"],
                "satisfaction_pct": round(totals_satisfaction_pct, 1),
            },
        }
