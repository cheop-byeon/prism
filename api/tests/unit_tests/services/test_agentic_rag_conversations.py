"""Prism session operations ported off the conversation APIs Dify 1.19 removed."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.app.entities.app_invoke_entities import InvokeFrom
from models import Account
from models.enums import ConversationFromSource, FeedbackRating
from models.model import App, AppMode, Conversation, Message, MessageFeedback
from services import agentic_rag_conversations as conversations
from services.errors.conversation import ConversationNotExistsError, LastConversationNotExistsError

_APP_ID = "prism-app"
_BASE = datetime(2026, 11, 1, 12, 0, 0)


def _app() -> App:
    # Prism's transient app: never persisted, only id/tenant_id/mode are read.
    return App(id=_APP_ID, tenant_id="tenant-1", name="Prism", mode=AppMode.AGENT_CHAT)


def _account(account_id: str = "account-1") -> Account:
    account = Account(name=account_id, email=f"{account_id}@example.com")
    account.id = account_id
    return account


def _conversation(session: Session, conversation_id: str, *, account_id: str = "account-1", age: int = 0):
    conversation = Conversation(
        app_id=_APP_ID,
        app_model_config_id=None,
        model_provider=None,
        override_model_configs=None,
        model_id=None,
        mode=AppMode.AGENT_CHAT,
        name=conversation_id,
        _inputs={},
        introduction="",
        system_instruction="",
        system_instruction_tokens=0,
        status="normal",
        invoke_from=None,
        from_source=ConversationFromSource.CONSOLE,
        from_end_user_id=None,
        from_account_id=account_id,
    )
    conversation.id = conversation_id
    conversation.updated_at = _BASE - timedelta(minutes=age)
    session.add(conversation)
    session.flush()
    return conversation


def _message(session: Session, conversation: Conversation, *, query: str = "What did the WG decide?") -> Message:
    message = Message(
        app_id=_APP_ID,
        conversation_id=conversation.id,
        _inputs={},
        query=query,
        message={},
        answer="answer",
        message_unit_price=Decimal(0),
        answer_unit_price=Decimal(0),
        currency="USD",
        from_source=ConversationFromSource.CONSOLE,
        from_account_id=conversation.from_account_id,
    )
    session.add(message)
    session.flush()
    return message


def test_paginate_lists_only_the_accounts_sessions_newest_first(sqlite_session: Session):
    for index in range(3):
        _conversation(sqlite_session, f"mine-{index}", age=index)
    _conversation(sqlite_session, "theirs", account_id="account-2")

    first = conversations.paginate(
        session=sqlite_session,
        app_model=_app(),
        account=_account(),
        last_id=None,
        limit=2,
        invoke_from=InvokeFrom.DEBUGGER,
    )
    rest = conversations.paginate(
        session=sqlite_session,
        app_model=_app(),
        account=_account(),
        last_id=first.data[-1].id,
        limit=2,
        invoke_from=InvokeFrom.DEBUGGER,
    )

    assert [row.id for row in first.data] == ["mine-0", "mine-1"]
    assert first.has_more is True
    assert [row.id for row in rest.data] == ["mine-2"]
    assert rest.has_more is False


def test_paginate_rejects_an_unknown_cursor(sqlite_session: Session):
    with pytest.raises(LastConversationNotExistsError):
        conversations.paginate(
            session=sqlite_session,
            app_model=_app(),
            account=_account(),
            last_id="missing",
            limit=10,
            invoke_from=InvokeFrom.DEBUGGER,
        )


def test_rename_is_scoped_to_the_owning_account(sqlite_session: Session):
    _conversation(sqlite_session, "session-1")

    renamed = conversations.rename(
        session=sqlite_session, app_model=_app(), conversation_id="session-1", account=_account(), name="Renamed"
    )

    assert renamed.name == "Renamed"
    with pytest.raises(ConversationNotExistsError):
        conversations.rename(
            session=sqlite_session,
            app_model=_app(),
            conversation_id="session-1",
            account=_account("account-2"),
            name="Hijacked",
        )


def test_auto_generate_name_uses_the_first_question(sqlite_session: Session):
    conversation = _conversation(sqlite_session, "session-1")
    _message(sqlite_session, conversation, query="Why was the draft adopted?")
    generator = MagicMock(return_value="Draft adoption")

    with (
        patch.object(conversations, "application_services"),
        patch.object(conversations, "DefaultModelConversationNameGenerator", return_value=generator),
    ):
        conversations.auto_generate_name(session=sqlite_session, app_model=_app(), conversation=conversation)

    assert conversation.name == "Draft adoption"
    assert generator.call_args.kwargs["query"] == "Why was the draft adopted?"


def test_auto_generate_name_keeps_the_old_name_when_generation_fails(sqlite_session: Session):
    conversation = _conversation(sqlite_session, "session-1")
    _message(sqlite_session, conversation)

    with (
        patch.object(conversations, "application_services"),
        patch.object(
            conversations, "DefaultModelConversationNameGenerator", return_value=MagicMock(side_effect=RuntimeError)
        ),
    ):
        conversations.auto_generate_name(session=sqlite_session, app_model=_app(), conversation=conversation)

    assert conversation.name == "session-1"


def test_set_feedback_creates_updates_and_clears(sqlite_session: Session):
    message = _message(sqlite_session, _conversation(sqlite_session, "session-1"))

    def rate(rating: FeedbackRating | None, content: str | None = None):
        return conversations.set_feedback(
            session=sqlite_session,
            app_model=_app(),
            message_id=message.id,
            account=_account(),
            rating=rating,
            content=content,
        )

    created = rate(FeedbackRating.LIKE)
    assert created is not None
    assert created.from_account_id == "account-1"
    updated = rate(FeedbackRating.DISLIKE, "missed the thread")
    assert updated is not None
    assert updated.id == created.id
    assert (updated.rating, updated.content) == (FeedbackRating.DISLIKE, "missed the thread")
    assert rate(None) is None
    assert sqlite_session.scalars(select(MessageFeedback)).all() == []


def test_delete_retires_then_queues_cleanup(sqlite_session: Session):
    _conversation(sqlite_session, "session-1")

    with (
        patch.object(conversations, "retire_conversation", return_value=("workspace-1",)) as retire,
        patch.object(conversations.ConversationService, "enqueue_delete_cleanup") as enqueue,
    ):
        conversations.delete(session=sqlite_session, app_model=_app(), conversation_id="session-1", account=_account())

    retire.assert_called_once()
    enqueue.assert_called_once_with(
        tenant_id="tenant-1", conversation_id="session-1", retired_workspace_ids=("workspace-1",)
    )
