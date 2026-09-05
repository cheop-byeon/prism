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
    "search_wg_minutes": "retrieval",
    "get_rfc_relations": "discovery",
    "find_related_rfc_family": "discovery",
    "get_iesg_ballot_comments": "retrieval",
}

# What each tool actually touches outside this script, and a real (not
# fabricated) example call -- shown in the UI's second-level "Detail" panel,
# once someone's already expanded a tool and wants to know how it connects
# to the real world, not just what it does. Unlike _TOOL_KIND/description,
# there's no single-source-of-truth function for this (it's about the
# implementation's own HTTP calls/subprocess use, not the model-facing
# contract), so it's hand-maintained here -- update it if a tool's actual
# resource changes.
_TOOL_DETAILS = {
    "list_available_ietf_corpora": {
        "resource": (
            "IETF Datatracker API (datatracker.ietf.org) -- live lookup of each "
            "discovered draft's owning working group."
        ),
        "example": (
            "list_available_ietf_corpora()\n"
            "# -> catalog of every draft this instance has data for, with dataset ids"
        ),
    },
    "search_ietf_evolution_dataset": {
        "resource": (
            "Internal Dify dataset only -- this project's own precomputed "
            "section-level draft diffs, no external call at query time."
        ),
        "example": (
            'search_ietf_evolution_dataset(\n  dataset_id="cf4328a3-f10e-47f2-b7d6-ef4ad555bce7",\n'
            '  query="Safety of LEDBAT"\n)'
        ),
    },
    "search_ietf_mailarchive_dataset": {
        "resource": (
            "Internal Dify dataset only -- built from the WG's real mailing-list "
            "archive ahead of time, no external call at query time."
        ),
        "example": (
            'search_ietf_mailarchive_dataset(\n  dataset_id="cd7ef874-417e-45a6-a917-461e4cdf20c3",\n'
            '  query="An INIT_CWND of"\n)'
        ),
    },
    "fetch_ietf_draft_text": {
        "resource": "ietf.org draft archive (www.ietf.org/archive/id/) -- live fetch of the actual draft text.",
        "example": 'fetch_ietf_draft_text(\n  draft_name="draft-ietf-ledbat-congestion",\n  revision="09"\n)',
    },
    "fetch_ietf_rfc_text": {
        "resource": "RFC Editor (www.rfc-editor.org) -- live fetch of the final published spec.",
        "example": 'fetch_ietf_rfc_text(rfc_number="6817")',
    },
    "diff_ietf_draft_revisions": {
        "resource": (
            "iddiff (ietf-tools/iddiff on PyPI) -- the same independent diff tool "
            "IETF's own author-tools site runs, executed locally."
        ),
        "example": (
            'diff_ietf_draft_revisions(\n  draft_name="draft-ietf-ledbat-congestion",\n'
            '  from_revision="08",\n  to_revision="09"\n)'
        ),
    },
    "cite_iddiff_url": {
        "resource": (
            "author-tools.ietf.org/iddiff -- IETF's own hosted diff viewer; "
            "builds a real clickable URL, no local computation."
        ),
        "example": (
            'cite_iddiff_url(\n  draft_name="draft-ietf-ledbat-congestion",\n'
            '  from_revision="08",\n  to_revision="09"\n)\n'
            "# -> https://author-tools.ietf.org/iddiff?url1=draft-ietf-ledbat-congestion-08"
            "&url2=draft-ietf-ledbat-congestion-09&difftype=--html"
        ),
    },
    "verify_answer": {
        "resource": (
            "No external resource -- a second, fresh call to the same LLM, "
            "auditing the draft answer against evidence already retrieved."
        ),
        "example": "Runs automatically before every final answer; not something a user calls directly.",
    },
    "search_wg_minutes": {
        "resource": (
            "Local files only -- meeting minutes already pulled via the Sources tab's "
            "\"Add sources\" section, no external call at query time."
        ),
        "example": 'search_wg_minutes(\n  wg="sidr",\n  query="Expire Time"\n)',
    },
    "get_rfc_relations": {
        "resource": (
            "IETF Datatracker API (datatracker.ietf.org) -- live lookup of an RFC's "
            "obsoletes/obsoleted-by and updates/updated-by relationships."
        ),
        "example": (
            'get_rfc_relations(rfc_number="4035")\n# -> obsoletes 9 earlier DNSSEC RFCs, updated by 7 later ones'
        ),
    },
    "find_related_rfc_family": {
        "resource": (
            "IETF Datatracker API (datatracker.ietf.org) -- citation graph + keyword lookup, "
            "several calls per invocation (all citing documents, then batched metadata)."
        ),
        "example": (
            'find_related_rfc_family(rfc_number="4035")\n'
            "# -> 11 DNSSEC-specific RFCs including RFC 9364 (the DNSSEC roadmap), out of 513 total citing docs"
        ),
    },
    "get_iesg_ballot_comments": {
        "resource": (
            "IETF Datatracker API (datatracker.ietf.org) -- the ballotpositiondocevent resource, "
            "which carries the actual DISCUSS/comment text a plain docevent listing doesn't expose."
        ),
        "example": (
            'get_iesg_ballot_comments(\n  draft_name="draft-ietf-sidr-bgpsec-protocol",\n'
            '  query="SKI"\n)\n# -> Stephen Farrell\'s DISCUSS questioning the hardcoded SHA-1 SKI length'
        ),
    },
}


def _tool_info() -> list[dict]:
    from scripts.agentic_rag.ask_decision_agent import tool_display_name, tool_summaries

    return [
        {
            **t,
            "kind": _TOOL_KIND.get(t["name"], "other"),
            "display_name": tool_display_name(t["name"]),
            **_TOOL_DETAILS.get(t["name"], {"resource": "", "example": ""}),
        }
        for t in tool_summaries()
    ]


_PAGE_HTML = (Path(__file__).parent / "serve_ui_template.html").read_text(encoding="utf-8")


def _sse_stream(app, work_fn):
    """Run work_fn(on_event) -> JSON-serializable result in a background
    thread, streaming every on_event(...) call plus the final result (or an
    error) to the browser as Server-Sent Events -- lets a synchronous,
    potentially slow function (an agent loop, a multi-lane LLM judgment
    pass) report live progress without becoming a generator itself.

    Runs in its own thread because Flask's dev server handles one request
    at a time by default; blocking synchronously here would make the whole
    UI unresponsive until work_fn finished, defeating the point of
    streaming. The thread needs its own app-context push since Flask's
    context is thread-local and doesn't cross into a manually spawned one.
    """
    import json
    import queue
    import threading

    from flask import Response

    events: queue.Queue = queue.Queue()
    done = object()

    def on_event(event: dict) -> None:
        events.put(("event", event))

    def runner() -> None:
        try:
            with app.app_context():
                result = work_fn(on_event)
            events.put(("result", result))
        except (SystemExit, Exception) as exc:
            traceback.print_exc()
            events.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            events.put((done, None))

    threading.Thread(target=runner, daemon=True).start()

    def generate():
        while True:
            kind, payload = events.get()
            if kind is done:
                break
            if kind == "event":
                yield f"data: {json.dumps({'type': 'progress', **payload})}\n\n"
            elif kind == "result":
                yield f"data: {json.dumps({'type': 'result', 'data': payload})}\n\n"
            elif kind == "error":
                yield f"data: {json.dumps({'type': 'error', 'message': payload})}\n\n"

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


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

    @app.route("/api/corpora/freshness", methods=["GET"])
    def api_corpora_freshness():
        """Per-corpus live state for the Corpora tab -- what the sidecar/
        Datatracker currently report, side by side with what's registered.
        Read-only: no download, no writing. Each per-corpus lookup is
        wrapped individually so one WG's sidecar hiccup doesn't blank the
        whole table.
        """
        from scripts.agentic_rag.ask_decision_agent import _list_available_corpora
        from services.ietf_import_service import IetfImportService

        try:
            _text, catalog = _list_available_corpora()
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

        corpora = []
        for entry in catalog:
            row = dict(entry)
            wg = entry.get("wg")
            if wg:
                try:
                    row["mail_status"] = IetfImportService.get_mailarchive_sync_status(wg)
                except Exception as exc:
                    row["mail_status"] = {"error": str(exc)}
            draft_name = entry.get("draft_name")
            if draft_name:
                try:
                    row["draft_relations"] = IetfImportService.get_draft_relations(draft_name)
                except Exception as exc:
                    row["draft_relations"] = {"error": str(exc)}
            corpora.append(row)
        return jsonify({"corpora": corpora})

    @app.route("/api/corpora/mail-sync", methods=["POST"])
    def api_corpora_mail_sync():
        """Trigger the sidecar's incremental IMAP sync for one mailing list --
        download only, per the standalone UI's scope: this pulls fresh raw
        mail into the sidecar's own store. Turning that into an updated Dify
        dataset is still the existing manual Studio step.
        """
        from services.ietf_import_service import IetfImportService

        data = request.get_json(silent=True) or {}
        list_name = (data.get("list_name") or "").strip()
        if not list_name:
            return jsonify({"error": "list_name is required"}), 400
        try:
            IetfImportService.trigger_mailarchive_sync(list_name)
            status = IetfImportService.get_mailarchive_sync_status(list_name)
            return jsonify(status)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/corpora/draft-download", methods=["POST"])
    def api_corpora_draft_download():
        """Download one draft revision's raw text to local storage -- download
        only, same scope as mail-sync above: attaching/indexing it as a
        dataset document is still the existing manual Studio step.
        """
        from services.ietf_import_service import IetfImportService

        data = request.get_json(silent=True) or {}
        draft_name = (data.get("draft_name") or "").strip()
        revision = (data.get("revision") or "").strip()
        if not draft_name or not revision:
            return jsonify({"error": "draft_name and revision are required"}), 400
        full_name = f"{draft_name}-{revision.zfill(2)}"
        try:
            result, status_code = IetfImportService.download_drafts_to_local(
                {"draft_names": [full_name], "format": "txt"}
            )
            return jsonify(result), status_code
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    # Every non-draft/RFC/mailarchive Datatracker doctype -- shown in the
    # Sources tab's "Add sources" section per WG. Listed here (not derived
    # from IetfImportService) purely for display order/labels; the actual
    # listing and download both go through Datatracker's own type-parameterized
    # API, so adding a row here is the only step needed to surface a new type.
    _WG_RESOURCE_TYPES = [
        ("agenda", "Agenda"),
        ("minutes", "Minutes"),
        ("narrativeminutes", "Narrative Minutes"),
        ("bluesheets", "Bluesheets"),
        ("slides", "Slides"),
        ("chatlog", "Chat Log"),
        ("polls", "Polls"),
        ("procmaterials", "Proceedings Materials"),
        ("recording", "Recording"),
        ("charter", "Charter"),
        ("conflrev", "Conflict Review"),
        ("bofreq", "BOF Request"),
        ("statchg", "Status Change"),
        ("review", "Review"),
        ("statement", "Statement"),
    ]
    # Downloading every document of a type in one click could mean hundreds
    # of sequential requests to ietf.org (337 slide decks for one old WG,
    # seen while designing this) -- capped per click rather than uncapped,
    # with the listing still reporting the true total so the UI can show
    # "downloaded 30 of 337" instead of silently truncating.
    _WG_RESOURCE_DOWNLOAD_CAP = 30

    @app.route("/api/sources/types", methods=["GET"])
    def api_sources_types():
        return jsonify({"types": [{"key": key, "label": label} for key, label in _WG_RESOURCE_TYPES]})

    @app.route("/api/sources/<wg>/<doc_type>", methods=["GET"])
    def api_sources_list(wg, doc_type):
        from services.ietf_import_service import IetfImportService

        try:
            return jsonify(IetfImportService.list_wg_resources(wg, doc_type))
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/sources/<wg>/<doc_type>/download", methods=["GET"])
    def api_sources_download(wg, doc_type):
        """Download every (capped) document of one type for a WG -- download
        only, same scope as the rest of this tab: files land under
        storage/ietf-downloads/resources/, recordings come back as a link
        instead of a downloaded file, and attaching/indexing anything into a
        Dify dataset is still a separate, manual Studio step.
        """
        from services.ietf_import_service import IetfImportService

        def work(on_event):
            listing = IetfImportService.list_wg_resources(wg, doc_type)
            documents = listing.get("documents", [])[:_WG_RESOURCE_DOWNLOAD_CAP]
            results = []
            for i, doc in enumerate(documents, start=1):
                name = doc.get("name", "")
                on_event({"stage": "downloading", "message": f"{name} ({i}/{len(documents)})"})
                try:
                    outcome = IetfImportService.download_wg_resource_to_local(doc_type, doc)
                    results.append({**outcome, "ok": True})
                except Exception as exc:
                    results.append({"name": name, "ok": False, "error": str(exc)})
            return {"results": results, "total_available": listing.get("count", len(results))}

        return _sse_stream(app, work)

    @app.route("/api/rfc-graph/<rfc_number>", methods=["GET"])
    def api_rfc_graph(rfc_number):
        """Consolidated {nodes, edges} graph for the RFC Graph tab's table
        and interactive graph -- read-only, no download/attach, same as
        every other Datatracker-backed lookup in this UI.
        """
        from services.ietf_import_service import IetfImportService

        try:
            result = IetfImportService.build_rfc_relationship_graph(rfc_number)
            if result.get("error"):
                return jsonify(result), 502
            return jsonify(result)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/rfc-errata/<rfc_number>", methods=["GET"])
    def api_rfc_errata(rfc_number):
        """Errata for one RFC -- node detail, fetched on demand when a
        graph node is clicked, not part of the graph payload itself.
        """
        from services.ietf_import_service import IetfImportService

        try:
            return jsonify({"errata": IetfImportService.get_rfc_errata(rfc_number)})
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/ask", methods=["GET"])
    def api_ask():
        from scripts.agentic_rag.ask_decision_agent import ask as ask_decision_agent

        query = (request.args.get("query") or "").strip()
        draft = request.args.get("draft") or None
        if not query:
            return jsonify({"error": "query is required"}), 400

        def work(on_event):
            trace: list[dict] = []
            answer = ask_decision_agent(query=query, draft=draft, trace=trace, on_event=on_event)
            return {"answer": answer, "trace": trace}

        return _sse_stream(app, work)

    @app.route("/api/deep-dive", methods=["GET"])
    def api_deep_dive():
        """Same agent/tools/endpoint shape as /api/ask, but a genuinely more
        thorough pass -- more iterations budgeted, and an addendum telling
        the model to verify against primary sources and try a second query
        rather than settle for the first result. Not just a different
        render of the same events (that's what Investigate already is).
        """
        from scripts.agentic_rag.ask_decision_agent import _DEEP_DIVE_ADDENDUM, _DEEP_DIVE_MAX_ITERATIONS
        from scripts.agentic_rag.ask_decision_agent import ask as ask_decision_agent

        query = (request.args.get("query") or "").strip()
        draft = request.args.get("draft") or None
        if not query:
            return jsonify({"error": "query is required"}), 400

        def work(on_event):
            trace: list[dict] = []
            answer = ask_decision_agent(
                query=query,
                draft=draft,
                trace=trace,
                on_event=on_event,
                max_iterations=_DEEP_DIVE_MAX_ITERATIONS,
                extra_instruction=_DEEP_DIVE_ADDENDUM,
            )
            return {"answer": answer, "trace": trace}

        return _sse_stream(app, work)

    @app.route("/api/decision-thread", methods=["GET"])
    def api_decision_thread():
        from scripts.agentic_rag.ask_decision_agent import decision_thread

        draft = (request.args.get("draft") or "").strip() or None
        query = (request.args.get("query") or "").strip()
        if not query:
            return jsonify({"error": "query is required"}), 400

        def work(on_event):
            return decision_thread(draft, query, on_event=on_event)

        return _sse_stream(app, work)

    print("Decision Rationale Agent UI running -- open http://0.0.0.0:8765/ in your browser")
    # threaded=True: an open SSE stream (ask/decision-thread) holds its
    # connection for as long as the background work takes -- without this,
    # Werkzeug's dev server would block every other request (even loading
    # the sidebar's /api/tools) until that one stream finished.
    app.run(host="0.0.0.0", port=8765, debug=False, threaded=True)


if __name__ == "__main__":
    main()
