"""Standalone local web UI for ask_decision_agent.py.

There is no way to test ask_decision_agent.py's actual behavior through
Dify's own Studio -- it bypasses Dify's AgentChatAppRunner entirely (see
ask_decision_agent.py's module docstring for why), so no app record, no
Studio configuration page, exists for it. Studio's agent-chat UI at
/app/<id>/configuration only ever shows sync_decision_agent.py's app,
which is a genuinely different, less capable implementation: 2 fixed
tools instead of 8, hard-wired to one draft, no discovery, no safety
nets, no citations. Testing through that page would show the OLD
behavior this whole effort moved away from, not the current one.

This is a deliberately small, separate Flask app -- not a new route
bolted onto the real Dify api service, which runs via gunicorn with no
hot reload (confirmed: FLASK_DEBUG=false), so new routes there would need
a container restart on every change. It reuses ask_decision_agent.py's
functions directly (same Dify app/DB context via app_factory.create_app())
but serves its own single HTML page and two small JSON endpoints.

Every tool ask_decision_agent.py has is hand-rolled and invisible to
Dify's own tool picker, since the whole agent loop bypasses Dify's
runner -- that's true of all eight of them equally, so the page states
it once (in the sidebar heading) instead of repeating it on every card.
What actually varies per tool is its description, and _TOOL_KIND below:
descriptions come from tool_summaries(), the same strings the model
itself sees when deciding whether to call a tool, so the page can never
drift from what the agent actually operates under.

Run (inside the api container, or via the ietf_ui docker-compose service
that wraps this same command with the right network/env/volumes):

    docker exec -w /app/api docker-api-1 python scripts/agentic_rag/serve_ui.py

Then open http://localhost:8765/ (or whatever port is mapped).
"""

# ruff: noqa: T201 -- stdout output is the point of a CLI tool

import sys
import traceback
from pathlib import Path

sys.path.insert(0, "/app/api")

from app_factory import create_app

# Purely a visual grouping -- unlike the old uniform "custom" badge, this
# actually varies per tool. Descriptions themselves come from
# tool_summaries() at request time, not from a second hand-written copy here.
_TOOL_KIND = {
    "list_available_ietf_corpora": "discovery",
    "search_ietf_evolution_dataset": "retrieval",
    "search_ietf_mailarchive_dataset": "retrieval",
    "fetch_ietf_draft_text": "fetch",
    "fetch_ietf_rfc_text": "fetch",
    "diff_ietf_draft_revisions": "diff",
    "cite_iddiff_url": "diff",
    "verify_answer": "verify",
}


def _tool_info() -> list[dict]:
    from scripts.agentic_rag.ask_decision_agent import tool_summaries

    return [{**t, "kind": _TOOL_KIND.get(t["name"], "other")} for t in tool_summaries()]


_PAGE_HTML = (Path(__file__).parent / "serve_ui_template.html").read_text(encoding="utf-8")


def main() -> None:
    from flask import jsonify, request

    app = create_app()

    @app.route("/", methods=["GET"])
    def index():
        return _PAGE_HTML

    @app.route("/api/tools", methods=["GET"])
    def api_tools():
        return jsonify(_tool_info())

    @app.route("/api/skills", methods=["GET"])
    def api_skills():
        from scripts.agentic_rag.ask_decision_agent import skill_texts

        return jsonify(skill_texts())

    @app.route("/api/corpora", methods=["GET"])
    def api_corpora():
        from scripts.agentic_rag.ask_decision_agent import _list_available_corpora

        try:
            _text, catalog = _list_available_corpora()
            return jsonify({"catalog": catalog})
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/ask", methods=["POST"])
    def api_ask():
        from scripts.agentic_rag.ask_decision_agent import ask as ask_decision_agent

        payload = request.get_json(force=True, silent=True) or {}
        query = (payload.get("query") or "").strip()
        draft = payload.get("draft") or None
        if not query:
            return jsonify({"error": "query is required"}), 400

        trace: list[dict] = []
        try:
            answer = ask_decision_agent(query=query, draft=draft, trace=trace)
            return jsonify({"answer": answer, "trace": trace})
        except SystemExit as exc:
            return jsonify({"error": str(exc), "trace": trace}), 400
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": f"{type(exc).__name__}: {exc}", "trace": trace}), 500

    @app.route("/api/decision-thread", methods=["GET"])
    def api_decision_thread():
        from scripts.agentic_rag.ask_decision_agent import decision_thread

        draft = (request.args.get("draft") or "").strip()
        query = (request.args.get("query") or "").strip()
        if not draft:
            return jsonify({"error": "draft is required"}), 400
        if not query:
            return jsonify({"error": "query is required"}), 400
        try:
            return jsonify(decision_thread(draft, query))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    print("Decision Rationale Agent UI running -- open http://0.0.0.0:8765/ in your browser")
    app.run(host="0.0.0.0", port=8765, debug=False)


if __name__ == "__main__":
    main()
