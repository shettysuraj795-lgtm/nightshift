"""Embed the KB and tickets and load them into Qdrant (collections `kb`, `tickets`).

Rebuilds both collections from scratch, so it is safe to run repeatedly.

Usage:
    uv run python scripts/ingest.py
"""

from app.retrieval.documents import load_kb_chunks, load_ticket_chunks
from app.retrieval.store import KB_COLLECTION, TICKETS_COLLECTION, Retriever


def main() -> None:
    retriever = Retriever()
    for name, chunks in ((KB_COLLECTION, load_kb_chunks()), (TICKETS_COLLECTION, load_ticket_chunks())):
        retriever.rebuild_collection(name, chunks)
        print(f"{name}: {len(chunks)} chunks indexed")


if __name__ == "__main__":
    main()
