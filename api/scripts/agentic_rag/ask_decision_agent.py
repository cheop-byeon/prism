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

With only one draft actually in the system today, none of this changes
observed behavior yet -- it's the infrastructure for when a second one is
added, not a demonstration that spans multiple drafts.

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


_INSTRUCTION = (
    "You are an assistant for IETF standards research. You explain WHY a "
    "specification decision was made, using these tools:\n\n"
    "- list_available_ietf_corpora: lists every draft this instance has "
    "data for, its working group, and the dataset_id of its evolution "
    "corpus and (when available) its mailarchive corpus. Call this first "
    "if you don't already know which dataset_id to pass to the two search "
    "tools below.\n"
    "- search_ietf_evolution_dataset(dataset_id, query): search an "
    "evolution/decision dataset -- tagged, section-level diffs of draft "
    "revisions, each entry one specific change in one specific section "
    "between two specific revisions.\n"
    "- search_ietf_mailarchive_dataset(dataset_id, query): search a "
    "mailarchive dataset -- the working-group mailing-list discussion "
    "that produced those changes.\n"
    "- fetch_ietf_draft_text / fetch_ietf_rfc_text: fetch a specific draft "
    "revision or the final published RFC directly from the primary source "
    "(ietf.org / rfc-editor.org) and search it for a phrase. Use these when "
    "the evolution dataset's diff for a change looks incomplete or "
    "ambiguous, when you want to confirm the EXACT wording of something "
    "before quoting it, or when you want to check what a later revision or "
    "the final RFC actually says -- the evolution dataset can have gaps for "
    "a specific query even when the underlying text exists.\n"
    "- diff_ietf_draft_revisions: diff two specific revisions directly with "
    "an independent diff tool, separate from the evolution dataset's own "
    "precomputed diff logic. Use this as a second opinion when the "
    "evolution dataset's diff for a change seems incomplete, ambiguous, or "
    "doesn't surface anything for a query you'd expect it to.\n"
    "- verify_answer: pass it your full draft answer and it checks every "
    "claim in it against the evidence you've actually retrieved so far, "
    "with fresh eyes, in a separate call. Call it before finalizing.\n\n"
    "For the user's question:\n\n"
    "0. If you don't already know the right dataset_id for this draft, "
    "call list_available_ietf_corpora first.\n"
    "1. Search the evolution dataset to locate the specific change being "
    "asked about. Use a tight query -- the identifier or phrase being "
    "asked about -- rather than the full question, since this dataset is "
    "keyword-indexed.\n"
    "2. Once you've identified the decision, search the mailarchive "
    "dataset for discussion of it. The evolution dataset only ever tells "
    "you WHAT "
    "changed; it is never itself evidence of WHY, no matter how "
    "self-explanatory the diff text sounds. Start with the decision's own "
    "wording as your query. If the results look thin, off-topic, or "
    "dominated by procedural/administrative traffic (WG Action, Document "
    "Action, I-D Action, Call for Papers, Last Call notices, "
    "meeting-announcement cover messages), try at least one differently "
    "phrased query before giving up -- you are not limited to one search "
    "per decision.\n"
    "3. Read what you retrieve critically. A message that merely shares a "
    "broad topic (the same protocol, a different parameter, generic "
    "discussion) is NOT the same as a message that directly discusses "
    "THIS decision.\n\n"
    "Once you have a draft answer, write it under exactly these three "
    "headings, in this order (keep them verbatim, bold, on their own "
    "line):\n\n"
    "**Rationale for the adopted approach** -- traceable to specific "
    "retrieved messages. If nothing you found clearly explains the "
    "motivation, say so explicitly rather than inferring a rationale from "
    "the decision text itself.\n"
    "**Alternatives considered** -- only a genuinely different "
    "approach/value a message actually proposes as a considered option. "
    "Never the decision's own before/after text repackaged as a proposal. "
    "If none, say so in one line.\n"
    "**Other relevant discussion** -- corrections, open questions, or "
    "objections that are real and worth surfacing but aren't a proposed "
    "alternative (e.g. someone's mistaken belief about the current value, "
    "later corrected). If there's nothing beyond the rationale above, say "
    "so in one line.\n\n"
    "Before treating that draft as final, call verify_answer with it. If it "
    "flags anything unsupported or misclassified, fix your draft "
    "accordingly -- delete an unsupported claim rather than softening it, "
    "and move a misclassified item to the right heading rather than just "
    "deleting it. Cite retrieved message indices, e.g. [3], and thread "
    "subjects where they appear."
)

_VERIFY_INSTRUCTION = (
    "You are a fact-checker auditing someone else's draft answer. You did "
    "not write it and do not know anything beyond what's given to you "
    "below. You are given EVIDENCE (verbatim excerpts actually retrieved "
    "from a mailing-list archive, a draft-revision diff dataset, and/or "
    "primary-source fetches) and a DRAFT ANSWER that claims to be grounded "
    "in that evidence. Your only job is to check whether it actually is.\n\n"
    "For every sentence in the draft that names a specific person, date, "
    "number, or direct quote: find the exact (or near-exact) passage in "
    "the evidence that supports it. If you cannot find one, say so "
    "explicitly -- list the claim and state 'no supporting evidence "
    "found'. A claim that merely sounds plausible or fits the pattern of "
    "the evidence is not the same as a claim that's actually in it.\n\n"
    "Separately, check every item listed under 'Alternatives considered': "
    "a genuine alternative is a message that proposes a different "
    "approach or value than what was adopted. Someone's mistaken belief "
    "about what the current value already was, later corrected in the "
    "same thread, is NOT an alternative -- if you see one miscategorized "
    "this way, say so and note it belongs under 'Other relevant "
    "discussion' instead. Likewise, the decision's own before/after text "
    "repackaged as if a person proposed it is not a real alternative.\n\n"
    "List every issue you find, quoting the specific draft sentence and "
    "explaining exactly what's wrong with it. If a section has no issues, "
    "say so briefly rather than padding your reply. End your reply with "
    "exactly one of these two lines, verbatim:\n"
    "VERDICT: no issues found\n"
    "VERDICT: issues found"
)

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


def _list_available_corpora() -> tuple[str, list[dict]]:
    """Return (formatted catalog for the tool observation, structured catalog).

    The structured form is `[{draft_name, wg, evolution_dataset_id,
    mailarchive_dataset_id}]` -- used by ask() to auto-seed the mailarchive
    safety net's candidate dataset id when there's exactly one unambiguous
    draft (today's reality; deliberately not guessed at when there's more
    than one).
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
        mailarchive_dataset_id = _MAILARCHIVE_REGISTRY.get(wg) if wg else None
        catalog.append(
            {
                "draft_name": draft_name,
                "wg": wg,
                "evolution_dataset_id": newest["dataset_id"],
                "mailarchive_dataset_id": mailarchive_dataset_id,
            }
        )
        lines.append(f"Draft: {draft_name}" + (f" (WG: {wg})" if wg else " (WG: could not be resolved)"))
        for entry in entries:
            marker = " <- most recent, recommended" if entry is newest else ""
            lines.append(f"  evolution dataset_id={entry['dataset_id']} name={entry['dataset_name']!r}{marker}")
        if mailarchive_dataset_id:
            lines.append(f"  mailarchive dataset_id={mailarchive_dataset_id}")
        else:
            lines.append("  mailarchive dataset: none registered for this WG -- discussion search unavailable")
    return "\n".join(lines), catalog


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
    return _filter_primary_source_text(diff_text, query, source_label=label)


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


def ask(
    query: str,
    evolution_dataset_id: str | None = None,
    mailarchive_dataset_id: str | None = None,
    model_provider: str = "langgenius/openrouter/openrouter",
    model_name: str = "google/gemini-2.5-flash",
    mail_top_k: int = 50,
    verbose: bool = False,
) -> str:
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

    if evolution_dataset_id:
        tenant_id = _get_dataset(evolution_dataset_id).tenant_id
    elif mailarchive_dataset_id:
        tenant_id = _get_dataset(mailarchive_dataset_id).tenant_id
    else:
        tenant_id = db.session.query(Tenant).first().id

    evolution_tool_name = "search_ietf_evolution_dataset"
    mailarchive_tool_name = "search_ietf_mailarchive_dataset"
    fetch_draft_tool_name = "fetch_ietf_draft_text"
    fetch_rfc_tool_name = "fetch_ietf_rfc_text"
    diff_revisions_tool_name = "diff_ietf_draft_revisions"
    discovery_tool_name = "list_available_ietf_corpora"
    verify_tool_name = "verify_answer"

    dataset_id_param = {"dataset_id": {"type": "string", "description": "dataset id, from list_available_ietf_corpora"}}
    tools = [
        _tool(
            discovery_tool_name,
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
            evolution_tool_name,
            "Search an evolution/decision dataset (tagged, section-level draft-revision diffs) by its dataset_id.",
            {**dataset_id_param, "query": {"type": "string", "description": "search query"}},
            ["dataset_id", "query"],
        ),
        _tool(
            mailarchive_tool_name,
            "Search a mailarchive dataset (working-group mailing-list discussion) by its dataset_id.",
            {**dataset_id_param, "query": {"type": "string", "description": "search query"}},
            ["dataset_id", "query"],
        ),
        _tool(
            fetch_draft_tool_name,
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
            fetch_rfc_tool_name,
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
            diff_revisions_tool_name,
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
            verify_tool_name,
            "Audit a draft answer against the evidence retrieved so far, with fresh eyes, before finalizing.",
            {"draft_answer": {"type": "string", "description": "the full draft answer to check"}},
            ["draft_answer"],
        ),
    ]

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

    def _run_tool_call(tool_name: str, args: dict, iteration: int) -> str:
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
            observation = _format_hits(hits)
            if is_mailarchive:
                mailarchive_called = True
                mailarchive_dataset_id_candidate = dataset_id
            else:
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

        if tool_name == verify_tool_name:
            draft_answer = args.get("draft_answer", "")
            if verbose:
                print(f"[iter {iteration}] {tool_name} draft_answer_len={len(draft_answer)}", file=sys.stderr)
            verify_called = True
            return _verify_answer(draft_answer, evidence_log, model_instance)

        return f"there is no tool named {tool_name}"

    def _force_tool_call(tool_name: str, args: dict, content: str, iteration: int) -> None:
        observation = _run_tool_call(tool_name, args, iteration)
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
                return content

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

            return content

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
    return content


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", required=True, help="the question to ask")
    parser.add_argument("--dataset", default=None, help="mailarchive dataset id (omit to let the agent discover it)")
    parser.add_argument(
        "--evolution-dataset", default=None, help="evolution/decision dataset id (omit to let the agent discover it)"
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
            model_provider=args.model_provider,
            model_name=args.model,
            mail_top_k=args.mail_top_k,
            verbose=args.verbose,
        )
        print(answer)


if __name__ == "__main__":
    main()
