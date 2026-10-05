import logging
import time

import click
from celery import shared_task

from core.db.session_factory import session_factory
from models import ConversationVariable
from models.model import Message, MessageAgentThought, MessageAnnotation, MessageFeedback
from models.tools import ToolConversationVariables, ToolFile
from models.web import PinnedConversation

logger = logging.getLogger(__name__)


@shared_task(queue="conversation")
def delete_conversation_related_data(conversation_id: str):
    """
    Delete related data conversation in correct order from datatbase to respect foreign key constraints

    Args:
        conversation_id: conversation Id
    """

    logger.info(
        click.style(f"Starting to delete conversation data from db for conversation_id {conversation_id}", fg="green")
    )
    start_at = time.perf_counter()

    with session_factory.create_session() as session:
        try:
            session.query(MessageAnnotation).where(MessageAnnotation.conversation_id == conversation_id).delete(
                synchronize_session=False
            )

            session.query(MessageFeedback).where(MessageFeedback.conversation_id == conversation_id).delete(
                synchronize_session=False
            )

            session.query(ToolConversationVariables).where(
                ToolConversationVariables.conversation_id == conversation_id
            ).delete(synchronize_session=False)

            session.query(ToolFile).where(ToolFile.conversation_id == conversation_id).delete(synchronize_session=False)

            session.query(ConversationVariable).where(ConversationVariable.conversation_id == conversation_id).delete(
                synchronize_session=False
            )

            # Has no FK/ondelete relationship to Message at the DB level
            # (confirmed: MessageAgentThought.message_id is a bare column,
            # no ForeignKey -- see models/model.py), so it was never
            # cleaned up here despite every agent-mode conversation
            # producing one row per tool call. Must run before the Message
            # delete below -- the subquery needs those rows to still exist
            # to know which thoughts belong to this conversation at all.
            message_ids_subquery = session.query(Message.id).where(Message.conversation_id == conversation_id)
            session.query(MessageAgentThought).where(
                MessageAgentThought.message_id.in_(message_ids_subquery)
            ).delete(synchronize_session=False)

            session.query(Message).where(Message.conversation_id == conversation_id).delete(synchronize_session=False)

            session.query(PinnedConversation).where(PinnedConversation.conversation_id == conversation_id).delete(
                synchronize_session=False
            )

            session.commit()

            end_at = time.perf_counter()
            logger.info(
                click.style(
                    (
                        f"Succeeded cleaning data from db for conversation_id {conversation_id} "
                        f"latency: {end_at - start_at}"
                    ),
                    fg="green",
                )
            )

        except Exception:
            logger.exception("Failed to delete data from db for conversation_id: %s failed", conversation_id)
            session.rollback()
            raise
