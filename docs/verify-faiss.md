# Verifying the FAISS Vector Store Implementation

## Prerequisites

- Docker and Docker Compose running
- `dev/rebuild-backend` script available (see `dev/rebuild-backend`)
- A knowledge base does NOT need to exist yet — create a fresh one for this test

---

## Step 1 — Switch the vector store to FAISS

Edit `docker/.env` and change the `VECTOR_STORE` line:

```diff
-VECTOR_STORE=weaviate
+VECTOR_STORE=faiss
```

Also add the index path (if not already present):

```env
FAISS_INDEX_PATH=storage/vector_index/faiss
```

> **Note:** Any knowledge bases created while using Weaviate will not be accessible
> under FAISS — their vectors live in Weaviate. Create a fresh knowledge base to test.

---

## Step 2 — Rebuild and restart the backend

```bash
dev/rebuild-backend
```

This builds the `dify-local-api:dev` Docker image with the new FAISS code and
restarts the `api`, `worker`, and `worker_beat` containers. Confirm the final log
lines show the API started without errors.

---

## Step 3 — Create a new knowledge base and upload a document

1. Open Dify in your browser and go to **Knowledge**.
2. Click **Create Knowledge Base**.
3. Upload any small text file (e.g. a `.txt` or `.md` file).
4. Under **Embedding Model**, select your custom model (the Ollama-compatible
   embedding server must be running — see `embed_server.py`).
5. Complete the wizard and wait for indexing to finish (status shows **Available**).

If indexing completes without an error, vectors were successfully written to FAISS.

---

## Step 4 — Confirm FAISS index files on disk

```bash
ls docker/volumes/app/storage/vector_index/faiss/
```

You should see one subdirectory per collection. Inside each:

```
<collection-name>/
├── index.faiss   ← FAISS IndexFlatIP binary
└── meta.json     ← document texts and metadata
```

---

## Step 5 — Test retrieval from the UI

1. Open the knowledge base you just created.
2. Click the **Retrieval Testing** tab.
3. Type a query related to the uploaded document content.
4. Confirm results are returned with similarity scores.

A successful result with non-zero scores confirms the full pipeline —
embedding → FAISS write → FAISS search — is working correctly.

---

## Reverting to Weaviate

To switch back, restore `docker/.env`:

```diff
-VECTOR_STORE=faiss
+VECTOR_STORE=weaviate
```

Then run `dev/rebuild-backend` again.
