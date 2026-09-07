"""Roadmap-aware RFC family discovery.

Some topics have an authoritative overview/roadmap RFC that itself names
and describes every RFC in its family -- RFC9364 is the canonical example
for DNSSEC ("DNS Security Extensions (DNSSEC): Overview, Requirements, and
Roadmap"). When one exists for a searched topic, its own description of
each member's role is far more useful than a bare BM25-ranked title list;
when none exists, callers fall back to plain corpus search results.

Both LLM calls here are small and cheap: the first sees only a handful of
short title+abstract pairs (the top BM25/semantic candidates), the second
sees one RFC's full text but runs at most once per topic search, not once
per candidate. Any failure (LLM error, unparseable response, fetch
failure) returns None rather than raising -- roadmap detection is a
best-effort enhancement on top of the plain search results, never a hard
dependency for a topic search to return something useful.
"""

from __future__ import annotations

import json
import re

import httpx

_RFC_EDITOR_BASE = "https://www.rfc-editor.org/rfc/"


def _extract_json(text: str, opener: str, closer: str):
    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _fetch_rfc_full_text(rfc_number: str) -> str | None:
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            resp = client.get(f"{_RFC_EDITOR_BASE}rfc{rfc_number}.txt")
            if resp.status_code != 200:
                return None
            return resp.text
    except httpx.HTTPError:
        return None


def detect_roadmap_candidate(topic_query: str, candidates: list[dict], model_instance) -> str | None:
    """Ask whether any of *candidates* (each {rfc, title, abstract}) is
    itself a roadmap/overview document describing a family of RFCs for
    this topic, the way RFC9364 does for DNSSEC. Returns that RFC's number
    (bare digits) if so, else None.
    """
    from dify_graph.model_runtime.entities import SystemPromptMessage, UserPromptMessage
    from scripts.agentic_rag.ask_decision_agent import _invoke_llm_with_retry

    if not candidates:
        return None
    listing = "\n".join(f"- RFC{c['rfc']}: {c['title']} -- {c.get('abstract', '')[:300]}" for c in candidates)
    try:
        result = _invoke_llm_with_retry(
            model_instance,
            prompt_messages=[
                SystemPromptMessage(
                    content=(
                        "Given a topic and a list of candidate RFCs, decide whether ANY of them is "
                        "itself a roadmap/overview document that names and describes a family of "
                        'related RFCs for this topic (like RFC9364 does for DNSSEC: "DNS Security '
                        'Extensions (DNSSEC): Overview, Requirements, and Roadmap"). Reply with ONLY '
                        'a JSON object: {"roadmap_rfc": "1234" or null}. Only set it when a candidate '
                        "genuinely reads as an overview/roadmap of the family itself, not just a "
                        "closely related spec."
                    )
                ),
                UserPromptMessage(content=f"Topic: {topic_query}\n\nCandidates:\n{listing}"),
            ],
            model_parameters={"temperature": 0.1, "top_p": 1},
            tools=[],
            stop=[],
            stream=False,
            user="topic_discover_roadmap_detect",
        )
    except Exception:
        return None
    content = result.message.content
    text = content if isinstance(content, str) else str(content)
    parsed = _extract_json(text, "{", "}")
    if not parsed:
        return None
    rfc = parsed.get("roadmap_rfc")
    if not rfc:
        return None
    digits = re.sub(r"[^0-9]", "", str(rfc))
    return digits or None


def extract_family_from_roadmap(rfc_number: str, model_instance) -> list[dict] | None:
    """Fetch *rfc_number*'s own full text and ask it to list every RFC it
    describes as part of its family, with a one-sentence role for each --
    returns [{rfc, role}], or None if the fetch or extraction failed.
    """
    from dify_graph.model_runtime.entities import SystemPromptMessage, UserPromptMessage
    from scripts.agentic_rag.ask_decision_agent import _invoke_llm_with_retry

    text = _fetch_rfc_full_text(rfc_number)
    if not text:
        return None
    # A real roadmap RFC can be much longer than the ~20-30K character
    # range a "typical" RFC runs (confirmed live: RFC9364 for DNSSEC is
    # ~23K chars and extracted cleanly; RFC6071 for IPsec/IKE is ~149K
    # chars -- an earlier 20K-char cap only saw its front matter and table
    # of contents, missing nearly the whole actual roadmap listing, and
    # picking up boilerplate IETF-process citations near the top, like
    # RFC2026/RFC2119, as if they were real family members). The model
    # here (gemini-2.5-flash) has a context window in the hundreds of
    # thousands of tokens, so a cap this generous is about avoiding a
    # truly pathological input, not a realistic roadmap document.
    if len(text) > 200000:
        text = text[:200000]
    try:
        result = _invoke_llm_with_retry(
            model_instance,
            prompt_messages=[
                SystemPromptMessage(
                    content=(
                        "This is the full text of an IETF roadmap/overview RFC. List every OTHER RFC "
                        "it names as part of its family, with a one-sentence description of that RFC's "
                        'role (in your own words, from what the text says). Reply with ONLY a JSON '
                        'array: [{"rfc": "1234", "role": "..."}, ...]. Do not include the roadmap RFC '
                        "itself. Only include RFCs the text actually describes as part of the family, "
                        "not every RFC merely referenced in passing (e.g. a citation for an unrelated "
                        "detail). Specifically exclude generic IETF process/boilerplate references that "
                        "appear in nearly every RFC's front matter and are unrelated to this document's "
                        "actual topic -- e.g. RFC2026 (BCP process), RFC2119/RFC8174 (key words like "
                        "MUST/SHOULD/MAY), or copyright/boilerplate RFCs -- even if they're cited "
                        "somewhere in the text."
                    )
                ),
                UserPromptMessage(content=text),
            ],
            model_parameters={"temperature": 0.1, "top_p": 1},
            tools=[],
            stop=[],
            stream=False,
            user="topic_discover_roadmap_extract",
        )
    except Exception:
        return None
    content = result.message.content
    text_out = content if isinstance(content, str) else str(content)
    parsed = _extract_json(text_out, "[", "]")
    if not isinstance(parsed, list):
        return None
    family = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        rfc = re.sub(r"[^0-9]", "", str(entry.get("rfc", "")))
        role = str(entry.get("role", "")).strip()
        if rfc and role:
            family.append({"rfc": rfc, "role": role})
    return family or None
