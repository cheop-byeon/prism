"""Pseudo-data prototype for a multi-hop "family search" workflow.

Motivating scenario (the user's own framing): a decision question about
one RFC can't be answered from that RFC's own mail archive, because the
actual discussion happened on a later sibling/child RFC that fixed or
clarified something (DNSSEC was the real-world example given -- RFC 4033/
4034/4035 and their later correctors). Proving that workflow shape --
check one RFC's mail, if empty rank its relatives by likely relevance,
check the top one, and if STILL not found ask the user before spending
another round-trip on the next one -- doesn't require importing a real
family's worth of mailarchive data first. This module is entirely
fabricated data for exactly that reason: validate the workflow cheaply
before paying the real import cost.

Deliberately isolated from every real tool/service in this project: no
real Datatracker calls, no real Dify dataset, and no real RFC numbers
(fictional "DEMO-RFC-####" ids), so none of this fabricated content can
be mistaken for real IETF history if it ever leaks into a screenshot or
a log.
"""

from __future__ import annotations

import json

_DEMO_FAMILY: dict[str, dict] = {
    "DEMO-RFC-9001": {
        "title": "Widget Key Rotation Protocol",
        "abstract": "Defines the Widget Key Rotation Protocol (WKRP), including a fixed 90-day key rotation interval.",
        "related": ["DEMO-RFC-9002", "DEMO-RFC-9003"],
        "emails": [
            {
                "from": "a.chen@example.org",
                "date": "2024-01-10",
                "subject": "Re: WG name bikeshedding",
                "body": "Can we just call it WKRP and move on? Nobody will remember the long name anyway.",
            },
            {
                "from": "j.oyelaran@example.org",
                "date": "2024-01-11",
                "subject": "Re: WG name bikeshedding",
                "body": "+1, WKRP it is. Let's focus on the actual protocol mechanics next.",
            },
        ],
    },
    # A red herring on purpose: its title sounds highly relevant to a
    # timing question, but its actual content is about a different kind
    # of timing entirely (message retransmission, not key rotation) --
    # tests whether the ranking step is fooled by surface wording alone.
    "DEMO-RFC-9002": {
        "title": "Widget Key Rotation Protocol: Timing Parameters",
        "abstract": "Specifies retry and retransmission timers for WKRP control messages.",
        "related": [],
        "emails": [
            {
                "from": "m.dubois@example.org",
                "date": "2024-03-02",
                "subject": "Retransmission timer too aggressive",
                "body": (
                    "The 2-second retransmission timer for control messages is causing "
                    "unnecessary retries on high-latency links. Proposing we bump it to 5s."
                ),
            },
            {
                "from": "a.chen@example.org",
                "date": "2024-03-03",
                "subject": "Re: Retransmission timer too aggressive",
                "body": (
                    "Agreed, 5s is safer. This has nothing to do with the key rotation "
                    "interval itself, just message retries."
                ),
            },
        ],
    },
    "DEMO-RFC-9003": {
        "title": "Widget Key Rotation Protocol: Operational Experience and Errata",
        "abstract": "Documents operational deployment experience and clarifications to WKRP based on field data.",
        "related": [],
        "emails": [
            {
                "from": "s.kowalski@example.org",
                "date": "2024-06-14",
                "subject": "Field data on key compromise detection windows",
                "body": (
                    "We pulled incident data from three large deployments: average time-to-detect "
                    "for a compromised key was 60-75 days. A 90-day rotation gives a safety margin "
                    "above the worst observed detection window (78 days) without rotating so often "
                    "that operators start disabling it."
                ),
            },
            {
                "from": "j.oyelaran@example.org",
                "date": "2024-06-15",
                "subject": "Re: Field data on key compromise detection windows",
                "body": (
                    "This matches what we saw too. 90 days as a round number with margin makes "
                    "sense. Let's cite this data in the -bis draft as the rationale for the interval."
                ),
            },
        ],
    },
}


def get_demo_roots() -> list[dict]:
    """Only documents with a family to expand into make sense as a
    starting point for this demo -- offering a leaf as a "root" would
    just be a one-step dead end with nothing to illustrate."""
    return [{"id": rfc_id, "title": rec["title"]} for rfc_id, rec in _DEMO_FAMILY.items() if rec["related"]]


def _format_emails(rfc_id: str) -> str:
    rec = _DEMO_FAMILY[rfc_id]
    if not rec["emails"]:
        return "(no emails)"
    return "\n\n".join(
        f"From: {e['from']}\nDate: {e['date']}\nSubject: {e['subject']}\n{e['body']}" for e in rec["emails"]
    )


def _extract_json(text: str, opener: str, closer: str):
    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def check_mail_for_answer(rfc_id: str, query: str, model_instance) -> dict:
    """Ask the model whether this RFC's (fake) mail archive answers the
    query -- the same judgment call a real search_ietf_mailarchive_dataset
    result gets subjected to in the real agent loop, just against
    synthetic data instead of a real Dify dataset.
    """
    from dify_graph.model_runtime.entities import SystemPromptMessage, UserPromptMessage
    from scripts.agentic_rag.ask_decision_agent import _invoke_llm_with_retry

    mail_text = _format_emails(rfc_id)
    result = _invoke_llm_with_retry(
        model_instance,
        prompt_messages=[
            SystemPromptMessage(
                content=(
                    'You are checking whether an email archive answers a specific question. Reply '
                    'with ONLY a JSON object: {"found": true or false, "answer": "..." or null}. Only '
                    "set found=true if the emails genuinely explain the rationale asked about -- "
                    "emails about a different topic, even from the same document, don't count."
                )
            ),
            UserPromptMessage(content=f"Question: {query}\n\nEmail archive for {rfc_id}:\n{mail_text}"),
        ],
        model_parameters={"temperature": 0.1, "top_p": 1},
        tools=[],
        stop=[],
        stream=False,
        user="family_search_demo",
    )
    content = result.message.content
    text = content if isinstance(content, str) else str(content)
    parsed = _extract_json(text, "{", "}")
    if not parsed:
        return {"found": False, "answer": None}
    return {"found": bool(parsed.get("found")), "answer": parsed.get("answer")}


def rank_candidates(query: str, candidate_ids: list[str], model_instance) -> list[str]:
    """Order candidates by likely relevance to the query using only their
    title+abstract -- the cheap signal available before actually checking
    each one's mail (checking mail is the expensive step this ranking
    exists to avoid doing for every candidate blindly, in order)."""
    from dify_graph.model_runtime.entities import SystemPromptMessage, UserPromptMessage
    from scripts.agentic_rag.ask_decision_agent import _invoke_llm_with_retry

    listing = "\n".join(
        f"- {cid}: {_DEMO_FAMILY[cid]['title']} -- {_DEMO_FAMILY[cid]['abstract']}" for cid in candidate_ids
    )
    result = _invoke_llm_with_retry(
        model_instance,
        prompt_messages=[
            SystemPromptMessage(
                content=(
                    "Rank these related documents by how likely each is to explain the rationale "
                    "behind the question asked, most likely first. Reply with ONLY a JSON array of "
                    'ids, e.g. ["DEMO-RFC-9003", "DEMO-RFC-9002"].'
                )
            ),
            UserPromptMessage(content=f"Question: {query}\n\nCandidates:\n{listing}"),
        ],
        model_parameters={"temperature": 0.1, "top_p": 1},
        tools=[],
        stop=[],
        stream=False,
        user="family_search_demo_rank",
    )
    content = result.message.content
    text = content if isinstance(content, str) else str(content)
    ranked = _extract_json(text, "[", "]")
    if not isinstance(ranked, list):
        return candidate_ids
    valid = [c for c in ranked if c in candidate_ids]
    missing = [c for c in candidate_ids if c not in valid]
    return valid + missing


def check_root(query: str, root: str, model_instance) -> dict:
    """First step: check the root's own mail. If empty, rank its
    relatives once -- the frontend walks that ranked queue one at a time
    via check_candidate, so ranking never needs to be redone mid-walk.
    """
    if root not in _DEMO_FAMILY:
        return {"error": f"Unknown demo RFC: {root}"}

    result = check_mail_for_answer(root, query, model_instance)
    if result["found"]:
        return {"status": "found", "rfc": root, "title": _DEMO_FAMILY[root]["title"], "answer": result["answer"]}

    candidates = list(_DEMO_FAMILY[root]["related"])
    if not candidates:
        return {"status": "exhausted"}
    ranked = rank_candidates(query, candidates, model_instance)
    return {
        "status": "dead_end",
        "checked": root,
        "queue": [{"id": cid, "title": _DEMO_FAMILY[cid]["title"]} for cid in ranked],
    }


def check_candidate(query: str, rfc_id: str, model_instance) -> dict:
    """One more hop: check a single candidate's mail (already ranked and
    queued by check_root) -- called again for each subsequent candidate
    only after the user confirms continuing."""
    if rfc_id not in _DEMO_FAMILY:
        return {"error": f"Unknown demo RFC: {rfc_id}"}

    result = check_mail_for_answer(rfc_id, query, model_instance)
    if result["found"]:
        return {"status": "found", "rfc": rfc_id, "title": _DEMO_FAMILY[rfc_id]["title"], "answer": result["answer"]}
    return {"status": "not_found", "checked": rfc_id}
