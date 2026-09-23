"""Create/refresh "Decision Rationale (Agent)" -- an agentic alternative to
the fixed "Decision -> Discussion" chatflow pipeline (see
../sync_decision_bot.py and ../sync_decision_bot_ux.py).

This is a deliberately separate app and a deliberately separate script:
nothing here touches the existing chatflow, its datasets' stored settings,
or its generator scripts. It exists to test a different hypothesis about
the same problem -- instead of a fixed graph (retrieve -> term-filter ->
judge -> explain -> fact-check, each step hand-coded), a single Dify
"Agent" app is given both datasets as tools and one system prompt, and
lets the model itself decide how many times to search, how to judge
relevance, and how to write the answer.

Architecturally this is NOT a Chatflow/Workflow (no `graph` of nodes) --
it's Dify's older, simpler "Agent" app type (`mode: agent-chat`), which
predates the workflow graph system and is configured via a single
`model_config` block (`AppModelConfigDict` in api/models/model.py) rather
than nodes/edges. A dataset is attached to it as a callable tool via
`model_config.agent_mode.tools`, each entry `{"dataset": {"enabled":
true, "id": <dataset-id>}}` -- confirmed by reading
`DatasetConfigManager.convert()` in
api/core/app/app_config/easy_ui_based_app/dataset/manager.py, not
guessed from documentation. The reasoning strategy (`agent_mode.strategy`)
is `function_call`, requiring the `langgenius/agent` marketplace plugin
to be installed -- Dify's newer per-node Agent strategy plugins do NOT
support attaching a dataset directly (only Built-in/API/Workflow/Plugin/
MCP tools), which is why this uses the app-level Agent mode instead of an
Agent node inside a chatflow.

Run inside the api container; pass --app-id to overwrite in place (import
updates the DRAFT only -- Publish afterwards in Studio). Only agent-chat
apps can be overwritten in place by their own app-id (confirmed via
AppDslService.import_app); omit --app-id to create a new app instead:

    docker exec -w /app/api docker-api-1 python scripts/agentic_rag/sync_decision_agent.py \\
        --dataset <mailarchive-dataset-id> \\
        --evolution-dataset <evolution-dataset-id> \\
        [--app-name "Decision Rationale (Agent)"] [--app-id <existing-app-id>]
"""

# ruff: noqa: T201 — stdout output is the point of a CLI tool

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "/app/api")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# One prompt carries what used to be five separate pipeline stages'
# worth of hand-coded logic: the administrative-notice filter, the
# term-overlap filter, the LLM judge, the three-heading explain step,
# and the fact-check pass. The agent's own tool-call loop replaces the
# fixed retrieve -> filter -> judge chain (it can search more than once,
# adaptively, which no fixed pipeline step could do); this instruction
# replaces the mechanical steps with judgment calls, the same way a
# person reading the two tools' output by hand would reason about it.
_AGENT_INSTRUCTION = (
    "You are an assistant for IETF standards research. You explain WHY a "
    "specification decision was made, using two tools:\n\n"
    "- The evolution/decision dataset: tagged, section-level diffs of "
    "draft revisions -- each entry is one specific change, in one "
    "specific section, between two specific revisions.\n"
    "- The mailarchive dataset: the working-group mailing-list "
    "discussion that produced those changes.\n\n"
    "For a user's question about a decision:\n\n"
    "1. Search the evolution dataset to locate the specific change being "
    "asked about. If the query genuinely matches multiple plausible "
    "changes, ask a brief clarifying question before proceeding rather "
    "than guessing which one.\n"
    "2. Once you've identified the decision, you MUST search the "
    "mailarchive dataset for discussion of it -- at least once, every "
    "time, with no exceptions. The evolution dataset only ever tells "
    "you WHAT changed; it is never itself evidence of WHY, no matter "
    "how self-explanatory the diff text sounds. Skipping the "
    "mailarchive search and writing a rationale from the diff text "
    "alone is the single most important mistake you can make here -- "
    "do not do it, even when you feel confident you already understand "
    "the change. Start with the decision's own wording as your query. "
    "If the results look thin, off-topic, or dominated by procedural/"
    "administrative traffic (WG Action, Document Action, I-D Action, "
    "Call for Papers, Last Call notices, meeting-announcement cover "
    "messages), try at least one differently-phrased query before "
    "giving up -- you are not limited to one search per decision.\n"
    "3. Read what you retrieve critically, the same way you'd read it "
    "yourself. A message that merely shares a broad topic (the same "
    "protocol, a different parameter, generic discussion) is NOT the "
    "same as a message that directly discusses THIS decision -- the "
    "same parameter/identifier and the same specific value or change. "
    "Disregard administrative/procedural messages even if they score "
    "well on a search. A message stating what it believes the CURRENT "
    "value already is -- even a mistaken belief that gets corrected "
    "later in the same thread -- is not a proposed alternative.\n"
    "4. Write your answer under exactly these three headings, in this "
    "order (keep them verbatim, bold, on their own line):\n\n"
    "**Rationale for the adopted approach** -- traceable to specific "
    "retrieved messages. If nothing you found clearly explains the "
    "motivation, say so explicitly rather than inferring a rationale "
    "from the decision text itself.\n"
    "**Alternatives considered** -- only a genuinely different "
    "approach/value a message actually proposes as a considered "
    "option. Never the decision's own before/after text repackaged as "
    "a proposal. If none, say so in one line instead of padding this "
    "section.\n"
    "**Other relevant discussion** -- corrections, open questions, or "
    "objections that are real and worth surfacing but aren't a "
    "proposed alternative (e.g. someone's mistaken belief about the "
    "current value, later corrected -- that belongs here, not under "
    "Alternatives). If there's nothing beyond the rationale above, say "
    "so in one line.\n\n"
    "5. Before you answer, verify every claim mechanically, not just by "
    "impression. For each sentence in your draft that names a specific "
    "person, date, or number: find the exact (or near-exact) sentence "
    "in what you actually retrieved that supports it. If you cannot "
    "locate that supporting sentence, the claim is not real -- delete "
    "it, don't soften it into vaguer language. A plausible-sounding "
    "detail that fits the pattern of the discussion is not the same as "
    "a detail that's actually in it; invented specifics (a number, a "
    "date, an attribution) are worse than no detail at all, because "
    "they read as more credible than a vague claim would. Never credit "
    "a message with a claim it didn't make, and never cite the decision "
    "text itself as if it were a retrieved mailing-list message. Cite "
    "thread subjects where they appear.\n\n"
    "The mailarchive discussion is matched by topic similarity, not "
    "guaranteed to agree with the decision: the same issue is often "
    "debated across multiple revisions, with different messages arguing "
    "for different outcomes than the one adopted. Only credit a message "
    "under Rationale if it actually argues for what was adopted; "
    "anything arguing for a different outcome belongs under "
    "Alternatives or Other relevant discussion instead."
)

_OPENING_STATEMENT = (
    "Ask me why an IETF draft decision was made -- a parameter value, a "
    "design choice, wording that changed between revisions -- and I'll "
    "search the draft-evolution and mailing-list archives myself, as "
    "many times as it takes, and explain the rationale grounded in what "
    "the working group actually discussed."
)


def _build_model_config(
    mailarchive_dataset_id: str,
    evolution_dataset_id: str,
    model_name: str,
) -> dict:
    return {
        "opening_statement": _OPENING_STATEMENT,
        "suggested_questions": [],
        "suggested_questions_after_answer": {"enabled": False},
        "speech_to_text": {"enabled": False},
        "text_to_speech": {"enabled": False, "language": "", "voice": ""},
        "retriever_resource": {"enabled": True},
        "sensitive_word_avoidance": {"enabled": False},
        "more_like_this": {"enabled": False},
        "external_data_tools": [],
        "model": {
            "provider": "langgenius/anthropic/anthropic",
            "name": model_name,
            "mode": "chat",
            "completion_params": {"temperature": 0.3, "top_p": 1},
        },
        "user_input_form": [],
        "dataset_query_variable": None,
        "pre_prompt": _AGENT_INSTRUCTION,
        "agent_mode": {
            "enabled": True,
            "strategy": "function_call",
            "max_iteration": 8,
            "tools": [
                {"dataset": {"enabled": True, "id": mailarchive_dataset_id}},
                {"dataset": {"enabled": True, "id": evolution_dataset_id}},
            ],
            "prompt": None,
        },
        "prompt_type": "simple",
        "dataset_configs": {
            "retrieval_model": "multiple",
            "top_k": 10,
            "score_threshold_enabled": False,
            "reranking_enabled": False,
            "datasets": {"strategy": "router", "datasets": []},
        },
        "file_upload": {
            "image": {
                "enabled": False,
                "number_limits": 3,
                "detail": "high",
                "transfer_methods": ["remote_url", "local_file"],
            }
        },
    }


def _build_dsl(
    app_name: str,
    mailarchive_dataset_id: str,
    evolution_dataset_id: str,
    model_name: str,
) -> str:
    dsl = {
        "app": {
            "name": app_name,
            "description": (
                "Agentic alternative to 'Decision -> Discussion': one Agent app, "
                "both datasets as tools, no fixed pipeline."
            ),
            "icon": "🕵️",
            "icon_background": "#EDE9FE",
            "mode": "agent-chat",
            "use_icon_as_answer_icon": False,
        },
        "kind": "app",
        "version": "0.6.0",
        "model_config": _build_model_config(mailarchive_dataset_id, evolution_dataset_id, model_name),
    }
    return yaml.safe_dump(dsl, allow_unicode=True, sort_keys=False)


def _verify_draft(app_id: str, mailarchive_dataset_id: str, evolution_dataset_id: str) -> None:
    """Reload the stored app_model_config and assert the agent tools and
    instruction actually persisted -- mirrors the same self-check
    ../sync_decision_bot.py does for its workflow graph, since a prior
    --app-id refresh once reported success without persisting."""
    import json

    from extensions.ext_database import db
    from models.model import App

    app = db.session.query(App).filter_by(id=app_id).first()
    if not app or not app.app_model_config:
        sys.exit("verification failed: app or app_model_config not found after import")

    config = app.app_model_config
    agent_mode = json.loads(config.agent_mode) if config.agent_mode else {}
    tool_dataset_ids = {
        tool["dataset"]["id"] for tool in agent_mode.get("tools", []) if "dataset" in tool
    }
    expected = {mailarchive_dataset_id, evolution_dataset_id}
    if tool_dataset_ids != expected:
        sys.exit(f"verification failed: expected dataset tools {expected}, got {tool_dataset_ids}")
    if not config.pre_prompt or "Rationale for the adopted approach" not in config.pre_prompt:
        sys.exit("verification failed: pre_prompt missing or doesn't contain expected heading")
    if agent_mode.get("strategy") != "function_call" or not agent_mode.get("enabled"):
        sys.exit(f"verification failed: unexpected agent_mode {agent_mode}")

    print("verified: stored app_model_config matches the generated DSL")


def _update_in_place(app_id: str, account, model_config: dict) -> str:
    """AppDslService.import_app() refuses to overwrite agent-chat apps in
    place ("Only workflow or advanced chat apps can be overwritten" --
    confirmed by hitting this directly) -- that restriction only applies
    to the DSL-import path, not to updating an app's config the same way
    Studio's own Save button does. This mirrors that exact flow (see
    controllers/console/app/model_config.py's ModelConfigResource.post):
    validate the dict through the app-mode-specific config manager, build
    a new AppModelConfig row from it, then repoint the app at the new row
    -- the same "new config row, swap the pointer" pattern Dify itself
    uses, rather than editing the existing row in place.
    """
    from extensions.ext_database import db
    from models.model import App, AppModelConfig
    from services.app_model_config_service import AppModelConfigService

    app_model = db.session.query(App).filter_by(id=app_id).first()
    if not app_model:
        sys.exit(f"app not found: {app_id}")
    if app_model.mode != "agent-chat":
        sys.exit(f"expected an agent-chat app, got mode={app_model.mode!r}")

    validated = AppModelConfigService.validate_configuration(
        tenant_id=account.current_tenant_id,
        config=model_config,
        app_mode=app_model.mode,
    )
    new_config = AppModelConfig(app_id=app_model.id, created_by=account.id, updated_by=account.id)
    new_config = new_config.from_model_config_dict(validated)
    db.session.add(new_config)
    db.session.flush()
    app_model.app_model_config_id = new_config.id
    db.session.commit()
    return app_model.id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, help="mailarchive dataset id (the discussion source)")
    parser.add_argument(
        "--evolution-dataset", required=True, help="evolution dataset id (the candidate changes source)"
    )
    parser.add_argument("--app-name", default="Decision Rationale (Agent)")
    parser.add_argument(
        "--app-id", default=None, help="overwrite this existing agent-chat app instead of creating a new one"
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-5",
        help=(
            "Anthropic model id, via the direct langgenius/anthropic/anthropic provider "
            "(must support function calling)"
        ),
    )
    args = parser.parse_args()

    from app_factory import create_app

    app = create_app()
    with app.test_request_context():
        from flask_login import login_user

        from extensions.ext_database import db
        from models.account import Account, Tenant
        from services.app_dsl_service import AppDslService

        account = db.session.query(Account).first()
        account.current_tenant = db.session.query(Tenant).first()
        login_user(account)

        if args.app_id:
            # AppDslService.import_app() refuses to overwrite agent-chat
            # apps in place -- update the config directly instead (see
            # _update_in_place's docstring).
            model_config = _build_model_config(args.dataset, args.evolution_dataset, args.model)
            app_id = _update_in_place(args.app_id, account, model_config)
            print("update status: completed")
            print("app id:", app_id)
        else:
            dsl = _build_dsl(args.app_name, args.dataset, args.evolution_dataset, args.model)

            result = AppDslService(db.session).import_app(
                account=account,
                import_mode="yaml-content",
                yaml_content=dsl,
            )
            db.session.commit()

            print("import status:", result.status)
            print("app id:", result.app_id)
            if result.status not in ("completed", "completed-with-warnings"):
                print("error:", getattr(result, "error", None))
                sys.exit(1)
            app_id = result.app_id

        _verify_draft(app_id, args.dataset, args.evolution_dataset)
        print("Open Studio -> the app, then Publish (the import only updates the draft).")


if __name__ == "__main__":
    main()
