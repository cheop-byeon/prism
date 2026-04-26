import json
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

from configs import dify_config
from core.rag.datasource.vdb.vector_base import BaseVector
from core.rag.datasource.vdb.vector_factory import AbstractVectorFactory
from core.rag.datasource.vdb.vector_type import VectorType
from core.rag.embedding.embedding_base import Embeddings
from core.rag.models.document import Document
from extensions.ext_redis import redis_client
from models.dataset import Dataset


class FaissConfig(BaseModel):
    persist_path: str


class FaissVector(BaseVector):
    def __init__(self, collection_name: str, config: FaissConfig):
        super().__init__(collection_name)
        self._collection_dir = Path(config.persist_path) / collection_name
        self._collection_dir.mkdir(parents=True, exist_ok=True)
        self._index_file = self._collection_dir / "index.faiss"
        self._meta_file = self._collection_dir / "meta.json"
        self._lock = threading.Lock()
        self._index = None
        self._meta: dict = {"ids": [], "texts": [], "metadatas": []}
        self._load()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        import faiss  # noqa: PLC0415

        if self._index_file.exists() and self._meta_file.exists():
            self._index = faiss.read_index(str(self._index_file))
            with open(self._meta_file) as f:
                self._meta = json.load(f)

    def _save(self) -> None:
        import faiss  # noqa: PLC0415

        if self._index is not None:
            faiss.write_index(self._index, str(self._index_file))
        tmp = self._meta_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._meta, f)
        os.replace(tmp, self._meta_file)  # atomic on POSIX

    # ------------------------------------------------------------------
    # Vector normalisation (cosine similarity via inner product)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(vectors: list[list[float]]) -> np.ndarray:
        arr = np.array(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.where(norms == 0.0, 1.0, norms)
        return arr / norms

    # ------------------------------------------------------------------
    # BaseVector interface
    # ------------------------------------------------------------------

    def get_type(self) -> str:
        return VectorType.FAISS

    def create(self, texts: list[Document], embeddings: list[list[float]], **kwargs) -> None:
        if texts:
            self.add_texts(texts, embeddings, **kwargs)

    def add_texts(self, documents: list[Document], embeddings: list[list[float]], **kwargs) -> list[str]:
        import faiss  # noqa: PLC0415

        if not documents:
            return []

        vectors = self._normalize(embeddings)
        dim = vectors.shape[1]

        lock_name = f"vector_indexing_lock_{self._collection_name}"
        with redis_client.lock(lock_name, timeout=20):
            # Reload from disk so concurrent writers don't overwrite each other.
            self._load()

            with self._lock:
                if self._index is None:
                    # IndexFlatIP + normalised vectors = cosine similarity.
                    self._index = faiss.IndexFlatIP(dim)

                existing_ids = set(self._meta["ids"])
                new_vecs: list[np.ndarray] = []
                new_docs: list[Document] = []
                for doc, vec in zip(documents, vectors):
                    doc_id = doc.metadata.get("doc_id", "")
                    if doc_id not in existing_ids:
                        new_docs.append(doc)
                        new_vecs.append(vec)
                        existing_ids.add(doc_id)

                if new_docs:
                    self._index.add(np.stack(new_vecs))
                    for doc in new_docs:
                        self._meta["ids"].append(doc.metadata.get("doc_id", ""))
                        self._meta["texts"].append(doc.page_content)
                        self._meta["metadatas"].append(doc.metadata)
                    self._save()

        return [doc.metadata.get("doc_id", "") for doc in documents]

    def text_exists(self, id: str) -> bool:
        return id in self._meta["ids"]

    def delete_by_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        id_set = set(ids)
        lock_name = f"vector_indexing_lock_{self._collection_name}"
        with redis_client.lock(lock_name, timeout=20):
            self._load()
            with self._lock:
                keep = [i for i, mid in enumerate(self._meta["ids"]) if mid not in id_set]
                if len(keep) == len(self._meta["ids"]):
                    return
                self._rebuild(keep)
                self._save()

    def delete_by_metadata_field(self, key: str, value: str) -> None:
        lock_name = f"vector_indexing_lock_{self._collection_name}"
        with redis_client.lock(lock_name, timeout=20):
            self._load()
            with self._lock:
                keep = [
                    i for i, meta in enumerate(self._meta["metadatas"])
                    if meta.get(key) != value
                ]
                if len(keep) == len(self._meta["metadatas"]):
                    return
                self._rebuild(keep)
                self._save()

    def _rebuild(self, keep: list[int]) -> None:
        """Reconstruct the FAISS index retaining only the given positional indices."""
        import faiss  # noqa: PLC0415

        if not keep or self._index is None:
            self._index = None
            self._meta = {"ids": [], "texts": [], "metadatas": []}
            return

        dim = self._index.d
        kept_vecs = np.empty((len(keep), dim), dtype=np.float32)
        for new_pos, old_pos in enumerate(keep):
            self._index.reconstruct(old_pos, kept_vecs[new_pos])

        new_index = faiss.IndexFlatIP(dim)
        new_index.add(kept_vecs)
        self._index = new_index
        self._meta = {
            "ids": [self._meta["ids"][i] for i in keep],
            "texts": [self._meta["texts"][i] for i in keep],
            "metadatas": [self._meta["metadatas"][i] for i in keep],
        }

    def search_by_vector(self, query_vector: list[float], **kwargs: Any) -> list[Document]:
        top_k: int = kwargs.get("top_k", 4)
        score_threshold: float = float(kwargs.get("score_threshold") or 0.0)
        document_ids_filter: list[str] | None = kwargs.get("document_ids_filter")

        with self._lock:
            if self._index is None or self._index.ntotal == 0:
                return []

            query = self._normalize([query_vector])
            # Fetch extra candidates when filtering so we still get top_k after.
            fetch_k = min(top_k * 10 if document_ids_filter else top_k, self._index.ntotal)
            scores, indices = self._index.search(query, fetch_k)

            docs: list[Document] = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0:
                    continue
                meta = dict(self._meta["metadatas"][idx])
                if document_ids_filter and meta.get("document_id") not in document_ids_filter:
                    continue
                if score < score_threshold:
                    continue
                meta["score"] = float(score)
                docs.append(Document(page_content=self._meta["texts"][idx], metadata=meta))
                if len(docs) >= top_k:
                    break

        return sorted(docs, key=lambda d: d.metadata.get("score", 0), reverse=True)

    def search_by_full_text(self, query: str, **kwargs: Any) -> list[Document]:
        # FAISS is a vector-only index; full-text search is not supported.
        return []

    def delete(self) -> None:
        with self._lock:
            self._index = None
            self._meta = {"ids": [], "texts": [], "metadatas": []}
        if self._index_file.exists():
            self._index_file.unlink()
        if self._meta_file.exists():
            self._meta_file.unlink()
        try:
            self._collection_dir.rmdir()
        except OSError:
            pass


class FaissVectorFactory(AbstractVectorFactory):
    def init_vector(self, dataset: Dataset, attributes: list, embeddings: Embeddings) -> BaseVector:
        if dataset.index_struct_dict:
            class_prefix: str = dataset.index_struct_dict["vector_store"]["class_prefix"]
            collection_name = class_prefix.lower()
        else:
            collection_name = Dataset.gen_collection_name_by_id(dataset.id).lower()
            dataset.index_struct = json.dumps(
                {"type": VectorType.FAISS, "vector_store": {"class_prefix": collection_name}}
            )

        return FaissVector(
            collection_name=collection_name,
            config=FaissConfig(
                persist_path=dify_config.FAISS_INDEX_PATH or "storage/vector_index/faiss",
            ),
        )
