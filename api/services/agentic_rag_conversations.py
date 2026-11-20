"""Conversation operations for Prism's agentic-RAG sessions.

Dify 1.19 replaced ConversationService's list/rename/auto-name/delete and
MessageService.create_feedback with application services scoped to end users
or installed apps, which look the app up in the `apps` table. Prism sessions
are console-account conversations on a transient app (see _prism_app in
agentic_rag_session_service), so neither scope fits. These are the 1.18
behaviours, kept on top of the pieces Dify still provides: get_conversation,
retire_conversation, enqueue_delete_cleanup and the default-model name
generator.
"""

import contextlib

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.app.entities.app_invoke_entities import InvokeFrom
from extensions.ext_application_services import application_services
from libs.datetime_utils import naive_utc_now
from libs.infinite_scroll_pagination import InfiniteScrollPagination
from models import Account
from models.enums import ConversationFromSource, FeedbackFromSource, FeedbackRating
from models.model import App, Conversation, Message, MessageFeedback
from repositories.conversation_lifecycle import retire_conversation
from services.conversation_name_generator import DefaultModelConversationNameGenerator
from services.conversation_service import ConversationService
from services.errors.conversation import LastConversationNotExistsError
from services.errors.message import MessageNotExistsError


def paginate(
    *, session: Session, app_model: App, account: Account, last_id: str | None, limit: int, invoke_from: InvokeFrom
) -> InfiniteScrollPagination:
    """The account's conversations in this app, most recently updated first."""
    stmt = select(Conversation).where(
        Conversation.is_deleted.is_(False),
        Conversation.app_id == app_model.id,
        Conversation.from_source == ConversationFromSource.CONSOLE,
        Conversation.from_end_user_id.is_(None),
        Conversation.from_account_id == account.id,
        or_(Conversation.invoke_from.is_(None), Conversation.invoke_from == invoke_from.value),
    )
    if last_id:
        last_conversation = session.scalar(stmt.where(Conversation.id == last_id))
        if not last_conversation:
            raise LastConversationNotExistsError()
        stmt = stmt.where(Conversation.updated_at < last_conversation.updated_at)

    conversations = session.scalars(stmt.order_by(Conversation.updated_at.desc()).limit(limit)).all()

    has_more = False
    if len(conversations) == limit:
        rest = stmt.where(Conversation.updated_at < conversations[-1].updated_at)
        has_more = (session.scalar(select(func.count()).select_from(rest.subquery())) or 0) > 0
    return InfiniteScrollPagination(data=conversations, limit=limit, has_more=has_more)


def rename(*, session: Session, app_model: App, conversation_id: str, account: Account, name: str) -> Conversation:
    conversation = ConversationService.get_conversation(app_model, conversation_id, account, session=session)
    conversation.name = name
    conversation.updated_at = naive_utc_now()
    session.commit()
    return conversation


def auto_generate_name(*, session: Session, app_model: App, conversation: Conversation) -> Conversation:
    """Name the conversation from its first question; keeps the old name if generation fails."""
    message = session.scalar(
        select(Message)
        .where(Message.app_id == app_model.id, Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc())
        .limit(1)
    )
    if not message:
        raise MessageNotExistsError()

    generate_name = DefaultModelConversationNameGenerator(trace_scopes=application_services().tracing.scopes)
    with contextlib.suppress(Exception):
        conversation.name = generate_name(
            tenant_id=app_model.tenant_id,
            app_id=app_model.id,
            conversation_id=conversation.id,
            query=message.query,
            app_mode=app_model.mode,
        )
    session.commit()
    return conversation


def delete(*, session: Session, app_model: App, conversation_id: str, account: Account) -> None:
    """Retire the conversation, then queue Dify's physical cleanup after the commit."""
    conversation = ConversationService.get_conversation(app_model, conversation_id, account, session=session)
    try:
        retired_workspace_ids = retire_conversation(app_model=app_model, conversation=conversation, session=session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    ConversationService.enqueue_delete_cleanup(
        tenant_id=app_model.tenant_id,
        conversation_id=conversation_id,
        retired_workspace_ids=retired_workspace_ids,
    )


def set_feedback(
    *,
    session: Session,
    app_model: App,
    message_id: str,
    account: Account,
    rating: FeedbackRating | None,
    content: str | None,
) -> MessageFeedback | None:
    """Create, update or (rating=None) clear the account's feedback on its own message."""
    message = session.scalar(
        select(Message)
        .where(
            Message.id == message_id,
            Message.app_id == app_model.id,
            Message.from_source == ConversationFromSource.CONSOLE,
            Message.from_end_user_id.is_(None),
            Message.from_account_id == account.id,
        )
        .limit(1)
    )
    if not message:
        raise MessageNotExistsError()

    feedback = message.admin_feedback_with_session(session)
    if not rating:
        if feedback:
            session.delete(feedback)
            session.commit()
        return None
    if feedback:
        feedback.rating = rating
        feedback.content = content
    else:
        feedback = MessageFeedback(
            app_id=app_model.id,
            conversation_id=message.conversation_id,
            message_id=message.id,
            rating=rating,
            content=content,
            from_source=FeedbackFromSource.ADMIN,
            from_end_user_id=None,
            from_account_id=account.id,
        )
        session.add(feedback)
    session.commit()
    return feedback
