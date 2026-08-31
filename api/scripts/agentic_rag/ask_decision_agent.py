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
import uuid
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


def _invoke_llm_with_retry(model_instance, **kwargs):
    from core.plugin.impl.exc import PluginInvokeError
    from dify_graph.model_runtime.errors.invoke import InvokeError

    for attempt in range(_LLM_RETRIES + 1):
        try:
            return model_instance.invoke_llm(**kwargs)
        except (PluginInvokeError, InvokeError):
            if attempt == _LLM_RETRIES:
                raise
            time.sleep(_LLM_RETRY_SECONDS)


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
    "ledbat": "bbcde280-4130-4dee-bbcb-73664cc43b58",
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

    if not by_draft:
        return "No evolution corpora found in this Dify instance.", []

    catalog: list[dict] = []
    lines: list[str] = []
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
    return "\n".join(lines), catalog


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


def _retrieve(dataset, query: str, top_k: int) -> list[str]:
    from core.rag.datasource.retrieval_service import RetrievalService
    from core.rag.index_processor.constant.index_type import IndexTechniqueType
    from core.rag.retrieval.retrieval_methods import RetrievalMethod

    if dataset.indexing_technique == IndexTechniqueType.ECONOMY:
        documents = RetrievalService.retrieve(
            retrieval_method=RetrievalMethod.KEYWORD_SEARCH,
            dataset_id=dataset.id,
            query=query,
            top_k=top_k,
        )
    else:
        retrieval_model = dataset.retrieval_model or {}
        documents = RetrievalService.retrieve(
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
    return [doc.page_content for doc in documents]


def _format_hits(hits: list[str]) -> str:
    if not hits:
        return "(no results)"
    return "\n\n".join(f"[{i}] {hit}" for i, hit in enumerate(hits, start=1))


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


def _format_evolution_hits(hits: list[str]) -> tuple[str, list[tuple[str, str, str, str]]]:
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
    """
    if not hits:
        return "(no results)", []
    blocks = []
    citations: list[tuple[str, str, str, str]] = []
    for i, hit in enumerate(hits, start=1):
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


def _verify_answer(draft_answer: str, evidence_log: list[str], model_instance) -> str:
    from dify_graph.model_runtime.entities import SystemPromptMessage, UserPromptMessage

    if not evidence_log:
        return (
            "No evidence has been retrieved yet -- search the evolution and mailarchive tools before calling "
            "verify_answer."
        )

    evidence_block = "\n\n".join(f"--- evidence [{i}] ---\n{e}" for i, e in enumerate(evidence_log, start=1))
    result = _invoke_llm_with_retry(
        model_instance,
        prompt_messages=[
            SystemPromptMessage(content=_VERIFY_INSTRUCTION),
            UserPromptMessage(
                content=(
                    f"=== EVIDENCE RETRIEVED SO FAR ===\n{evidence_block}\n\n"
                    f"=== DRAFT ANSWER TO CHECK ===\n{draft_answer}"
                )
            ),
        ],
        model_parameters={"temperature": 0.1, "top_p": 1},
        tools=[],
        stop=[],
        stream=False,
        user="ask_decision_agent_verify",
    )
    content = result.message.content
    return content if isinstance(content, str) else str(content)


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
    ]


def tool_summaries() -> list[dict]:
    """Plain-dict {name, description} for every tool, straight from
    _build_tools() -- for a caller (serve_ui.py) that wants to display what
    each tool does without duplicating the description text by hand.
    """
    return [{"name": t.name, "description": t.description} for t in _build_tools()]


def skill_texts() -> dict[str, str]:
    """The agent's own orchestration instructions, verbatim -- for a caller
    (serve_ui.py) that wants to show HOW the tools above get combined into an
    answer, not just what each one does in isolation.
    """
    return {"agent": _INSTRUCTION, "verify": _VERIFY_INSTRUCTION}


def ask(
    query: str,
    evolution_dataset_id: str | None = None,
    mailarchive_dataset_id: str | None = None,
    draft: str | None = None,
    model_provider: str = "langgenius/openrouter/openrouter",
    model_name: str = "google/gemini-2.5-flash",
    mail_top_k: int = 50,
    verbose: bool = False,
    trace: list[dict] | None = None,
) -> str:
    """... (see module docstring for the full design).

    trace: optional list to append structured tool-call events to (tool
    name, args, iteration, whether it was self-directed or safety-net
    forced, and a preview of what it returned) -- for a caller that wants
    to show the tool-call activity itself (e.g. a UI), not just the final
    answer. None (the default) skips this entirely; every existing
    caller (CLI, battery scripts) omits it and is unaffected.
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
            dataset_cache[dataset_id] = db.session.query(Dataset).filter_by(id=dataset_id).first()
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

    prompt_messages: list = [
        SystemPromptMessage(content=_INSTRUCTION),
        UserPromptMessage(content=f"{query}{hint_suffix}"),
    ]

    model_manager = ModelManager()
    model_instance = model_manager.get_model_instance(
        tenant_id=tenant_id,
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

    def _run_tool_call(tool_name: str, args: dict, iteration: int, forced: bool = False) -> str:
        """Thin wrapper around _dispatch_tool_call that records a trace
        entry when the caller asked for one (see ask()'s trace param) --
        kept separate from dispatch itself so every call path (model-
        initiated and safety-net forced) is traced uniformly in one place.
        """
        observation = _dispatch_tool_call(tool_name, args, iteration)
        if trace is not None:
            trace.append(
                {
                    "tool": tool_name,
                    "args": args,
                    "iteration": iteration,
                    "forced": forced,
                    "observation_preview": (observation[:400] + " …") if len(observation) > 400 else observation,
                }
            )
        return observation

    def _dispatch_tool_call(tool_name: str, args: dict, iteration: int) -> str:
        # Split across two statements to stay under the line-length limit;
        # ruff's FURB154 wants these merged, which would violate E501 instead.
        nonlocal mailarchive_called, last_evolution_call, verify_called, discovery_called  # noqa: FURB154
        nonlocal evolution_dataset_id_candidate, mailarchive_dataset_id_candidate

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
            if is_mailarchive:
                observation = _format_hits(hits)
                mailarchive_called = True
                mailarchive_dataset_id_candidate = dataset_id
            else:
                observation, citations = _format_evolution_hits(hits)
                evolution_citations.extend(citations)
                last_evolution_call = (refined_query, observation)
                evolution_dataset_id_candidate = dataset_id
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
            return _verify_answer(draft_answer, evidence_log, model_instance)

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

    for iteration in range(1, _MAX_ITERATIONS + 1):
        # Three forced safety-net rounds are budgeted in _MAX_ITERATIONS; after
        # all floors are satisfied, or once the model does everything on its
        # own, tool-free rounds are forced to guarantee termination.
        is_last = iteration == _MAX_ITERATIONS
        result = _invoke_llm_with_retry(
            model_instance,
            prompt_messages=prompt_messages,
            model_parameters={"temperature": 0.3, "top_p": 1},
            tools=[] if is_last else tools,
            stop=[],
            stream=False,
            user="ask_decision_agent_script",
        )
        message = result.message
        content = message.content if isinstance(message.content, str) else str(message.content or "")

        if not message.tool_calls:
            if is_last:
                return _finalize_answer(content)

            if (
                not evolution_dataset_id_candidate
                and not mailarchive_dataset_id_candidate
                and not (discovery_called or discovery_forced)
            ):
                # Safety net #0: nothing known yet and discovery never ran --
                # force it so the mailarchive floor below has a candidate to
                # use. No-op once a CLI hint or a prior discovery call has
                # already populated a candidate.
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

            return _finalize_answer(content)

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
    return _finalize_answer(content)


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
