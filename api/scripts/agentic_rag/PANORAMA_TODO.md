# Panorama / agentic RAG — known issues to fix

Deliberately NOT applied directly to `ask_decision_agent.py` yet — that file
already has two other uncommitted fixes from this session (see below), and
this repo pulls upstream changes to that file regularly. Stacking a third
un-reconciled edit on top raises the chance of a messy merge next pull.
Apply these once the working tree is synced with upstream and it's safe to
edit again.

## 1. TODO: fetch tools use exact-substring matching instead of keyword search

**File:** `api/scripts/agentic_rag/ask_decision_agent.py`, `_filter_primary_source_text` (~line 1130), used by `fetch_ietf_rfc_text`, `fetch_ietf_draft_text`, and `diff_ietf_draft_revisions`.

**Current behavior:**
```python
matches = [p.strip() for p in paragraphs if query.lower() in p.lower()]
```
The entire `query` string must appear as one literal, contiguous, exact-word-order substring inside a paragraph. Not keyword search, not BM25, not fuzzy — an exact phrase match.

**Why it's a bug, not a deliberate constraint:** every other tool in the same toolkit (`search_rfc_corpus_bm25`, `find_similar_rfcs_by_meaning`, `search_local_source`, the evolution/mailarchive search tools) does real keyword or semantic matching, and is described to the model that way. These three tools' schema just says `"optional phrase"` and the skill prompt (`agent_skill.md`) never flags that they behave completely differently from every other tool with a `query` parameter. The model (tested: Haiku 4.5, but nothing here is Haiku-specific) reasonably treats `query` the same way across all nine-plus tools and writes natural multi-word topic descriptions, which almost never appear as an exact substring in real RFC prose.

**Measured impact (live-tested, 19 Panorama runs across all 6 families, this session):**
- Every single run (19/19) re-fetched at least one document 3+ times with varied queries — the retry-until-something-matches signature.
- Worst cases: RFC 1519 fetched 13 times, RFC 4862 and RFC 8205 12 times each, RFC 7126 12 times, RFC 1092 11 times.
- Directly reproduced: of 12 distinct queries tried against RFC 1519 in one run, 10 (83%) returned zero matches. The only two that worked were the shortest — a 2-word phrase and a single word. RFC 1519 does contain content relevant to every one of the 10 failed queries; the tool just couldn't find it.
- Distinct-sources-touched ÷ total-tool-calls ratio, averaged across all 19 runs: ~0.35. Only about 1 in 3 tool calls reaches a genuinely new source; the other 2 in 3 are repeat calls to an already-visited document, mostly this retry pattern.
- Net effect: Panorama's `max_iterations=20` budget, meant for breadth across many RFCs/drafts/WGs, is regularly half-consumed (or worse) by one document's retry loop before ever moving on. Very likely the direct cause of at least one live "couldn't find the rationale" honest-failure answer (IPv6 clean-break-from-IPv4 question: RFC 1752 fetched 7 times, most likely similarly starved).

**Proposed fix:** match on the query's individual words (order-independent, all-must-be-present), not the whole phrase contiguously:
```python
words = [w for w in re.findall(r"\w+", query.lower()) if w]
matches = [p.strip() for p in paragraphs if all(w in p.lower() for w in words)]
```
This is the minimal, safe change — same function signature, same paragraph-granularity, just a real keyword match instead of a phrase match. (A BM25/ranked version would be better still, but this alone would have caught RFC 1519's and RFC 7541's actual content in the failed test cases.)

**Before applying:** re-run at minimum the HPACK question (`api/scripts/agentic_rag/panorama_design_questions.md`, HTTP #9) and the RFC-1519 CIDR question (IPv4 #2) to confirm the fix actually resolves both documented failures, and re-check the distinct-sources/total-calls ratio improves.

## Also already sitting uncommitted in this file (applied, not yet fixed-in-place committed)

For context when reconciling with a future pull — these two are already live-tested and working, just not committed:

1. **`temperature`/`top_p` both set breaks newer Anthropic models** (3 call sites, ~lines 750/1592/2522) — `top_p` dropped (was a no-op `1` anyway).
2. **Forced final tool-free turn had no explicit "answer now" instruction** (~line 2518) — added an explicit `UserPromptMessage` telling the model tools are gone and it must answer using only gathered evidence, since Haiku (unlike the default Gemini) was reading an empty tool list as "no results this round" rather than "no tools exist anymore" and narrating another search instead of answering.

Both validated across 8+ live Panorama runs with zero regressions.
