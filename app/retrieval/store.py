"""Embeddings (FastEmbed, local) and vector search (Qdrant).

Two collections: `kb` (one point per article section) and `tickets` (one
point per ticket). Superseded articles are excluded from KB search unless
nothing else scores above the minimum.
"""

import uuid
from dataclasses import dataclass
from functools import cached_property

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams

from app.chaos import check_vector_db
from app.config import Settings, get_settings
from app.retrieval.documents import Chunk

KB_COLLECTION = "kb"
TICKETS_COLLECTION = "tickets"


@dataclass(frozen=True)
class RetrievedDoc:
    """A search hit, with everything the prompt and citation check need."""

    doc_id: str
    title: str
    section: str
    text: str
    score: float
    superseded: bool
    collection: str


class Retriever:
    """Embeds text and searches Qdrant. Create once and reuse: loading the model is slow."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        # Short timeout: if Qdrant is down, the pipeline should degrade quickly, not hang.
        self.client = QdrantClient(url=self.settings.qdrant_url, timeout=self.settings.qdrant_timeout_seconds)

    @cached_property
    def embedder(self) -> TextEmbedding:
        """Loaded on first use; downloads the model once into the cache folder."""
        return TextEmbedding(self.settings.embedding_model, cache_dir=self.settings.embedding_cache_dir)

    # --- Ingestion --------------------------------------------------------

    def rebuild_collection(self, name: str, chunks: list[Chunk]) -> None:
        """Drop and recreate a collection, then embed and upload all chunks."""
        vectors = list(self.embedder.passage_embed([c.embedding_text() for c in chunks]))
        if self.client.collection_exists(name):
            self.client.delete_collection(name)
        self.client.create_collection(name, vectors_config=VectorParams(size=len(vectors[0]), distance=Distance.COSINE))
        self.client.upsert(name, points=[
            PointStruct(
                # Stable IDs: the same chunk always gets the same point ID.
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{name}/{chunk.doc_id}/{chunk.section}")),
                vector=vector.tolist(),
                payload={"doc_id": chunk.doc_id, "title": chunk.title, "section": chunk.section,
                         "text": chunk.text, "superseded": chunk.superseded},
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ])

    # --- Search -----------------------------------------------------------

    def _search(self, collection: str, vector: list[float], limit: int, query_filter: Filter | None) -> list[RetrievedDoc]:
        check_vector_db()  # chaos flag: fail as if Qdrant were unreachable
        response = self.client.query_points(
            collection, query=vector, limit=limit, query_filter=query_filter,
            score_threshold=self.settings.retrieval_min_score, with_payload=True,
        )
        return [
            RetrievedDoc(
                doc_id=p.payload["doc_id"], title=p.payload["title"], section=p.payload["section"],
                text=p.payload["text"], score=p.score, superseded=p.payload["superseded"], collection=collection,
            )
            for p in response.points
        ]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self.embedder.query_embed(text))).tolist()

    def search_kb(self, vector: list[float]) -> list[RetrievedDoc]:
        """Top KB sections; superseded articles only if nothing current matches."""
        current_only = Filter(must_not=[FieldCondition(key="superseded", match=MatchValue(value=True))])
        hits = self._search(KB_COLLECTION, vector, self.settings.retrieval_kb_top_k, current_only)
        return hits or self._search(KB_COLLECTION, vector, self.settings.retrieval_kb_top_k, None)

    def search_tickets(self, vector: list[float]) -> list[RetrievedDoc]:
        return self._search(TICKETS_COLLECTION, vector, self.settings.retrieval_ticket_top_k, None)
