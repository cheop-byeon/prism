from pydantic import Field
from pydantic_settings import BaseSettings


class FaissConfig(BaseSettings):
    """
    Configuration settings for FAISS vector store.

    FAISS is a file-based, in-process vector store.  Each collection is
    stored as two files in FAISS_INDEX_PATH/<collection_name>/:
      - index.faiss  — the FAISS index (IndexFlatIP, cosine similarity)
      - meta.json    — document texts and metadata
    """

    FAISS_INDEX_PATH: str | None = Field(
        description=(
            "Directory where FAISS index files are persisted. "
            "Defaults to 'storage/vector_index/faiss' relative to the working directory."
        ),
        default=None,
    )
