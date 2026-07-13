"""Create/refresh a chatflow app whose users pick an imported change OR type freely.

Builds a "Decision -> Discussion" chatbot: the Start form carries an optional
Select variable listing the draft's imported section changes; the chat box
stays available for free text. A code node combines whatever the user
provided (decision, question, or both) into the retrieval query, and the
LLM answers the typed question with the retrieved discussion as context.

Run inside the api container; re-run after new evolution imports to refresh
the dropdown options (pass --app-id to overwrite the existing app in place):

    docker exec -w /app/api docker-api-1 python scripts/sync_decision_bot.py \
        --draft draft-ietf-ledbat-congestion \
        --dataset <mailarchive-dataset-id> \
        [--app-name "Decision -> Discussion"] [--app-id <existing-app-id>] \
        [--max-options 60]
"""

# ruff: noqa: T201 — stdout output is the point of a CLI tool

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "/app/api")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_MAX_OPTION_CHARS = 64

_CODE = (
    "def main(decision: str, query: str) -> dict:\n"
    "    decision = (decision or '').strip()\n"
    "    query = (query or '').strip()\n"
    "    if decision and query:\n"
    "        return {'result': f'{decision} {query}'}\n"
    "    return {'result': decision or query}\n"
)

# Looks up the selected decision's real diff text (retrieved from the
# evolution dataset) and builds the mailarchive query from it; without a
# selection, the typed question passes through untouched.
# Two-stage retrieval for ANY input: stage 1 (kr_evo) resolves the decision -
# from the dropdown label OR the user's free text - to the best-matching
# change record; stage 2 searches the mail archive with the change's own
# wording. In free-text mode the diff excerpt is BLENDED with (not
# substituted for) the typed text, so a weak stage-1 match adds noise but
# cannot hijack the search; the resolved change is displayed so mismatches
# are visible. Queries are never constrained to the dropdown's options.
_CODE_MIX = (
    "def main(decision: str, query: str, evo: list) -> dict:\n"
    "    decision = (decision or '').strip()\n"
    "    query = (query or '').strip()\n"
    "    change_text, header = '', ''\n"
    "    if evo:\n"
    "        content = (evo[0].get('content') if isinstance(evo[0], dict) else '') or ''\n"
    "        lines = content.split('\\n')\n"
    "        header = lines[0].strip() if lines else ''\n"
    "        change_text = '\\n'.join(lines[2:]).strip() if len(lines) > 2 else content\n"
    "    if decision:\n"
    "        base = change_text or decision\n"
    "        mail_query = (base[:800] + ' ' + query).strip()\n"
    "        display = ('What the change says:\\n' + change_text[:1200] + '\\n\\n---\\n') if change_text else ''\n"
    "    elif query and change_text:\n"
    "        mail_query = (query + ' ' + change_text[:400]).strip()\n"
    "        excerpt = change_text[:600]\n"
    "        display = ('Best-matching change: ' + header + '\\n' + excerpt + '\\n\\n---\\n') if header else ''\n"
    "    else:\n"
    "        mail_query = query\n"
    "        display = ''\n"
    "    return {'result': mail_query, 'change_text': change_text[:1500], 'display_text': display}\n"
)

_SYSTEM_PROMPT = (
    "You are an assistant for IETF standards research. The user asks about a "
    "specification change - selected from the decision list, typed freely, "
    "or both. Use the retrieved mailing list discussion below to explain what "
    "was discussed and decided, citing thread subjects and message URLs.\n\n"
    "Retrieved discussion:\n{{#context#}}"
)


def _node(node_id: str, data: dict, x: int) -> dict:
    return {
        "id": node_id,
        "type": "custom",
        "position": {"x": x, "y": 227},
        "positionAbsolute": {"x": x, "y": 227},
        "sourcePosition": "right",
        "targetPosition": "left",
        "selected": False,
        "width": 244,
        "height": 90,
        "data": data,
    }


def _edge(source: str, source_type: str, target: str, target_type: str) -> dict:
    return {
        "id": f"{source}-{target}",
        "type": "custom",
        "source": source,
        "sourceHandle": "source",
        "target": target,
        "targetHandle": "target",
        "data": {"sourceType": source_type, "targetType": target_type},
    }


def _build_options(draft_name: str, max_options: int) -> list[str]:
    from services.ietf_import_service import IetfImportService

    sorted_revs, texts = IetfImportService._fetch_all_revisions(draft_name)
    parsed = {rev: IetfImportService._parse_sections(texts[rev]) for rev in sorted_revs}
    options: list[str] = []
    seen: set[str] = set()
    for from_rev, to_rev in zip(sorted_revs, sorted_revs[1:]):
        for record in IetfImportService._diff_version_pair(
            parsed[from_rev], parsed[to_rev], from_rev, to_rev, draft_name, skip_minor_edits=True
        ):
            label = (
                f"§{record['section_id']} {record['from_rev']}→{record['to_rev']} "
                f"{record['section_title']}"
            )[:_MAX_OPTION_CHARS]
            if label not in seen:
                seen.add(label)
                options.append(label)
            if len(options) >= max_options:
                return options
    return options


def _build_dsl(
    app_name: str,
    options: list[str],
    dataset_id: str,
    embedding: tuple[str, str],
    evolution_dataset_id: str | None = None,
) -> str:
    start_id, code_id, kr_id, llm_id, answer_id = "start1", "code1", "kr1", "llm1", "answer1"
    evo_kr_id, mix_id = "krevo1", "codemix1"
    provider, model_name = embedding

    retrieval_config = {
        "top_k": 4,
        "score_threshold": None,
        "reranking_mode": "weighted_score",
        "reranking_enable": False,
        "weights": {
            "weight_type": "customized",
            "vector_setting": {
                "vector_weight": 1,
                "embedding_provider_name": provider,
                "embedding_model_name": model_name,
            },
            "keyword_setting": {"keyword_weight": 0},
        },
    }

    nodes = [
        _node(start_id, {
            "type": "start",
            "title": "Start",
            "desc": "",
            "selected": False,
            "variables": [{
                "variable": "decision",
                "label": "Decision (optional — or just type your question)",
                "type": "select",
                "required": False,
                "max_length": None,
                "options": options,
            }],
        }, 30),
        _node(code_id, {
            "type": "code",
            "title": "Pick query",
            "desc": "Combines the selected decision with the typed question.",
            "code_language": "python3",
            "code": _CODE,
            "variables": [
                {"variable": "decision", "value_selector": [start_id, "decision"]},
                {"variable": "query", "value_selector": ["sys", "query"]},
            ],
            "outputs": {"result": {"type": "string", "children": None}},
        }, 334),
        _node(kr_id, {
            "type": "knowledge-retrieval",
            "title": "Find discussions",
            "desc": "",
            "query_variable_selector": [mix_id if evolution_dataset_id else code_id, "result"],
            "dataset_ids": [dataset_id],
            "retrieval_mode": "multiple",
            "multiple_retrieval_config": retrieval_config,
        }, 638),
        _node(llm_id, {
            "type": "llm",
            "title": "Answer",
            "desc": "",
            "model": {
                "provider": "langgenius/openrouter/openrouter",
                "name": "llama-3.3-70b-instruct",
                "mode": "chat",
                "completion_params": {"max_tokens": 1024, "temperature": 0.4, "top_p": 1},
            },
            "prompt_template": [
                {"role": "system", "text": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "text": (
                        f"Selected decision: {{{{#{start_id}.decision#}}}}\n"
                        + (
                            f"What the change says:\n{{{{#{mix_id}.change_text#}}}}\n"
                            if evolution_dataset_id
                            else ""
                        )
                        + "Question: {{#sys.query#}}"
                    ),
                },
            ],
            "context": {"enabled": True, "variable_selector": [kr_id, "result"]},
            "vision": {"enabled": False, "configs": {"variable_selector": []}},
            # Free-tier OpenRouter models return transient 429s under load;
            # retry twice with a pause instead of surfacing an empty answer.
            "retry_config": {"retry_enabled": True, "max_retries": 2, "retry_interval": 8000},
            # Conversation memory on (window off = keep the whole dialog), so
            # follow-up questions in the same chat stay coherent.
            "memory": {"window": {"enabled": False, "size": 50}},
        }, 942),
        _node(answer_id, {
            "type": "answer",
            "title": "Answer",
            "desc": "",
            "answer": f"{{{{#{llm_id}.text#}}}}",
        }, 1246),
    ]
    if evolution_dataset_id:
        nodes.insert(2, _node(evo_kr_id, {
            "type": "knowledge-retrieval",
            "title": "Look up change text",
            "desc": "Fetches the selected decision's real diff from the evolution dataset.",
            "query_variable_selector": [code_id, "result"],
            "dataset_ids": [evolution_dataset_id],
            "retrieval_mode": "multiple",
            "multiple_retrieval_config": {**retrieval_config, "top_k": 1},
        }, 486))
        nodes.insert(3, _node(mix_id, {
            "type": "code",
            "title": "Build mail query",
            "desc": "Diff text (plus the question) becomes the search query.",
            "code_language": "python3",
            "code": _CODE_MIX,
            "variables": [
                {"variable": "decision", "value_selector": [start_id, "decision"]},
                {"variable": "query", "value_selector": ["sys", "query"]},
                {"variable": "evo", "value_selector": [evo_kr_id, "result"]},
            ],
            "outputs": {
                "result": {"type": "string", "children": None},
                "change_text": {"type": "string", "children": None},
                "display_text": {"type": "string", "children": None},
            },
        }, 562))

    show_id = "answershow1"
    if evolution_dataset_id:
        # Streams the resolved diff text to the user IMMEDIATELY (before the
        # mail retrieval + LLM answer), so picking a decision visibly shows
        # what the change says. Empty when no decision is selected.
        nodes.insert(4, _node(show_id, {
            "type": "answer",
            "title": "Show change",
            "desc": "",
            "answer": f"{{{{#{mix_id}.display_text#}}}}",
        }, 714))
        edges = [
            _edge(start_id, "start", code_id, "code"),
            _edge(code_id, "code", evo_kr_id, "knowledge-retrieval"),
            _edge(evo_kr_id, "knowledge-retrieval", mix_id, "code"),
            _edge(mix_id, "code", show_id, "answer"),
            _edge(show_id, "answer", kr_id, "knowledge-retrieval"),
            _edge(kr_id, "knowledge-retrieval", llm_id, "llm"),
            _edge(llm_id, "llm", answer_id, "answer"),
        ]
    else:
        edges = [
            _edge(start_id, "start", code_id, "code"),
            _edge(code_id, "code", kr_id, "knowledge-retrieval"),
            _edge(kr_id, "knowledge-retrieval", llm_id, "llm"),
            _edge(llm_id, "llm", answer_id, "answer"),
        ]

    dsl = {
        "app": {
            "name": app_name,
            "description": "Pick an imported spec change or type a question; finds the mailing list discussion.",
            "icon": "🔎",
            "icon_background": "#E0F2FE",
            "mode": "advanced-chat",
            "use_icon_as_answer_icon": False,
        },
        "kind": "app",
        "version": "0.6.0",
        "workflow": {
            "conversation_variables": [],
            "environment_variables": [],
            "features": {
                "opening_statement": (
                    "Pick a decision from the form, then click a suggestion below (or type your own question). "
                    "No decision selected? Just ask freely."
                ),
                "suggested_questions": [
                    "Why was this change made?",
                    "Quote what this change says, then summarize the discussion behind it.",
                    "What alternatives or objections were raised?",
                ],
                "suggested_questions_after_answer": {"enabled": True},
                "retriever_resource": {"enabled": True},
                "sensitive_word_avoidance": {"enabled": False},
                "speech_to_text": {"enabled": False},
                "text_to_speech": {"enabled": False, "language": "", "voice": ""},
                "file_upload": {"enabled": False},
            },
            "graph": {"nodes": nodes, "edges": edges},
        },
    }
    return yaml.safe_dump(dsl, allow_unicode=True, sort_keys=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--draft", required=True, help="draft base name for the decision options")
    parser.add_argument("--dataset", required=True, help="mailarchive dataset id to search")
    parser.add_argument("--app-name", default="Decision → Discussion")
    parser.add_argument("--app-id", default=None, help="overwrite this existing app instead of creating a new one")
    parser.add_argument("--evolution-dataset", default=None,
                        help="evolution dataset id; enables real-diff lookup for selected decisions")
    parser.add_argument("--max-options", type=int, default=60)
    args = parser.parse_args()

    from app_factory import create_app

    app = create_app()
    with app.test_request_context():
        from flask_login import login_user

        from extensions.ext_database import db
        from models.account import Account, Tenant
        from models.dataset import Dataset
        from services.app_dsl_service import AppDslService

        account = db.session.query(Account).first()
        account.current_tenant = db.session.query(Tenant).first()
        login_user(account)

        dataset = db.session.query(Dataset).filter_by(id=args.dataset).first()
        if not dataset:
            sys.exit(f"dataset not found: {args.dataset}")
        embedding = (dataset.embedding_model_provider, dataset.embedding_model)

        options = _build_options(args.draft, args.max_options)
        print(f"{len(options)} decision options built from {args.draft}")

        dsl = _build_dsl(args.app_name, options, args.dataset, embedding, args.evolution_dataset)
        result = AppDslService(db.session).import_app(
            account=account,
            import_mode="yaml-content",
            yaml_content=dsl,
            app_id=args.app_id,
        )
        db.session.commit()
        print(f"import status: {result.status}")
        if result.error:
            print(f"error: {result.error}")
        print(f"app id: {result.app_id}")

        # Verify the draft workflow actually matches what we generated.
        # An earlier --app-id refresh once reported "completed" without
        # persisting anything, so never trust the status line alone.
        _verify_draft(result.app_id, dsl)
        print("verified: stored draft matches the generated DSL")
        print("Open Studio -> the app, then Publish (the import only updates the draft).")


def _verify_draft(app_id: str, dsl: str) -> None:
    import json

    from extensions.ext_database import db
    from models.workflow import Workflow

    expected = yaml.safe_load(dsl)["workflow"]
    db.session.expire_all()
    workflow = (
        db.session.query(Workflow).filter_by(app_id=app_id, version="draft").first()
    )
    if workflow is None:
        sys.exit("VERIFICATION FAILED: no draft workflow stored for the app")

    stored_graph = json.loads(workflow.graph)
    stored_features = json.loads(workflow.features)

    expected_ids = {node["id"] for node in expected["graph"]["nodes"]}
    stored_ids = {node["id"] for node in stored_graph.get("nodes", [])}
    problems: list[str] = []
    if not expected_ids <= stored_ids:
        problems.append(f"missing nodes: {sorted(expected_ids - stored_ids)}")

    for key in ("opening_statement", "suggested_questions"):
        if stored_features.get(key) != expected["features"].get(key):
            problems.append(f"feature mismatch: {key}")

    expected_start = next(n for n in expected["graph"]["nodes"] if n["data"]["type"] == "start")
    stored_start = next((n for n in stored_graph.get("nodes", []) if n["data"].get("type") == "start"), None)
    expected_options = expected_start["data"]["variables"][0]["options"]
    stored_options = (stored_start or {}).get("data", {}).get("variables", [{}])[0].get("options")
    if stored_options != expected_options:
        problems.append(
            f"decision options mismatch: stored {len(stored_options or [])}, expected {len(expected_options)}"
        )

    if problems:
        sys.exit("VERIFICATION FAILED: " + "; ".join(problems) + " — re-run the script or check the app in Studio")


if __name__ == "__main__":
    main()
