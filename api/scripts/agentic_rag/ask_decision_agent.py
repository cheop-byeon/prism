"""Seeded-agent variant of the Decision Rationale agent.

sync_decision_agent.py builds a standalone Agent-chat app (mode: agent-chat,
strategy: function_call) that decides for itself, per turn, whether to call
the mailarchive dataset tool. Two full 7-case battery runs against that app
(before and after hardening the prompt with an explicit "you MUST search
mailarchive" rule) both showed a 2-of-7 skip rate -- and one of those skips
produced a fully fabricated named source with an exact timestamp, with zero
supporting tool call to trace it to.

The root cause isn't a weak prompt: core/agent/fc_agent_runner.py calls
model_instance.invoke_llm(...) with no tool_choice parameter at all (grepped
the whole api/core tree -- there is no tool_choice/forced-tool concept
anywhere in this codebase), so every iteration is "auto" and whether the
mailarchive tool gets called is entirely the model's own judgment call.

A first version of this script fixed that by dropping the agent loop
entirely: retrieve both datasets deterministically, then one plain LLM call
over the fixed results. That guaranteed the search but also gave up the
thing agentic RAG is actually supposed to buy -- the model could no longer
rewrite the query and search again if the first pass came back thin, or ask
a clarifying question.

A second version force-seeded BOTH searches up front, using the raw
question text as the query for each. That regressed the evolution/decision
lookup: that dataset is keyword/BM25-indexed (economy indexing technique),
and both battery runs show the model reliably formulates a tight key-phrase
query for it on its own (e.g. "Safety of LEDBAT" rather than the full
question) -- feeding it the raw natural-language question instead diluted
the keyword match and picked the wrong decision entirely in testing (a
"Safety of LEDBAT" question resolved to an unrelated TARGET/GAIN
parameter-removal diff). Evolution search was never actually the reliability
problem in the battery runs -- it was always called, correctly -- so
force-seeding it was solving a problem that didn't exist while introducing a
new one.

A third version only intervened exactly where the real problem was: the
agent runs its normal function-calling loop, untouched, from turn one, with
both datasets as live tools and the model free to phrase and rephrase each
query itself. The one change was a safety net -- if the model is ever about
to produce a final answer without having called the mailarchive tool even
once, that's intercepted and a mailarchive search is forced in Python before
the loop is allowed to finish.

This version adds two more tools rather than more prompt text, closing a gap
that no amount of prompting can close on its own: the model's only way to
check its own claims was re-reading the same retrieved excerpts it already
had, in the same call that generated the claims -- which is architecturally
weaker than a genuinely separate check (this is exactly why
sync_decision_bot.py's fixed pipeline has a dedicated, separate fact-check
LLM call rather than relying on the explain step to self-police).

- `fetch_ietf_draft_text` / `fetch_ietf_rfc_text`: the model can fetch a
  specific draft revision or the final RFC directly from the primary source
  (ietf.org/rfc-editor.org) and search it for a phrase, instead of trusting
  only whatever the evolution dataset happened to precompute. This is the
  same manual verification step used out-of-band, by hand, to catch (and
  then correct) a real reasoning mistake earlier in this project: an
  INIT_CWND claim was initially flagged as fabricated because it wasn't in
  the evolution dataset's retrieved chunk, until fetching the actual rev-09
  draft text directly confirmed the claim was correct and the dataset's
  chunk was simply incomplete for that query. The agent gets that same
  ability now, as a tool call rather than a manual side-channel.
- `verify_answer`: a separate, fresh LLM call (its own system prompt, no
  chat history, low temperature) that audits a draft answer against every
  piece of evidence actually retrieved so far -- the fixed pipeline's
  fact-check step, exposed as something the agent can invoke on itself
  rather than either a forced Python step or a same-call self-check. As
  with mailarchive, self-direction alone isn't trusted to be reliable: a
  safety net forces exactly one verify_answer call before any final answer
  is accepted, mirroring the mailarchive safety net below. If the model
  already called it voluntarily, nothing is overridden.

A fifth tool, `diff_ietf_draft_revisions`, adds a second, genuinely
independent opinion on what changed between two revisions: it shells out to
the real `iddiff` tool (ietf-tools/iddiff on PyPI, added as a project
dependency in api/pyproject.toml) rather than reusing
ietf_import_service.py's own _diff_version_pair logic that built the
evolution dataset in the first place -- a dataset-side gap and a bug in the
diff tool that produced it would be the same bug seen twice; a different
tool's independent diff is a real second signal, not a second look through
the same lens.

Also added: a plain Python-level retry around every invoke_llm() call
(_invoke_llm_with_retry), mirroring sync_decision_bot.py's own
retry_config fix for the exact same transient-429 failure mode -- that fix
is a Dify workflow-node setting, which doesn't exist for a script that
calls invoke_llm() directly, so this reimplements the same 2-retries/8s
policy by hand.

Everything above was still bound to one hard-coded evolution/mailarchive
dataset pair, passed in via CLI flags before the agent ever ran -- fine for
one draft (LEDBAT), but not a system that could handle any IETF draft
without a human hard-wiring dataset ids for it first. This version makes
dataset selection something the agent discovers, not something we pre-wire:

- `list_available_ietf_corpora`: enumerates every evolution dataset in this
  Dify instance by content signature -- every chunk generate_draft_evolution
  produces starts with a self-describing `ietf-evolution draft:<name>`
  header, so which draft a dataset belongs to is recoverable directly from
  its data, regardless of how the dataset's display name was later edited by
  hand (confirmed necessary: the live datasets table has evolution datasets
  named inconsistently, e.g. "draft-ietf-ledbat-..." vs
  "draft-ietf-ledbat-congestion-evolution-paragraph"). For each discovered
  draft, the tool resolves its owning working group live via IETF
  Datatracker (draft -> group -> group's list_email/acronym) and looks that
  acronym up in a small hand-maintained _MAILARCHIVE_REGISTRY dict. That
  registry step is unavoidable for now: unlike the evolution side, mailarchive
  datasets carry no equivalent self-describing header in their content (spot
  checked directly against the live data) and this instance's actual
  mailarchive datasets have messy, human-picked names ("ledbat-ml",
  "ledbat-ml-new", "ledbat-new-new") with no structured link back to a WG at
  all -- there is nothing in the data to auto-discover there, so a small
  registry (edited by hand whenever a new WG's mailarchive dataset is built)
  is the honest, minimal fix rather than inventing a fake auto-discovery
  mechanism the data doesn't actually support.
- `search_ietf_evolution_dataset` / `search_ietf_mailarchive_dataset` replace
  the old per-dataset-id-named tools (`dataset_<uuid>`) with two fixed-name
  tools that take an explicit `dataset_id` argument, so the same tool works
  for whichever dataset the agent discovered rather than being permanently
  bound to one dataset chosen before the conversation even started.
- `evolution_dataset_id` / `mailarchive_dataset_id` are now optional hints to
  `ask()` rather than required. Passed explicitly (as every existing battery
  test script does), behavior is unchanged. Omitted, the agent must discover
  first -- and a new safety-net floor forces exactly one
  list_available_ietf_corpora call if the model tries to answer without ever
  having called it or been given a hint, mirroring the mailarchive/verify
  safety nets already in place. The mailarchive floor itself now forces a
  search using whichever dataset_id was discovered or hinted, and simply
  doesn't force a guess when that's genuinely unresolvable (no corpus found,
  or more than one draft matched with no way to know which one is meant) --
  forcing an arbitrary dataset id would be worse than not forcing at all.

Verified end-to-end with a second draft (draft-ietf-sidr-bgpsec-protocol /
RFC 8205, WG sidr) added purely to prove this generalizes -- zero dataset
hints, correct discovery, correct answer, citations checked against the
raw retrieval.

Two more additions make this practical for how people actually ask
questions, not just how the discovery mechanism happens to work internally:

- list_available_ietf_corpora's catalog now also resolves and shows each
  draft's published RFC number (via the datatracker doc-page redirect --
  the draft-keyed API record's own rfc/rfc_number fields are always null,
  confirmed directly, even for long-published drafts). People naturally
  say "why does RFC 8205 ..." rather than naming the draft file, and
  without this the agent had no way to connect the two.
- A --draft CLI flag (name, RFC number, or WG acronym) resolves eagerly
  against that same catalog before the agent loop starts, converting to
  the same dataset-id hints --evolution-dataset/--dataset already
  support -- a human-typeable alternative to raw UUIDs nobody would
  actually know, with the same efficiency win of skipping discovery
  entirely when given. An unresolvable or ambiguous hint fails loudly
  with the list of known drafts rather than silently guessing one.

Every answer now also carries a live, clickable citation -- a link on
IETF's own hosted diff viewer (author-tools.ietf.org/iddiff) for the
actual revision pair the answer is about, confirmed to support anchoring
straight to the exact changed hunk (every hunk on a rendered page gets its
own <tr id="diffNNNN">, in document order -- verified directly against a
real page). Every evolution-dataset hit already carries this URL for free
(cite_iddiff_url exists separately for the pricier, hunk-precise version
on demand). Getting it to reliably show up in the final answer text needed
the same lesson as the mailarchive-search and verify_answer safety nets:
prompt instruction alone wasn't enough -- a real run cited the right
revision pair inline while a *different* run of the same question cited
nothing at all. _finalize_answer appends one in Python, deterministically,
skipping only when the model already included a citation itself (checked
by URL substring, not an exact phrase, since confirmed the model paraphrases
the lead-in text around the link) -- and when appending, matches whichever
seen evolution hit's revision pair is actually mentioned in the answer
text, not just the top-ranked hit of the last search (confirmed necessary:
a real run's answer discussed a rev08->rev09 change while the top hit for
that query was a different, less relevant rev04->rev05 pair).

No Dify core files are touched, and neither the existing sync_decision_bot*.py
chatflow nor sync_decision_agent.py's Agent app are modified -- this is a new,
separate script.

Run inside the api container:

    docker exec -w /app/api docker-api-1 python scripts/agentic_rag/ask_decision_agent.py \
        --dataset <mailarchive-dataset-id> --evolution-dataset <evolution-dataset-id> \
        --query "Why was INIT_CWND set to 4?"
"""

# ruff: noqa: T201 — stdout output is the point of a CLI tool

import argparse
import json
import operator
import re
import sys
import time
import traceback
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, "/app/api")

from app_factory import create_app

# sync_decision_bot.py hit this exact failure mode (OpenRouter 429 under free-tier
# load) and fixed it with a workflow-node retry_config -- a Dify graph-node setting
# that doesn't exist here, since this script calls invoke_llm() directly in Python.
# _LLM_RETRIES/_LLM_RETRY_SECONDS mirror that fix's numbers (2 retries, 8s) for the
# same reason: smooth a transient spike, not paper over a persistent failure.
_LLM_RETRIES = 2
_LLM_RETRY_SECONDS = 8


def _invoke_llm_with_retry(model_instance, usage_sink: list | None = None, **kwargs):
    from core.plugin.impl.exc import PluginInvokeError
    from dify_graph.model_runtime.errors.invoke import InvokeError

    for attempt in range(_LLM_RETRIES + 1):
        try:
            result = model_instance.invoke_llm(**kwargs)
            if usage_sink is not None:
                usage_sink.append(result.usage)
            return result
        except (PluginInvokeError, InvokeError):
            if attempt == _LLM_RETRIES:
                raise
            time.sleep(_LLM_RETRY_SECONDS)


def _summarize_usage(usage_log: list) -> dict:
    """Collapses every LLMUsage seen across one ask() call (the main loop's
    iterations plus any verify_answer call) into one dict a caller can store
    on a single Message row -- Message has no concept of "one turn, several
    LLM calls", so this is the honest total for that turn.
    """
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "total_price": Decimal(0), "latency": 0.0}
    currency = "USD"
    for usage in usage_log:
        total["prompt_tokens"] += usage.prompt_tokens
        total["completion_tokens"] += usage.completion_tokens
        total["total_tokens"] += usage.total_tokens
        total["total_price"] += usage.total_price
        total["latency"] += usage.latency
        currency = usage.currency
    total["currency"] = currency
    return total


def _load_skill(filename: str) -> str:
    """Load an agent instruction file from this directory.

    Kept as a real file (not a Python string constant) for the same reason
    Claude Code's own Skills are files, not code: prompt wording is content,
    not logic -- reading, diffing, and editing it shouldn't require touching
    Python string-escaping, and a single file can be loaded by both this
    script and serve_ui.py's UI without a second copy to drift out of sync.
    """
    return (Path(__file__).parent / filename).read_text(encoding="utf-8").strip()


_INSTRUCTION = _load_skill("agent_skill.md")
_VERIFY_INSTRUCTION = _load_skill("verify_skill.md")

# Three forced safety-net rounds (discovery, mailarchive, verify_answer) plus
# room for the model's own self-directed searches/fetches/re-verification.
_MAX_ITERATIONS = 10

# Single source of truth for "no model was picked" -- ask() and
# decision_thread() each used to hardcode this pair as their own default
# parameter values, which could silently drift out of sync on a future
# edit to just one of them.
_DEFAULT_MODEL_PROVIDER = "langgenius/openrouter/openrouter"
_DEFAULT_MODEL_NAME = "google/gemini-2.5-flash"

# The single Research entry point: one genuinely thorough pass that decides
# its own scope, replacing the old Deep Dive/Panorama split -- ask() takes
# both as optional overrides (see its docstring), so a direct ask() call
# (eval/battery scripts, the old Ask/Investigate baseline) that omits them
# is completely unaffected.
_RESEARCH_MAX_ITERATIONS = 20
_RESEARCH_ADDENDUM = (
    "Go deeper than usual on this question: don't settle for the first search "
    "result. If evidence looks thin, ambiguous, or contradictory, try at "
    "least one differently-phrased search before concluding there's nothing "
    "more. When it would materially strengthen the answer, fetch the actual "
    "draft or RFC text to confirm exact wording rather than trusting the "
    "evolution dataset's diff alone, and use diff_ietf_draft_revisions as an "
    "independent second opinion on a change that matters. Prioritize "
    "thoroughness over speed -- more tool calls are fine if they add real "
    "evidence, not just repetition.\n\n"
    "Early on, decide for yourself whether this question is scoped to one draft or RFC's own "
    "history, or whether it's actually about the WIDER RFC landscape -- and proceed accordingly; "
    "don't assume it's narrow just because it names one draft. When it needs the wider landscape, "
    "proactively use get_rfc_relations, find_related_rfc_family, and search_rfc_corpus_bm25 to "
    "find other RFCs connected to the one in question -- structurally (obsoletes/updates), by "
    "citation (find_related_rfc_family), and by topic (search_rfc_corpus_bm25, which takes an RFC "
    "number directly as its query and searches using that RFC's own title+abstract). "
    "find_related_rfc_family requires an exact keyword match and can come back empty even when a "
    "real family exists (a confirmed, known limitation -- not evidence that nothing exists), so if "
    "it's empty, try search_rfc_corpus_bm25 with the same RFC number before concluding there's no "
    "broader family. When the question is about evolution across several RFCs, explain what each "
    "one specifically added, corrected, or superseded -- cite the actual RFC numbers you found via "
    "these tools, don't rely on your own prior knowledge of them without checking Datatracker first. "
    "If the user is asking you to gather, pull, crawl, or build up local material on a topic (not "
    "just explain it), use add_local_source on the RFCs you found that genuinely look central to the "
    "question -- not every RFC discovery/search tool returns, since each add_local_source call is a "
    "real, moderately expensive fetch, not a free lookup. After pulling one, search_local_source can "
    "query it immediately in this same conversation, so you can actually read what you just pulled "
    "before finishing your answer, not just report that you pulled it. "
    "search_local_source's mail coverage is scoped per WORKING GROUP, not per draft: once you pull one "
    "draft, searching it also searches its WG's entire mail archive, not just that one draft's own "
    "thread. So if the central RFCs you found span more than one WG (each add_local_source/"
    "list_available_ietf_corpora result names the WG), pull and search at least one representative "
    "draft from EACH distinct WG before concluding the discussion was narrow -- stopping at the first "
    "WG that returns a hit, when the question is genuinely cross-WG, understates the breadth a "
    "wide-scope question actually needs."
)

_ARCHIVE_BASE = "https://www.ietf.org/archive/id/"
_RFC_EDITOR_BASE = "https://www.rfc-editor.org/rfc/"
_DATATRACKER_BASE = "https://datatracker.ietf.org"

# No structured metadata links a mailarchive dataset back to a WG or draft --
# unlike the evolution side's self-describing `ietf-evolution draft:X` header,
# mailarchive dataset content is just raw thread/message text (confirmed by
# inspecting the live data), and this instance's actual mailarchive datasets
# have inconsistent, hand-picked names with no derivable link at all. This is
# a small, hand-maintained bridge until/unless that changes -- add an entry
# whenever a new WG's mailarchive dataset is built.
_MAILARCHIVE_REGISTRY: dict[str, str] = {
    # Both entries point at BM25/economy datasets. A rigorous comparison
    # (raw retrieval rank at top_k=50, plus full end-to-end answer quality)
    # showed the semantic/parent-child convention -- originally thought to
    # be the better choice, per LEDBAT's own earlier build -- actually
    # measurably underperforms BM25 for this content on BOTH drafts: the
    # lc_semantic sub-chunker over-fragments heavily-quoted, threaded mail
    # text into tiny decontextualized pieces (some a single word), and those
    # fragments crowd out substantive hits in semantic search results. The
    # semantic datasets are kept, unused, as records rather than deleted:
    # ledbat-new-new (bbcde280-4130-4dee-bbcb-73664cc43b58) and
    # draft-ietf-sidr-bgpsec-protocol-mailarchive-semantic
    # (929b0b5f-262f-46fd-88c7-940a96374373).
    "ledbat": "cd7ef874-417e-45a6-a917-461e4cdf20c3",
    "sidr": "369c22a1-c5c5-41be-8be7-97daa311f8a2",
}


def _discover_evolution_corpora() -> list[dict]:
    """Auto-discover evolution datasets by content signature, not by name.

    Every chunk generate_draft_evolution produces starts with a self-describing
    `ietf-evolution draft:<name>` header -- that's a reliable signature
    regardless of how the dataset's display name was later edited by hand
    (the live datasets table has exactly this problem: "draft-ietf-ledbat-..."
    vs "draft-ietf-ledbat-congestion-evolution-paragraph" for the same draft).
    """
    from extensions.ext_database import db
    from models.dataset import Dataset, DocumentSegment

    header_re = re.compile(r"^ietf-evolution draft:(\S+)")
    results: list[dict] = []
    for dataset in db.session.query(Dataset).all():
        segment = (
            db.session.query(DocumentSegment)
            .filter_by(dataset_id=dataset.id)
            .order_by(DocumentSegment.position)
            .first()
        )
        if not segment or not segment.content:
            continue
        match = header_re.match(segment.content)
        if not match:
            continue
        results.append(
            {
                "dataset_id": dataset.id,
                "dataset_name": dataset.name,
                "draft_name": match.group(1),
                "created_at": dataset.created_at,
            }
        )
    return results


def _resolve_wg_for_draft(draft_name: str) -> str | None:
    """Live lookup of a draft's owning WG acronym via IETF Datatracker
    (draft -> group -> acronym) -- not cached, matching the pattern used by
    the other fetch tools. Returns None on any lookup failure."""
    import httpx

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            doc_resp = client.get(
                f"{_DATATRACKER_BASE}/api/v1/doc/document/{draft_name}/", params={"format": "json"}
            )
            if doc_resp.status_code != 200:
                return None
            group_url = doc_resp.json().get("group")
            if not group_url:
                return None
            group_resp = client.get(f"{_DATATRACKER_BASE}{group_url}", params={"format": "json"})
            if group_resp.status_code != 200:
                return None
            return group_resp.json().get("acronym")
    except httpx.HTTPError:
        return None


def _resolve_rfc_number_for_draft(draft_name: str) -> str | None:
    """Live lookup of the RFC number a draft was published as, if any.

    Datatracker's draft-keyed document record always has rfc/rfc_number set
    to null (checked directly -- confirmed even for long-published drafts),
    so the only lookup that actually works is the plain doc page's redirect:
    https://datatracker.ietf.org/doc/<draft_name>/ 302s to
    https://datatracker.ietf.org/doc/rfcNNNN/ once a draft is published,
    and just stays on the draft's own page otherwise. Returns None if
    unpublished or on any lookup failure.
    """
    import httpx

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(f"{_DATATRACKER_BASE}/doc/{draft_name}/")
            if resp.status_code != 200:
                return None
            match = re.search(r"/doc/rfc(\d+)/", str(resp.url))
            return match.group(1) if match else None
    except httpx.HTTPError:
        return None


def _list_available_corpora() -> tuple[str, list[dict]]:
    """Return (formatted catalog for the tool observation, structured catalog).

    The structured form is `[{draft_name, rfc_number, wg, evolution_dataset_id,
    mailarchive_dataset_id}]` -- used by ask() to auto-seed the mailarchive
    safety net's candidate dataset id when there's exactly one unambiguous
    draft (today's reality; deliberately not guessed at when there's more
    than one), and to resolve a user-given --draft hint (name, RFC number, or
    WG acronym) to the right entry.

    Includes each draft's published RFC number (when it has one) specifically
    so a question phrased as "why does RFC 8205 ..." resolves correctly --
    people reasonably refer to a published spec by its RFC number, not its
    draft name, and the draft name alone (the only thing the content-signature
    discovery in _discover_evolution_corpora can see) doesn't carry that.
    """
    corpora = _discover_evolution_corpora()
    by_draft: dict[str, list[dict]] = {}
    for entry in corpora:
        by_draft.setdefault(entry["draft_name"], []).append(entry)

    catalog: list[dict] = []
    lines: list[str] = []

    if not by_draft:
        lines.append("No evolution corpora found in this Dify instance.")
    else:
        for draft_name in sorted(by_draft):
            entries = sorted(by_draft[draft_name], key=operator.itemgetter("created_at"), reverse=True)
            newest = entries[0]
            wg = _resolve_wg_for_draft(draft_name)
            rfc_number = _resolve_rfc_number_for_draft(draft_name)
            mailarchive_dataset_id = _MAILARCHIVE_REGISTRY.get(wg) if wg else None
            catalog.append(
                {
                    "draft_name": draft_name,
                    "rfc_number": rfc_number,
                    "wg": wg,
                    "evolution_dataset_id": newest["dataset_id"],
                    "mailarchive_dataset_id": mailarchive_dataset_id,
                }
            )
            rfc_suffix = f", published as RFC {rfc_number}" if rfc_number else " (not yet published as an RFC)"
            lines.append(
                f"Draft: {draft_name}{rfc_suffix}" + (f" (WG: {wg})" if wg else " (WG: could not be resolved)")
            )
            for entry in entries:
                marker = " <- most recent, recommended" if entry is newest else ""
                lines.append(f"  evolution dataset_id={entry['dataset_id']} name={entry['dataset_name']!r}{marker}")
            if mailarchive_dataset_id:
                lines.append(f"  mailarchive dataset_id={mailarchive_dataset_id}")
            else:
                lines.append("  mailarchive dataset: none registered for this WG -- discussion search unavailable")

    # Local sources (Sources tab "Add a new draft as a source") are never a
    # real Dify dataset -- see services.ietf_import_service.register_local_source
    # -- so they don't belong in `catalog` (which drives real dataset_id
    # hints/resolution elsewhere in ask()). Mentioned here purely as extra
    # context so the model knows search_local_source is worth trying for
    # them, since there's no other discovery path that would tell it these
    # exist at all.
    registered_names = set(by_draft.keys())
    local_lines = _list_indexed_local_sources(exclude=registered_names)
    if local_lines:
        lines.append("")
        lines.append(
            "Local-only sources (added via the Sources tab, indexed locally via BM25 -- "
            "search with search_local_source, NOT search_ietf_evolution_dataset/"
            "search_ietf_mailarchive_dataset, since these have no real Dify dataset):"
        )
        lines.extend(local_lines)

    return "\n".join(lines), catalog


def _list_indexed_local_sources(exclude: set[str]) -> list[str]:
    """One line per locally-tracked source that actually has a built
    search_local_source index -- an unindexed local source (downloaded but
    "Build index" never clicked) has nothing for the tool to search, so
    it's left out rather than advertising a call that would just fail.
    """
    from services.ietf_import_service import IetfImportService
    from services.local_source_index_service import LocalSourceIndexService

    lines = []
    for draft_name in IetfImportService.list_local_source_names():
        if draft_name in exclude:
            continue
        status = LocalSourceIndexService.get_index_status(draft_name)
        if not status.get("built"):
            continue
        by_kind = status.get("by_kind", {})
        kind_summary = ", ".join(f"{n} {k}" for k, n in by_kind.items())
        lines.append(f"  {draft_name}: {status.get('total_documents', 0)} documents indexed ({kind_summary})")
    return lines


def _resolve_draft_hint(hint: str, catalog: list[dict]) -> dict | None:
    """Match a human-given --draft hint (draft name, RFC number, or WG
    acronym, in any casing) against a corpora catalog entry. Returns None if
    it matches nothing or more than one entry -- callers should treat both as
    "couldn't resolve" rather than guessing.
    """
    needle = hint.strip().lower()
    needle_rfc = re.sub(r"^rfc\s*", "", needle)

    matches = [
        entry
        for entry in catalog
        if needle == entry["draft_name"].lower()
        or (entry.get("wg") and needle == entry["wg"].lower())
        or (entry.get("rfc_number") and needle_rfc == entry["rfc_number"])
    ]
    return matches[0] if len(matches) == 1 else None


_EVOLUTION_ENTRY_RE = re.compile(
    r"^ietf-evolution draft:(\S+) from:(\S+) to:(\S+) section:(\S+) change:(added|removed|modified)(?: \| (.*))?"
)
# generate_draft_evolution (ietf_import_service.py) emits exactly one of
# these three fixed phrasings depending on change type -- a closed set, not
# a heuristic, so matching them directly is reliable rather than guessing at
# arbitrary before/after prose.
_BODY_MODIFIED_RE = re.compile(r"Before \(rev \S+\):\n(.*?)\n\nAfter \(rev \S+\):\n(.*)", re.DOTALL)
_BODY_ADDED_RE = re.compile(r"New section added in rev \S+:\n(.*)", re.DOTALL)
_BODY_REMOVED_RE = re.compile(r"Section removed after rev \S+:\n(.*)", re.DOTALL)
_THREAD_SUBJECT_RE = re.compile(r"^Thread: (.+?) \(")
# build_thread_records (services/email_threading.py) embeds each message's
# archive URL in its own marker line so citations survive child splitting.
# That url is a real https://mailarchive.ietf.org/... permalink only for
# mail from ~2014 onward (Archived-At header); older mail falls back to a
# synthetic "mid:<message-id>" identity (services/ietf_import_service.py),
# which is not a fetchable link -- callers must check the scheme.
_MESSAGE_MARKER_RE = re.compile(r"^--- Message \d+ \| (.+?) \| (.+?) \| (\S+) ---$", re.MULTILINE)


def _extract_message_links(text: str) -> list[dict]:
    """Pull {from, date, url} per message marker out of a mailarchive
    segment's text, keeping only entries with a real http(s) permalink --
    pre-2014 mail has no Archived-At header and only carries a non-fetchable
    "mid:..." identity, so it's dropped here rather than shown as a dead link.
    """
    links = []
    for match in _MESSAGE_MARKER_RE.finditer(text):
        from_field, date_field, url = match.group(1), match.group(2), match.group(3)
        if url.startswith("http://") or url.startswith("https://"):
            links.append({"from": from_field, "date": date_field, "url": url})
    return links


def _resolve_discussion_links(mailarchive_dataset, candidate: dict) -> list[dict]:
    """Find the real archive link(s) for a winning discussion candidate.

    The candidate's own text usually already contains build_thread_records()'s
    marker line for the message it's from -- but Economy/BM25 indexing can
    split one thread-part document into several keyword segments, and a BM25
    hit can land on a segment further into a message's body, past its marker
    line (spot-checked directly against live data: this genuinely happens).
    When that leaves _extract_message_links empty, walk backward through the
    same document's earlier segments (by position) for the nearest preceding
    marker instead of surfacing no link at all.
    """
    links = _extract_message_links(candidate["text"])
    if links or not candidate.get("document_id"):
        return links

    from extensions.ext_database import db
    from models.dataset import DocumentSegment

    siblings = (
        db.session.query(DocumentSegment)
        .filter(
            DocumentSegment.dataset_id == mailarchive_dataset.id,
            DocumentSegment.document_id == candidate["document_id"],
        )
        .order_by(DocumentSegment.position)
        .all()
    )
    cutoff = next((i for i, seg in enumerate(siblings) if seg.index_node_id == candidate.get("doc_id")), None)
    if cutoff is None:
        return []
    for seg in reversed(siblings[: cutoff + 1]):
        found = _extract_message_links(seg.content)
        if found:
            return found[-1:]
    return []


def _find_discussion_candidates(mailarchive_dataset, query_text: str, top_k: int = 5) -> list[dict]:
    """Gather candidate mailarchive messages for one specific change's own
    wording -- NOT a relevance verdict. Tried treating "any non-administrative
    BM25 hit" as the verdict directly (dropping Secretariat/datatracker bot
    noise via the same _is_administrative_notice heuristic
    services/ietf_import_service.py uses at ingestion time); on a real
    89-edge thread that flagged 100% of edges as "having discussion" --
    zero signal, because a WG mailing list is topically homogeneous enough
    that "shares vocabulary with the change" and "actually explains this
    specific change" aren't separable by keyword retrieval alone. The
    administrative filter still earns its keep here as a cheap pre-filter
    to shrink what the judgment call in _judge_lane_discussion has to read,
    just not as the accept/reject decision itself.

    Each returned dict is {"text", "document_id", "doc_id"} rather than a
    bare string -- document_id/doc_id (the segment's index_node_id) are kept
    so a winning candidate's real archive URL can be traced back to its
    source segment later, even when the candidate text itself doesn't carry
    a message marker line (see _resolve_discussion_links).
    """
    from services.ietf_import_service import IetfImportService

    if not mailarchive_dataset or not query_text.strip():
        return []
    # Lesson from this project's own retrieval testing: a short, distinctive
    # snippet beats a whole paragraph as a BM25 query.
    query = " ".join(query_text.split()[:30])
    hits = _retrieve_documents(mailarchive_dataset, query, top_k=top_k)
    kept = []
    for hit in hits:
        text = hit.page_content
        subject_match = _THREAD_SUBJECT_RE.match(text)
        subject = subject_match.group(1) if subject_match else ""
        if subject and IetfImportService._is_administrative_notice(subject):
            continue
        kept.append(
            {
                "text": text,
                "document_id": hit.metadata.get("document_id") if hit.metadata else None,
                "doc_id": hit.metadata.get("doc_id") if hit.metadata else None,
            }
        )
        if len(kept) >= 3:
            break
    return kept


_LANE_DISCUSSION_INSTRUCTION = (
    "You are auditing whether specific email discussions genuinely explain specific "
    "design-decision changes in an IETF draft. You are given one section's decision "
    "text across several consecutive revisions, and for each transition between two "
    "revisions, a numbered list of candidate mailing-list messages found via keyword "
    "search -- not yet verified as actually relevant.\n\n"
    "For each transition, decide whether ANY candidate specifically explains why the "
    "text changed the way it did for THAT transition -- not just a message about the "
    "same general topic or protocol. Multiple unrelated decisions get discussed in "
    "the same time period on a WG mailing list; a candidate only counts if it "
    "specifically connects to this exact change. It is common and expected for a "
    "transition to have no genuinely relevant candidate -- say so plainly rather "
    "than picking the closest topical match out of reluctance to say none.\n\n"
    "Respond with exactly one line per transition, in the same order given, in this "
    "exact format and nothing else:\n"
    "TRANSITION <n>: RELEVANT <candidate number>\n"
    "or\n"
    "TRANSITION <n>: NONE"
)


def _judge_lane_discussion(
    lane: dict,
    mailarchive_dataset,
    tenant_id: str,
    model_provider: str,
    model_name: str,
    on_event=None,
) -> None:
    """Mutate lane['edges'] in place, setting has_discussion/discussion per
    edge via a SINGLE LLM call over the whole lane's transitions together --
    not one call per edge. Giving the model every transition's before/after
    text and candidates at once (rather than one edge in isolation) lets it
    correctly attribute which candidate belongs to which transition, and
    keeps the cost to one call per lane regardless of how many edges it has
    (a 13-lane thread costs ~13 calls, not one per edge).

    No-ops (leaves has_discussion=False, discussion=None) if no edge has any
    candidates, or if the model's response doesn't parse -- conservative by
    design, matching this project's existing rule that an unsupported claim
    should be dropped, not guessed into looking supported.
    """
    from core.model_manager import ModelManager
    from dify_graph.model_runtime.entities import SystemPromptMessage, UserPromptMessage
    from dify_graph.model_runtime.entities.model_entities import ModelType

    for edge in lane["edges"]:
        edge["has_discussion"] = False
        edge["discussion"] = None
        edge["discussion_links"] = []

    if not any(edge.get("_candidates") for edge in lane["edges"]):
        return

    prompt_lines = [f"Section {lane['section']} ({lane['title'] or 'untitled'})", ""]
    for i, edge in enumerate(lane["edges"], start=1):
        prompt_lines.append(f"=== TRANSITION {i}: rev {edge['from_rev']} -> rev {edge['to_rev']} ===")
        candidates = edge.get("_candidates") or []
        if not candidates:
            prompt_lines.append("(no candidate messages found)")
        for j, cand in enumerate(candidates, start=1):
            prompt_lines.append(f"--- candidate {j} ---\n{cand['text'][:600]}")
        prompt_lines.append("")

    model_instance = ModelManager().get_model_instance(
        tenant_id=tenant_id, provider=model_provider, model_type=ModelType.LLM, model=model_name
    )
    if on_event:
        title_suffix = f" ({lane['title']})" if lane["title"] else ""
        on_event({"stage": "thinking", "message": f"Thinking about §{lane['section']}{title_suffix}…"})
    try:
        result = _invoke_llm_with_retry(
            model_instance,
            prompt_messages=[
                SystemPromptMessage(content=_LANE_DISCUSSION_INSTRUCTION),
                UserPromptMessage(content="\n".join(prompt_lines)),
            ],
            model_parameters={"temperature": 0.1},
            tools=[],
            stop=[],
            stream=False,
            user="ask_decision_agent_lane_discussion",
        )
    except Exception:
        return

    content = result.message.content
    content = content if isinstance(content, str) else str(content)

    verdict_re = re.compile(r"TRANSITION\s+(\d+):\s*(RELEVANT\s+(\d+)|NONE)", re.IGNORECASE)
    for match in verdict_re.finditer(content):
        idx = int(match.group(1)) - 1
        if not (0 <= idx < len(lane["edges"])):
            continue
        if match.group(3):
            candidates = lane["edges"][idx].get("_candidates") or []
            cand_idx = int(match.group(3)) - 1
            if 0 <= cand_idx < len(candidates):
                winner = candidates[cand_idx]
                lane["edges"][idx]["has_discussion"] = True
                lane["edges"][idx]["discussion"] = winner["text"]
                lane["edges"][idx]["discussion_links"] = _resolve_discussion_links(mailarchive_dataset, winner)


def _parse_evolution_body(body: str) -> tuple[str | None, str | None]:
    """Extract (before_text, after_text) from an evolution segment's body --
    the part after its self-describing header line. Either side can be None
    (an "added" entry has no before-state; a "removed" one has no after).
    """
    match = _BODY_MODIFIED_RE.search(body)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    match = _BODY_ADDED_RE.search(body)
    if match:
        return None, match.group(1).strip()
    match = _BODY_REMOVED_RE.search(body)
    if match:
        return match.group(1).strip(), None
    return None, None


def _drafts_with_matches(query: str, catalog: list[dict]) -> list[str]:
    """Which draft names have at least one evolution-dataset segment whose
    header+body contains *query* (case-insensitive) -- used to auto-resolve
    "no specific draft given" deterministically, via the same content
    search decision_thread() itself uses, rather than guessing or requiring
    an LLM call just to pick a draft.
    """
    from extensions.ext_database import db
    from models.dataset import DocumentSegment

    needle = query.strip().lower()
    matches = []
    for entry in catalog:
        segments = db.session.query(DocumentSegment).filter_by(dataset_id=entry["evolution_dataset_id"]).all()
        if any(s.content and needle in s.content.lower() for s in segments):
            matches.append(entry["draft_name"])
    return matches


def decision_thread(
    draft_hint: str | None,
    query: str,
    model_provider: str = _DEFAULT_MODEL_PROVIDER,
    model_name: str = _DEFAULT_MODEL_NAME,
    on_event=None,
) -> dict:
    """Trace one specific decision's actual text across every revision where
    it changed, compressing over every revision where it didn't -- unlike
    a full-corpus change graph, this is a per-version, paragraph-level view
    of a single parameter/decision's history (e.g. "how did INIT_CWND's
    value change over time", not "what changed in every section").

    Matches every evolution-dataset segment whose header+body contains
    *query* (case-insensitive substring), across the WHOLE draft history --
    not just one from/to hop -- then chains each match's before/after text
    into per-revision nodes. Paragraph-split duplicates of the same logical
    change (same from_rev/to_rev pair -- see _split_modified_paragraphs in
    ietf_import_service.py) are collapsed to the first match, same spirit as
    evolution_timeline()'s per-edge dedup.

    draft_hint may be empty/None -- unlike ask(), which can lean on the
    model's own reasoning to disambiguate an unspecified draft from the
    question's wording, decision_thread() has no LLM step for draft
    selection, so "which draft" is resolved deterministically instead: every
    known draft is content-searched for the query, and the hint auto-
    resolves only when exactly one draft has any match. Raises ValueError
    if a given hint doesn't resolve to exactly one known draft, if no draft
    matches when none was given, or if more than one does (silently picking
    one would risk showing the wrong draft's history, which is worse than
    asking to be specific).

    on_event: optional callback invoked with {"stage": ..., "message": ...}
    at each major step -- for a caller (serve_ui.py's SSE route) that wants
    to stream live progress rather than block silently until the whole,
    potentially multi-lane, multi-LLM-call result is ready. None (the
    default) skips this entirely.
    """
    from extensions.ext_database import db
    from models.dataset import Dataset, DocumentSegment

    def _emit(stage: str, message: str) -> None:
        if on_event:
            on_event({"stage": stage, "message": message})

    _, catalog = _list_available_corpora()
    if draft_hint and draft_hint.strip():
        _emit("resolving", f"Resolving draft {draft_hint!r}…")
        resolved = _resolve_draft_hint(draft_hint, catalog)
        if not resolved:
            known = ", ".join(entry["draft_name"] for entry in catalog) or "(none found)"
            raise ValueError(f"{draft_hint!r} did not match exactly one known draft. Known drafts: {known}")
    else:
        _emit("resolving", f"No draft given -- searching every known draft for {query!r}…")
        matching_drafts = _drafts_with_matches(query, catalog)
        if not matching_drafts:
            raise ValueError(f"No known draft has any revision mentioning {query!r}.")
        if len(matching_drafts) > 1:
            raise ValueError(
                f"{query!r} matches more than one draft ({', '.join(matching_drafts)}) -- pick one specific draft."
            )
        resolved = next(entry for entry in catalog if entry["draft_name"] == matching_drafts[0])
        _emit("resolving", f"Auto-resolved to {resolved['draft_name']} (only draft mentioning {query!r})")

    draft_name = resolved["draft_name"]
    _emit("searching", f"Searching evolution dataset for {query!r}…")
    segments = (
        db.session.query(DocumentSegment)
        .filter_by(dataset_id=resolved["evolution_dataset_id"])
        .order_by(DocumentSegment.position)
        .all()
    )
    mailarchive_dataset = None
    if resolved.get("mailarchive_dataset_id"):
        mailarchive_dataset = db.session.query(Dataset).filter_by(id=resolved["mailarchive_dataset_id"]).first()

    needle = query.strip().lower()

    def _rev_key(rev: str) -> int:
        return int(rev) if rev.isdigit() else 10**9

    raw_matches = []
    for segment in segments:
        if not segment.content or needle not in segment.content.lower():
            continue
        match = _EVOLUTION_ENTRY_RE.match(segment.content)
        if not match:
            continue
        _draft, from_rev, to_rev, section, change_type, title = match.groups()
        before_text, after_text = _parse_evolution_body(segment.content[match.end() :])
        raw_matches.append(
            {
                "from_rev": from_rev,
                "to_rev": to_rev,
                "section": section,
                "type": change_type,
                "title": (title or "").strip(),
                "before": before_text,
                "after": after_text,
            }
        )
    raw_matches.sort(key=lambda m: _rev_key(m["from_rev"]))

    def _section_sort_key(section: str) -> tuple:
        # (0, int) for numeric parts, (1, str) for non-numeric ones ("M1"-
        # style appendix sections) -- keeps every tuple position the same
        # shape so mixed numeric/non-numeric sections never raise comparing
        # an int to a str.
        return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.findall(r"\d+|\D+", section))

    def _build_lane(section_matches: list[dict]) -> dict:
        nodes: dict[str, str] = {}
        edges: list[dict] = []
        seen_hops: set[tuple[str, str]] = set()
        title = ""
        for m in section_matches:
            hop = (m["from_rev"], m["to_rev"])
            if hop in seen_hops:
                continue
            seen_hops.add(hop)
            title = title or m["title"]
            if m["before"] and m["from_rev"] not in nodes:
                nodes[m["from_rev"]] = m["before"]
            if m["after"]:
                nodes[m["to_rev"]] = m["after"]
            edges.append(
                {
                    "from_rev": m["from_rev"],
                    "to_rev": m["to_rev"],
                    "type": m["type"],
                    "diff_url": _iddiff_page_url(draft_name, m["from_rev"], m["to_rev"]),
                }
            )

        ordered_revs = sorted(nodes.keys(), key=_rev_key)
        node_list = [{"rev": rev, "text": nodes[rev]} for rev in ordered_revs]

        # Rebuild edges aligned 1:1 with consecutive node pairs -- a matched
        # hop can skip revisions the query didn't match at all (e.g.
        # rephrased away and back), leaving no recorded edge directly
        # between two adjacent nodes. Falling back to a freshly built diff
        # URL for that exact pair means the caller never has to
        # special-case a missing connector.
        edge_by_hop = {(e["from_rev"], e["to_rev"]): e for e in edges}
        display_edges = []
        for from_rev, to_rev in zip(ordered_revs, ordered_revs[1:], strict=False):
            edge = edge_by_hop.get((from_rev, to_rev)) or {
                "from_rev": from_rev,
                "to_rev": to_rev,
                "type": "modified",
                "diff_url": _iddiff_page_url(draft_name, from_rev, to_rev),
            }
            display_edges.append(edge)

        for i, edge in enumerate(display_edges):
            edge["_candidates"] = _find_discussion_candidates(mailarchive_dataset, node_list[i + 1]["text"])

        return {"section": section_matches[0]["section"], "title": title, "nodes": node_list, "edges": display_edges}

    # A bare keyword can genuinely span unrelated places -- a value-setting
    # sentence, a pseudocode listing, a requirements discussion -- each in a
    # different section. Merging all of it into one flat sequence would jump
    # between unrelated stories, so each section becomes its own
    # independently-tracked lane instead. Not renumbering-aware (a section
    # that changes number mid-history splits into two lanes) -- this
    # codebase has no tool to resolve true cross-revision section identity
    # either (_compute_section_lineage in ietf_import_service.py only
    # aggregates by exact section-id string too), so this isn't introducing
    # a new inconsistency.
    by_section: dict[str, list[dict]] = {}
    for m in raw_matches:
        by_section.setdefault(m["section"], []).append(m)
    sorted_sections = sorted(by_section, key=_section_sort_key)
    _emit(
        "matched",
        f"Found {len(raw_matches)} matching change(s) across {len(sorted_sections)} section(s)",
    )
    lanes = []
    for section in sorted_sections:
        _emit("building", f"Building §{section} timeline…")
        lanes.append(_build_lane(by_section[section]))

    if mailarchive_dataset:
        for lane in lanes:
            _judge_lane_discussion(
                lane, mailarchive_dataset, mailarchive_dataset.tenant_id, model_provider, model_name, on_event=on_event
            )
    for lane in lanes:
        for edge in lane["edges"]:
            edge.pop("_candidates", None)
            edge.setdefault("has_discussion", False)
            edge.setdefault("discussion", None)
            edge.setdefault("discussion_links", [])

    return {
        "draft_name": draft_name,
        "rfc_number": resolved.get("rfc_number"),
        "query": query,
        "lanes": lanes,
    }


def _retrieve(dataset, query: str, top_k: int) -> list[str]:
    return [doc.page_content for doc in _retrieve_documents(dataset, query, top_k)]


def _retrieve_documents(dataset, query: str, top_k: int) -> list:
    """Same dispatch _retrieve does, but keeps each hit's full Document
    (page_content + metadata) instead of discarding metadata -- callers that
    need to trace a hit back to its source segment/document (e.g. resolving
    a discussion candidate's real archive URL) need doc.metadata["document_id"]
    and ["doc_id"], which the flattened string list can't carry.
    """
    from core.rag.datasource.retrieval_service import RetrievalService
    from core.rag.index_processor.constant.index_type import IndexTechniqueType
    from core.rag.retrieval.retrieval_methods import RetrievalMethod

    if dataset.indexing_technique == IndexTechniqueType.ECONOMY:
        return RetrievalService.retrieve(
            retrieval_method=RetrievalMethod.KEYWORD_SEARCH,
            dataset_id=dataset.id,
            query=query,
            top_k=top_k,
        )
    retrieval_model = dataset.retrieval_model or {}
    return RetrievalService.retrieve(
        retrieval_method=retrieval_model.get("search_method", RetrievalMethod.SEMANTIC_SEARCH.value),
        dataset_id=dataset.id,
        query=query,
        top_k=top_k,
        score_threshold=(
            retrieval_model.get("score_threshold", 0.0) if retrieval_model.get("score_threshold_enabled") else 0.0
        ),
        reranking_model=(
            retrieval_model.get("reranking_model") if retrieval_model.get("reranking_enable") else None
        ),
        reranking_mode=retrieval_model.get("reranking_mode") or "reranking_model",
        weights=retrieval_model.get("weights"),
    )


def _format_hits(hits: list[str], start: int = 1) -> str:
    """start lets a caller keep citation numbers globally unique across
    multiple search calls in the same ask() run (pass len(evidence-so-far)
    + 1) -- defaults to 1 so every existing caller/test is unaffected.
    """
    if not hits:
        return "(no results)"
    return "\n\n".join(f"[{i}] {hit}" for i, hit in enumerate(hits, start=start))


_EVOLUTION_HEADER_RE = re.compile(r"^ietf-evolution draft:(\S+) from:(\S+) to:(\S+)")
_IDDIFF_TOOL_BASE = "https://author-tools.ietf.org/iddiff"


def _iddiff_page_url(draft_name: str, from_rev: str, to_rev: str) -> str:
    """Build a live-diff URL on IETF's own hosted iddiff-as-a-service tool
    (author-tools.ietf.org) -- a real, clickable citation a human reader can
    follow to see the canonical diff themselves, rather than trusting our own
    rendering of it. url1/url2 both being real, adjacent draft revisions
    (guaranteed by how _diff_version_pair iterates) means this URL is always
    valid, no network call needed to build it.
    """
    from urllib.parse import urlencode

    params = {
        "url1": f"{draft_name}-{from_rev}",
        "url2": f"{draft_name}-{to_rev}",
        "difftype": "--html",
    }
    return f"{_IDDIFF_TOOL_BASE}?{urlencode(params)}"


def _format_evolution_hits(hits: list[str], start: int = 1) -> tuple[str, list[tuple[str, str, str, str]]]:
    """Like _format_hits, but appends a cheap, always-available "Live diff"
    citation URL to each hit -- parsed straight from the hit's own
    self-describing header (draft:X from:A to:B), zero extra network calls.
    Give the agent something to cite immediately; cite_iddiff_url (a
    separate, explicit tool) is for when it wants the pricier, more precise
    version that jumps to the exact changed hunk on that page.

    Also returns the raw (header, from_rev, to_rev, url) tuples for every
    hit, in rank order -- ask()'s _finalize_answer uses these to find the
    citation that actually matches what the final answer discusses, rather
    than blindly citing rank [1] (confirmed necessary: a real run cited the
    correct revision pair inline in its own text while the top-ranked
    evolution hit for that query was a *different*, less relevant pair).

    start: see _format_hits -- same global-numbering purpose.
    """
    if not hits:
        return "(no results)", []
    blocks = []
    citations: list[tuple[str, str, str, str]] = []
    for i, hit in enumerate(hits, start=start):
        match = _EVOLUTION_HEADER_RE.match(hit)
        if match:
            draft_name, from_rev, to_rev = match.groups()
            url = _iddiff_page_url(draft_name, from_rev, to_rev)
            header = hit.splitlines()[0]
            citations.append((header, from_rev, to_rev, url))
            blocks.append(f"[{i}] {hit}\nLive diff: {url}")
        else:
            blocks.append(f"[{i}] {hit}")
    return "\n\n".join(blocks), citations


def _http_get_text(url: str) -> str | None:
    import httpx

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                return None
            return resp.text
    except httpx.HTTPError:
        return None


def _filter_primary_source_text(text: str, query: str | None, source_label: str, max_chars: int = 6000) -> str:
    """Return either the whole document (capped) or just paragraphs matching *query*."""
    if not query:
        if len(text) > max_chars:
            return (
                f"[{source_label}, showing first {max_chars} of {len(text)} chars -- "
                f"pass a `query` to search for a specific phrase instead]\n\n{text[:max_chars]}"
            )
        return f"[{source_label}, full text]\n\n{text}"

    paragraphs = re.split(r"\n\s*\n", text)
    matches = [p.strip() for p in paragraphs if query.lower() in p.lower()]
    if not matches:
        return f"[{source_label}] No paragraph containing {query!r} was found in this document."
    joined = "\n\n---\n\n".join(matches)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n\n[truncated]"
    return f"[{source_label}, {len(matches)} paragraph(s) matching {query!r}]\n\n{joined}"


def _fetch_draft_text(draft_name: str, revision: str, query: str | None) -> str:
    revision = revision.strip().zfill(2)
    url = f"{_ARCHIVE_BASE}{draft_name}-{revision}.txt"
    text = _http_get_text(url)
    if text is None:
        return f"Could not fetch {url} -- check the draft name and revision number (e.g. '09')."
    return _filter_primary_source_text(text, query, source_label=f"{draft_name}-{revision}")


def _fetch_rfc_text(rfc_number: str, query: str | None) -> str:
    number = re.sub(r"[^0-9]", "", rfc_number)
    if not number:
        return f"'{rfc_number}' doesn't look like an RFC number."
    url = f"{_RFC_EDITOR_BASE}rfc{number}.txt"
    text = _http_get_text(url)
    if text is None:
        return f"Could not fetch {url} -- check the RFC number."
    return _filter_primary_source_text(text, query, source_label=f"RFC{number}")


# Where the Sources tab's "Add sources" downloads land (services/ietf_import_service.py
# download_wg_resource_to_local) -- searched here as plain local files, not a Dify
# dataset, since these are occasional cross-checks, not a corpus worth indexing.
_RESOURCES_DIR = Path("/app/api/storage/ietf-downloads/resources")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _search_wg_minutes(wg: str, query: str, max_results: int = 5) -> str:
    """Search this WG's already-downloaded meeting minutes for a phrase --
    local file search only, never a live fetch or a new download. Downloads
    are strictly user-triggered (the Sources tab's own "Download" button,
    never this agent -- see that tab's design), so a WG with nothing
    downloaded yet is told so plainly rather than the tool fetching
    anything itself.
    """
    files = sorted(_RESOURCES_DIR.glob(f"minutes-*-{wg}.*")) if _RESOURCES_DIR.exists() else []
    if not files:
        return (
            f"No minutes downloaded locally for WG '{wg}' yet -- the mail archive alone may not have this "
            "discussion. Suggest the user download minutes from the Sources tab if they want it checked."
        )

    terms = [t.lower() for t in query.split() if t]
    matches: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix == ".html":
            text = _HTML_TAG_RE.sub(" ", text)
        lowered = text.lower()
        if terms and not all(term in lowered for term in terms):
            continue
        idx = lowered.find(terms[0]) if terms else 0
        start, end = max(0, idx - 200), min(len(text), idx + 400)
        snippet = " ".join(text[start:end].split())
        matches.append(f"{path.stem}: ...{snippet}...")
        if len(matches) >= max_results:
            break

    if not matches:
        return f"Searched {len(files)} downloaded minutes for WG '{wg}' -- no match for {query!r}."
    return "\n\n".join(matches)


def _get_rfc_relations(rfc_number: str) -> str:
    """Format IetfImportService.get_rfc_relations() for the model -- which
    RFCs this one obsoletes/is obsoleted by and updates/is updated by, so a
    decision that looks unexplained in this RFC's own history can be
    checked against the family it actually belongs to (e.g. a later RFC
    fixing or superseding a specific part of an earlier one).
    """
    from services.ietf_import_service import IetfImportService

    data = IetfImportService.get_rfc_relations(rfc_number)
    if data.get("error"):
        return f"Could not look up relations for {rfc_number}: {data['error']}"

    lines = [f"Relations for {data['rfc']}:"]
    if data["obsoletes"]:
        lines.append(f"  Obsoletes: {', '.join(data['obsoletes'])}")
    if data["obsoleted_by"]:
        lines.append(f"  Obsoleted by: {', '.join(data['obsoleted_by'])}")
    if data["updates"]:
        lines.append(f"  Updates: {', '.join(data['updates'])}")
    if data["updated_by"]:
        lines.append(f"  Updated by: {', '.join(data['updated_by'])}")
    if len(lines) == 1:
        lines.append("  No obsoletes/updates relations found -- this RFC stands alone in that graph.")
    return "\n".join(lines)


def _find_related_rfc_family(rfc_number: str) -> str:
    """Format IetfImportService.find_related_rfc_family() for the model --
    the topically-related RFCs found via citation + keyword overlap, for
    topics (DNSSEC is the motivating example) that are explained across a
    cluster of RFCs with no obsoletes/updates relation tying them together
    at all. A heuristic, not a guarantee -- an empty family means the
    heuristic didn't find one, not that no related RFCs exist.
    """
    from services.ietf_import_service import IetfImportService

    data = IetfImportService.find_related_rfc_family(rfc_number)
    if data.get("error"):
        return f"Could not look up the RFC family for {rfc_number}: {data['error']}"

    if not data["family"]:
        keyword_note = f" (topic keyword tried: {data['topic_keyword']!r})" if data.get("topic_keyword") else ""
        return (
            f"No related-RFC family found for {data['rfc']}{keyword_note} -- "
            "this heuristic found nothing, not proof nothing exists."
        )

    lines = [f"RFCs related to {data['rfc']} (topic keyword: {data['topic_keyword']!r}):"]
    for entry in data["family"]:
        arrow = "cites" if entry.get("direction") == "cites" else "cited by"
        level = entry["std_level"].upper() if entry["std_level"] else "?"
        lines.append(f"  [{arrow}] {entry['rfc']} ({level}): {entry['title']}")
    return "\n".join(lines)


def _search_rfc_corpus_bm25(query: str, scope: str, top_k: int) -> str:
    """Format RfcCorpusIndexService.search_bm25() for the model -- a
    whole-corpus lexical search, unlike find_related_rfc_family which only
    considers RFCs already linked to one specific RFC by citation. Useful
    both for "find an RFC by topic" when there's no starting RFC yet, and
    as a cross-check when the citation-based family heuristic comes up
    empty (e.g. an RFC whose own keyword is a narrow acronym that related
    RFCs' titles don't repeat -- BM25 over the full title+abstract text
    can still surface them on shared vocabulary elsewhere in the text).
    """
    from services.rfc_corpus_index_service import RfcCorpusIndexService

    data = RfcCorpusIndexService.search_bm25(query, scope=scope, top_k=top_k)
    if data.get("error"):
        return f"BM25 search failed: {data['error']}"
    if not data["results"]:
        return f"No RFCs matched {query!r} via BM25 (scope={scope})."
    lines = [f"Top BM25 matches for {query!r} (scope={scope}):"]
    for r in data["results"]:
        lines.append(f"  {r['rfc']} (score {r['score']}): {r['title']}")
    return "\n".join(lines)


def _find_similar_rfcs_by_meaning(query: str, top_k: int) -> str:
    """Format RfcCorpusIndexService.search_semantic() for the model --
    embedding-based similarity over title+abstract, which catches RFCs
    using different terminology for the same idea (an RFC's own internal
    acronym vs. the descriptive phrase other RFCs use in their titles)
    that neither exact-keyword matching nor BM25's term overlap would.
    """
    from services.rfc_corpus_index_service import RfcCorpusIndexService

    data = RfcCorpusIndexService.search_semantic(query, top_k=top_k)
    if data.get("error"):
        return f"Semantic search failed: {data['error']}"
    if not data["results"]:
        return f"No RFCs found for {query!r}."
    lines = [f"RFCs semantically closest to {query!r}:"]
    for r in data["results"]:
        lines.append(f"  {r['rfc']} (similarity {r['similarity']}): {r['title']}")
    return "\n".join(lines)


def _search_local_source(draft_name: str, query: str, top_k: int) -> str:
    """Format LocalSourceIndexService.search() for the model -- BM25 over
    one locally-tracked source's own downloaded content (draft/RFC text
    plus threaded mail; see services/local_source_index_service.py). This
    is the ONLY way this agent can search a Sources-tab-added draft that
    isn't a real Dify dataset -- search_ietf_evolution_dataset and
    search_ietf_mailarchive_dataset only work for drafts with an actual
    dataset_id, which a local source never has.

    Accepts an RFC number too, resolved the same way add_local_source does
    -- confirmed live that a model which had just pulled a source by RFC
    number turned around and searched for it by that same RFC number
    rather than its draft name (both refer to the same thing to a human,
    and there's no reason the model should have to track which form this
    one tool happens to require).
    """
    from services.ietf_import_service import IetfImportService
    from services.local_source_index_service import LocalSourceIndexService

    rfc_match = _RFC_IDENTIFIER_RE.match(draft_name.strip())
    if rfc_match:
        resolved = IetfImportService.resolve_draft_name_for_rfc(rfc_match.group(1))
        if resolved:
            draft_name = resolved

    data = LocalSourceIndexService.search(draft_name, query, top_k=top_k)
    if data.get("error"):
        return f"Local search failed for {draft_name!r}: {data['error']}"
    if not data["results"]:
        return f"No local matches for {query!r} in {draft_name!r}."
    lines = [f"Top local BM25 matches for {query!r} in {draft_name!r}:"]
    for r in data["results"]:
        lines.append(f"  [{r['kind']}] {r['id']} (score {r['score']}): {r['snippet']}")
    return "\n".join(lines)


_RFC_IDENTIFIER_RE = re.compile(r"^(?:rfc)?\s*(\d+)$", re.IGNORECASE)


def _add_local_source(identifier: str) -> str:
    """The one tool in this agent with a real side effect: given an RFC
    number or a draft name, actually pull it -- register it as a tracked
    local source, download every revision + its RFC + its WG's mail (raw
    and threaded, see IetfImportService.sync_local_source), then build a
    BM25 index over all of it (LocalSourceIndexService.build_index) so
    search_local_source can use it immediately in the same conversation.
    This is genuinely not free (real network fetches, real disk writes,
    real time -- seconds to roughly a minute depending on the WG's mail
    volume), which is why every call is announced as a progress step, not
    silently absorbed into "thinking".

    Accepts an RFC number (resolved to its draft name via
    IetfImportService.resolve_draft_name_for_rfc -- the reverse of the
    forward draft->RFC lookup used elsewhere) or a draft name directly,
    since find_related_rfc_family/search_rfc_corpus_bm25/get_rfc_relations
    all surface RFC numbers, not draft names, and this project's whole
    local-source machinery is keyed by draft name.
    """
    from services.ietf_import_service import IetfImportService
    from services.local_source_index_service import LocalSourceIndexService

    identifier = identifier.strip()
    rfc_match = _RFC_IDENTIFIER_RE.match(identifier)
    if rfc_match:
        draft_name = IetfImportService.resolve_draft_name_for_rfc(rfc_match.group(1))
        if not draft_name:
            return (
                f"Could not resolve RFC{rfc_match.group(1)} to a draft name -- it may never have "
                "been a separate Internet-Draft, or Datatracker's page layout didn't match. Try "
                "passing the draft name directly if you know it."
            )
    else:
        draft_name = identifier

    result = IetfImportService.sync_local_source(draft_name)
    if result.get("error"):
        return f"Could not sync {draft_name!r}: {result['error']}"

    revisions = result.get("revisions", [])
    ok_revs = sum(1 for r in revisions if r.get("ok"))
    rfc_number = result.get("rfc_number")
    rfc_info = result.get("rfc") or {}
    wg = result.get("wg")
    mail_info = result.get("mail") or {}

    summary = [f"Pulled {draft_name}: {ok_revs}/{len(revisions)} revisions"]
    if rfc_number:
        summary.append(f"RFC {rfc_number} {'ok' if rfc_info.get('ok') else 'failed: ' + str(rfc_info.get('error'))}")
    else:
        summary.append("not yet published as an RFC")
    if wg:
        if mail_info.get("ok"):
            years = mail_info.get("years", [])
            total_emails = sum(y.get("total_emails", 0) for y in years)
            summary.append(f"WG {wg} mail: {total_emails} messages across {len(years)} years, threaded")
        else:
            summary.append(f"WG {wg} mail sync failed: {mail_info.get('error')}")
    else:
        summary.append("no WG resolved -- mail unavailable")

    index_result = LocalSourceIndexService.build_index(draft_name, wg, rfc_number)
    if index_result.get("error"):
        summary.append(f"indexing failed: {index_result['error']}")
    else:
        by_kind = ", ".join(f"{n} {k}" for k, n in index_result.get("by_kind", {}).items())
        summary.append(f"indexed {index_result.get('total_documents', 0)} documents ({by_kind})")
        summary.append(f"-- now searchable via search_local_source(draft_name={draft_name!r}, ...)")

    return "; ".join(summary)


def _get_iesg_ballot_comments(draft_name: str, query: str | None = None) -> str:
    """Format IetfImportService.get_iesg_ballot_comments() for the model --
    IESG DISCUSS/comment text from standards-track review, a real venue for
    decision debate separate from the mailing list and meeting minutes (an
    AD raising a specific technical objection, tied to the exact revision
    it applied to). Optionally filtered to entries mentioning a phrase, the
    same way fetch_ietf_draft_text/fetch_ietf_rfc_text narrow a big primary
    source down to what's actually relevant.
    """
    from services.ietf_import_service import IetfImportService

    data = IetfImportService.get_iesg_ballot_comments(draft_name)
    if data.get("error"):
        return f"Could not fetch IESG ballot comments for {draft_name}: {data['error']}"

    comments = data["comments"]
    if query:
        needle = query.lower()
        comments = [c for c in comments if needle in c["discuss"].lower() or needle in c["comment"].lower()]

    if not comments:
        scope = f" mentioning {query!r}" if query else ""
        return f"No IESG ballot discuss/comment text found for {draft_name}{scope}."

    blocks = []
    for c in comments:
        header = f"--- {c['balloter']} | {c['position']} | rev {c['revision']} | {c['time'][:10]} ---"
        body_parts = []
        if c["discuss"]:
            body_parts.append(f"[DISCUSS]\n{c['discuss']}")
        if c["comment"]:
            body_parts.append(f"[COMMENT]\n{c['comment']}")
        blocks.append(f"{header}\n" + "\n\n".join(body_parts))
    return "\n\n".join(blocks)


def _diff_draft_revisions(draft_name: str, from_revision: str, to_revision: str, query: str | None) -> str:
    """Diff two draft revisions with the real `iddiff` tool (ietf-tools/iddiff on
    PyPI) -- a genuinely independent second opinion on what changed, using its
    own diff/section-splitting logic entirely separate from
    ietf_import_service.py's _diff_version_pair pipeline that built the
    evolution dataset. Useful when that dataset's precomputed diff for a
    change looks incomplete, ambiguous, or absent for a given query.
    """
    import os
    import tempfile

    from iddiff.iddiff import get_abdiff

    from_rev = from_revision.strip().zfill(2)
    to_rev = to_revision.strip().zfill(2)
    text_a = _http_get_text(f"{_ARCHIVE_BASE}{draft_name}-{from_rev}.txt")
    if text_a is None:
        return f"Could not fetch {draft_name}-{from_rev} -- check the draft name and revision number."
    text_b = _http_get_text(f"{_ARCHIVE_BASE}{draft_name}-{to_rev}.txt")
    if text_b is None:
        return f"Could not fetch {draft_name}-{to_rev} -- check the draft name and revision number."

    path_a = path_b = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f1:
            f1.write(text_a)
            path_a = f1.name
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f2:
            f2.write(text_b)
            path_b = f2.name
        diff_text = get_abdiff(path_a, path_b)
    finally:
        for path in (path_a, path_b):
            if path:
                os.unlink(path)

    if not diff_text.strip():
        return f"iddiff found no textual differences between {draft_name}-{from_rev} and {draft_name}-{to_rev}."
    label = f"{draft_name} rev {from_rev} -> rev {to_rev}, via the independent iddiff tool (not the evolution dataset)"
    citation_url = _iddiff_page_url(draft_name, from_rev, to_rev)
    return f"{_filter_primary_source_text(diff_text, query, source_label=label)}\n\nLive diff: {citation_url}"


def _find_iddiff_anchor(page_url: str, query: str) -> str | None:
    """Fetch a rendered author-tools.ietf.org iddiff page and find which
    numbered hunk (<tr id="diffNNNN">, one per contiguous changed block)
    contains *query* -- confirmed directly against a real page that every
    hunk gets one of these anchors, in document order. Returns the anchor id
    (e.g. "diff0023") or None if the page can't be fetched or nothing
    matches (ambiguous multi-hunk matches are also treated as no match --
    citing the wrong exact line would be worse than citing the whole page).
    """
    html = _http_get_text(page_url)
    if html is None:
        return None
    parts = re.split(r'<tr id="(diff\d+)">', html)
    needle = query.lower()
    matches = []
    for i in range(1, len(parts), 2):
        hunk_id = parts[i]
        hunk_text = re.sub(r"<[^>]+>", " ", parts[i + 1] if i + 1 < len(parts) else "")
        hunk_text = re.sub(r"\s+", " ", hunk_text).lower()
        if needle in hunk_text:
            matches.append(hunk_id)
    return matches[0] if len(matches) == 1 else None


def _cite_iddiff_url(draft_name: str, from_revision: str, to_revision: str, query: str | None) -> str:
    """Build a citable, clickable live-diff URL on IETF's own hosted
    author-tools.ietf.org/iddiff service, optionally anchored to the exact
    changed hunk. This is for producing something to put IN the final
    answer for a human to click through, not evidence for the model to
    read -- see _format_evolution_hits for the cheap version attached to
    every evolution search hit automatically, and _diff_draft_revisions for
    the independent-second-opinion tool this shares its URL-building with.
    """
    from_rev = from_revision.strip().zfill(2)
    to_sentinel = to_revision.strip().lower()
    if to_sentinel in ("rfc", "final"):
        rfc_number = _resolve_rfc_number_for_draft(draft_name)
        if not rfc_number:
            return (
                f"{draft_name} doesn't appear to have been published as an RFC yet -- "
                "give a specific revision instead."
            )
        to_target = f"rfc{rfc_number}"
        to_label = f"RFC {rfc_number}"
    else:
        to_rev = to_revision.strip().zfill(2)
        to_target = f"{draft_name}-{to_rev}"
        to_label = f"rev {to_rev}"

    from urllib.parse import urlencode

    params = {"url1": f"{draft_name}-{from_rev}", "url2": to_target, "difftype": "--html"}
    url = f"{_IDDIFF_TOOL_BASE}?{urlencode(params)}"

    if not query:
        return f"{url}  ({draft_name} rev {from_rev} -> {to_label})"

    anchor = _find_iddiff_anchor(url, query)
    if anchor:
        return f"{url}#{anchor}  ({draft_name} rev {from_rev} -> {to_label}, jumps to the matching hunk)"
    return f"{url}  ({draft_name} rev {from_rev} -> {to_label}, couldn't pinpoint an exact hunk for {query!r})"


def _verify_answer(
    draft_answer: str, evidence_log: list[str], model_instance, usage_sink: list | None = None
) -> str:
    from dify_graph.model_runtime.entities import SystemPromptMessage, UserPromptMessage

    if not evidence_log:
        return (
            "No evidence has been retrieved yet -- search the evolution and mailarchive tools before calling "
            "verify_answer."
        )

    evidence_block = "\n\n".join(f"--- evidence [{i}] ---\n{e}" for i, e in enumerate(evidence_log, start=1))
    result = _invoke_llm_with_retry(
        model_instance,
        usage_sink=usage_sink,
        prompt_messages=[
            SystemPromptMessage(content=_VERIFY_INSTRUCTION),
            UserPromptMessage(
                content=(
                    f"=== EVIDENCE RETRIEVED SO FAR ===\n{evidence_block}\n\n"
                    f"=== DRAFT ANSWER TO CHECK ===\n{draft_answer}"
                )
            ),
        ],
        model_parameters={"temperature": 0.1},
        tools=[],
        stop=[],
        stream=False,
        user="ask_decision_agent_verify",
    )
    content = result.message.content
    return content if isinstance(content, str) else str(content)


# Matches verify_skill.md's "CLAIM [N]: stated|inferred|unresolved" line
# convention -- same regex-over-a-text-convention approach as
# _extract_message_links elsewhere in this file, not a new architectural
# pattern. There's no structured-output mode in play here, just a line
# format the verifier is asked to follow.
_CLAIM_LABEL_RE = re.compile(r"^CLAIM \[(\d+)\]:\s*(stated|inferred|unresolved)\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_claim_labels(verify_response: str) -> dict[int, str]:
    """Extracts verify_answer's per-citation classification into
    {citation_number: status}. A citation the verifier didn't emit a CLAIM
    line for at all is simply absent from this dict -- the caller (ask())
    defaults an unclassified citation to "unresolved" rather than this
    function guessing, since failing toward under-claiming confidence is
    the whole point of having three named states instead of a numeric
    score.
    """
    return {int(n): status.lower() for n, status in _CLAIM_LABEL_RE.findall(verify_response)}


def _tool(name: str, description: str, properties: dict, required: list[str]):
    from dify_graph.model_runtime.entities import PromptMessageTool

    return PromptMessageTool(
        name=name,
        description=description,
        parameters={"type": "object", "properties": properties, "required": required},
    )


# Tool names are module-level constants (rather than only local variables
# inside ask()) so a caller outside the agent loop -- serve_ui.py's tool
# listing, for one -- can build the exact same tool set without duplicating
# a second copy of these strings.
EVOLUTION_TOOL_NAME = "search_ietf_evolution_dataset"
MAILARCHIVE_TOOL_NAME = "search_ietf_mailarchive_dataset"
FETCH_DRAFT_TOOL_NAME = "fetch_ietf_draft_text"
FETCH_RFC_TOOL_NAME = "fetch_ietf_rfc_text"
DIFF_REVISIONS_TOOL_NAME = "diff_ietf_draft_revisions"
CITE_DIFF_TOOL_NAME = "cite_iddiff_url"
DISCOVERY_TOOL_NAME = "list_available_ietf_corpora"
VERIFY_TOOL_NAME = "verify_answer"
MINUTES_TOOL_NAME = "search_wg_minutes"
RFC_RELATIONS_TOOL_NAME = "get_rfc_relations"
RFC_FAMILY_TOOL_NAME = "find_related_rfc_family"
IESG_BALLOT_TOOL_NAME = "get_iesg_ballot_comments"
RFC_BM25_SEARCH_TOOL_NAME = "search_rfc_corpus_bm25"
RFC_SEMANTIC_SEARCH_TOOL_NAME = "find_similar_rfcs_by_meaning"
LOCAL_SOURCE_SEARCH_TOOL_NAME = "search_local_source"
ADD_LOCAL_SOURCE_TOOL_NAME = "add_local_source"

# Short, human-facing labels for live progress/UI display -- kept separate
# from the tool names themselves (those are the function-calling contract;
# renaming them would be pure churn) and separate from tool_summaries()'s
# descriptions (those are the model's real instructions, not a UI label).
_TOOL_DISPLAY_NAMES = {
    DISCOVERY_TOOL_NAME: "Discover drafts",
    EVOLUTION_TOOL_NAME: "Search decisions",
    MAILARCHIVE_TOOL_NAME: "Search discussions",
    FETCH_DRAFT_TOOL_NAME: "Fetch draft text",
    FETCH_RFC_TOOL_NAME: "Fetch RFC text",
    DIFF_REVISIONS_TOOL_NAME: "Diff revisions",
    CITE_DIFF_TOOL_NAME: "Cite a diff",
    VERIFY_TOOL_NAME: "Verify answer",
    MINUTES_TOOL_NAME: "Search minutes",
    RFC_RELATIONS_TOOL_NAME: "Check RFC relations (obsoletes/updates)",
    RFC_FAMILY_TOOL_NAME: "Find RFC family (citations)",
    RFC_BM25_SEARCH_TOOL_NAME: "Search all RFCs (BM25 keyword)",
    RFC_SEMANTIC_SEARCH_TOOL_NAME: "Find similar RFCs (semantic)",
    IESG_BALLOT_TOOL_NAME: "Check IESG ballot",
    LOCAL_SOURCE_SEARCH_TOOL_NAME: "Search local source (BM25)",
    ADD_LOCAL_SOURCE_TOOL_NAME: "Pull a new RFC/draft locally",
}


def tool_display_name(tool_name: str) -> str:
    return _TOOL_DISPLAY_NAMES.get(tool_name, tool_name)


def _build_tools() -> list:
    """Build the actual tool schemas passed to invoke_llm() -- the real
    descriptions the model uses to decide when to call each tool, not
    prose written separately for a human. This is the single source of
    truth for what each tool "does" in the agent's own words: ask() uses
    it directly, and serve_ui.py's /api/tools endpoint calls it too, so
    what a human sees in the UI can never drift from what the agent
    actually operates under -- there was no other description to fall
    back on to keep in sync, so this makes drift structurally impossible
    instead of a discipline problem.
    """
    dataset_id_param = {"dataset_id": {"type": "string", "description": "dataset id, from list_available_ietf_corpora"}}
    return [
        _tool(
            DISCOVERY_TOOL_NAME,
            (
                "List every IETF draft this instance has an evolution/decision "
                "corpus for, its resolved working group, and the dataset_id of "
                "its mailarchive corpus when one is available. Call this first "
                "if you don't already know which dataset_id to search."
            ),
            {},
            [],
        ),
        _tool(
            EVOLUTION_TOOL_NAME,
            "Search an evolution/decision dataset (tagged, section-level draft-revision diffs) by its dataset_id.",
            {**dataset_id_param, "query": {"type": "string", "description": "search query"}},
            ["dataset_id", "query"],
        ),
        _tool(
            MAILARCHIVE_TOOL_NAME,
            "Search a mailarchive dataset (working-group mailing-list discussion) by its dataset_id.",
            {**dataset_id_param, "query": {"type": "string", "description": "search query"}},
            ["dataset_id", "query"],
        ),
        _tool(
            FETCH_DRAFT_TOOL_NAME,
            "Fetch a specific revision of an IETF draft directly from ietf.org and optionally search it for a phrase.",
            {
                "draft_name": {"type": "string", "description": "e.g. draft-ietf-ledbat-congestion"},
                "revision": {"type": "string", "description": "two-digit revision number, e.g. '09'"},
                "query": {
                    "type": "string",
                    "description": "optional phrase -- only paragraphs containing it are returned",
                },
            },
            ["draft_name", "revision"],
        ),
        _tool(
            FETCH_RFC_TOOL_NAME,
            "Fetch the final published RFC text directly from rfc-editor.org and optionally search it for a phrase.",
            {
                "rfc_number": {"type": "string", "description": "e.g. '6817' or 'RFC6817'"},
                "query": {
                    "type": "string",
                    "description": "optional phrase -- only paragraphs containing it are returned",
                },
            },
            ["rfc_number"],
        ),
        _tool(
            DIFF_REVISIONS_TOOL_NAME,
            (
                "Diff two revisions of a draft directly with the independent `iddiff` "
                "tool (not the evolution dataset's precomputed diff) and optionally "
                "search the diff for a phrase. Use this as a second opinion when the "
                "evolution dataset's diff for a change looks incomplete, ambiguous, "
                "or doesn't turn up anything for a query you'd expect it to."
            ),
            {
                "draft_name": {"type": "string", "description": "e.g. draft-ietf-ledbat-congestion"},
                "from_revision": {"type": "string", "description": "two-digit revision number, e.g. '08'"},
                "to_revision": {"type": "string", "description": "two-digit revision number, e.g. '09'"},
                "query": {
                    "type": "string",
                    "description": "optional phrase -- only diff blocks containing it are returned",
                },
            },
            ["draft_name", "from_revision", "to_revision"],
        ),
        _tool(
            CITE_DIFF_TOOL_NAME,
            (
                "Build a live, clickable citation URL on IETF's own hosted diff "
                "viewer (author-tools.ietf.org) for a specific change, so a human "
                "reader can verify it themselves instead of trusting your summary. "
                "Every evolution-dataset search result already comes with a "
                "'Live diff' URL for free -- call this instead only when you want "
                "a MORE precise link that jumps straight to the exact changed "
                "hunk (pass the distinctive phrase you're citing as query). Set "
                "to_revision to 'rfc' to cite against the final published RFC "
                "instead of another draft revision."
            ),
            {
                "draft_name": {"type": "string", "description": "e.g. draft-ietf-ledbat-congestion"},
                "from_revision": {"type": "string", "description": "two-digit revision number, e.g. '08'"},
                "to_revision": {
                    "type": "string",
                    "description": "two-digit revision number, e.g. '09' -- or 'rfc' for the final published RFC",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "a distinctive phrase from the exact change you're citing, to jump to that hunk "
                        "precisely -- omit to just link the whole diff page"
                    ),
                },
            },
            ["draft_name", "from_revision", "to_revision"],
        ),
        _tool(
            VERIFY_TOOL_NAME,
            "Audit a draft answer against the evidence retrieved so far, with fresh eyes, before finalizing.",
            {"draft_answer": {"type": "string", "description": "the full draft answer to check"}},
            ["draft_answer"],
        ),
        _tool(
            MINUTES_TOOL_NAME,
            (
                "Search a working group's already-downloaded meeting minutes for a phrase -- a discussion "
                "that never happened on the mailing list can still show up here, since some decisions get "
                "made or explained live in a meeting. Only covers minutes already pulled via the Sources "
                "tab; if none are downloaded for this WG, it says so rather than fetching anything itself."
            ),
            {
                "wg": {"type": "string", "description": "WG acronym, e.g. 'sidr'"},
                "query": {"type": "string", "description": "search phrase"},
            },
            ["wg", "query"],
        ),
        _tool(
            RFC_RELATIONS_TOOL_NAME,
            (
                "Look up which RFCs a given RFC obsoletes/is obsoleted by, and updates/is updated by, via "
                "IETF Datatracker. Use this when a decision looks unexplained in this RFC's own history -- "
                "it may actually be explained by an earlier RFC this one obsoletes, or fixed/clarified by a "
                "later RFC that updates it (common in families like DNSSEC, where several RFCs define "
                "different pieces and later ones amend earlier ones)."
            ),
            {"rfc_number": {"type": "string", "description": "e.g. '4035' or 'RFC4035'"}},
            ["rfc_number"],
        ),
        _tool(
            RFC_FAMILY_TOOL_NAME,
            (
                "Find the topically-related family of RFCs around one RFC, beyond structural "
                "obsoletes/updates relations -- some topics (DNSSEC is the motivating example) are "
                "actually explained across a cluster of RFCs tied together only by citation and shared "
                "topic, with one or more BCP/Informational documents acting as an overview or roadmap "
                "(e.g. RFC 9364 for DNSSEC). Use this when get_rfc_relations comes back empty or "
                "insufficient and the decision still looks like it might be explained elsewhere in the "
                "same topic family. Requires an exact keyword match between this RFC's own topic "
                "keyword and a candidate's title, so it can come back empty even when a real family "
                "exists (e.g. an RFC whose keyword is its own narrow internal acronym, not shared "
                "vocabulary with related RFCs' titles) -- if that happens, try search_rfc_corpus_bm25 "
                "or find_similar_rfcs_by_meaning instead of concluding no family exists."
            ),
            {"rfc_number": {"type": "string", "description": "e.g. '4035' or 'RFC4035'"}},
            ["rfc_number"],
        ),
        _tool(
            RFC_BM25_SEARCH_TOOL_NAME,
            (
                "Keyword-search the ENTIRE published RFC corpus (~9,930 RFCs) via BM25 ranking -- "
                "unlike find_related_rfc_family, which only considers RFCs already linked to one "
                "RFC by citation, this searches every RFC ever published for a topic phrase. Pass "
                "an RFC number instead of a phrase to search using that RFC's own title+abstract "
                "text. Use this to find an RFC by topic when you don't have a starting RFC yet, or "
                "as a cross-check when find_related_rfc_family comes back empty -- that heuristic "
                "requires an exact keyword match and can miss real relations that BM25's broader "
                "term overlap over title+abstract still catches."
            ),
            {
                "query": {
                    "type": "string",
                    "description": (
                        "a topic phrase (e.g. 'DNSSEC key rollover'), or an RFC number to search "
                        "using its own title+abstract"
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "'title_abstract' (default, always available) or 'full_text' (requires a "
                        "one-time index build; the tool returns build instructions if it isn't built yet)"
                    ),
                },
                "top_k": {"type": "integer", "description": "how many results to return (default 10)"},
            },
            ["query"],
        ),
        _tool(
            RFC_SEMANTIC_SEARCH_TOOL_NAME,
            (
                "Semantic-search the whole RFC corpus (title+abstract) by meaning via a local "
                "embedding model, not exact wording -- catches RFCs that describe the same idea "
                "with different terminology (e.g. an RFC's own internal acronym vs. the descriptive "
                "phrase other related RFCs use), which neither find_related_rfc_family's exact-"
                "keyword match nor search_rfc_corpus_bm25's term overlap would find. Requires a "
                "one-time index build; returns build instructions if it isn't built yet."
            ),
            {
                "query": {
                    "type": "string",
                    "description": "a topic phrase, or an RFC number to search using its own title+abstract",
                },
                "top_k": {"type": "integer", "description": "how many results to return (default 10)"},
            },
            ["query"],
        ),
        _tool(
            LOCAL_SOURCE_SEARCH_TOOL_NAME,
            (
                "Keyword-search (BM25) the local-only content of one draft added via the Sources tab's "
                "\"Add a new draft as a source\" flow -- draft/RFC text plus its WG's mail, already "
                "threaded (parent/child, no BM25 or embedding model involved in the threading itself). "
                "This is the ONLY tool that can search such a draft's mail or text: it has no real Dify "
                "dataset, so search_ietf_evolution_dataset and search_ietf_mailarchive_dataset don't work "
                "for it. list_available_ietf_corpora's observation lists which local sources currently "
                "have a built index (and how many documents of each kind) -- only those can be searched; "
                "one that's been added/synced but never indexed via \"Build index\" has nothing to search "
                "yet, and this tool will say so rather than silently returning nothing."
            ),
            {
                "draft_name": {
                    "type": "string",
                    "description": (
                        "the local source's draft name (e.g. 'draft-ietf-quic-transport') or its RFC "
                        "number (e.g. '4035') -- either resolves to the same source"
                    ),
                },
                "query": {"type": "string", "description": "a topic phrase or keyword"},
                "top_k": {"type": "integer", "description": "how many results to return (default 5)"},
            },
            ["draft_name", "query"],
        ),
        _tool(
            ADD_LOCAL_SOURCE_TOOL_NAME,
            (
                "Actually pull a new RFC or draft in as a searchable local source -- registers it, "
                "downloads every revision plus its RFC plus its WG's mail (raw and threaded), and "
                "builds a local BM25 index over all of it in one call, so search_local_source can use "
                "it immediately afterward in this same conversation. This is the ONLY tool in this "
                "agent that writes anything or costs real time (real network fetches and disk writes, "
                "typically seconds up to roughly a minute depending on the WG's mail volume) -- every "
                "other tool is a read-only lookup. Use it when the user asks you to gather, pull, crawl, "
                "or build up material on a topic (e.g. 'find RFCs related to DNSSEC and pull the "
                "relevant ones'): first use find_related_rfc_family / search_rfc_corpus_bm25 / "
                "get_rfc_relations to discover candidate RFCs, THEN call this only on the ones that "
                "genuinely look central to the question -- not every match found, since each call is a "
                "real, moderately expensive operation, not a free lookup. Accepts either an RFC number "
                "(e.g. '4035', resolved to its original draft name automatically) or a draft name "
                "directly."
            ),
            {
                "identifier": {
                    "type": "string",
                    "description": (
                        "an RFC number (e.g. '4035' or 'RFC4035') or a draft name "
                        "(e.g. 'draft-ietf-dnsext-dnssec-protocol')"
                    ),
                },
            },
            ["identifier"],
        ),
        _tool(
            IESG_BALLOT_TOOL_NAME,
            (
                "Fetch IESG DISCUSS/comment text left on a draft during its standards-track review -- a "
                "real, distinct venue from the mailing list and meeting minutes where a decision can get "
                "argued out (an Area Director raising a specific technical objection, tied to the exact "
                "revision it applied to). Only returns positions with actual discuss/comment text, never "
                "a bare vote. Optionally filter to comments mentioning a phrase."
            ),
            {
                "draft_name": {"type": "string", "description": "e.g. draft-ietf-sidr-bgpsec-protocol"},
                "query": {
                    "type": "string",
                    "description": "optional phrase -- only discuss/comment text containing it is returned",
                },
            },
            ["draft_name"],
        ),
    ]


def tool_summaries() -> list[dict]:
    """Plain-dict {name, description} for every tool, straight from
    _build_tools() -- for a caller (serve_ui.py) that wants to display what
    each tool does without duplicating the description text by hand.
    """
    return [{"name": t.name, "description": t.description} for t in _build_tools()]


def skill_texts() -> dict:
    """The agent's own orchestration instructions, verbatim -- for a caller
    (serve_ui.py) that wants to show HOW the tools above get combined into an
    answer, not just what each one does in isolation.

    "guardrails" is a list, not a single fixed field, on purpose: verify_answer
    is the only automatic check today (the model can't finalize an answer
    without it running at least once), but it's not conceptually the only
    kind of guardrail this agent could ever have -- a future one (e.g. a
    citation-completeness check) would just be another entry here, not a
    reshape of the response.
    """
    return {
        "agent": _INSTRUCTION,
        "guardrails": [
            {
                "name": "Fact-check before finalizing",
                "purpose": (
                    "A second, independent LLM call audits every claim in the draft answer against the "
                    "evidence actually retrieved so far, before it's shown -- catches unsupported claims and "
                    "misclassified alternatives. The model can voluntarily call this itself, but a safety net "
                    "forces exactly one call before any final answer is accepted either way."
                ),
                "instruction": _VERIFY_INSTRUCTION,
            },
        ],
    }


def ask(
    query: str,
    evolution_dataset_id: str | None = None,
    mailarchive_dataset_id: str | None = None,
    draft: str | None = None,
    model_provider: str = _DEFAULT_MODEL_PROVIDER,
    model_name: str = _DEFAULT_MODEL_NAME,
    mail_top_k: int = 50,
    verbose: bool = False,
    trace: list[dict] | None = None,
    on_event=None,
    max_iterations: int | None = None,
    extra_instruction: str | None = None,
    history: list[dict] | None = None,
    usage: list[dict] | None = None,
    cancel_event=None,
    model_tenant_id: str | None = None,
    claim_labels: dict[int, str] | None = None,
    evidence: list[dict] | None = None,
) -> str:
    """... (see module docstring for the full design).

    max_iterations: overrides _MAX_ITERATIONS for this call (None keeps the
    default). extra_instruction: appended to the system prompt for this call
    only (None leaves _INSTRUCTION unchanged) -- both let a caller (the
    Deep Dive tab) run a genuinely more thorough pass without a second copy
    of this whole function; every existing caller omits both and is
    unaffected.

    history: prior turns of the same Prism session, oldest first, each
    {"query": str, "answer": str} -- spliced in as alternating
    User/AssistantPromptMessages before the current query so the model sees
    this as a continuing conversation. None (the default, every existing
    caller) omits history entirely and behaves exactly as before.

    usage: optional out-param list (same pattern as `trace`) -- if given, one
    dict summarizing every LLMUsage seen across this whole ask() call (main
    loop iterations plus any verify_answer call) is appended to it just
    before returning, for a caller that wants to persist cost/token totals
    for the turn (e.g. onto a Message row). None (the default) skips this
    entirely; every existing caller is unaffected.

    cancel_event: optional threading.Event -- checked once at the top of
    each main-loop iteration; if set, the loop stops immediately and
    returns early (through _finish, so trace/usage still reflect whatever
    was actually done) instead of running to completion or to
    max_iterations. This is real, not a UI-only illusion: the SSE
    connection closing client-side does NOT stop the loop running
    server-side, since it's a genuinely separate background thread (see
    serve_ui.py's _sse_stream) -- a caller that wants a working Stop button
    needs this. None (the default, every existing caller) never checks it.

    trace: optional list to append structured tool-call events to (tool
    name, args, iteration, whether it was self-directed or safety-net
    forced, and a preview of what it returned) -- for a caller that wants
    to show the tool-call activity itself (e.g. a UI), not just the final
    answer. None (the default) skips this entirely; every existing
    caller (CLI, battery scripts) omits it and is unaffected.

    on_event: optional callback invoked with {"stage": ..., "message": ...}
    live, as each tool call starts and before each LLM call -- for a caller
    (serve_ui.py's SSE route) that wants to stream progress rather than
    block silently until the whole answer is ready. trace still records
    the same tool calls after the fact for the collapsible inspector;
    on_event is for showing it as it happens.

    model_tenant_id: resolve the model against THIS tenant's own Dify
    Settings/Model Provider configuration instead of the dataset-derived
    tenant above -- a caller's bring-your-own-key path: every Prism
    account that self-registered owns exactly one real Dify tenant (see
    services.agentic_rag_session_service.get_owned_tenant_id), and if
    they've configured a provider credential there themselves (via Dify's
    own real, already-built Settings/Model Provider infrastructure --
    nothing bespoke), that's what gets used, billed to their own key.
    None (the default, every existing caller) leaves model resolution
    exactly as before, against the dataset's own tenant.

    claim_labels: optional out-param dict (same pattern as `trace`/`usage`)
    -- populated with {citation_number: "stated"|"inferred"|"unresolved"},
    parsed from verify_answer's own per-citation audit (see
    _parse_claim_labels/verify_skill.md), for a caller that wants to show
    each [N] citation's evidence strength rather than a bare bracket. A
    citation verify_answer never classified is simply absent here -- the
    caller should treat a missing entry the same as "unresolved" (fail
    toward under-claiming confidence, not over-claiming it). None (the
    default) skips this entirely; every existing caller is unaffected.

    evidence: optional out-param list of {"text": str, "kind": "mail"|
    "evolution"} (same pattern as `trace`/`usage`) -- populated with every
    mailarchive/evolution search hit actually seen this call, un-truncated,
    in the same globally-unique numbering the model itself cites (so entry
    N-1 is what citation [N] in the answer refers to) -- for a caller that
    wants to show a citation's real source text, not just trace's own
    400-char observation_preview. Only these two tools' results are ever
    numbered at all (every other tool's output is
    unnumbered text the model can quote but never bracket-cite), so this is
    not the same list as everything ask() retrieved overall. None (the
    default) skips this entirely; every existing caller is unaffected.
    """
    from core.model_manager import ModelManager
    from dify_graph.model_runtime.entities import (
        AssistantPromptMessage,
        SystemPromptMessage,
        ToolPromptMessage,
        UserPromptMessage,
    )
    from dify_graph.model_runtime.entities.model_entities import ModelType
    from extensions.ext_database import db
    from models.account import Tenant
    from models.dataset import Dataset

    dataset_cache: dict[str, Dataset] = {}

    def _get_dataset(dataset_id: str) -> Dataset | None:
        if dataset_id not in dataset_cache:
            try:
                dataset_cache[dataset_id] = db.session.query(Dataset).filter_by(id=dataset_id).first()
            except Exception:
                # dataset_id is agent-supplied (an LLM tool-call argument), not
                # validated input -- confirmed live that a malformed value
                # (the model had confused a document COUNT it had just seen in
                # another tool's own output text for a dataset id) reaches
                # here as a non-UUID string, and the `id` column's UUID cast
                # raises inside Postgres before SQLAlchemy's .first() ever
                # returns, aborting the whole request instead of giving the
                # agent the ordinary "no dataset found" observation below.
                # Roll back so the aborted transaction doesn't also fail
                # every subsequent query this request makes.
                db.session.rollback()
                dataset_cache[dataset_id] = None
        return dataset_cache[dataset_id]

    if evolution_dataset_id and not _get_dataset(evolution_dataset_id):
        sys.exit(f"evolution dataset not found: {evolution_dataset_id}")
    if mailarchive_dataset_id and not _get_dataset(mailarchive_dataset_id):
        sys.exit(f"mailarchive dataset not found: {mailarchive_dataset_id}")

    # Candidates start from CLI hints (if given) and are otherwise filled in
    # by list_available_ietf_corpora the first time it's called with exactly
    # one unambiguous draft -- see the mailarchive safety net below.
    evolution_dataset_id_candidate = evolution_dataset_id
    mailarchive_dataset_id_candidate = mailarchive_dataset_id

    # A human-friendly --draft hint (name, RFC number, or WG acronym) is a
    # deterministic fast path, same spirit as the raw dataset-id hints but
    # something an actual person could plausibly type -- resolved eagerly so
    # the agent skips discovery entirely, same efficiency win raw ids give.
    # Doesn't override raw ids if both are somehow given.
    if draft and not (evolution_dataset_id_candidate and mailarchive_dataset_id_candidate):
        _, draft_catalog = _list_available_corpora()
        resolved = _resolve_draft_hint(draft, draft_catalog)
        if not resolved:
            known = ", ".join(entry["draft_name"] for entry in draft_catalog) or "(none found)"
            sys.exit(f"--draft {draft!r} did not match exactly one known draft. Known drafts: {known}")
        if verbose:
            print(f"[draft hint] {draft!r} resolved to {resolved['draft_name']}", file=sys.stderr)
        if not evolution_dataset_id_candidate:
            evolution_dataset_id_candidate = resolved["evolution_dataset_id"]
        if not mailarchive_dataset_id_candidate and resolved.get("mailarchive_dataset_id"):
            mailarchive_dataset_id_candidate = resolved["mailarchive_dataset_id"]

    if evolution_dataset_id_candidate:
        tenant_id = _get_dataset(evolution_dataset_id_candidate).tenant_id
    elif mailarchive_dataset_id_candidate:
        tenant_id = _get_dataset(mailarchive_dataset_id_candidate).tenant_id
    else:
        tenant_id = db.session.query(Tenant).first().id

    evolution_tool_name = EVOLUTION_TOOL_NAME
    mailarchive_tool_name = MAILARCHIVE_TOOL_NAME
    fetch_draft_tool_name = FETCH_DRAFT_TOOL_NAME
    fetch_rfc_tool_name = FETCH_RFC_TOOL_NAME
    diff_revisions_tool_name = DIFF_REVISIONS_TOOL_NAME
    cite_diff_tool_name = CITE_DIFF_TOOL_NAME
    discovery_tool_name = DISCOVERY_TOOL_NAME
    verify_tool_name = VERIFY_TOOL_NAME
    minutes_tool_name = MINUTES_TOOL_NAME
    rfc_relations_tool_name = RFC_RELATIONS_TOOL_NAME
    rfc_family_tool_name = RFC_FAMILY_TOOL_NAME
    iesg_ballot_tool_name = IESG_BALLOT_TOOL_NAME
    rfc_bm25_search_tool_name = RFC_BM25_SEARCH_TOOL_NAME
    rfc_semantic_search_tool_name = RFC_SEMANTIC_SEARCH_TOOL_NAME
    local_source_search_tool_name = LOCAL_SOURCE_SEARCH_TOOL_NAME
    add_local_source_tool_name = ADD_LOCAL_SOURCE_TOOL_NAME
    tools = _build_tools()

    # When hints are given (as every existing battery test script does), tell
    # the model directly instead of letting it burn a turn on discovery just
    # to learn a dataset_id it could have been told immediately -- discovery
    # stays available if it wants to double check or the hint turns out wrong.
    known_ids = []
    if evolution_dataset_id_candidate:
        known_ids.append(f"evolution dataset_id={evolution_dataset_id_candidate}")
    if mailarchive_dataset_id_candidate:
        known_ids.append(f"mailarchive dataset_id={mailarchive_dataset_id_candidate}")
    hint_suffix = f"\n\n(Already known: {'; '.join(known_ids)}.)" if known_ids else ""

    system_instruction = f"{_INSTRUCTION}\n\n{extra_instruction}" if extra_instruction else _INSTRUCTION
    history_messages: list = []
    for turn in history or []:
        history_messages.append(UserPromptMessage(content=turn["query"]))
        history_messages.append(AssistantPromptMessage(content=turn["answer"]))

    prompt_messages: list = [
        SystemPromptMessage(content=system_instruction),
        *history_messages,
        UserPromptMessage(content=f"{query}{hint_suffix}"),
    ]

    # model_tenant_id (an account's own owned tenant, when they have BYOK
    # credentials configured there for model_provider) takes priority over
    # the dataset-derived tenant_id above -- ModelManager needs no special
    # handling for this at all, since it resolves whatever that tenant
    # itself has configured via Dify's own real Settings/Model Provider
    # storage. None (every existing caller) leaves this exactly as before.
    model_instance = ModelManager().get_model_instance(
        tenant_id=model_tenant_id or tenant_id,
        provider=model_provider,
        model_type=ModelType.LLM,
        model=model_name,
    )

    mailarchive_called = False
    mailarchive_forced = False
    verify_called = False
    verify_forced = False
    discovery_called = False
    discovery_forced = False
    last_evolution_call = None  # (query, observation) -- used to build a forced mailarchive query if needed
    evidence_log: list[str] = []  # every dataset/fetch observation, fed to verify_answer
    evolution_citations: list[tuple[str, str, str, str]] = []  # (header, from_rev, to_rev, url), every hit ever seen
    usage_log: list = []  # every LLMUsage seen this call (main loop + any verify_answer), summarized in _finish
    # Reflects whichever verify_answer call was most recent when the loop
    # exits -- if the model revises its draft again after that call without
    # a further verify_answer pass (the safety net only guarantees at least
    # one, not one on every subsequent edit), these labels can trail the
    # exact final wording slightly. Citation numbers themselves stay valid
    # regardless (numbered_evidence below is append-only, never renumbered),
    # so this already-accepted gap in the verify-then-revise design is minor
    # here.
    last_verify_response = ""
    # Flat, one-entry-per-hit list backing the [N] citations the model
    # actually writes in its answer -- NOT the same as evidence_log above,
    # which holds one entry per TOOL CALL (each internally containing
    # possibly several hits) for verify_answer's own content-based fact-
    # checking, which never needed numeric alignment with the answer's own
    # citations. Only mailarchive/evolution search hits are ever numbered at
    # all (see the shared dispatch branch below) -- every other tool
    # (fetch_draft_text, search_wg_minutes, get_iesg_ballot_comments, ...)
    # returns unnumbered text the model can quote but never bracket-cite, so
    # there is nothing to track here for those.
    numbered_evidence: list[dict] = []

    def _mentions_revision_pair(text: str, from_rev: str, to_rev: str) -> bool:
        def mentions(rev: str) -> bool:
            # "rev 08" / "revision 08" / "rev. 8" -- require the word so
            # bare digits from dates ("2008") or other numbers don't match.
            pattern = rf"rev(?:ision)?\.?\s*0*{int(rev)}\b"
            return re.search(pattern, text, re.IGNORECASE) is not None

        return mentions(from_rev) and mentions(to_rev)

    def _finalize_answer(content: str) -> str:
        """Append a live-diff citation footer for a human to click through --
        in Python, not left to the model to remember. Testing showed it
        reliably read the "Live diff" URL that comes free with every
        evolution hit as evidence, but didn't reliably reproduce it in the
        final answer text even when explicitly instructed to; the earlier
        mailarchive-search and verify_answer safety nets exist for the exact
        same reason -- prompt compliance for something that matters isn't
        the same as guaranteeing it.

        Skips entirely if the model already cited one itself (confirmed this
        happens sometimes) -- trust it rather than risk citing a different,
        wrong pair alongside a correct inline one. Otherwise picks whichever
        seen evolution hit has its revision pair actually mentioned in the
        answer text, not just the top-ranked hit of the last search: a real
        run cited "revision 08"/"revision 09" inline while the top-ranked
        hit for that query was a different, less relevant rev04->rev05 pair
        -- rank order in a search isn't the same as relevance to the answer
        that got written from it. Falls back to the most recent search's
        top hit only when nothing in the answer text matches anything seen.
        """
        if "author-tools.ietf.org/iddiff" in content:
            return content
        for header, from_rev, to_rev, url in evolution_citations:
            if _mentions_revision_pair(content, from_rev, to_rev):
                return f"{content}\n\n---\nLive diff: {url}\n({header})"
        # Nothing in the answer text matched a seen hit's revision pair --
        # fall back to the most recent search's own top (rank [1]) hit.
        if last_evolution_call:
            match = re.search(
                r"^\[1\] (ietf-evolution draft:\S+ from:\S+ to:\S+ section:\S+ change:\S+ \| [^\n]+)"
                r"\n.*?Live diff: (\S+)",
                last_evolution_call[1],
                re.DOTALL,
            )
            if match:
                header, url = match.groups()
                return f"{content}\n\n---\nLive diff: {url}\n({header})"
        return content

    def _finish(content: str) -> str:
        """Every return path funnels through here (not just _finalize_answer
        directly) so the usage summary is appended exactly once regardless
        of which of the three exit points was taken.
        """
        final = _finalize_answer(content)
        if usage is not None:
            usage.append(_summarize_usage(usage_log))
        if claim_labels is not None:
            claim_labels.update(_parse_claim_labels(last_verify_response))
        if evidence is not None:
            evidence.extend(numbered_evidence)
        return final

    def _run_tool_call(tool_name: str, args: dict, iteration: int, forced: bool = False) -> str:
        """Thin wrapper around _dispatch_tool_call that records a trace
        entry when the caller asked for one (see ask()'s trace param) --
        kept separate from dispatch itself so every call path (model-
        initiated and safety-net forced) is traced uniformly in one place.

        Also the one seam every tool call funnels through, so it's the one
        place to catch a tool's own failure (a network timeout, a malformed
        upstream response, a subprocess error) and turn it into an
        observation the model can react to -- try something else, or note
        in the final answer that this couldn't be verified -- instead of an
        uncaught exception aborting the whole ask() call and losing
        everything already found this turn. Two specific instances of
        exactly this were already fixed one call site at a time (a
        malformed mail message, a bad agent-supplied dataset_id); this
        generalizes that same fix to every tool instead of waiting to hit
        the next one. db.session.rollback() mirrors the dataset_id fix's
        own reasoning: an aborted DB-level exception otherwise poisons
        every subsequent query in the same request, not just this one.
        """
        if on_event:
            on_event({"stage": "tool_call", "tool": tool_name, "forced": forced})
        errored = False
        try:
            observation = _dispatch_tool_call(tool_name, args, iteration)
        except Exception as exc:
            from extensions.ext_database import db

            db.session.rollback()
            errored = True
            observation = (
                f"{tool_name} failed: {type(exc).__name__}: {exc}. "
                "Try a different tool or approach, or note in the final answer that this couldn't be verified."
            )
            if verbose:
                traceback.print_exc()
        if trace is not None:
            trace.append(
                {
                    "tool": tool_name,
                    "args": args,
                    "iteration": iteration,
                    "forced": forced,
                    "errored": errored,
                    "observation_preview": (observation[:400] + " …") if len(observation) > 400 else observation,
                }
            )
        return observation

    def _dispatch_tool_call(tool_name: str, args: dict, iteration: int) -> str:
        # Split across two statements to stay under the line-length limit;
        # ruff's FURB154 wants these merged, which would violate E501 instead.
        nonlocal mailarchive_called, last_evolution_call, verify_called, discovery_called  # noqa: FURB154
        nonlocal evolution_dataset_id_candidate, mailarchive_dataset_id_candidate
        nonlocal last_verify_response

        if tool_name == discovery_tool_name:
            discovery_called = True
            catalog_text, catalog = _list_available_corpora()
            if verbose:
                print(f"[iter {iteration}] {tool_name}", file=sys.stderr)
            if len(catalog) == 1:
                if not evolution_dataset_id_candidate:
                    evolution_dataset_id_candidate = catalog[0]["evolution_dataset_id"]
                if not mailarchive_dataset_id_candidate and catalog[0]["mailarchive_dataset_id"]:
                    mailarchive_dataset_id_candidate = catalog[0]["mailarchive_dataset_id"]
            evidence_log.append(catalog_text)
            return catalog_text

        if tool_name in (evolution_tool_name, mailarchive_tool_name):
            is_mailarchive = tool_name == mailarchive_tool_name
            dataset_id = args.get("dataset_id") or (
                mailarchive_dataset_id_candidate if is_mailarchive else evolution_dataset_id_candidate
            )
            if not dataset_id:
                return f"No dataset_id given and none known yet -- call {discovery_tool_name} first."
            dataset = _get_dataset(dataset_id)
            if not dataset:
                return f"No dataset found with id {dataset_id!r}."
            refined_query = args.get("query", query)
            top_k = mail_top_k if is_mailarchive else 5
            if verbose:
                print(
                    f"[iter {iteration}] {tool_name} dataset_id={dataset_id!r} query={refined_query!r}",
                    file=sys.stderr,
                )
            hits = _retrieve(dataset, refined_query, top_k=top_k)
            # Global, ever-increasing start rather than each call restarting
            # at 1 -- otherwise "[1]" from this search and "[1]" from an
            # earlier one would both appear in the model's own answer text
            # meaning two different things, with nothing to tell them apart.
            citation_start = len(numbered_evidence) + 1
            if is_mailarchive:
                observation = _format_hits(hits, start=citation_start)
                mailarchive_called = True
                mailarchive_dataset_id_candidate = dataset_id
            else:
                observation, citations = _format_evolution_hits(hits, start=citation_start)
                evolution_citations.extend(citations)
                last_evolution_call = (refined_query, observation)
                evolution_dataset_id_candidate = dataset_id
            kind = "mail" if is_mailarchive else "evolution"
            numbered_evidence.extend({"text": h, "kind": kind} for h in hits)
            evidence_log.append(observation)
            return observation

        if tool_name == fetch_draft_tool_name:
            draft_name = args.get("draft_name", "")
            revision = args.get("revision", "")
            fetch_query = args.get("query")
            if verbose:
                print(
                    f"[iter {iteration}] {tool_name} draft={draft_name!r} rev={revision!r} query={fetch_query!r}",
                    file=sys.stderr,
                )
            observation = _fetch_draft_text(draft_name, revision, fetch_query)
            evidence_log.append(observation)
            return observation

        if tool_name == fetch_rfc_tool_name:
            rfc_number = args.get("rfc_number", "")
            fetch_query = args.get("query")
            if verbose:
                print(f"[iter {iteration}] {tool_name} rfc={rfc_number!r} query={fetch_query!r}", file=sys.stderr)
            observation = _fetch_rfc_text(rfc_number, fetch_query)
            evidence_log.append(observation)
            return observation

        if tool_name == diff_revisions_tool_name:
            draft_name = args.get("draft_name", "")
            from_revision = args.get("from_revision", "")
            to_revision = args.get("to_revision", "")
            diff_query = args.get("query")
            if verbose:
                print(
                    f"[iter {iteration}] {tool_name} draft={draft_name!r} "
                    f"from={from_revision!r} to={to_revision!r} query={diff_query!r}",
                    file=sys.stderr,
                )
            observation = _diff_draft_revisions(draft_name, from_revision, to_revision, diff_query)
            evidence_log.append(observation)
            return observation

        if tool_name == cite_diff_tool_name:
            draft_name = args.get("draft_name", "")
            from_revision = args.get("from_revision", "")
            to_revision = args.get("to_revision", "")
            cite_query = args.get("query")
            if verbose:
                print(
                    f"[iter {iteration}] {tool_name} draft={draft_name!r} "
                    f"from={from_revision!r} to={to_revision!r} query={cite_query!r}",
                    file=sys.stderr,
                )
            # Not appended to evidence_log: this is a citation URL to embed
            # in the final answer, not evidence to fact-check claims against.
            return _cite_iddiff_url(draft_name, from_revision, to_revision, cite_query)

        if tool_name == verify_tool_name:
            draft_answer = args.get("draft_answer", "")
            if verbose:
                print(f"[iter {iteration}] {tool_name} draft_answer_len={len(draft_answer)}", file=sys.stderr)
            verify_called = True
            last_verify_response = _verify_answer(draft_answer, evidence_log, model_instance, usage_sink=usage_log)
            return last_verify_response

        if tool_name == minutes_tool_name:
            wg = args.get("wg", "")
            minutes_query = args.get("query", "")
            if verbose:
                print(f"[iter {iteration}] {tool_name} wg={wg!r} query={minutes_query!r}", file=sys.stderr)
            observation = _search_wg_minutes(wg, minutes_query)
            evidence_log.append(observation)
            return observation

        if tool_name == rfc_relations_tool_name:
            rfc_number = args.get("rfc_number", "")
            if verbose:
                print(f"[iter {iteration}] {tool_name} rfc={rfc_number!r}", file=sys.stderr)
            observation = _get_rfc_relations(rfc_number)
            evidence_log.append(observation)
            return observation

        if tool_name == rfc_family_tool_name:
            rfc_number = args.get("rfc_number", "")
            if verbose:
                print(f"[iter {iteration}] {tool_name} rfc={rfc_number!r}", file=sys.stderr)
            observation = _find_related_rfc_family(rfc_number)
            evidence_log.append(observation)
            return observation

        if tool_name == iesg_ballot_tool_name:
            draft_name = args.get("draft_name", "")
            ballot_query = args.get("query")
            if verbose:
                print(f"[iter {iteration}] {tool_name} draft={draft_name!r} query={ballot_query!r}", file=sys.stderr)
            observation = _get_iesg_ballot_comments(draft_name, ballot_query)
            evidence_log.append(observation)
            return observation

        if tool_name == rfc_bm25_search_tool_name:
            search_query = args.get("query", "")
            scope = args.get("scope") or "title_abstract"
            top_k = args.get("top_k") or 10
            if verbose:
                print(f"[iter {iteration}] {tool_name} query={search_query!r} scope={scope!r}", file=sys.stderr)
            observation = _search_rfc_corpus_bm25(search_query, scope, top_k)
            evidence_log.append(observation)
            return observation

        if tool_name == rfc_semantic_search_tool_name:
            search_query = args.get("query", "")
            top_k = args.get("top_k") or 10
            if verbose:
                print(f"[iter {iteration}] {tool_name} query={search_query!r}", file=sys.stderr)
            observation = _find_similar_rfcs_by_meaning(search_query, top_k)
            evidence_log.append(observation)
            return observation

        if tool_name == local_source_search_tool_name:
            local_draft_name = args.get("draft_name", "")
            search_query = args.get("query", "")
            top_k = args.get("top_k") or 5
            if verbose:
                print(
                    f"[iter {iteration}] {tool_name} draft={local_draft_name!r} query={search_query!r}",
                    file=sys.stderr,
                )
            observation = _search_local_source(local_draft_name, search_query, top_k)
            evidence_log.append(observation)
            return observation

        if tool_name == add_local_source_tool_name:
            identifier = args.get("identifier", "")
            if verbose:
                print(f"[iter {iteration}] {tool_name} identifier={identifier!r}", file=sys.stderr)
            observation = _add_local_source(identifier)
            evidence_log.append(observation)
            return observation

        return f"there is no tool named {tool_name}"

    def _force_tool_call(tool_name: str, args: dict, content: str, iteration: int) -> None:
        observation = _run_tool_call(tool_name, args, iteration, forced=True)
        forced_call_id = str(uuid.uuid4())
        prompt_messages.append(
            AssistantPromptMessage(
                content=content,
                tool_calls=[
                    AssistantPromptMessage.ToolCall(
                        id=forced_call_id,
                        type="function",
                        function=AssistantPromptMessage.ToolCall.ToolCallFunction(
                            name=tool_name, arguments=json.dumps(args)
                        ),
                    )
                ],
            )
        )
        prompt_messages.append(ToolPromptMessage(content=observation, tool_call_id=forced_call_id, name=tool_name))

    effective_max_iterations = max_iterations or _MAX_ITERATIONS
    for iteration in range(1, effective_max_iterations + 1):
        # Three forced safety-net rounds are budgeted in _MAX_ITERATIONS; after
        # all floors are satisfied, or once the model does everything on its
        # own, tool-free rounds are forced to guarantee termination.
        is_last = iteration == effective_max_iterations
        if cancel_event is not None and cancel_event.is_set():
            return _finish(f"Cancelled by user after {iteration - 1} step(s).")
        if on_event:
            on_event({"stage": "thinking"})
        result = _invoke_llm_with_retry(
            model_instance,
            usage_sink=usage_log,
            prompt_messages=prompt_messages,
            model_parameters={"temperature": 0.3},
            tools=[] if is_last else tools,
            stop=[],
            stream=False,
            user="ask_decision_agent_script",
        )
        message = result.message
        content = message.content if isinstance(message.content, str) else str(message.content or "")

        if not message.tool_calls:
            if is_last:
                return _finish(content)

            if (
                not evolution_dataset_id_candidate
                and not mailarchive_dataset_id_candidate
                and not (discovery_called or discovery_forced)
                and not verify_called
            ):
                # Safety net #0: nothing known yet and discovery never ran --
                # force it so the mailarchive floor below has a candidate to
                # use. No-op once a CLI hint or a prior discovery call has
                # already populated a candidate.
                #
                # The `not verify_called` guard: confirmed live (Panorama,
                # an RFC-relationship question with no draft/dataset context
                # at all) that without it, a model which legitimately never
                # needed discovery -- it answered entirely via
                # get_rfc_relations/find_related_rfc_family/
                # search_rfc_corpus_bm25 and already ran verify_answer --
                # still got a discovery call forced on it AFTER verification,
                # out of order and by then pointless. That forced call
                # wasn't just wasted: the very next LLM invocation failed
                # with a generic, reproducible provider error every retry
                # (not transient -- same failure 3/3 times), strongly
                # suggesting the late, out-of-order forced call corrupted
                # the conversation state the provider expected. Once
                # verify_answer has already run, the answer is already
                # substantiated by whatever tools the model actually used --
                # forcing discovery that late can only break things, not
                # improve the answer.
                discovery_forced = True
                if verbose:
                    print(f"[forced] {discovery_tool_name}", file=sys.stderr)
                _force_tool_call(discovery_tool_name, {}, content, iteration)
                continue

            if not (mailarchive_called or mailarchive_forced) and mailarchive_dataset_id_candidate:
                # Safety net #1: never let a final answer through without at
                # least one mailarchive search having happened, when we
                # actually know which dataset that would even be -- forcing
                # a guess would be worse than not forcing at all.
                mailarchive_forced = True
                mail_query = f"{query}\n\n{last_evolution_call[1]}" if last_evolution_call else query
                if verbose:
                    print(
                        f"[forced] {mailarchive_tool_name} dataset_id={mailarchive_dataset_id_candidate!r} "
                        f"query={mail_query!r}",
                        file=sys.stderr,
                    )
                _force_tool_call(
                    mailarchive_tool_name,
                    {"dataset_id": mailarchive_dataset_id_candidate, "query": mail_query},
                    content,
                    iteration,
                )
                continue

            if not (verify_called or verify_forced):
                # Safety net #2: never let a final answer through without at
                # least one verify_answer pass over it.
                verify_forced = True
                if verbose:
                    print(f"[forced] {verify_tool_name}", file=sys.stderr)
                _force_tool_call(verify_tool_name, {"draft_answer": content}, content, iteration)
                continue

            return _finish(content)

        # agent_skill.md asks the model for a one-sentence rationale alongside
        # each tool call -- surface it live rather than only the fixed
        # "Using: <tool>" status label, so the stream shows actual reasoning
        # instead of a canned progress indicator.
        if content.strip() and on_event:
            on_event({"stage": "reasoning", "message": content.strip()})

        assistant_msg = AssistantPromptMessage(content=content, tool_calls=message.tool_calls)
        prompt_messages.append(assistant_msg)

        for tool_call in message.tool_calls:
            try:
                args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
            except json.JSONDecodeError:
                args = {}
            observation = _run_tool_call(tool_call.function.name, args, iteration)
            prompt_messages.append(
                ToolPromptMessage(content=observation, tool_call_id=tool_call.id, name=tool_call.function.name)
            )

    # Ran out of iterations while the model still wanted to call tools; the
    # final iteration above was forced tool-free, so `content` holds its answer.
    return _finish(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", required=True, help="the question to ask")
    parser.add_argument("--dataset", default=None, help="mailarchive dataset id (omit to let the agent discover it)")
    parser.add_argument(
        "--evolution-dataset", default=None, help="evolution/decision dataset id (omit to let the agent discover it)"
    )
    parser.add_argument(
        "--draft",
        default=None,
        help=(
            "which draft this question is about -- draft name, RFC number (e.g. '8205' or 'RFC 8205'), "
            "or WG acronym. A human-friendly alternative to --dataset/--evolution-dataset; ignored if both "
            "of those are given. Omit to let the agent discover it from the question itself."
        ),
    )
    parser.add_argument("--model", default="google/gemini-2.5-flash")
    parser.add_argument("--model-provider", default="langgenius/openrouter/openrouter")
    parser.add_argument("--mail-top-k", type=int, default=50)
    parser.add_argument(
        "--verbose", action="store_true", help="print each tool call (seeded and self-directed) to stderr"
    )
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        answer = ask(
            query=args.query,
            evolution_dataset_id=args.evolution_dataset,
            mailarchive_dataset_id=args.dataset,
            draft=args.draft,
            model_provider=args.model_provider,
            model_name=args.model,
            mail_top_k=args.mail_top_k,
            verbose=args.verbose,
        )
        print(answer)


if __name__ == "__main__":
    main()
