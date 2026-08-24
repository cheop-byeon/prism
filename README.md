# Prism

<img src="./images/prism-logo.png" alt="Prism" width="200" align="right">
<sup>*[Spectrum icons](https://www.flaticon.com/free-icons/spectrum)*</sup>

This repo is build upon [Dify](https://github.com/langgenius/dify), an open-source LLM app development platform, extended with a pipeline for importing IETF standards data (mailing lists, RFCs, internet-drafts, and working-group GitHub repos) into Dify knowledge bases for RAG.

For everything about Dify itself — install docs, general features, community, contributing — see the [upstream repository](https://github.com/langgenius/dify). This README only covers what's specific to this project.

## Background

Prism is based on the [Ph.D. project](https://folk.universitetetioslo.no/michawe/research/projects/rdr/index.html) of Jie Bian, extracting and explaining the design rationale behind RFC/Internet-Draft decisions from the discussion that produced them. See the [project page](https://folk.universitetetioslo.no/michawe/research/projects/rdr/index.html) for further research details, and [mwelzl/prism](https://github.com/mwelzl/prism) for the companion search-engine repository this fork feeds into.

## Quick start

> Before installing, make sure your machine meets the following minimum system requirements:
>
> - CPU >= 2 Core
> - RAM >= 4 GiB

The easiest way to start the server is through [Docker Compose](docker/docker-compose.yaml). Make sure [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/) are installed, then:

```bash
cd dify
cd docker
cp .env.example .env
docker compose up -d
```

Access the dashboard at [http://localhost/install](http://localhost/install) and start the initialization process.

## My additions

IETF standards work is scattered across mailing-list archives, an RFC index, draft revisions, and GitHub. There was no way to ask an LLM things like "what changed in this draft, and why did the working group decide that?" — answering that needs both the spec text *and* the mailing-list discussion behind each change, linked together. This project builds that pipeline on top of Dify's RAG stack. Deep dives on each piece:

- **[Ingestion](docs/prism/ingestion.md)** — a Python 3.13 sidecar for mailing lists, RFCs/drafts, and a Dify-native GitHub issues/PRs import path.
- **[Conversation threading & text quality](docs/prism/threading.md)** — grouping raw messages into threads, stripping quoted noise, filtering administrative notices.
- **["Decision → Discussion" chatflow](docs/prism/decision-bot.md)** — the piece that actually answers *why*, and the draft-evolution diff pipeline it's built on.
- **[Pluggable chunking strategies](docs/prism/chunking-strategies.md)** — 24 named strategies across custom/LlamaIndex/LangChain backends, for any dataset.
- **[Embedding & retrieval infrastructure](docs/prism/embedding-and-retrieval.md)** — a custom fine-tuned embedding model, a from-scratch BM25 keyword store, and Weaviate hardening.
- **[Chunk metadata inspection](docs/prism/chunk-metadata.md)** — surfacing pipeline metadata (draft lineage, thread provenance) in the console's chunk detail view.

Two smaller pieces, not big enough for their own doc:

- **Import UI** — a React import wizard (`web/app/components/datasets/create/ietf-importer/`) with WG-aware auto-fill — backed by a ~380-entry working-group → GitHub-repo mapping (`wg-repos.ts`) — and an I-D/RFC info card synced against the Datatracker (predecessors, replaces/replaced-by, published-as RFC).
- **Exploratory / in progress** — a couple of root-level scripts (`gh2csv_code.py`, `gh2csv_ids.py`) are an earlier, standalone exploration of the same "decision → discussion" idea applied to GitHub PRs instead of mailing-list threads — for each merged PR, aggregating its own comments plus the comments/reviews of every issue it closed into one record. Not wired into the app; kept as a reference for a possible PR-centric counterpart to the mailing-list chatflow. `WG-list(update).md` is the staging source used to refresh `wg-repos.ts` above.

This is still actively evolving — see the commit history on `feature/ietf-mailarchive-import` for the current state of the work.

## Local development

The quick-start above (`docker compose up -d` with the published images) is the fastest way to just run the app. Day-to-day development on this fork uses a different, more granular setup — everything lives in `dev/`:

- **`dev/setup`** — one-time bootstrap: copies the `.env.example` files (`api/`, `web/`, `docker/middleware.env.example`), then `uv sync --group dev` and `pnpm install`.
- **`dev/start-active-dev`** — the main hybrid loop: backend (`api worker worker_beat ietfdata_service weaviate sandbox ssrf_proxy plugin_daemon`) runs in Docker via `docker-compose.override.yaml`, which builds a local `dify-local-api:dev` image from `../api` instead of pulling `langgenius/dify-api`; the frontend runs natively (`pnpm dev`) for hot reload.
- **`dev/rebuild-backend`** — after backend code/dependency changes: clears the old `dify-local-api:dev` image and build cache, rebuilds `api` + `ietfdata_service`, force-recreates both, and tails their logs.
- **`dev/start-api`** / **`dev/start-web`** — fully native (no Docker) versions of each side, for working against `dev/start-docker-compose`'s middleware-only stack (Postgres/Redis/Weaviate).
- **`dev/set-vector-store <store>`** — patches `VECTOR_STORE` in `docker/.env` and rebuilds.

The `ietfdata_service` sidecar is always included in the Docker set (it's defined in the base `docker-compose.yaml`, not the override) and is reachable only on the internal network at `http://ietfdata_service:8010` — there's no host port for it.

**The embedding model has no compose service or env var at all.** `RFC-DRAlign-QN` is served by a host-level Ollama install, not a container: `dev/start-embedding-server` looks for the GGUF at `$HOME/Models/rfc-dralign-q8-pooled.gguf` (override with `RFC_DRALIGN_GGUF`), registers it with `ollama create rfc-dralign-q8` if not already present, and health-checks `http://localhost:11434/api/embed`. Dify itself is then pointed at it manually, once, via Settings → Model Provider → Ollama, with `base_url: http://host.docker.internal:11434` and model `rfc-dralign-q8` (4096 dims) — this step doesn't show up in any `.env` file, so it's easy to miss on a fresh machine.

## Advanced Setup

### Custom configurations

If you need to customize the configuration, refer to the comments in [.env.example](docker/.env.example) and update the corresponding values in your `.env` file. You may also need to adjust `docker-compose.yaml` (image versions, port mappings, volume mounts) for your environment. After changes, re-run `docker compose up -d`. Full list of environment variables [here](https://docs.dify.ai/getting-started/install-self-hosted/environments).

## License

This repository is licensed under the [Dify Open Source License](LICENSE), based on Apache 2.0 with additional conditions.
