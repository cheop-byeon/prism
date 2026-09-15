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
    "search_rfc_corpus_bm25": "discovery",
    "find_similar_rfcs_by_meaning": "discovery",
    "get_iesg_ballot_comments": "retrieval",
    "search_local_source": "retrieval",
    "add_local_source": "action",
}

# Two different questions were being flattened into one flat list: what
# domain a tool touches (_TOOL_KIND above) versus what ROLE it plays in the
# agent's reasoning. Every entry here is a genuine external capability
# (fetch/search/query something outside the model) except verify_answer,
# which is a guardrail -- it doesn't reach outside the conversation at all,
# it re-invokes the same LLM to audit the draft answer against evidence
# already retrieved. Grouping them separately in the UI says that
# distinction out loud instead of listing a grounding check next to
# "fetch RFC text" as if they were the same kind of thing.
_TOOL_GROUP = {
    "verify_answer": "guardrail",
}

# Tools with a safety-net floor (ask()'s _force_tool_call) that invokes
# them even if the model doesn't choose to -- not purely agent-driven, a
# hybrid with a hardcoded minimum. Surfaced in the UI so "how agentic is
# this really" has an honest answer instead of every tool looking equally
# optional.
_TOOL_FORCIBLE = {
    "list_available_ietf_corpora",
    "search_ietf_mailarchive_dataset",
    "verify_answer",
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
    "search_rfc_corpus_bm25": {
        "resource": (
            "Local BM25 index over the whole published RFC corpus (~9,930 RFCs' title+abstract, "
            "built once via build_rfc_corpus_index.py and cached to disk) -- no external call at "
            "query time. A 'full_text' scope also exists but needs its own separate, much larger "
            "one-time index build first."
        ),
        "example": (
            'search_rfc_corpus_bm25(query="RFC6378", top_k=8)\n'
            "# -> RFC7324, RFC5654, RFC6372, RFC5921... real MPLS-TP linear-protection relatives that\n"
            "# find_related_rfc_family missed entirely (its exact-keyword match required the literal\n"
            "# text \"PSC\" -- RFC 6378's own internal acronym -- in each candidate's title)"
        ),
    },
    "find_similar_rfcs_by_meaning": {
        "resource": (
            "Local embedding index (rfc-dralign-q8 via Ollama on the host machine, title+abstract "
            "for the whole corpus, built once via build_rfc_corpus_index.py) -- no external call at "
            "query time beyond embedding the query itself."
        ),
        "example": (
            'find_similar_rfcs_by_meaning(query="RFC6378", top_k=8)\n'
            "# -> ranks the corpus by meaning, not exact wording -- catches relatives that use\n"
            "# different terminology than either RFC 6378's own keywords or BM25's term overlap would"
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
    "search_local_source": {
        "resource": (
            "Local BM25 index built from one Sources-tab local source's own downloaded files -- draft/"
            "RFC text plus its WG's threaded mail (services/local_source_index_service.py), built via "
            "the Sources tab's \"Build index\" button. No external call at query time; no real Dify "
            "dataset involved at all."
        ),
        "example": (
            'search_local_source(\n  draft_name="draft-ietf-quic-transport",\n'
            '  query="stream priority"\n)\n# -> ranked mail threads and draft/RFC text from that '
            "source's own local index, e.g. the \"HTTP/3 with strict priorities\" thread"
        ),
    },
    "add_local_source": {
        "resource": (
            "IETF Datatracker + ietf.org archive + WG mailing list, all live -- registers the source "
            "(IetfImportService.register_local_source), downloads every revision, its RFC, and its WG's "
            "mail (raw + threaded), then builds a local BM25 index over all of it. The only tool here "
            "that writes to disk or costs real time -- everything else is a read-only lookup."
        ),
        "example": (
            'add_local_source(identifier="4035")\n'
            "# -> resolves RFC4035 to draft-ietf-dnsext-dnssec-protocol, pulls its revisions + RFC + "
            "dnsext WG mail,\n# indexes all of it, ready for search_local_source in the same turn"
        ),
    },
}


def _tool_info() -> list[dict]:
    from scripts.agentic_rag.ask_decision_agent import tool_display_name, tool_summaries

    return [
        {
            **t,
            "kind": _TOOL_KIND.get(t["name"], "other"),
            "group": _TOOL_GROUP.get(t["name"], "capability"),
            "forced": t["name"] in _TOOL_FORCIBLE,
            "display_name": tool_display_name(t["name"]),
            **_TOOL_DETAILS.get(t["name"], {"resource": "", "example": ""}),
        }
        for t in tool_summaries()
    ]


_PAGE_HTML = (Path(__file__).parent / "serve_ui_template.html").read_text(encoding="utf-8")

# The whole-corpus obsoletes/updates graph takes ~2 minutes to build (a few
# dozen paginated relation fetches plus ~80 batched metadata lookups) but
# never changes within a container's lifetime -- cached here so only the
# first request pays that cost; every request after returns instantly.
_global_graph_cache: dict | None = None


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

        Also merges in locally-tracked sources (see IetfImportService.
        register_local_source): drafts added from this tab that don't have a
        Dify dataset yet, so they render as their own unit -- same mail-
        status/draft-relations facets as a real corpus, resolved live the
        same way, just flagged `is_local` so the frontend can add the
        raw-download actions a real corpus doesn't need (it already has an
        indexed dataset; a local source only has whatever's on disk).
        """
        from scripts.agentic_rag.ask_decision_agent import _list_available_corpora
        from services.ietf_import_service import IetfImportService

        try:
            _text, catalog = _list_available_corpora()
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

        registered_names = {entry["draft_name"] for entry in catalog}
        entries = list(catalog)
        for draft_name in IetfImportService.list_local_source_names():
            if draft_name in registered_names:
                continue
            entries.append(
                {
                    "draft_name": draft_name,
                    "rfc_number": IetfImportService.resolve_rfc_number_for_draft(draft_name),
                    "wg": IetfImportService.resolve_wg_for_draft(draft_name),
                    "evolution_dataset_id": None,
                    "mailarchive_dataset_id": None,
                    "is_local": True,
                }
            )

        corpora = []
        for entry in entries:
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
            if entry.get("is_local"):
                try:
                    row["local_status"] = IetfImportService.get_local_download_status(
                        draft_name, wg, entry.get("rfc_number")
                    )
                except Exception as exc:
                    row["local_status"] = {"error": str(exc)}
                try:
                    from services.local_source_index_service import LocalSourceIndexService

                    row["index_status"] = LocalSourceIndexService.get_index_status(draft_name)
                except Exception as exc:
                    row["index_status"] = {"error": str(exc)}
            corpora.append(row)
        return jsonify({"corpora": corpora})

    @app.route("/api/corpora/rfc-suggestions", methods=["GET"])
    def api_corpora_rfc_suggestions():
        """Type-ahead suggestions for "Add a new draft as a source" -- the
        exact same IetfImportService.get_rfc_suggestions the Studio dataset-
        creation IETF importer popup already uses for its own RFC/draft
        search box (see web/app/components/datasets/create/ietf-importer),
        reused here rather than building a second, parallel lookup. Matches
        that popup's own debounce/query behavior: an empty query still
        returns a seed list, narrowing as the caller types a keyword.
        """
        from services.ietf_import_service import IetfImportService

        query = (request.args.get("query") or "").strip()
        try:
            limit = int(request.args.get("limit", "20"))
        except ValueError:
            limit = 20
        limit = max(1, min(50, limit))
        try:
            suggestions = IetfImportService.get_rfc_suggestions(query=query, limit=limit)
            return jsonify({"suggestions": suggestions})
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/corpora/mailing-lists", methods=["GET"])
    def api_corpora_mailing_lists():
        """Every known IETF mailing list name -- the exact same
        IetfImportService.get_mailing_lists the Studio importer popup uses
        for its own WG/mailing-list dropdown. A plain list (no per-entry
        live lookups, unlike rfc-suggestions), so the frontend fetches it
        once and filters client-side as the caller types -- same as the
        popup does.
        """
        from services.ietf_import_service import IetfImportService

        try:
            return jsonify({"lists": IetfImportService.get_mailing_lists()})
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/corpora/wg-documents", methods=["GET"])
    def api_corpora_wg_documents():
        """Every Internet-Draft (published or not) for one WG -- the exact
        same IetfImportService.get_wg_documents the Studio importer popup
        uses for its own "pick a specific draft/RFC in this WG" picker, once
        a WG is chosen. A single paginated Datatracker document-listing call,
        not the per-entry-expensive path rfc-suggestions has -- reliable for
        drilling into a WG's full draft list, including ones never published
        as an RFC (rfc_number comes back null for those).
        """
        from services.ietf_import_service import IetfImportService

        wg = (request.args.get("wg") or "").strip()
        if not wg:
            return jsonify({"error": "wg is required"}), 400
        try:
            return jsonify({"documents": IetfImportService.get_wg_documents(wg)})
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

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

    @app.route("/api/corpora/draft-download-all", methods=["GET"])
    def api_corpora_draft_download_all():
        """Download every existing revision of one draft (plus its published
        RFC, if any) in a single action -- the bulk counterpart to
        draft-download above. Streamed via SSE since a well-revised draft can
        mean 20-30+ sequential archive fetches, worth showing progress for
        rather than leaving the button looking stuck.
        """
        from services.ietf_import_service import IetfImportService

        draft_name = (request.args.get("draft_name") or "").strip()
        if not draft_name:
            return jsonify({"error": "draft_name is required"}), 400

        def work(on_event):
            return IetfImportService.download_all_draft_revisions_to_local(draft_name, on_event=on_event)

        return _sse_stream(app, work)

    @app.route("/api/corpora/local-sync", methods=["GET"])
    def api_corpora_local_sync():
        """The single "Sync" action for a local source's own unit card:
        draft revisions + RFC (idempotent -- only what's missing gets
        fetched) plus its WG's mail, both raw and threaded, all in one
        call. Replaces three separate buttons (draft-download-all,
        mailarchive-download, mailarchive-thread-download) that turned out
        to be more granularity than useful -- a local source realistically
        has exactly two things you'd ever want to do with it: get current
        (this), or make it searchable (local-index below).
        """
        from services.ietf_import_service import IetfImportService

        draft_name = (request.args.get("draft_name") or "").strip()
        if not draft_name:
            return jsonify({"error": "draft_name is required"}), 400

        def work(on_event):
            return IetfImportService.sync_local_source(draft_name, on_event=on_event)

        return _sse_stream(app, work)

    @app.route("/api/corpora/local-index", methods=["POST"])
    def api_corpora_local_index():
        """Build (or rebuild) a local BM25 index over one source's own
        downloaded content -- draft/RFC text plus threaded mail, whatever's
        actually on disk for it. See LocalSourceIndexService; this is a
        from-scratch rebuild every time (cheap -- a handful of local text
        files, not a whole corpus), not an incremental update.
        """
        from services.ietf_import_service import IetfImportService
        from services.local_source_index_service import LocalSourceIndexService

        data = request.get_json(silent=True) or {}
        draft_name = (data.get("draft_name") or "").strip()
        if not draft_name:
            return jsonify({"error": "draft_name is required"}), 400
        try:
            wg = IetfImportService.resolve_wg_for_draft(draft_name)
            rfc_number = IetfImportService.resolve_rfc_number_for_draft(draft_name)
            result = LocalSourceIndexService.build_index(draft_name, wg, rfc_number)
            if result.get("error"):
                return jsonify(result), 400
            return jsonify(result)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/corpora/topic-discover", methods=["GET"])
    def api_corpora_topic_discover():
        """Discover RFCs for a bare topic phrase (e.g. "DNSSEC") -- no LLM
        needed for the base case: BM25 + semantic search (see
        RfcCorpusIndexService) over the whole published-RFC corpus, merged
        and deduped. On top of that, one small, best-effort LLM check asks
        whether any of the top candidates is itself a roadmap/overview
        document that names and describes a family of RFCs for this topic
        (RFC9364 for DNSSEC is the canonical example) -- if so, a second
        call extracts that family with each member's role, which is far
        more useful than a bare ranked title list. Both calls degrade
        silently to the plain merged results on any failure (see
        services/rfc_family_roadmap_service.py) -- a topic search should
        never come back empty just because the roadmap-detection step had
        a bad day.
        """
        from scripts.agentic_rag.ask_decision_agent import _list_available_corpora
        from services.ietf_import_service import IetfImportService
        from services.rfc_corpus_index_service import RfcCorpusIndexService
        from services.rfc_family_roadmap_service import detect_roadmap_candidate, extract_family_from_roadmap

        query = (request.args.get("query") or "").strip()
        if not query:
            return jsonify({"error": "query is required"}), 400
        try:
            top_k = int(request.args.get("top_k", "20"))
        except ValueError:
            top_k = 20
        top_k = max(1, min(50, top_k))

        try:
            bm25 = RfcCorpusIndexService.search_bm25(query, scope="title_abstract", top_k=top_k)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500
        if bm25.get("error"):
            return jsonify({"error": bm25["error"]}), 500

        merged: dict[str, dict] = {}
        for r in bm25.get("results", []):
            merged[r["rfc"]] = {"rfc": r["rfc"].removeprefix("RFC"), "title": r["title"], "score": r["score"]}
        try:
            semantic = RfcCorpusIndexService.search_semantic(query, top_k=top_k)
            if not semantic.get("error"):
                for r in semantic.get("results", []):
                    if r["rfc"] not in merged:
                        merged[r["rfc"]] = {
                            "rfc": r["rfc"].removeprefix("RFC"),
                            "title": r["title"],
                            "score": r["similarity"],
                        }
        except Exception:
            # the embedding index is a separate, optional one-time build -- BM25 alone is fine without it
            traceback.print_exc()

        import operator

        ranked = sorted(merged.values(), key=operator.itemgetter("score"), reverse=True)[:top_k]

        # Known RFC numbers this instance already has -- real corpora and
        # already-added local sources alike -- so the UI can badge/pre-uncheck
        # them instead of offering to "add" something already tracked.
        _text, catalog = _list_available_corpora()
        known_rfc_numbers = {str(entry["rfc_number"]) for entry in catalog if entry.get("rfc_number")}
        for draft_name in IetfImportService.list_local_source_names():
            rfc_number = IetfImportService.resolve_rfc_number_for_draft(draft_name)
            if rfc_number:
                known_rfc_numbers.add(str(rfc_number))

        # The roadmap RFC itself doesn't necessarily rank near the top on
        # pure keyword score -- confirmed live: RFC9364 ("DNS Security
        # Extensions (DNSSEC)") ranked 11th for the bare query "DNSSEC",
        # just outside an earlier top-10 cutoff, since its title/abstract
        # reads as a general overview rather than packing in the
        # DNSSEC-specific compound terms narrower specs use. So the whole
        # ranked set (already capped at top_k) goes to roadmap detection,
        # not a further-truncated slice of it.
        title_abstract_corpus = RfcCorpusIndexService.build_title_abstract_corpus()
        for c in ranked:
            c["abstract"] = title_abstract_corpus.get(f"RFC{c['rfc']}", {}).get("abstract", "")

        roadmap = None
        try:
            model_instance = _demo_model_instance()
            roadmap_rfc = detect_roadmap_candidate(query, ranked, model_instance)
            if roadmap_rfc:
                family = extract_family_from_roadmap(roadmap_rfc, model_instance)
                if family:
                    for member in family:
                        member["already_local"] = member["rfc"] in known_rfc_numbers
                    roadmap = {
                        "rfc": roadmap_rfc,
                        "title": title_abstract_corpus.get(f"RFC{roadmap_rfc}", {}).get("title", ""),
                        "family": family,
                    }
        except Exception:
            traceback.print_exc()  # best-effort enhancement -- fall through to the plain ranked list

        family_rfcs = {m["rfc"] for m in roadmap["family"]} if roadmap else set()
        other = [
            {
                "rfc": c["rfc"],
                "title": c["title"],
                "score": round(c["score"], 3),
                "already_local": c["rfc"] in known_rfc_numbers,
            }
            for c in ranked
            if c["rfc"] not in family_rfcs and (not roadmap or c["rfc"] != roadmap["rfc"])
        ]

        return jsonify({"topic": query, "roadmap": roadmap, "other": other})

    @app.route("/api/corpora/topic-pull", methods=["GET"])
    def api_corpora_topic_pull():
        """Pull a user-selected list of RFC numbers (from topic-discover)
        one at a time -- each resolved to its draft name, synced (revisions
        + RFC + WG mail, raw and threaded), and indexed. The same
        primitives add_local_source gives the agent, just user-selected via
        the Sources tab instead of agent-selected in conversation -- a bulk
        pull should always go through an explicit selection step first,
        never fire unconfirmed (see add_local_source's own docstring on why
        each pull is a real, moderately expensive operation).
        """
        from services.ietf_import_service import IetfImportService
        from services.local_source_index_service import LocalSourceIndexService

        rfc_numbers_param = (request.args.get("rfc_numbers") or "").strip()
        if not rfc_numbers_param:
            return jsonify({"error": "rfc_numbers is required"}), 400
        rfc_numbers = [n.strip() for n in rfc_numbers_param.split(",") if n.strip()]

        def work(on_event):
            results = []
            for i, rfc_number in enumerate(rfc_numbers, start=1):
                on_event({"stage": "pulling", "message": f"RFC{rfc_number} ({i}/{len(rfc_numbers)})"})
                draft_name = IetfImportService.resolve_draft_name_for_rfc(rfc_number)
                if not draft_name:
                    results.append({"rfc": rfc_number, "ok": False, "error": "could not resolve a draft name"})
                    continue
                sync_result = IetfImportService.sync_local_source(draft_name)
                if sync_result.get("error"):
                    results.append(
                        {"rfc": rfc_number, "draft_name": draft_name, "ok": False, "error": sync_result["error"]}
                    )
                    continue
                wg = sync_result.get("wg")
                index_result = LocalSourceIndexService.build_index(draft_name, wg, rfc_number)
                results.append(
                    {
                        "rfc": rfc_number,
                        "draft_name": draft_name,
                        "ok": True,
                        "revisions": sum(1 for r in sync_result.get("revisions", []) if r.get("ok")),
                        "mail_ok": bool((sync_result.get("mail") or {}).get("ok")),
                        "indexed": index_result.get("total_documents", 0),
                    }
                )
            return {"results": results}

        return _sse_stream(app, work)

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

    @app.route("/api/rfc-graph/global", methods=["GET"])
    def api_rfc_graph_global():
        """The whole-corpus obsoletes/updates graph -- the RFC Graph tab's
        default "overall picture" view. Streamed via SSE since a cold build
        takes ~2 minutes; cached at module level (see _global_graph_cache)
        so it only runs once per container lifetime. Registered as a static
        path so Werkzeug matches it before the dynamic
        /api/rfc-graph/<rfc_number> route below, regardless of registration
        order.
        """
        from services.ietf_import_service import IetfImportService

        def work(on_event):
            global _global_graph_cache
            if _global_graph_cache is not None:
                on_event({"stage": "building", "message": "Using cached graph…"})
                return _global_graph_cache
            result = IetfImportService.get_global_structural_graph(on_event=on_event)
            if not result.get("error"):
                _global_graph_cache = result
            return result

        return _sse_stream(app, work)

    @app.route("/api/rfc-graph/focus", methods=["GET"])
    def api_rfc_graph_focus():
        """The RFC Graph tab's Focus mode -- an N-hop neighborhood (1-3
        hops, obsoletes/updates/family all included) around one RFC,
        streamed via SSE since a hop-3 expansion on a well-connected RFC can
        take a while (each hop's per-node fetch is itself uncapped in count,
        just bounded in time -- see build_focus_neighborhood). Registered as
        a static path for the same Werkzeug-ordering reason as
        /api/rfc-graph/global above.
        """
        from services.ietf_import_service import IetfImportService

        rfc_number = (request.args.get("rfc") or "").strip()
        if not rfc_number:
            return jsonify({"error": "rfc is required"}), 400
        try:
            max_hops = int(request.args.get("hops", "2"))
        except ValueError:
            max_hops = 2
        max_hops = max(1, min(3, max_hops))

        def work(on_event):
            return IetfImportService.build_focus_neighborhood(rfc_number, max_hops=max_hops, on_event=on_event)

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

    def _make_research_work(*, tier, query, draft, session_id, max_iterations=None, extra_instruction=None):
        """Shared body for /api/ask, /api/deep-dive, /api/panorama: resumes
        (or creates) an AgenticRagSession, splices its prior turns in as
        history, runs the agent, then persists the new turn -- so any of
        the three tiers can continue the same saved conversation.
        """
        from models.agentic_rag import AgenticRagTier
        from scripts.agentic_rag.ask_decision_agent import ask as ask_decision_agent
        from services.agentic_rag_session_service import AgenticRagSessionService

        def work(on_event):
            nonlocal session_id
            session = AgenticRagSessionService.get_session(session_id) if session_id else None
            if session is None:
                session = AgenticRagSessionService.create_session(title_seed=query)
                session_id = session.id
            history = AgenticRagSessionService.get_history_for_prompt(session_id)

            trace: list[dict] = []
            answer = ask_decision_agent(
                query=query,
                draft=draft,
                trace=trace,
                on_event=on_event,
                max_iterations=max_iterations,
                extra_instruction=extra_instruction,
                history=[{"query": t["query"], "answer": t["answer"]} for t in history],
            )
            AgenticRagSessionService.append_turn(
                session_id=session_id,
                tier=AgenticRagTier(tier),
                query=query,
                answer=answer,
                trace=trace,
            )
            return {"answer": answer, "trace": trace, "session_id": session_id}

        return work

    @app.route("/api/ask", methods=["GET"])
    def api_ask():
        query = (request.args.get("query") or "").strip()
        draft = request.args.get("draft") or None
        session_id = request.args.get("session_id") or None
        # Investigate is a different live-reasoning render of this same
        # endpoint, not a separate route -- the tier tag persisted with the
        # turn still needs to tell the two apart, so the caller passes it.
        tier = request.args.get("tier") or "ask"
        if tier not in ("ask", "investigate"):
            tier = "ask"
        if not query:
            return jsonify({"error": "query is required"}), 400

        return _sse_stream(app, _make_research_work(tier=tier, query=query, draft=draft, session_id=session_id))

    @app.route("/api/deep-dive", methods=["GET"])
    def api_deep_dive():
        """Same agent/tools/endpoint shape as /api/ask, but a genuinely more
        thorough pass -- more iterations budgeted, and an addendum telling
        the model to verify against primary sources and try a second query
        rather than settle for the first result. Not just a different
        render of the same events (that's what Investigate already is).
        """
        from scripts.agentic_rag.ask_decision_agent import _DEEP_DIVE_ADDENDUM, _DEEP_DIVE_MAX_ITERATIONS

        query = (request.args.get("query") or "").strip()
        draft = request.args.get("draft") or None
        session_id = request.args.get("session_id") or None
        if not query:
            return jsonify({"error": "query is required"}), 400

        return _sse_stream(
            app,
            _make_research_work(
                tier="deepdive",
                query=query,
                draft=draft,
                session_id=session_id,
                max_iterations=_DEEP_DIVE_MAX_ITERATIONS,
                extra_instruction=_DEEP_DIVE_ADDENDUM,
            ),
        )

    @app.route("/api/panorama", methods=["GET"])
    def api_panorama():
        """Same agent/tools/endpoint shape as /api/ask and /api/deep-dive,
        but a separate third tier pointed outward at the wider RFC
        landscape -- deliberately not a modification of Deep Dive (which
        stays exactly as it was, verified working for single-RFC depth) so
        this untested new direction (finding and connecting SEVERAL RFCs)
        can be iterated on independently.
        """
        from scripts.agentic_rag.ask_decision_agent import _PANORAMA_ADDENDUM, _PANORAMA_MAX_ITERATIONS

        query = (request.args.get("query") or "").strip()
        draft = request.args.get("draft") or None
        session_id = request.args.get("session_id") or None
        if not query:
            return jsonify({"error": "query is required"}), 400

        return _sse_stream(
            app,
            _make_research_work(
                tier="panorama",
                query=query,
                draft=draft,
                session_id=session_id,
                max_iterations=_PANORAMA_MAX_ITERATIONS,
                extra_instruction=_PANORAMA_ADDENDUM,
            ),
        )

    @app.route("/api/sessions", methods=["GET"])
    def api_sessions_list():
        from services.agentic_rag_session_service import AgenticRagSessionService

        return jsonify({"sessions": AgenticRagSessionService.list_sessions()})

    @app.route("/api/sessions/<session_id>", methods=["GET"])
    def api_sessions_get(session_id):
        from services.agentic_rag_session_service import AgenticRagSessionService

        session = AgenticRagSessionService.get_session(session_id)
        if session is None:
            return jsonify({"error": "session not found"}), 404
        turns = AgenticRagSessionService.get_turns(session_id)
        return jsonify({"id": session.id, "title": session.title, "turns": turns})

    @app.route("/api/sessions/<session_id>", methods=["DELETE"])
    def api_sessions_delete(session_id):
        from services.agentic_rag_session_service import AgenticRagSessionService

        if not AgenticRagSessionService.delete_session(session_id):
            return jsonify({"error": "session not found"}), 404
        return jsonify({"ok": True})

    def _demo_model_instance():
        """Same minimal model-instance setup ask() uses when there's no
        dataset context to derive a tenant from -- this demo never touches
        a real Dify dataset at all, so it always falls back to the first
        tenant, same as ask() does in that situation.
        """
        from core.model_manager import ModelManager
        from dify_graph.model_runtime.entities.model_entities import ModelType
        from extensions.ext_database import db
        from models.account import Tenant

        tenant_id = db.session.query(Tenant).first().id
        return ModelManager().get_model_instance(
            tenant_id=tenant_id,
            provider="langgenius/openrouter/openrouter",
            model_type=ModelType.LLM,
            model="google/gemini-2.5-flash",
        )

    @app.route("/api/family-search-demo/roots", methods=["GET"])
    def api_family_search_demo_roots():
        from services.family_search_demo_service import get_demo_roots

        return jsonify(get_demo_roots())

    @app.route("/api/family-search-demo/check-root", methods=["GET"])
    def api_family_search_demo_check_root():
        """First hop: check the root's own (fake) mail archive; if empty,
        rank its relatives once and hand back the whole queue, so the
        frontend can walk it one confirmed step at a time without asking
        the backend to re-rank on every hop.
        """
        from services.family_search_demo_service import check_root

        query = (request.args.get("query") or "").strip()
        root = (request.args.get("root") or "").strip()
        if not query or not root:
            return jsonify({"error": "query and root are required"}), 400
        try:
            return jsonify(check_root(query, root, _demo_model_instance()))
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/family-search-demo/check-candidate", methods=["GET"])
    def api_family_search_demo_check_candidate():
        """One more hop, only ever called after the user confirms
        continuing -- checks a single already-ranked candidate's (fake)
        mail archive."""
        from services.family_search_demo_service import check_candidate

        query = (request.args.get("query") or "").strip()
        rfc_id = (request.args.get("rfc_id") or "").strip()
        if not query or not rfc_id:
            return jsonify({"error": "query and rfc_id are required"}), 400
        try:
            return jsonify(check_candidate(query, rfc_id, _demo_model_instance()))
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

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
