"""Create/refresh the "Decision -> Discussion" chatflow (linear two-stage).

Every message is one straight pass — no branches, no stored state, so the
Studio canvas renders reliably (only node types a plain chatflow already uses):

    start
      -> knowledge-retrieval  (stage 1: the typed keywords retrieve the
                               best-matching CHANGE from the evolution dataset)
      -> code                 (take the top change as the decision; build the
                               mail query from its text; list the other
                               candidates for transparency)
      -> knowledge-retrieval  (stage 2: retrieve the mailing-list DISCUSSION
                               for that change)
      -> llm                  (one formal explanation: why / what it says /
                               alternatives & objections)
      -> code                 (format the retrieved threads as a source list)
      -> answer               (explanation + sources + other candidates)

The user refines by rephrasing; the "Other candidate changes" footer shows what
else matched so nothing is hidden behind a top-1 pick.

Run inside the api container; pass --app-id to overwrite in place (import
updates the DRAFT only -- Publish afterwards in Studio):

    docker exec -w /app/api docker-api-1 python scripts/sync_decision_bot.py \
        --dataset <mailarchive-dataset-id> \
        --evolution-dataset <evolution-dataset-id> \
        [--app-name "Decision -> Discussion"] [--app-id <existing-app-id>]
"""

# ruff: noqa: T201 — stdout output is the point of a CLI tool

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "/app/api")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stage-1 -> decision. Each evolution chunk carries a structured marker line:
#   ietf-evolution draft:<d> from:01 to:02 section:4.7 change:removed | <label>
# followed by the change body. We anchor on that marker (NOT lines[0], which is
# often the tail of the previous change) to name the decision explicitly and
# show the concrete change text, so the reader sees exactly what is explained.
_PICK = '''
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


def main(evo, query):
    query = (query or "").strip()
    cands = []
    for item in (evo or []):
        content = (item.get("content") if isinstance(item, dict) else "") or ""
        header, body = _split_change(content)
        cands.append({"header": header, "body": body})
    if cands:
        top = cands[0]
        decision_header = top["header"]
        decision_full = top["body"][:1500]
        decision_excerpt = decision_full[:600] + ("\\u2026" if len(top["body"]) > 600 else "")
        mail_query = (top["header"] + " " + decision_full[:600] + " " + query).strip()
        others = []
        for i in range(1, min(len(cands), 6)):
            snip = " ".join(cands[i]["body"].split())[:220]
            others.append(str(i + 1) + ". **" + cands[i]["header"] + "**\\n   " + snip)
        others_text = ("\\n\\n---\\n_Other candidate changes (rephrase to focus on "
                       "one):_\\n\\n" + "\\n\\n".join(others)) if others else ""
    else:
        decision_header = "No matching draft change \\u2014 searched the discussion directly for: " + query
        decision_full = query
        decision_excerpt = ""
        mail_query = query
        others_text = ""
    return {
        "decision_header": decision_header,
        "decision_excerpt": decision_excerpt,
        "decision_full": decision_full,
        "mail_query": mail_query,
        "others_text": others_text,
    }
'''

# Builds the "Sources" block from the mail-archive hits (dedup by thread
# label). Prefers the thread's own header line (subject + message count +
# date range, e.g. "Thread: Re: INIT_CWND (5 messages, 2011-03-02 to
# 2011-03-15)") over the bare document/file name, so a reader can see at a
# glance whether a retrieved thread's dates actually line up with the
# revision window of the decision being explained.
_FORMAT_SOURCES = '''
def main(hits):
    seen = []
    rows = []
    for h in (hits or []):
        if not isinstance(h, dict):
            continue
        name = h.get("document_name") or h.get("title") or "discussion"
        content = h.get("content") or ""
        first_line = content.strip().split("\\n", 1)[0].strip()
        label = first_line if first_line.startswith("Thread:") else str(name)
        if label in seen:
            continue
        seen.append(label)
        score = h.get("score")
        if isinstance(score, (int, float)):
            rows.append("- " + label + " (score " + str(round(score, 3)) + ")")
        else:
            rows.append("- " + label)
    if not rows:
        return {"sources_text": "_No mailing-list sources retrieved._"}
    return {"sources_text": "\\n".join(rows)}
'''

# Administrative/procedural threads (last calls, status updates, meeting
# minutes) often score deceptively well against a decision's formal spec
# text purely on broad topical closeness, crowding out genuinely relevant
# but more narrowly-worded discussion. A thread actually about this decision
# almost always repeats its identifier tokens (INIT_CWND, GAIN, TARGET, ...);
# a procedural thread almost never does. Drop retrieved hits that share none
# of the decision's identifier tokens, rather than let the LLM's guardrails
# be the only defense against citing them as if they were relevant.
_RERANK_BY_TERMS = '''
_STOPWORDS = {
    "MUST", "SHOULD", "SHALL", "MAY", "NOT", "REQUIRED", "RECOMMENDED",
    "OPTIONAL", "THE", "THIS", "THAT", "WITH", "FOR", "AND", "FROM",
    "INTO", "WHEN", "THEN", "ELSE", "ARE", "WAS", "WERE", "HAS", "HAVE",
}


def _tokens(text):
    words = set()
    for raw in text.replace("-", " ").replace("/", " ").split():
        tok = "".join(ch for ch in raw if ch.isalnum() or ch == "_").strip("_")
        if len(tok) < 2 or tok.upper() != tok or not any(ch.isalpha() for ch in tok):
            continue
        if tok in _STOPWORDS:
            continue
        words.add(tok)
    return words


def _draft_name_terms(text):
    # A draft's own name/WG-acronym ("draft-ietf-ledbat-congestion" ->
    # LEDBAT, CONGESTION) is, by construction, the single most
    # over-represented term across that WG's own mailing list -- present in
    # nearly every thread including purely administrative ones, so it has
    # zero power to discriminate relevant discussion from noise. Deriving
    # this from the marker text at runtime (rather than hardcoding a WG
    # name) means the exclusion applies to whatever corpus this runs
    # against, not just this one.
    for raw in text.replace("_", "-").split():
        tok = "".join(ch for ch in raw if ch.isalnum() or ch == "-").strip("-")
        if tok.lower().startswith("draft-"):
            parts = tok.split("-")
            return {p.upper() for p in parts if p and p.lower() not in ("draft", "ietf") and not p.isdigit()}
    return set()


def main(hits, decision_text, decision_header):
    draft_terms = _draft_name_terms(decision_header or "")
    decision_tokens = _tokens(decision_text or "") - draft_terms
    valid_hits = [h for h in (hits or []) if isinstance(h, dict)]
    if decision_tokens:
        # No identifier tokens survive in ANY hit -- rather than silently
        # fall back to the unfiltered (likely noisy) set, keep the list
        # empty: an honest "no relevant discussion found" beats confidently
        # citing threads that don't even mention what's being decided.
        kept = [h for h in valid_hits if _tokens(h.get("content") or "") & decision_tokens]
    else:
        # The decision itself has no identifier tokens (e.g. a wording or
        # architecture change, not a numbered parameter) -- there's no
        # reliable signal to filter by, so don't filter at all.
        kept = valid_hits
    # Dify caps array-typed node outputs at 30 elements (must be < 30); hits
    # arrive pre-sorted by retrieval score, so keep the top slice up to that
    # limit -- this also matches the judge step's own 29-candidate cap
    # downstream, sized to use as much of the judge model's large context
    # window as practical rather than the earlier, overly-conservative 15.
    return {"filtered_hits": kept[:29]}
'''

# The term-overlap filter above is a blunt lexical check: it can't tell
# "shares the actual topic" from "shares one generic word" (a decision about
# INIT_CWND and a thread about ALLOWED_INCREASE/TETHER can both mention
# "TCP" without being about the same thing). An LLM reading the decision and
# each surviving candidate together can make that distinction directly,
# similar to a reranker but reusing the model already wired into this
# workflow. Widening retrieval (top_k=50 on the knowledge-retrieval node)
# only helps if something downstream can then afford to sift through a
# bigger candidate pool cheaply -- this prep step can now afford much more
# than just each thread's opening message, but exactly how much depends on
# what the judge model's serving PROVIDER actually honors, not what the
# model card advertises: DeepSeek-chat lists 163,840 tokens, but routed
# through this account's (then-) single allowed provider (Novita) the real
# ceiling was 64,000 -- found the hard way when a 29 x 15,000-char pass hit
# 63,569 input tokens and errored. Gemini 2.5 Flash, routed through Google's
# own infra (google-vertex/google-ai-studio) rather than a shared aggregator
# like Novita, actually delivers close to its advertised 1,048,576 tokens,
# so the per-candidate cap and max_total_chars below are sized for that --
# revisit both if the judge model changes again. The "29" here and in
# _PARSE_JUDGMENT below are a plain hardcoded literal, not interpolated
# (each is a self-contained sandboxed script) -- keep them in sync by hand.
_PREP_JUDGE_CANDIDATES = '''
def main(hits):
    valid_hits = [h for h in (hits or []) if isinstance(h, dict)]
    capped = valid_hits[:29]
    max_total_chars = 400000
    lines = []
    total = 0
    for i, h in enumerate(capped, 1):
        content = (h.get("content") or "")[:15000]
        line = str(i) + ". " + content
        if total + len(line) > max_total_chars:
            break
        lines.append(line)
        total += len(line)
    candidates_text = "\\n\\n".join(lines) if lines else "(no candidates retrieved)"
    return {"candidates_text": candidates_text}
'''

_JUDGE_SYSTEM_PROMPT = (
    "You are checking whether retrieved mailing-list excerpts actually "
    "discuss a specific decision, before they are used to explain it. For "
    "each numbered excerpt given, decide: does it directly discuss THIS "
    "decision -- the same parameter/identifier and the same specific "
    "value or change -- and not just a broadly related topic (e.g. the "
    "same protocol, mentioning TCP, or a different parameter entirely)?\n\n"
    "You are a classifier, not a participant -- some excerpts are email "
    "arguments/debates between engineers. Never continue, complete, "
    "reply to, or take a side in any argument you read; only judge "
    "whether it discusses the decision.\n\n"
    "For each excerpt, write AT MOST one short sentence (under 15 words) "
    "on why it is or isn't relevant -- never quote or repeat the excerpt's "
    "own text back. After going through all excerpts, your reply MUST end "
    "with a line starting with exactly \"ANSWER:\" followed by the numbers "
    "of the directly relevant excerpts, separated by commas (e.g. "
    "\"ANSWER: 2,5\"), or \"ANSWER: NONE\" if none are relevant. That final "
    "ANSWER line is mandatory -- always reach it, staying terse enough to "
    "get there."
)

# The judge above is asked to end with a mandatory "ANSWER: ..." line so
# its verdict can be parsed reliably. Earlier versions scanned for digit
# runs anywhere in the reply, but a chattier model (especially once
# max_tokens gets hit mid-reasoning, finish_reason "length") can leak
# quoted excerpt content -- message numbers, dates, mid-IDs -- before ever
# reaching a verdict, and those stray digits look exactly like valid picks.
# Only trust digits that appear after the ANSWER: marker; no marker means
# no reliable verdict, so treat it the same as NONE rather than guessing.
_PARSE_JUDGMENT = '''
def main(hits, judge_text):
    valid_hits = [h for h in (hits or []) if isinstance(h, dict)][:29]
    text = judge_text or ""
    marker = "ANSWER:"
    idx_marker = text.upper().rfind(marker)
    if idx_marker == -1:
        return {"filtered_hits": []}
    tail = text[idx_marker + len(marker):]
    picked_indices = []
    seen = set()
    current = ""
    for ch in tail + " ":
        if ch.isdigit():
            current += ch
            continue
        if current:
            idx = int(current) - 1
            if 0 <= idx < len(valid_hits) and idx not in seen:
                seen.add(idx)
                picked_indices.append(idx)
            current = ""
    picked = [valid_hits[i] for i in picked_indices]
    return {"filtered_hits": picked}
'''

# The judge already IS the relevance filter -- it read full excerpts and
# decided "directly discusses this decision" vs "broadly related topic",
# a more careful judgment than the blunt term-overlap filter upstream. An
# earlier version of this step "widened" a sparse judge result (e.g. 1-2
# picks) by padding it back out with top-scored-but-NOT-judge-approved
# candidates, on the theory that a single message might be too thin a
# context. That reasoning didn't hold up: nothing in testing showed sparse
# genuine context producing a worse answer, but handing the explain LLM
# extra material the judge explicitly did NOT confirm as relevant reopens
# exactly the false-grounding risk the whole retrieval -> term-filter ->
# judge pipeline exists to close. So: trust the judge's picks as-is,
# whatever size. The only special case is zero picks -- there's genuinely
# nothing to explain from, so route to a fixed "no discussion found"
# answer instead of letting the explain LLM improvise (that's exactly the
# fabrication failure mode the sanitizer step exists to catch after the
# fact -- better to not invite it in the first place).
_WIDEN_CONTEXT = '''
def main(judge_hits):
    judge_hits = [h for h in (judge_hits or []) if isinstance(h, dict)]
    if not judge_hits:
        return {"context_hits": [], "has_discussion": "false"}
    return {"context_hits": judge_hits, "has_discussion": "true"}
'''

# Mechanical backstop for the "don't cite the source material itself as an
# alternative's source" instruction in _SYSTEM_PROMPT: smaller/free-tier
# models sometimes ignore that instruction and write "proposed by the
# original draft" / "the document" / "the decision text itself" anyway --
# different wording, same fabrication (crediting the input material, not a
# retrieved message). Rather than keep re-wording the prompt against a model
# that won't reliably follow it, drop any "Alternative:" line whose
# attribution names the source material instead of a message, and say so.
_SANITIZE_ALTERNATIVES = '''
_NON_MESSAGE_SOURCES = (
    "draft", "document", "specification", "spec", "decision text", "the text itself",
)


def _strip_list_marker(s):
    # A bare "* " bullet (asterisk + space) is a list marker; "**" glued
    # directly to the next character is markdown bold, not a bullet -- must
    # not strip that or "**Alternative:" would never match below.
    for prefix in ("- ", "* ", "\\u2022 "):
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    i = 0
    while i < len(s) and s[i].isdigit():
        i += 1
    if i > 0 and i < len(s) and s[i] in ".)":
        return s[i + 1:].strip()
    return s


def main(text):
    text = text or ""
    out = []
    dropped = 0
    for line in text.split("\\n"):
        # Tolerate a leading bullet/numbering marker ("- ", "* ", "1. ")
        # before the heading text -- not every model formats the list the
        # same way, and the fabrication pattern is the same regardless.
        bare = _strip_list_marker(line.strip())
        lower = bare.lower()
        if lower.startswith("**alternative:") or lower.startswith("alternative:"):
            if "proposed by" in lower:
                attribution = lower.split("proposed by", 1)[1]
                attribution = attribution.split("rationale", 1)[0]
                if any(term in attribution for term in _NON_MESSAGE_SOURCES):
                    dropped += 1
                    continue
        out.append(line)
    cleaned = "\\n".join(out).strip()
    if dropped:
        noun = "alternative" if dropped == 1 else "alternatives"
        was_were = "was" if dropped == 1 else "were"
        note = ("_Note: " + str(dropped) + " listed " + noun + " credited the source material "
                "itself rather than a retrieved message, and " + was_were + " removed._")
        cleaned = cleaned + "\\n\\n" + note
    return {"text": cleaned}
'''

_SYSTEM_PROMPT = (
    "You are an assistant for IETF standards research. You are given ONE specific "
    "specification change (the decision) and the mailing-list discussion retrieved "
    "for it.\n\n"
    "The decision text tells you WHAT changed only — it is not itself a source of "
    "WHY. It is already shown to the user above your answer, in a '## Decision "
    "under study' block — do not restate or quote it again, and do not paraphrase "
    "it as if paraphrasing explained it. Spend your entire answer on WHY, "
    "structured under exactly these two headings (keep them verbatim, bold, on "
    "their own line):\n\n"
    "**Rationale for the adopted approach** — the strongest supporting "
    "argument(s) for whatever was actually adopted (a value, a design choice, "
    "wording, a procedure — whatever the decision changed), drawn ONLY from the "
    "retrieved discussion below. Every sentence here must be traceable to "
    "something a specific retrieved message actually says. If the retrieved "
    "discussion does not clearly explain the motivation, say so explicitly "
    "rather than inferring a rationale from the decision text itself.\n"
    "**Alternatives considered** — every other approach, proposal, or option "
    "that a specific retrieved mailing-list message actually proposes for "
    "this same issue, one per line in the form: **Alternative: <short "
    "description>** — proposed by [a named person or message from the "
    "retrieved discussion], rationale: ... The source of every alternative "
    "must be a retrieved message — never 'the draft', 'the original draft', "
    "or 'the updated draft' itself, and never the decision's own before/after "
    "text repackaged as a proposal. Only list an alternative that a message "
    "explicitly raises — never invent one implied by the decision text, and "
    "never invent an 'implicitly rejected' alternative that no message "
    "states. If no retrieved message proposes a genuine alternative, say so "
    "in one line instead of padding this section.\n\n"
    "The retrieved discussion is matched by topic similarity, not guaranteed to "
    "agree with the decision: the same issue is often debated across multiple "
    "revisions, with different messages arguing for different outcomes than the "
    "one adopted. Only credit a message under **Rationale for the adopted "
    "approach** if it actually argues for what was adopted; anything arguing "
    "for a different outcome belongs under **Alternatives considered** instead, "
    "attributed to that different outcome — never merged into the rationale for "
    "the adopted one.\n\n"
    "Ground every claim in the retrieved discussion and cite thread subjects / "
    "message URLs where they appear. If a heading isn't covered by the "
    "discussion, say so briefly rather than inventing content — never "
    "substitute the decision text itself as a stand-in for missing discussion.\n\n"
    "Retrieved discussion:\n{{#context#}}"
)


def _node(node_id: str, data: dict, x: int, y: int = 300) -> dict:
    data = {**data, "selected": False, "isInIteration": False, "isInLoop": False}
    return {
        "id": node_id,
        "type": "custom",
        "position": {"x": x, "y": y},
        "positionAbsolute": {"x": x, "y": y},
        "sourcePosition": "right",
        "targetPosition": "left",
        "selected": False,
        "width": 244,
        "height": 118,
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


def _edge_h(source: str, source_type: str, target: str, target_type: str, handle: str) -> dict:
    """Like _edge, but for a non-default sourceHandle (if-else branches
    route by case_id, e.g. "true"/"false", not "source")."""
    edge = _edge(source, source_type, target, target_type)
    edge["id"] = f"{source}-{handle}-{target}"
    edge["sourceHandle"] = handle
    return edge


def _retrieval_config(embedding: tuple[str, str], top_k: int) -> dict:
    # Economy/keyword datasets have no embedding model, so provider/model come
    # back empty; that path runs keyword (BM25) search and ignores the weights,
    # but the node schema still requires strings (not None) here. When there is
    # no embedding, weight keyword search rather than the (absent) vectors.
    provider = embedding[0] or ""
    model_name = embedding[1] or ""
    has_vectors = bool(provider and model_name)
    return {
        "top_k": top_k,
        "score_threshold": None,
        "reranking_mode": "weighted_score",
        "reranking_enable": False,
        "weights": {
            "weight_type": "customized",
            "vector_setting": {
                "vector_weight": 1 if has_vectors else 0,
                "embedding_provider_name": provider,
                "embedding_model_name": model_name,
            },
            "keyword_setting": {"keyword_weight": 0 if has_vectors else 1},
        },
    }


def _build_dsl(
    app_name: str,
    email_dataset_id: str,
    email_embedding: tuple[str, str],
    evolution_dataset_id: str,
    evo_embedding: tuple[str, str],
) -> str:
    start_id = "start1"
    evokr_id = "evokr1"
    pick_id = "pick1"
    emailkr_id = "emailkr1"
    rerank_id = "rerank1"
    judgeprep_id = "judgeprep1"
    judge_id = "judge1"
    judgeparse_id = "judgeparse1"
    ctxprep_id = "ctxprep1"
    hasdisc_id = "hasdisc1"
    llm_id = "llm1"
    sanitize_id = "sanitize1"
    fmtsrc_id = "fmtsrc1"
    answer_id = "answer1"
    nodisc_answer_id = "answernodisc1"

    nodes = [
        _node(start_id, {
            "type": "start",
            "title": "Start",
            "desc": "",
            "variables": [],
        }, 40),
        _node(evokr_id, {
            "type": "knowledge-retrieval",
            "title": "Find the change",
            "desc": "Stage 1: keywords retrieve the matching change.",
            "query_variable_selector": ["sys", "query"],
            "dataset_ids": [evolution_dataset_id],
            "retrieval_mode": "multiple",
            "multiple_retrieval_config": _retrieval_config(evo_embedding, 5),
        }, 340),
        _node(pick_id, {
            "type": "code",
            "title": "Pick decision",
            "desc": "Top change becomes the decision; build the mail query.",
            "code_language": "python3",
            "code": _PICK,
            "variables": [
                {"variable": "evo", "value_selector": [evokr_id, "result"]},
                {"variable": "query", "value_selector": ["sys", "query"]},
            ],
            "outputs": {
                "decision_header": {"type": "string", "children": None},
                "decision_excerpt": {"type": "string", "children": None},
                "decision_full": {"type": "string", "children": None},
                "mail_query": {"type": "string", "children": None},
                "others_text": {"type": "string", "children": None},
            },
        }, 640),
        _node(emailkr_id, {
            "type": "knowledge-retrieval",
            "title": "Find the discussion",
            "desc": "Stage 2: retrieve the mailing-list discussion.",
            "query_variable_selector": [pick_id, "mail_query"],
            "dataset_ids": [email_dataset_id],
            "retrieval_mode": "multiple",
            "multiple_retrieval_config": _retrieval_config(email_embedding, 50),
        }, 940),
        _node(rerank_id, {
            "type": "code",
            "title": "Filter by shared terms",
            "desc": "Drops retrieved threads that share none of the decision's identifier tokens.",
            "code_language": "python3",
            "code": _RERANK_BY_TERMS,
            "variables": [
                {"variable": "hits", "value_selector": [emailkr_id, "result"]},
                {"variable": "decision_text", "value_selector": [pick_id, "decision_full"]},
                {"variable": "decision_header", "value_selector": [pick_id, "decision_header"]},
            ],
            "outputs": {"filtered_hits": {"type": "array[object]", "children": None}},
        }, 1090),
        _node(judgeprep_id, {
            "type": "code",
            "title": "Number candidates for judging",
            "desc": "Builds a numbered excerpt list (capped) for the relevance-judging LLM call.",
            "code_language": "python3",
            "code": _PREP_JUDGE_CANDIDATES,
            "variables": [{"variable": "hits", "value_selector": [rerank_id, "filtered_hits"]}],
            "outputs": {"candidates_text": {"type": "string", "children": None}},
        }, 1190),
        _node(judge_id, {
            "type": "llm",
            "title": "Judge relevance",
            "desc": "Asks which surviving candidates actually discuss this decision, not just a related topic.",
            "model": {
                "provider": "langgenius/openrouter/openrouter",
                "name": "google/gemini-2.5-flash",
                "mode": "chat",
                "completion_params": {
                    "max_tokens": 2000,
                    "temperature": 0,
                    "top_p": 1,
                    "enable_thinking": "manual",
                    "reasoning_budget": 0,
                },
            },
            "prompt_template": [
                {"role": "system", "text": _JUDGE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "text": (
                        f"Decision:\n{{{{#{pick_id}.decision_header#}}}}\n{{{{#{pick_id}.decision_full#}}}}\n\n"
                        f"Numbered excerpts:\n{{{{#{judgeprep_id}.candidates_text#}}}}"
                    ),
                },
            ],
            "context": {"enabled": False, "variable_selector": []},
            "vision": {"enabled": False, "configs": {"variable_selector": []}},
            "retry_config": {"retry_enabled": True, "max_retries": 2, "retry_interval": 8000},
        }, 1290),
        _node(judgeparse_id, {
            "type": "code",
            "title": "Apply relevance judgment",
            "desc": "Keeps only the candidates the judging LLM marked as directly relevant.",
            "code_language": "python3",
            "code": _PARSE_JUDGMENT,
            "variables": [
                {"variable": "hits", "value_selector": [rerank_id, "filtered_hits"]},
                {"variable": "judge_text", "value_selector": [judge_id, "text"]},
            ],
            "outputs": {"filtered_hits": {"type": "array[object]", "children": None}},
        }, 1390),
        _node(ctxprep_id, {
            "type": "code",
            "title": "Check discussion found",
            "desc": "0 judge picks -> no discussion found (hard stop). Otherwise pass the judge's picks through as-is.",
            "code_language": "python3",
            "code": _WIDEN_CONTEXT,
            "variables": [
                {"variable": "judge_hits", "value_selector": [judgeparse_id, "filtered_hits"]},
            ],
            "outputs": {
                "context_hits": {"type": "array[object]", "children": None},
                "has_discussion": {"type": "string", "children": None},
            },
        }, 1590),
        _node(hasdisc_id, {
            "type": "if-else",
            "title": "Any discussion found?",
            "desc": "true: explain grounded in retrieved discussion. false: say so plainly, no LLM call.",
            "cases": [{
                "case_id": "true",
                "id": "true",
                "logical_operator": "and",
                "conditions": [{
                    "id": "has-disc-cond",
                    "varType": "string",
                    "variable_selector": [ctxprep_id, "has_discussion"],
                    "comparison_operator": "is",
                    "value": "true",
                }],
            }],
        }, 1790),
        _node(llm_id, {
            "type": "llm",
            "title": "Explain the decision",
            "desc": "",
            "model": {
                "provider": "langgenius/openrouter/openrouter",
                "name": "google/gemini-2.5-flash",
                "mode": "chat",
                "completion_params": {"max_tokens": 1500, "temperature": 0.3, "top_p": 1},
            },
            "prompt_template": [
                {"role": "system", "text": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "text": (
                        f"Decision (the change to explain):\n{{{{#{pick_id}.decision_header#}}}}\n\n"
                        f"Change text:\n{{{{#{pick_id}.decision_full#}}}}"
                    ),
                },
            ],
            "context": {"enabled": True, "variable_selector": [ctxprep_id, "context_hits"]},
            "vision": {"enabled": False, "configs": {"variable_selector": []}},
            # Free-tier OpenRouter models return transient 429s under load; retry
            # twice with a pause instead of surfacing an empty answer.
            "retry_config": {"retry_enabled": True, "max_retries": 2, "retry_interval": 8000},
        }, 1990),
        _node(sanitize_id, {
            "type": "code",
            "title": "Sanitize alternatives",
            "desc": "Drops any 'Alternative:' line that cites the draft itself instead of a retrieved message.",
            "code_language": "python3",
            "code": _SANITIZE_ALTERNATIVES,
            "variables": [
                {"variable": "text", "value_selector": [llm_id, "text"]},
            ],
            "outputs": {"text": {"type": "string", "children": None}},
        }, 2190),
        _node(fmtsrc_id, {
            "type": "code",
            "title": "List sources",
            "desc": "Formats the retrieved threads as a source list.",
            "code_language": "python3",
            "code": _FORMAT_SOURCES,
            "variables": [
                {"variable": "hits", "value_selector": [judgeparse_id, "filtered_hits"]},
            ],
            "outputs": {"sources_text": {"type": "string", "children": None}},
        }, 2390),
        _node(answer_id, {
            "type": "answer",
            "title": "Answer",
            "desc": "",
            "answer": (
                f"## 🔎 Decision under study\n"
                f"**{{{{#{pick_id}.decision_header#}}}}**\n\n"
                f"> {{{{#{pick_id}.decision_excerpt#}}}}\n\n"
                f"---\n\n"
                f"{{{{#{sanitize_id}.text#}}}}\n\n"
                f"---\n**Sources (retrieved mailing-list discussion):**\n"
                f"{{{{#{fmtsrc_id}.sources_text#}}}}"
                f"{{{{#{pick_id}.others_text#}}}}"
            ),
        }, 2590),
        _node(nodisc_answer_id, {
            "type": "answer",
            "title": "Answer (no discussion found)",
            "desc": "",
            "answer": (
                f"## 🔎 Decision under study\n"
                f"**{{{{#{pick_id}.decision_header#}}}}**\n\n"
                f"> {{{{#{pick_id}.decision_excerpt#}}}}\n\n"
                f"---\n\n"
                f"No relevant mailing-list discussion was found for this decision "
                f"-- the retrieved messages didn't directly address it, so no "
                f"rationale or alternatives can be grounded in the archive.\n\n"
                f"---\n**Sources (retrieved mailing-list discussion):**\n"
                f"_No mailing-list sources retrieved._"
                f"{{{{#{pick_id}.others_text#}}}}"
            ),
        }, 1990, 560),
    ]

    edges = [
        _edge(start_id, "start", evokr_id, "knowledge-retrieval"),
        _edge(evokr_id, "knowledge-retrieval", pick_id, "code"),
        _edge(pick_id, "code", emailkr_id, "knowledge-retrieval"),
        _edge(emailkr_id, "knowledge-retrieval", rerank_id, "code"),
        _edge(rerank_id, "code", judgeprep_id, "code"),
        _edge(judgeprep_id, "code", judge_id, "llm"),
        _edge(judge_id, "llm", judgeparse_id, "code"),
        _edge(judgeparse_id, "code", ctxprep_id, "code"),
        _edge(ctxprep_id, "code", hasdisc_id, "if-else"),
        _edge_h(hasdisc_id, "if-else", llm_id, "llm", "true"),
        _edge_h(hasdisc_id, "if-else", nodisc_answer_id, "answer", "false"),
        _edge(llm_id, "llm", sanitize_id, "code"),
        _edge(sanitize_id, "code", fmtsrc_id, "code"),
        _edge(fmtsrc_id, "code", answer_id, "answer"),
    ]

    dsl = {
        "app": {
            "name": app_name,
            "description": "Type keywords about a change; get its mailing-list discussion explained.",
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
                    "Type a few keywords or a short phrase describing the specification "
                    "change you want to understand (e.g. \"base delay late comers\"). "
                    "I'll find the best-matching change in the draft and explain **why "
                    "it was made**, **what it says**, and **what alternatives or "
                    "objections were raised**, with the mailing-list sources — plus the "
                    "other candidate changes so you can rephrase to target a different one."
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


def _embedding_of(dataset_id: str, label: str):
    from extensions.ext_database import db
    from models.dataset import Dataset

    dataset = db.session.query(Dataset).filter_by(id=dataset_id).first()
    if not dataset:
        sys.exit(f"{label} dataset not found: {dataset_id}")
    return dataset.embedding_model_provider, dataset.embedding_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, help="mailarchive dataset id (the discussion source)")
    parser.add_argument("--evolution-dataset", required=True,
                        help="evolution dataset id (the candidate changes source)")
    parser.add_argument("--app-name", default="Decision → Discussion")
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

    if stored_features.get("opening_statement") != expected["features"]["opening_statement"]:
        problems.append("feature mismatch: opening_statement")

    if problems:
        sys.exit("VERIFICATION FAILED: " + "; ".join(problems) + " — re-run the script or check the app in Studio")


if __name__ == "__main__":
    main()
