"""Create/refresh the "UX" chatflow — an interactive-picker variant of
"Decision -> Discussion" (see sync_decision_bot.py for the original,
single-pass design and why it deliberately avoids branches/stored state).

This variant answers a real gap in that design: the original always takes
`cands[0]` from stage-1 retrieval and asks the user to rephrase if it grabbed
the wrong change. Here, stage 1 shows up to the top 10 candidates and waits
for the user to pick one, instead of guessing. This does NOT reintroduce the
old dropdown's problem (a capped, pre-generated list that gates what's
reachable) — retrieval still runs unconstrained on free text every time;
only the disambiguation step becomes interactive.

Two-path graph, one IF-ELSE node deciding which path a given message takes:

    start -> code (detect: is this message a bare number replying to a
                   just-shown option set, or a fresh query?)
          -> if-else
               [true]  code (resolve the stored pick)
                       -> assigner (clear the stored candidates)
                       -> knowledge-retrieval (stage 2: the discussion)
                       -> llm -> code (sources) -> answer (full explanation)
               [false] knowledge-retrieval (stage 1: top-10 candidates)
                       -> code (build the numbered options + encode them)
                       -> assigner (store the candidates on the conversation)
                       -> answer ("which one did you mean?")

The candidates are handed between turns via a conversation variable
(`pending_candidates`), encoded as plain text (header\\u0001body, joined by
\\u0002) rather than JSON — no imports needed in the sandboxed code nodes,
matching this project's existing code-node style.

Run inside the api container; pass --app-id to overwrite in place (import
updates the DRAFT only -- Publish afterwards in Studio):

    docker exec -w /app/api docker-api-1 python scripts/sync_decision_bot_ux.py \\
        --dataset <mailarchive-dataset-id> \\
        --evolution-dataset <evolution-dataset-id> \\
        [--app-name "UX"] [--app-id <existing-app-id>]
"""

# ruff: noqa: T201 — stdout output is the point of a CLI tool

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "/app/api")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sync_decision_bot import (
    _FORMAT_SOURCES,
    _SYSTEM_PROMPT,
    _edge,
    _embedding_of,
    _node,
    _retrieval_config,
    _verify_draft,
)

# Stage-1 -> "is this a pick, or a fresh query?" The only messages treated as
# a pick are a bare positive integer (matched by isdigit(), not a hardcoded
# list — so this stays correct regardless of how many candidates stage 1
# shows) arriving while there's a stored candidate set from the previous
# turn; anything else (including a number typed as part of an unrelated
# search) falls through to a fresh search, since `pending` is only non-empty
# right after we've just shown options.
_DETECT_PICK = '''
def main(query, pending):
    q = (query or "").strip()
    is_pick = "false"
    picked_index = -1
    if q.isdigit() and int(q) >= 1 and pending:
        items = [p for p in pending.split("\\u0002") if p]
        if items:
            is_pick = "true"
            picked_index = min(int(q) - 1, len(items) - 1)
    return {"is_pick": is_pick, "picked_index": picked_index}
'''

# Stage-1 (fresh query) -> top 10 candidates, shown as numbered options and
# encoded onto the conversation variable for the next turn to resolve.
# Marker-parsing logic duplicated from sync_decision_bot.py's _PICK rather
# than imported, since it has to run inside Dify's sandboxed code node as a
# self-contained string, not as a real Python import.
_MAX_CANDIDATES = 10

_PICK_TOP_N = '''
def _parse_marker(line):
    label = ""
    left = line
    if "|" in line:
        left, label = line.split("|", 1)
        label = label.strip()
    fields = {}
    for tok in left.split():
        if ":" in tok:
            k, v = tok.split(":", 1)
            fields[k] = v
    return fields, label


def _fmt_header(fields, label):
    parts = []
    if fields.get("section"):
        parts.append("Section " + fields["section"])
    if fields.get("from") and fields.get("to"):
        parts.append("rev " + fields["from"] + "\\u2192" + fields["to"])
    if fields.get("change"):
        parts.append(fields["change"])
    if fields.get("draft"):
        parts.append(fields["draft"])
    meta = " \\u00b7 ".join(parts)
    if label and meta:
        return meta + " \\u2014 " + label
    return label or meta or "(unlabelled change)"


def _split_change(content):
    lines = content.split("\\n")
    hdr_idx = -1
    for i in range(len(lines)):
        if lines[i].strip().startswith("ietf-evolution"):
            hdr_idx = i
            break
    if hdr_idx >= 0:
        fields, label = _parse_marker(lines[hdr_idx])
        header = _fmt_header(fields, label)
        body = "\\n".join(lines[hdr_idx + 1:]).strip()
    else:
        header = (lines[0].strip() if lines else "")[:120] or "(unlabelled change)"
        body = content.strip()
    return header, body


def main(evo):
    cands = []
    for item in (evo or []):
        content = (item.get("content") if isinstance(item, dict) else "") or ""
        header, body = _split_change(content)
        cands.append({"header": header, "body": body})
    # 10 here must match _MAX_CANDIDATES (and the evolution knowledge-retrieval
    # node's top_k) in the generator script -- this is a plain hardcoded
    # literal, not interpolated, since this whole block is a string embedded
    # in the generated DSL, not live Python the generator script controls at
    # runtime.
    top = cands[:10]
    if not top:
        return {
            "options_text": "No matching draft changes found for that query \\u2014 try different keywords.",
            "candidates_json": "",
        }
    lines = []
    encoded = []
    for i, c in enumerate(top):
        snip = " ".join(c["body"].split())[:220]
        lines.append(str(i + 1) + ". **" + c["header"] + "**\\n   " + snip)
        safe_header = c["header"].replace("\\u0001", " ").replace("\\u0002", " ")
        safe_body = c["body"][:1500].replace("\\u0001", " ").replace("\\u0002", " ")
        encoded.append(safe_header + "\\u0001" + safe_body)
    reply_hint = "1" if len(top) == 1 else ("1 or 2" if len(top) == 2 else "1 to " + str(len(top)))
    footer = "\\n\\n_Reply with " + reply_hint + " \\u2014 or rephrase for different results._"
    options_text = "\\n\\n".join(lines) + footer
    return {"options_text": options_text, "candidates_json": "\\u0002".join(encoded)}
'''

# [true] branch -> decode the stored candidate the user picked, in the same
# shape sync_decision_bot.py's _PICK produces, so the downstream stage-2
# retrieval / llm / answer nodes are unchanged from the original design.
_RESOLVE_PICK = '''
def main(pending, idx, query):
    items = [p for p in (pending or "").split("\\u0002") if p]
    parsed = []
    for it in items:
        if "\\u0001" in it:
            h, b = it.split("\\u0001", 1)
        else:
            h, b = it, ""
        parsed.append({"header": h, "body": b})
    if not parsed:
        return {
            "decision_header": "No stored candidate to resolve \\u2014 please rephrase your query.",
            "decision_excerpt": "",
            "decision_full": "",
            "mail_query": (query or "").strip(),
            "others_text": "",
        }
    i = idx if 0 <= idx < len(parsed) else 0
    top = parsed[i]
    decision_header = top["header"]
    decision_full = top["body"][:1500]
    decision_excerpt = decision_full[:600] + ("\\u2026" if len(top["body"]) > 600 else "")
    mail_query = (top["header"] + " " + decision_full[:600] + " " + (query or "")).strip()
    others = []
    for j, c in enumerate(parsed):
        if j == i:
            continue
        snip = " ".join(c["body"].split())[:220]
        others.append(str(j + 1) + ". **" + c["header"] + "**\\n   " + snip)
    others_text = ("\\n\\n---\\n_Other candidates from this search:_\\n\\n" + "\\n\\n".join(others)) if others else ""
    return {
        "decision_header": decision_header,
        "decision_excerpt": decision_excerpt,
        "decision_full": decision_full,
        "mail_query": mail_query,
        "others_text": others_text,
    }
'''


def _edge_h(source: str, source_type: str, target: str, target_type: str, handle: str) -> dict:
    """Like sync_decision_bot._edge, but for a non-default sourceHandle
    (if-else branches route by case_id, e.g. "true"/"false", not "source")."""
    edge = _edge(source, source_type, target, target_type)
    edge["id"] = f"{source}-{handle}-{target}"
    edge["sourceHandle"] = handle
    return edge


_PENDING_VAR_ID = "6f1b6b1a-6f5a-4a3b-9b1a-0c1e2c9c9b01"


def _build_dsl(
    app_name: str,
    email_dataset_id: str,
    email_embedding: tuple[str, str],
    evolution_dataset_id: str,
    evo_embedding: tuple[str, str],
) -> str:
    start_id = "start1"
    detect_id = "detect1"
    branch_id = "branch1"
    # [true] branch — resolve a stored pick, then the original stage-2 path
    resolve_id = "resolve1"
    clear_id = "clear1"
    emailkr_id = "emailkr2"
    llm_id = "llm2"
    fmtsrc_id = "fmtsrc2"
    answer_final_id = "answerfinal1"
    # [false] branch — fresh query, up to _MAX_CANDIDATES candidates, wait for a pick
    evokr_id = "evokr1"
    toppick_id = "toppick1"
    store_id = "store1"
    answer_options_id = "answeroptions1"

    nodes = [
        _node(start_id, {"type": "start", "title": "Start", "desc": "", "variables": []}, 40, 300),
        _node(detect_id, {
            "type": "code",
            "title": "Detect pick reply",
            "desc": "Is this '1'/'2'/'3' answering the options just shown, or a fresh query?",
            "code_language": "python3",
            "code": _DETECT_PICK,
            "variables": [
                {"variable": "query", "value_selector": ["sys", "query"]},
                {"variable": "pending", "value_selector": ["conversation", "pending_candidates"]},
            ],
            "outputs": {
                "is_pick": {"type": "string", "children": None},
                "picked_index": {"type": "number", "children": None},
            },
        }, 300, 300),
        _node(branch_id, {
            "type": "if-else",
            "title": "Pick reply?",
            "desc": "true: resolve the stored candidate. false: run a fresh search.",
            "cases": [{
                "case_id": "true",
                "id": "true",
                "logical_operator": "and",
                "conditions": [{
                    "id": "is-pick-cond",
                    "varType": "string",
                    "variable_selector": [detect_id, "is_pick"],
                    "comparison_operator": "is",
                    "value": "true",
                }],
            }],
        }, 560, 300),
        # --- [true] branch: resolve the pick, then the original stage-2 path ---
        _node(resolve_id, {
            "type": "code",
            "title": "Resolve picked candidate",
            "desc": "Decodes the stored top-3 and picks the chosen index.",
            "code_language": "python3",
            "code": _RESOLVE_PICK,
            "variables": [
                {"variable": "pending", "value_selector": ["conversation", "pending_candidates"]},
                {"variable": "idx", "value_selector": [detect_id, "picked_index"]},
                {"variable": "query", "value_selector": ["sys", "query"]},
            ],
            "outputs": {
                "decision_header": {"type": "string", "children": None},
                "decision_excerpt": {"type": "string", "children": None},
                "decision_full": {"type": "string", "children": None},
                "mail_query": {"type": "string", "children": None},
                "others_text": {"type": "string", "children": None},
            },
        }, 820, 120),
        _node(clear_id, {
            "type": "assigner",
            "title": "Clear stored candidates",
            "desc": "So a stray later '1' isn't mistaken for a pick against stale options.",
            "assigned_variable_selector": ["conversation", "pending_candidates"],
            "write_mode": "clear",
            "input_variable_selector": ["sys", "query"],
        }, 1080, 120),
        _node(emailkr_id, {
            "type": "knowledge-retrieval",
            "title": "Find the discussion",
            "desc": "Stage 2: retrieve the mailing-list discussion for the picked change.",
            "query_variable_selector": [resolve_id, "mail_query"],
            "dataset_ids": [email_dataset_id],
            "retrieval_mode": "multiple",
            "multiple_retrieval_config": _retrieval_config(email_embedding, 5),
        }, 1340, 120),
        _node(llm_id, {
            "type": "llm",
            "title": "Explain the decision",
            "desc": "",
            "model": {
                "provider": "langgenius/openrouter/openrouter",
                "name": "llama-3.3-70b-instruct",
                "mode": "chat",
                "completion_params": {"max_tokens": 1200, "temperature": 0.3, "top_p": 1},
            },
            "prompt_template": [
                {"role": "system", "text": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "text": (
                        f"Decision (the change to explain):\n{{{{#{resolve_id}.decision_header#}}}}\n\n"
                        f"Change text:\n{{{{#{resolve_id}.decision_full#}}}}"
                    ),
                },
            ],
            "context": {"enabled": True, "variable_selector": [emailkr_id, "result"]},
            "vision": {"enabled": False, "configs": {"variable_selector": []}},
            "retry_config": {"retry_enabled": True, "max_retries": 2, "retry_interval": 8000},
        }, 1640, 120),
        _node(fmtsrc_id, {
            "type": "code",
            "title": "List sources",
            "desc": "Formats the retrieved threads as a source list.",
            "code_language": "python3",
            "code": _FORMAT_SOURCES,
            "variables": [{"variable": "hits", "value_selector": [emailkr_id, "result"]}],
            "outputs": {"sources_text": {"type": "string", "children": None}},
        }, 1940, 120),
        _node(answer_final_id, {
            "type": "answer",
            "title": "Answer (explanation)",
            "desc": "",
            "answer": (
                f"## 🔎 Decision under study\n"
                f"**{{{{#{resolve_id}.decision_header#}}}}**\n\n"
                f"> {{{{#{resolve_id}.decision_excerpt#}}}}\n\n"
                f"---\n\n"
                f"{{{{#{llm_id}.text#}}}}\n\n"
                f"---\n**Sources (retrieved mailing-list discussion):**\n"
                f"{{{{#{fmtsrc_id}.sources_text#}}}}"
                f"{{{{#{resolve_id}.others_text#}}}}"
            ),
        }, 2240, 120),
        # --- [false] branch: fresh query, show top 3, store them ---
        _node(evokr_id, {
            "type": "knowledge-retrieval",
            "title": "Find candidate changes",
            "desc": "Stage 1: keywords retrieve the top matching changes.",
            "query_variable_selector": ["sys", "query"],
            "dataset_ids": [evolution_dataset_id],
            "retrieval_mode": "multiple",
            "multiple_retrieval_config": _retrieval_config(evo_embedding, _MAX_CANDIDATES),
        }, 820, 480),
        _node(toppick_id, {
            "type": "code",
            "title": "Build candidate options",
            "desc": f"Numbers up to the top {_MAX_CANDIDATES} candidates and encodes them for the next turn.",
            "code_language": "python3",
            "code": _PICK_TOP_N,
            "variables": [{"variable": "evo", "value_selector": [evokr_id, "result"]}],
            "outputs": {
                "options_text": {"type": "string", "children": None},
                "candidates_json": {"type": "string", "children": None},
            },
        }, 1080, 480),
        _node(store_id, {
            "type": "assigner",
            "title": "Store candidates",
            "desc": "Saves the candidates so the next message can pick one.",
            "assigned_variable_selector": ["conversation", "pending_candidates"],
            "write_mode": "over-write",
            "input_variable_selector": [toppick_id, "candidates_json"],
        }, 1340, 480),
        _node(answer_options_id, {
            "type": "answer",
            "title": "Answer (options)",
            "desc": "",
            "answer": f"## 🔎 A few possible matches\n\n{{{{#{toppick_id}.options_text#}}}}",
        }, 1640, 480),
    ]

    edges = [
        _edge(start_id, "start", detect_id, "code"),
        _edge(detect_id, "code", branch_id, "if-else"),
        _edge_h(branch_id, "if-else", resolve_id, "code", "true"),
        _edge_h(branch_id, "if-else", evokr_id, "knowledge-retrieval", "false"),
        _edge(resolve_id, "code", clear_id, "assigner"),
        _edge(clear_id, "assigner", emailkr_id, "knowledge-retrieval"),
        _edge(emailkr_id, "knowledge-retrieval", llm_id, "llm"),
        _edge(llm_id, "llm", fmtsrc_id, "code"),
        _edge(fmtsrc_id, "code", answer_final_id, "answer"),
        _edge(evokr_id, "knowledge-retrieval", toppick_id, "code"),
        _edge(toppick_id, "code", store_id, "assigner"),
        _edge(store_id, "assigner", answer_options_id, "answer"),
    ]

    dsl = {
        "app": {
            "name": app_name,
            "description": "Interactive-picker variant: shows the top 3 matching changes and lets you pick one.",
            "icon": "🧭",
            "icon_background": "#FEF3C7",
            "mode": "advanced-chat",
            "use_icon_as_answer_icon": False,
        },
        "kind": "app",
        "version": "0.6.0",
        "workflow": {
            "conversation_variables": [{
                "id": _PENDING_VAR_ID,
                "name": "pending_candidates",
                "selector": ["conversation", "pending_candidates"],
                "value": "",
                "value_type": "string",
                "description": "Encoded top-3 candidates awaiting a 1/2/3 pick (header\\u0001body, joined by \\u0002).",
            }],
            "environment_variables": [],
            "features": {
                "opening_statement": (
                    "Type a few keywords or a short phrase describing the specification "
                    "change you want to understand (e.g. \"base delay late comers\"). "
                    "I'll show the top 3 matching changes — reply with 1, 2, or 3 to "
                    "get the full explanation (**why it was made**, **what it says**, "
                    "**what alternatives or objections were raised**), or just rephrase "
                    "if none of the three are the one you meant."
                ),
                "suggested_questions": [],
                "suggested_questions_after_answer": {"enabled": False},
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
    parser.add_argument("--dataset", required=True, help="mailarchive dataset id (the discussion source)")
    parser.add_argument("--evolution-dataset", required=True,
                        help="evolution dataset id (the candidate changes source)")
    parser.add_argument("--app-name", default="UX")
    parser.add_argument("--app-id", default=None, help="overwrite this existing app instead of creating a new one")
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

        email_embedding = _embedding_of(args.dataset, "mailarchive")
        evo_embedding = _embedding_of(args.evolution_dataset, "evolution")

        dsl = _build_dsl(
            args.app_name,
            args.dataset,
            email_embedding,
            args.evolution_dataset,
            evo_embedding,
        )
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

        _verify_draft(result.app_id, dsl)
        print("verified: stored draft matches the generated DSL")
        print("Open Studio -> the app, then Publish (the import only updates the draft).")


if __name__ == "__main__":
    main()
