"""Load KB articles and tickets and split them into retrievable chunks.

KB articles are split by `## ` heading, one chunk per section, so a citation
points at a specific part of a runbook. Each ticket is one chunk.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@dataclass(frozen=True)
class Chunk:
    """A piece of text to embed, with the metadata stored next to it in Qdrant."""

    doc_id: str
    title: str
    section: str
    text: str
    superseded: bool = False

    def embedding_text(self) -> str:
        """What gets embedded: the title and section give the text context."""
        return f"{self.title}\n{self.section}\n{self.text}"


def split_front_matter(markdown: str) -> tuple[dict, str]:
    """Return (front matter as a dict, body) for a `---` delimited article."""
    _, front, body = markdown.split("---\n", 2)
    return yaml.safe_load(front), body


def chunk_article(markdown: str) -> list[Chunk]:
    """One chunk per `## ` section; the `# ` title line is not a chunk itself."""
    front, body = split_front_matter(markdown)
    chunks = []
    for block in body.split("\n## ")[1:]:
        heading, _, text = block.partition("\n")
        chunks.append(Chunk(
            doc_id=front["id"],
            title=front["title"],
            section=heading.strip(),
            text=text.strip(),
            superseded=bool(front.get("superseded", False)),
        ))
    return chunks


def load_kb_chunks(kb_dir: Path = DATA_DIR / "kb") -> list[Chunk]:
    """All KB chunks, in file name order so ingestion is reproducible."""
    chunks = []
    for path in sorted(kb_dir.glob("*.md")):
        chunks.extend(chunk_article(path.read_text(encoding="utf-8")))
    return chunks


def load_ticket_chunks(path: Path = DATA_DIR / "tickets.json") -> list[Chunk]:
    """One chunk per closed ticket: symptom, resolution and linked runbook."""
    tickets = json.loads(path.read_text(encoding="utf-8"))
    return [
        Chunk(
            doc_id=t["number"],
            title=f"{t['number']}: {t['job']}",
            section="Ticket",
            text=f"Symptom: {t['symptom']}\nResolution: {t['resolution']}\nLinked KB: {t['linked_kb']}",
        )
        for t in tickets
    ]
