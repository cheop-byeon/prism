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

import os
import sys
import threading
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, "/app/api")

# Lets this whole app be reverse-proxied under a path prefix (e.g. nginx
# serving it at https://domain/prism rather than its own subdomain) without
# a rebuild -- unset in local dev, where it's reached directly at the root.
_URL_PREFIX = os.environ.get("PRISM_URL_PREFIX", "").rstrip("/")

from app_factory import create_app
from libs.helper import RateLimiter

# Keyed by a per-request id handed to the client as the first SSE event (see
# _make_research_work) so a Stop button can actually halt the background
# thread ask() runs in -- not just close the client's own connection, which
# does nothing server-side (see ask()'s cancel_event docstring). Scoped to
# only the three research routes on purpose: _sse_stream is shared by many
# other routes in this file (mail sync, RFC graph, topic-pull, ...) that
# this change doesn't touch at all.
_ACTIVE_CANCEL_EVENTS: dict[str, threading.Event] = {}

# Research is the single expensive path now -- a multi-iteration agent loop,
# each iteration a real LLM call, always at the old Deep Dive/Panorama
# budget (there's no cheaper tier left to fall back to). Reuses Dify's own
# generic Redis sliding-window limiter (already used for things like
# password-reset throttling) rather than hand-rolling one; keyed per-account
# now that this route requires real login. 20/hour is a starting guess for a
# small-team internal tool, not a measured number.
_RESEARCH_RATE_LIMITER = RateLimiter(prefix="prism_research", max_attempts=20, time_window=3600)

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
_LOGIN_PAGE_TEMPLATE = (Path(__file__).parent / "login_template.html").read_text(encoding="utf-8")
# A separate, from-scratch page following the prism-spectrum mockup's own
# design system (po-shell/po-rail/po-viewtabs, its own --po-* palette) --
# deliberately not a variant of _PAGE_HTML, so the two never mix mid-
# migration (see commit reverting the earlier in-place attempt). Served on
# its own port (_SPECTRUM_PORT below) rather than a second route on 8765,
# so it's a genuinely separate page to open side by side, not a mode
# switch inside the existing one.
_SPECTRUM_PAGE_HTML = (Path(__file__).parent / "serve_ui_spectrum_template.html").read_text(encoding="utf-8")
_SPECTRUM_PORT = 8766
# Resized/re-encoded from the original (1122x1402, 1.7MB) down to ~95KB --
# this loads on every visit to the login page, unauthenticated, so size
# matters here in a way it doesn't for the rest of this app.
_LOGIN_HERO_JPEG = (Path(__file__).parent / "prism_hero.jpg").read_bytes()

# Standard 4-color Google "G" mark -- used on the login page's OAuth button.
_GOOGLE_ICON_SVG = (
    '<svg viewBox="0 0 24 24">'
    '<path fill="#4285F4" d="M23.52 12.27c0-.85-.08-1.67-.22-2.45H12v4.63h6.46c-.28 1.5-1.13 2.77-2.4 3.62v3h3.88'
    'c2.27-2.09 3.58-5.17 3.58-8.8z"/>'
    '<path fill="#34A853" d="M12 24c3.24 0 5.96-1.07 7.95-2.9l-3.88-3c-1.08.72-2.45 1.15-4.07 1.15-3.13 0-5.78-2.11'
    '-6.73-4.96H1.27v3.1C3.25 21.3 7.31 24 12 24z"/>'
    '<path fill="#FBBC05" d="M5.27 14.29A7.2 7.2 0 0 1 4.86 12c0-.79.14-1.56.4-2.29v-3.1H1.27A11.99 11.99 0 0 0 0 12'
    'c0 1.94.46 3.77 1.27 5.39l4-3.1z"/>'
    '<path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.45-3.45C17.95 1.19 15.24 0 12 0 7.31 0 3.25 2.7 1.27'
    ' 6.61l4 3.1C6.22 6.86 8.87 4.75 12 4.75z"/>'
    "</svg>"
)


def _render_login_page() -> str:
    """Fills in login_template.html's two placeholders:

    __API_BASE__: the real Dify api service's own base URL (NOT this
    standalone page's own server) -- CONSOLE_API_URL is meant to hold this
    but is unset in this dev environment (confirmed live), which would also
    break Google OAuth's own redirect_uri construction independent of
    Prism, so the fallback matters beyond just this page. Falls back to
    localhost:5001, the api container's actual published port (confirmed
    live via `docker port`).

    __GOOGLE_BUTTON__: only rendered when Google OAuth is actually
    configured (GOOGLE_CLIENT_ID/SECRET set) -- reuses Dify's real,
    already-working /console/api/oauth/login/google flow as-is; nothing
    OAuth-related is reimplemented here.
    """
    from configs import dify_config

    api_base = dify_config.CONSOLE_API_URL or "http://localhost:5001"
    google_configured = bool(dify_config.GOOGLE_CLIENT_ID and dify_config.GOOGLE_CLIENT_SECRET)
    google_html = (
        f'<a class="btn-google" href="{api_base}/console/api/oauth/login/google">'
        f"{_GOOGLE_ICON_SVG}Sign in with Google</a>"
        '<div class="divider">or</div>'
        if google_configured
        else ""
    )
    return (
        _LOGIN_PAGE_TEMPLATE.replace("__API_BASE__", api_base)
        .replace("__GOOGLE_BUTTON__", google_html)
        .replace("__PRISM_URL_PREFIX__", _URL_PREFIX)
    )


def _render_index_page(account) -> str:
    """Fills in serve_ui_template.html's account-menu placeholders --
    per user direction after seeing the custom "My keys" modal: "we
    should not do anything extra here, but just reuse dify's UI and
    relevant functions." Dify's real Settings/Model Provider page
    already does credential management AND plugin installation/browsing
    together, better than anything worth hand-building here (confirmed
    live: it has its own "Install model providers" marketplace browser
    built in) -- so this links out to it directly instead.

    __API_BASE__: same fallback as _render_login_page, reused here for
    the account menu's real logout call (POST __API_BASE__/console/api/logout).

    __CONSOLE_WEB_URL__: the real Dify web console's own base URL --
    CONSOLE_WEB_URL defaults to localhost:3000 in this dev environment;
    ./dev/start-web is the actual, already-documented way to run it.
    Session cookies aren't port-scoped (confirmed live: opening it in the
    same browser landed already logged in, no separate sign-in), so
    "Settings" opens straight into Model Provider with no extra login
    step. tab=provider was found live by clicking through Settings'
    sidebar there and reading the URL it produces itself -- not guessed.

    __ACCOUNT_EMAIL__ / __ACCOUNT_INITIAL__: shown in the account menu,
    matching what Dify's own header shows under the account name.
    """
    from configs import dify_config

    api_base = dify_config.CONSOLE_API_URL or "http://localhost:5001"
    console_web_url = dify_config.CONSOLE_WEB_URL or "http://localhost:3000"
    initial = (account.email or "?")[0].upper()
    return (
        _PAGE_HTML.replace("__API_BASE__", api_base)
        .replace("__CONSOLE_WEB_URL__", console_web_url)
        .replace("__ACCOUNT_EMAIL__", account.email)
        .replace("__ACCOUNT_INITIAL__", initial)
        .replace("__PRISM_URL_PREFIX__", _URL_PREFIX)
    )


def _render_spectrum_page(account) -> str:
    """Same placeholder-fill as _render_index_page, against
    _SPECTRUM_PAGE_HTML instead -- kept as a separate function (not a
    parameter on _render_index_page) since the two pages' placeholder needs
    are expected to diverge as the spectrum page grows its own account/nav
    chrome."""
    from configs import dify_config

    api_base = dify_config.CONSOLE_API_URL or "http://localhost:5001"
    console_web_url = dify_config.CONSOLE_WEB_URL or "http://localhost:3000"
    initial = (account.email or "?")[0].upper()
    return (
        _SPECTRUM_PAGE_HTML.replace("__API_BASE__", api_base)
        .replace("__CONSOLE_WEB_URL__", console_web_url)
        .replace("__ACCOUNT_EMAIL__", account.email)
        .replace("__ACCOUNT_INITIAL__", initial)
        .replace("__PRISM_URL_PREFIX__", _URL_PREFIX)
    )


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


def _authenticate_console_request(request):
    """Replicates the "console"/"inner_api" branch of
    extensions.ext_login.load_user_from_request by hand.

    That loader only verifies a token when request.blueprint is "console" or
    "inner_api" (checked live) -- every route below is registered directly
    on the bare app with no blueprint at all (this is a small standalone
    Flask app, not a route bolted onto the real api service -- see this
    file's module docstring), so request.blueprint is always None here and
    none of that loader's branches ever fire. @login_required as-is would
    therefore reject every request regardless of a valid cookie.

    extract_access_token checks a cookie first, header second -- so a
    browser tab that's already logged into Dify's console sends the same
    cookie here (same host, just a different port; cookies aren't
    port-scoped) and this authenticates exactly like the real console does,
    without needing serve_ui.py's routes to live under that blueprint name.

    Returns the logged-in Account, or None if unauthenticated.
    """
    from werkzeug.exceptions import Unauthorized

    from libs.passport import PassportService
    from libs.token import extract_access_token
    from services.account_service import AccountService

    auth_token = extract_access_token(request)
    if not auth_token:
        return None
    try:
        decoded = PassportService().verify(auth_token)
    except Unauthorized:
        return None
    if decoded.get("token_source"):
        return None
    user_id = decoded.get("user_id")
    if not user_id:
        return None
    return AccountService.load_logged_in_account(account_id=user_id)


# Mutating data-sync/download actions -- restricted to real members of the
# tenant that owns the actual IETF datasets (see _is_data_admin), not just
# anyone logged into Prism. Without this, every self-service sign-up could
# independently trigger a full mailarchive IMAP sync, draft download, BM25
# rebuild, or bulk RFC pull -- all hitting the same shared data one person
# already maintains, not per-user data each visitor is supposed to own.
# Read-only browsing of what's already synced (GET /api/corpora,
# /api/sources/<wg>/<doc_type>, topic-discover, ...) stays open to everyone,
# same as Research/Sessions/Feedback -- only the actions that write/fetch
# new data are gated here.
_DATA_ADMIN_ONLY_EXACT_PATHS = {
    "/api/corpora/mail-sync",
    "/api/corpora/draft-download",
    "/api/corpora/draft-download-all",
    "/api/corpora/local-sync",
    "/api/corpora/local-index",
    "/api/corpora/local-embedding-index",
    "/api/corpora/topic-pull",
    # Read-only, but org-wide across every account -- not one account's own
    # data the way Sessions/Research are, so it's gated the same as the
    # mutating actions above rather than left open to every self-service
    # sign-up.
    "/api/observability",
}


def _is_data_admin_route(path: str) -> bool:
    if path in _DATA_ADMIN_ONLY_EXACT_PATHS:
        return True
    return path.startswith("/api/sources/") and path.endswith("/download")


def _is_data_admin(account) -> bool:
    """Whether `account` is a genuine member of the tenant that owns the
    real IETF datasets -- NOT the account's own personal tenant, which
    every self-service sign-up gets automatically. ask_decision_agent.py's
    default tenant resolution (a dataset's real owner, or the first tenant
    in the DB) is unrelated to this check and to data-admin status either
    way; a personal tenant only ever comes into play as an opt-in
    bring-your-own-key override for model resolution specifically (see
    get_owned_tenant_id/_tenant_has_custom_credential), never for dataset
    access. Self-service sign-ups are never added as members of the real
    workspace, so this check excludes them by construction -- only
    accounts someone deliberately invited to it pass.
    """
    from extensions.ext_database import db
    from models.account import Tenant, TenantAccountJoin

    admin_tenant_id = db.session.query(Tenant).first().id
    return (
        db.session.query(TenantAccountJoin)
        .filter(
            TenantAccountJoin.tenant_id == admin_tenant_id,
            TenantAccountJoin.account_id == account.id,
        )
        .first()
        is not None
    )


def _tenant_has_custom_credential(tenant_id: str, provider: str) -> bool:
    """Whether this tenant has its own real Dify Settings/Model Provider
    credential saved for `provider` -- the signal that decides whether a
    Prism request should resolve the model against this tenant (an
    account's own personal workspace, for bring-your-own-key) instead of
    the dataset's own tenant. True only once ModelProviderService's own
    create_provider_credential has actually validated and stored one;
    never a claim this session invents on its own.
    """
    from core.provider_manager import ProviderManager

    configuration = ProviderManager().get_configurations(tenant_id).get(provider)
    return configuration is not None and configuration.is_custom_configuration_available()


def main() -> None:
    from flask import g, jsonify, redirect, request

    app = create_app()

    @app.before_request
    def _require_console_login():
        # Normalized against _URL_PREFIX so every literal path check below
        # (and _DATA_ADMIN_ONLY_EXACT_PATHS/_is_data_admin_route) can keep
        # comparing against the app's own un-prefixed route names, regardless
        # of whatever path nginx is actually proxying this app under.
        path = request.path.removeprefix(_URL_PREFIX) or "/"
        # Must stay reachable while logged out, or nobody could ever reach
        # the form that logs them in (or the hero image it displays) --
        # everything else in this app is gated, these two routes are the
        # deliberate exception.
        if path in ("/login", "/login-hero.jpg"):
            return None
        account = _authenticate_console_request(request)
        if account is None:
            if path == "/":
                return redirect(f"{_URL_PREFIX}/login")
            return jsonify({"error": "Log in to Dify's console first."}), 401
        g.account = account
        if _is_data_admin_route(path) and not _is_data_admin(account):
            return jsonify({"error": "This action is restricted to data maintainers."}), 403

    @app.route(f"{_URL_PREFIX}/login", methods=["GET"])
    def login_page():
        return _render_login_page()

    @app.route(f"{_URL_PREFIX}/login-hero.jpg", methods=["GET"])
    def login_hero():
        return _LOGIN_HERO_JPEG, 200, {"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=86400"}

    @app.route(f"{_URL_PREFIX}/", methods=["GET"])
    def index():
        # Same app/routes bound to two ports (see main()) -- request.host
        # carries whichever port the browser actually connected to, so this
        # is the one place the two pages fork, rather than duplicating every
        # /api/* route under a second prefix.
        if request.host.endswith(f":{_SPECTRUM_PORT}"):
            return _render_spectrum_page(g.account)
        return _render_index_page(g.account)

    @app.route(f"{_URL_PREFIX}/api/models", methods=["GET"])
    def api_models():
        """Lists this deployment's actually-configured, active LLMs via the
        same ProviderManager ask() itself uses -- so the Research controls
        can offer a real choice instead of every request being silently
        pinned to ask()'s hardcoded default, with no way to try a stronger
        model on a hard question.

        Also includes every active model in this account's OWN tenant (their
        real, personal Dify workspace -- see get_owned_tenant_id), tagged
        "byok" -- covers any provider they've configured a personal
        credential for via Dify's real Settings/Model Provider service
        (/api/my-llm-credentials below), including ones the shared admin
        tenant has never configured at all.

        "default" names ask()/decision_thread()'s actual hardcoded fallback
        (imported from there directly, not a second copy of the literal) --
        there's no real "no model chosen" state once a request reaches the
        agent, so the picker selects this for real on load instead of
        showing a vague "(default model)" placeholder that hides which
        concrete model is actually about to run.
        """
        from core.provider_manager import ProviderManager
        from dify_graph.model_runtime.entities.model_entities import ModelType
        from extensions.ext_database import db
        from models.account import Tenant
        from scripts.agentic_rag.ask_decision_agent import _DEFAULT_MODEL_NAME, _DEFAULT_MODEL_PROVIDER
        from services.agentic_rag_session_service import get_owned_tenant_id

        admin_tenant_id = db.session.query(Tenant).first().id
        active_models = ProviderManager().get_configurations(admin_tenant_id).get_models(
            model_type=ModelType.LLM, only_active=True
        )
        entries: dict[tuple[str, str], dict] = {
            (m.provider.provider, m.model): {
                "provider": m.provider.provider,
                "model": m.model,
                "label": m.label.en_US,
                "byok": False,
            }
            for m in active_models
        }

        owned_tenant_id = get_owned_tenant_id(g.account)
        if owned_tenant_id and owned_tenant_id != admin_tenant_id:
            personal_models = ProviderManager().get_configurations(owned_tenant_id).get_models(
                model_type=ModelType.LLM, only_active=True
            )
            for m in personal_models:
                key = (m.provider.provider, m.model)
                if key in entries:
                    entries[key]["byok"] = True
                else:
                    entries[key] = {
                        "provider": m.provider.provider,
                        "model": m.model,
                        "label": m.label.en_US,
                        "byok": True,
                    }

        return jsonify(
            {
                "models": list(entries.values()),
                "default": {"provider": _DEFAULT_MODEL_PROVIDER, "model": _DEFAULT_MODEL_NAME},
            }
        )

    @app.route(f"{_URL_PREFIX}/api/tools", methods=["GET"])
    def api_tools():
        return jsonify(_tool_info())

    @app.route(f"{_URL_PREFIX}/api/skills", methods=["GET"])
    def api_skills():
        from scripts.agentic_rag.ask_decision_agent import skill_texts

        return jsonify(skill_texts())

    @app.route(f"{_URL_PREFIX}/api/corpora", methods=["GET"])
    def api_corpora():
        from scripts.agentic_rag.ask_decision_agent import _list_available_corpora

        try:
            _text, catalog = _list_available_corpora()
            return jsonify({"catalog": catalog})
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route(f"{_URL_PREFIX}/api/document-text", methods=["GET"])
    def api_document_text():
        """The Documents page's one real backend need: given an RFC number,
        draft name, or WG/topic hint, fetch that document's real text and
        return it pre-segmented into sections -- everything else on that
        page (the lookup itself, passage selection, "Explain this
        decision") is client-side against this one payload.
        """
        from scripts.agentic_rag.ask_decision_agent import resolve_document_text

        hint = (request.args.get("q") or "").strip()
        if not hint:
            return jsonify({"error": "q is required"}), 400
        try:
            result = resolve_document_text(hint)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500
        if result is None:
            return jsonify({"error": f"Couldn't find or fetch a document for {hint!r}."}), 404
        return jsonify(result)

    @app.route(f"{_URL_PREFIX}/api/corpora/freshness", methods=["GET"])
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

        Pagination (page_size/offset) and filtering (q/kind) are opt-in via
        query params -- confirmed live that a full unpaginated fetch across
        ~45 registered+local entries takes 1-2 minutes (each local-only
        entry alone costs 2 live Datatracker calls just to resolve its own
        wg/rfc_number, on top of the per-entry mail/draft-relations lookups
        below), which is what actually made the Prism-Spectrum Sources page
        slow enough to prompt paging it. Omitting every one of these params
        reproduces the
        original unpaginated behavior exactly, so the classic page's own
        call site (which passes none) is unaffected. When page_size is
        given, the expensive local-only wg/rfc_number resolution itself
        also only runs for that page's entries -- slicing after building
        the enrichment rows would still pay that cost for every local
        source up front, defeating the point.
        """
        from scripts.agentic_rag.ask_decision_agent import _list_available_corpora
        from services.ietf_import_service import IetfImportService

        try:
            _text, catalog = _list_available_corpora()
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

        registered_names = {entry["draft_name"] for entry in catalog}
        # Identity only at this point -- catalog entries already carry a
        # real rfc_number/wg with no live lookup; local-only entries defer
        # that live resolution until after paging/filtering below, since
        # it's the expensive part.
        identities = [{**entry, "is_local": False} for entry in catalog]
        for draft_name in IetfImportService.list_local_source_names():
            if draft_name in registered_names:
                continue
            identities.append({"draft_name": draft_name, "is_local": True})

        query = (request.args.get("q") or "").strip().lower()
        kind = (request.args.get("kind") or "").strip().lower()
        if query:
            identities = [
                e
                for e in identities
                if query in e["draft_name"].lower()
                or query in str(e.get("rfc_number") or "").lower()
                or query in str(e.get("wg") or "").lower()
            ]
        if kind in ("local", "dataset"):
            identities = [e for e in identities if e["is_local"] == (kind == "local")]

        # Stable order so paging through "load more" never reshuffles
        # already-seen rows underneath the caller.
        import operator

        identities.sort(key=operator.itemgetter("is_local", "draft_name"))
        total = len(identities)

        page_size_param = request.args.get("page_size")
        offset_param = request.args.get("offset")
        paginated = page_size_param is not None or offset_param is not None
        if paginated:
            try:
                page_size = max(1, min(50, int(page_size_param or 12)))
            except ValueError:
                page_size = 12
            try:
                offset = max(0, int(offset_param or 0))
            except ValueError:
                offset = 0
            identities = identities[offset : offset + page_size]
        else:
            offset = 0
            page_size = total

        def _enrich(entry: dict) -> dict:
            # Identity resolution AND enrichment for one entry, run off the
            # main thread below -- confirmed live that even a single local
            # entry's full cost (2 live calls just to resolve its own wg/
            # rfc_number, plus mail/draft-relations/local-status lookups)
            # is several real Datatracker round trips; doing that
            # sequentially for a 12-entry page still took ~2 minutes.
            # Independent per entry, so running the whole page concurrently
            # instead cuts total time to roughly the slowest single entry.
            if entry["is_local"]:
                entry = dict(entry)
                entry["rfc_number"] = IetfImportService.resolve_rfc_number_for_draft(entry["draft_name"])
                entry["wg"] = IetfImportService.resolve_wg_for_draft(entry["draft_name"])
                entry["evolution_dataset_id"] = None
                entry["mailarchive_dataset_id"] = None

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
                try:
                    from services.job_status_service import JobStatusService

                    row["embedding_index_status"] = JobStatusService.get_status(f"embed:{draft_name}") or {
                        "status": "not_started"
                    }
                except Exception as exc:
                    row["embedding_index_status"] = {"status": "error", "message": str(exc)}
            return row

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(12, max(1, len(identities)))) as pool:
            corpora = list(pool.map(_enrich, identities))
        return jsonify(
            {
                "corpora": corpora,
                "total": total,
                "offset": offset,
                "page_size": page_size,
                "has_more": paginated and offset + page_size < total,
            }
        )

    @app.route(f"{_URL_PREFIX}/api/corpora/rfc-suggestions", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/mailing-lists", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/wg-documents", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/mail-sync", methods=["POST"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/draft-download", methods=["POST"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/draft-download-all", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/local-sync", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/local-index", methods=["POST"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/local-embedding-index", methods=["POST"])
    def api_corpora_local_embedding_index():
        """Kick off the (slow) embedding tier for one source's own local
        content -- unlike build_index's BM25 tier above, this runs as a
        real Celery task (see tasks/local_source_embedding_index_task.py)
        so it keeps going after this request returns, and after the
        requesting page closes. Poll api_corpora_local_embedding_index_status
        for progress instead of waiting on this request.
        """
        from services.ietf_import_service import IetfImportService
        from services.job_status_service import JobStatusService
        from tasks.local_source_embedding_index_task import local_source_embedding_index_task

        data = request.get_json(silent=True) or {}
        draft_name = (data.get("draft_name") or "").strip()
        if not draft_name:
            return jsonify({"error": "draft_name is required"}), 400

        job_key = f"embed:{draft_name}"
        existing = JobStatusService.get_status(job_key)
        if existing and existing["status"] in ("queued", "running"):
            return jsonify({"job_key": job_key, **existing})

        wg = IetfImportService.resolve_wg_for_draft(draft_name)
        rfc_number = IetfImportService.resolve_rfc_number_for_draft(draft_name)
        JobStatusService.set_status(job_key, "queued", message="Queued…")
        local_source_embedding_index_task.delay(job_key, draft_name, wg, rfc_number)
        return jsonify({"job_key": job_key, "status": "queued"})

    @app.route(f"{_URL_PREFIX}/api/corpora/local-embedding-index/status", methods=["GET"])
    def api_corpora_local_embedding_index_status():
        from services.job_status_service import JobStatusService

        draft_name = (request.args.get("draft_name") or "").strip()
        if not draft_name:
            return jsonify({"error": "draft_name is required"}), 400
        status = JobStatusService.get_status(f"embed:{draft_name}")
        if status is None:
            return jsonify({"status": "not_found"})
        return jsonify(status)

    @app.route(f"{_URL_PREFIX}/api/corpora/topic-discover", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/corpora/topic-pull", methods=["GET"])
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
                on_event(
                    {
                        "stage": "pulling",
                        "current": i,
                        "total": len(rfc_numbers),
                        "message": f"RFC{rfc_number} ({i}/{len(rfc_numbers)})",
                    }
                )
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

    @app.route(f"{_URL_PREFIX}/api/sources/types", methods=["GET"])
    def api_sources_types():
        return jsonify({"types": [{"key": key, "label": label} for key, label in _WG_RESOURCE_TYPES]})

    @app.route(f"{_URL_PREFIX}/api/sources/<wg>/<doc_type>", methods=["GET"])
    def api_sources_list(wg, doc_type):
        from services.ietf_import_service import IetfImportService

        try:
            listing = IetfImportService.list_wg_resources(wg, doc_type)
            listing.update(IetfImportService.get_local_resource_status(doc_type, listing.get("documents", [])))
            return jsonify(listing)
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": str(exc)}), 500

    @app.route(f"{_URL_PREFIX}/api/sources/<wg>/<doc_type>/download", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/rfc-graph/global", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/rfc-graph/focus", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/rfc-graph/<rfc_number>", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/rfc-errata/<rfc_number>", methods=["GET"])
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

    def _make_research_work(
        *,
        tier,
        query,
        draft,
        session_id,
        account,
        max_iterations=None,
        extra_instruction=None,
        model_provider=None,
        model_name=None,
    ):
        """Shared body for /api/research: resumes (or creates) a
        Conversation owned by `account`, splices its prior turns in as
        history, runs the agent, then persists the new turn as a real
        Message -- so a session can be continued across multiple asks, and
        it's the same account on every message.

        `account` is a plain argument, not read from flask.g inside work()
        -- flask.g is request-context-scoped, and work() runs in a
        background thread under a manually-pushed app_context (see
        _sse_stream) that has no request context at all, so g would be
        empty there. Capturing it here, before the thread starts, is what
        makes it available.
        """
        from scripts.agentic_rag.ask_decision_agent import ask as ask_decision_agent
        from services.agentic_rag_session_service import AgenticRagSessionService

        request_id = str(uuid.uuid4())
        cancel_event = threading.Event()
        _ACTIVE_CANCEL_EVENTS[request_id] = cancel_event

        def work(on_event):
            nonlocal session_id
            try:
                # Sent before anything else so a Stop button has the id to
                # cancel against even during the very first LLM call.
                on_event({"stage": "started", "request_id": request_id})

                session = AgenticRagSessionService.get_session(session_id, account=account) if session_id else None
                if session is None:
                    session = AgenticRagSessionService.create_session(title_seed=query, account=account)
                    session_id = session.id
                history = AgenticRagSessionService.get_history_for_prompt(session_id, account=account)

                # Only overrides ask()'s own hardcoded default (google/gemini-2.5-flash
                # via openrouter) when the Research controls' model picker actually sent
                # one -- omitting the kwargs entirely (not passing None through) keeps
                # every other caller (CLI, battery scripts) exactly as it was.
                model_kwargs = {}
                if model_provider and model_name:
                    model_kwargs = {"model_provider": model_provider, "model_name": model_name}

                # This account's own tenant, if it has its own real Dify
                # Settings/Model Provider credential configured for this
                # provider -- None (the common case: no personal tenant, or
                # one that hasn't configured this provider) leaves ask() on
                # the dataset's own tenant exactly as before. Deliberately
                # no fallback when a personal credential IS configured but
                # fails at call time: the exception propagates as a real
                # error (see _sse_stream), not a silent switch back to the
                # shared key -- that would spend the org's money unasked.
                model_tenant_id = None
                if model_provider:
                    from services.agentic_rag_session_service import get_owned_tenant_id

                    owned_tenant_id = get_owned_tenant_id(account)
                    if owned_tenant_id and _tenant_has_custom_credential(owned_tenant_id, model_provider):
                        model_tenant_id = owned_tenant_id

                trace: list[dict] = []
                usage: list[dict] = []
                claim_labels: dict[int, str] = {}
                evidence: list[dict] = []
                answer = ask_decision_agent(
                    query=query,
                    draft=draft,
                    trace=trace,
                    on_event=on_event,
                    max_iterations=max_iterations,
                    extra_instruction=extra_instruction,
                    history=[{"query": t["query"], "answer": t["answer"]} for t in history],
                    usage=usage,
                    cancel_event=cancel_event,
                    model_tenant_id=model_tenant_id,
                    claim_labels=claim_labels,
                    evidence=evidence,
                    **model_kwargs,
                )
                usage_summary = usage[0] if usage else None
                message_id = AgenticRagSessionService.append_turn(
                    session_id=session_id,
                    account=account,
                    tier=tier,
                    query=query,
                    answer=answer,
                    trace=trace,
                    usage=usage_summary,
                    model_provider=model_provider,
                    model_name=model_name,
                    claim_labels=claim_labels,
                    evidence=evidence,
                )
                # _summarize_usage's total_price is a Decimal -- fine for the
                # DB column above, but the SSE payload below goes through a
                # plain json.dumps (see _sse_stream), which has no Decimal
                # support the way Flask's jsonify does, so it's cast to
                # float here rather than crashing the whole result event.
                usage_for_client = None
                if usage_summary:
                    usage_for_client = {
                        "prompt_tokens": usage_summary.get("prompt_tokens", 0),
                        "completion_tokens": usage_summary.get("completion_tokens", 0),
                        "total_tokens": usage_summary.get("total_tokens", 0),
                        "total_price": float(usage_summary.get("total_price", 0) or 0),
                        "currency": usage_summary.get("currency", "USD"),
                        "latency": usage_summary.get("latency", 0.0),
                    }
                return {
                    "answer": answer,
                    "trace": trace,
                    "session_id": session_id,
                    "message_id": message_id,
                    "usage": usage_for_client,
                    "claim_labels": claim_labels,
                    "evidence": evidence,
                }
            finally:
                _ACTIVE_CANCEL_EVENTS.pop(request_id, None)

        return work

    @app.route(f"{_URL_PREFIX}/api/research", methods=["GET"])
    def api_research():
        """The single research entry point: always the old Deep Dive/
        Panorama iteration budget, with an addendum that also asks the
        model to decide for itself, early in the run, whether the question
        needs the wider RFC landscape -- no separate tier/endpoint for that
        choice anymore (see _RESEARCH_ADDENDUM in ask_decision_agent.py).
        The old Ask/Investigate single-pass, no-addendum behavior isn't
        gone -- it's just ask()'s own default when max_iterations/
        extra_instruction are omitted, still usable directly from Python
        (eval/battery scripts) as a baseline, just no longer exposed here.
        """
        from scripts.agentic_rag.ask_decision_agent import _RESEARCH_ADDENDUM, _RESEARCH_MAX_ITERATIONS

        query = (request.args.get("query") or "").strip()
        draft = request.args.get("draft") or None
        session_id = request.args.get("session_id") or None
        if not query:
            return jsonify({"error": "query is required"}), 400
        if _RESEARCH_RATE_LIMITER.is_rate_limited(g.account.id):
            return jsonify({"error": "Research rate limit reached -- try again later."}), 429
        _RESEARCH_RATE_LIMITER.increment_rate_limit(g.account.id)

        work = _make_research_work(
            tier="research",
            query=query,
            draft=draft,
            session_id=session_id,
            account=g.account,
            max_iterations=_RESEARCH_MAX_ITERATIONS,
            extra_instruction=_RESEARCH_ADDENDUM,
            model_provider=request.args.get("model_provider") or None,
            model_name=request.args.get("model_name") or None,
        )
        return _sse_stream(app, work)

    @app.route(f"{_URL_PREFIX}/api/sessions", methods=["GET"])
    def api_sessions_list():
        from services.agentic_rag_session_service import AgenticRagSessionService
        from services.errors.conversation import LastConversationNotExistsError

        search_query = request.args.get("q") or None
        if search_query:
            sessions = AgenticRagSessionService.search_sessions(account=g.account, query=search_query)
            return jsonify({"sessions": sessions, "has_more": False})

        last_id = request.args.get("last_id") or None
        try:
            return jsonify(AgenticRagSessionService.list_sessions(account=g.account, last_id=last_id))
        except LastConversationNotExistsError:
            return jsonify({"error": "last_id not found"}), 400

    @app.route(f"{_URL_PREFIX}/api/observability", methods=["GET"])
    def api_observability():
        """Org-wide usage/cost/quality trends -- gated to data admins by
        _require_console_login (see _DATA_ADMIN_ONLY_EXACT_PATHS), same as
        every other action that reaches beyond one account's own data.
        """
        from services.agentic_rag_session_service import AgenticRagSessionService

        try:
            days = int(request.args.get("days", 30))
        except ValueError:
            days = 30
        days = max(1, min(90, days))
        return jsonify(AgenticRagSessionService.get_observability_stats(days=days))

    @app.route(f"{_URL_PREFIX}/api/sessions/<session_id>", methods=["GET"])
    def api_sessions_get(session_id):
        from services.agentic_rag_session_service import AgenticRagSessionService

        session = AgenticRagSessionService.get_session(session_id, account=g.account)
        if session is None:
            return jsonify({"error": "session not found"}), 404
        turns = AgenticRagSessionService.get_turns(session_id, account=g.account)
        return jsonify(
            {
                "id": session.id,
                "title": session.name,
                "turns": turns,
                "model_provider": session.model_provider,
                "model_id": session.model_id,
            }
        )

    @app.route(f"{_URL_PREFIX}/api/sessions/<session_id>", methods=["PATCH"])
    def api_sessions_rename(session_id):
        from services.agentic_rag_session_service import AgenticRagSessionService
        from services.errors.conversation import ConversationNotExistsError

        name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 400
        try:
            session = AgenticRagSessionService.rename_session(session_id, account=g.account, name=name[:200])
        except ConversationNotExistsError:
            return jsonify({"error": "session not found"}), 404
        return jsonify({"id": session.id, "title": session.name})

    @app.route(f"{_URL_PREFIX}/api/sessions/<session_id>", methods=["DELETE"])
    def api_sessions_delete(session_id):
        from services.agentic_rag_session_service import AgenticRagSessionService

        if not AgenticRagSessionService.delete_session(session_id, account=g.account):
            return jsonify({"error": "session not found"}), 404
        return jsonify({"ok": True})

    @app.route(f"{_URL_PREFIX}/api/messages/<message_id>/feedback", methods=["POST"])
    def api_message_feedback(message_id):
        from services.agentic_rag_session_service import AgenticRagSessionService
        from services.errors.message import MessageNotExistsError

        body = request.get_json(silent=True) or {}
        rating = body.get("rating")
        if rating not in ("like", "dislike", None):
            return jsonify({"error": "rating must be 'like', 'dislike', or null"}), 400
        try:
            result = AgenticRagSessionService.set_feedback(
                message_id=message_id, account=g.account, rating=rating, content=body.get("content")
            )
        except (MessageNotExistsError, ValueError) as exc:
            return jsonify({"error": str(exc) or "message not found"}), 404
        return jsonify(result)

    @app.route(f"{_URL_PREFIX}/api/research/<request_id>/cancel", methods=["POST"])
    def api_research_cancel(request_id):
        """Sets the cancel_event a Deep Dive/Panorama/Ask run is already
        checking (see ask()'s cancel_event param) -- a real stop, not just
        the client closing its own SSE connection, which doesn't touch the
        background thread the agent loop actually runs in.
        """
        cancel_event = _ACTIVE_CANCEL_EVENTS.get(request_id)
        if cancel_event is None:
            return jsonify({"error": "not found or already finished"}), 404
        cancel_event.set()
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
        from scripts.agentic_rag.ask_decision_agent import _DEFAULT_MODEL_NAME, _DEFAULT_MODEL_PROVIDER

        tenant_id = db.session.query(Tenant).first().id
        return ModelManager().get_model_instance(
            tenant_id=tenant_id,
            provider=_DEFAULT_MODEL_PROVIDER,
            model_type=ModelType.LLM,
            model=_DEFAULT_MODEL_NAME,
        )

    @app.route(f"{_URL_PREFIX}/api/family-search-demo/roots", methods=["GET"])
    def api_family_search_demo_roots():
        from services.family_search_demo_service import get_demo_roots

        return jsonify(get_demo_roots())

    @app.route(f"{_URL_PREFIX}/api/family-search-demo/check-root", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/family-search-demo/check-candidate", methods=["GET"])
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

    @app.route(f"{_URL_PREFIX}/api/decision-thread", methods=["GET"])
    def api_decision_thread():
        from scripts.agentic_rag.ask_decision_agent import decision_thread

        draft = (request.args.get("draft") or "").strip() or None
        query = (request.args.get("query") or "").strip()
        if not query:
            return jsonify({"error": "query is required"}), 400

        def work(on_event):
            return decision_thread(draft, query, on_event=on_event)

        return _sse_stream(app, work)

    print(f"Prism spectrum UI running -- open http://0.0.0.0:{_SPECTRUM_PORT}/ in your browser")
    # Same app object, same route table (including every /api/* route) bound
    # to a second port in its own thread -- index() (above) is the only
    # place that forks behavior between the two, by which port the request
    # actually arrived on. This avoids standing up a second process, a
    # second set of route registrations, or any cross-origin/CORS concern
    # between the two pages, since each port serves its own page and APIs
    # from the same origin.
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=_SPECTRUM_PORT, debug=False, threaded=True),
        daemon=True,
    ).start()

    print("Decision Rationale Agent UI running -- open http://0.0.0.0:8765/ in your browser")
    # threaded=True: an open SSE stream (ask/decision-thread) holds its
    # connection for as long as the background work takes -- without this,
    # Werkzeug's dev server would block every other request (even loading
    # the sidebar's /api/tools) until that one stream finished.
    app.run(host="0.0.0.0", port=8765, debug=False, threaded=True)


if __name__ == "__main__":
    main()
