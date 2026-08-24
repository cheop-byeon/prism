# Ingestion

IETF standards work is scattered across mailing-list archives, an RFC index, draft revisions, and GitHub. There was no way to ask an LLM things like "what changed in this draft, and why did the working group decide that?" — answering that needs both the spec text *and* the mailing-list discussion behind each change, linked together. This is the pipeline that pulls all of that into Dify knowledge bases.

A Python 3.13 sidecar service (`tools/ietfdata_service/`, FastAPI) handles all IETF-specific data access, since the `ietfdata` library needs a newer Python than Dify's API runtime — the two talk over HTTP (`IETFDATA_SERVICE_URL`, internal-network-only, no host port). It exposes:

- **Mailing lists** — `GET /mailarchive/lists`, per-list sync status, and a background-thread `POST /sync`; the import endpoint itself (`POST /import/mailarchive`) supports boolean query parsing (`AND`/`OR`/quoted phrases) against subject/body, JSONL or mbox output, pagination or `fetch_all`, and dedups by `Message-ID` (IETF's IMAP double-delivers messages).
- **RFCs & drafts** — `GET /rfcs/suggestions` (autocomplete search that also builds each draft's full version history by cross-referencing the Datatracker's submission records — this is what backs the draft-evolution version tracking, see [Decision → Discussion](decision-bot.md)), format-availability probing (txt/html/pdf/xml), and bulk import by ID or by filter (since/until/stream/wg/status).
- **Text cleanup** — a front/back-matter stripper (`_extract_technical_body_text`) that removes RFC/I-D title pages, boilerplate, and page headers/footers from form-feed-paginated plaintext before anything gets chunked.

The sidecar's mailarchive output (JSONL) and mbox exports needed new extractors on Dify's side: `api/core/rag/extractor/jsonl_extractor.py` parses newline-delimited JSON (one record per line, `body`/`content` promoted to chunk text, every other field kept as metadata), registered for `.jsonl` alongside a plain-text `.mbox` fallback in `extract_processor.py`.

GitHub import (issues/PRs, async via Celery with progress tracking and resume-on-failure) is a separate, Dify-native path — `api/services/github_import_service.py` + `api/tasks/github_import_task.py` — with three interchangeable loader backends (raw GraphQL, LangChain's `GitHubIssuesLoader`, LlamaIndex's `GitHubRepositoryIssuesReader`), exporting one JSONL record per issue/PR.

See also: the Import UI entry in the [main README](../../README.md#my-additions) for the React wizard that drives GitHub/RFC/draft import from the console.
