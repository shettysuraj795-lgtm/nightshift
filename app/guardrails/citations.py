"""Citation validation: a citation must point at a retrieved document and quote it.

The excerpt is compared after normalising both sides (case, whitespace,
bullet and number markers, backticks), so harmless formatting differences do
not count as fabrication but invented wording does.
"""

import re
from dataclasses import dataclass

from app.retrieval.store import RetrievedDoc
from app.schemas import Citation

_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)


def normalise(text: str) -> str:
    text = _LIST_MARKER.sub("", text)
    text = text.replace("`", "").replace("**", "")
    return " ".join(text.lower().split())


@dataclass(frozen=True)
class CitationCheck:
    valid: list[Citation]
    dropped: int


def validate_citations(citations: list[Citation], retrieved: list[RetrievedDoc]) -> CitationCheck:
    texts: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    for doc in retrieved:
        texts.setdefault(doc.doc_id, []).append(normalise(doc.text))
        titles.setdefault(doc.doc_id, doc.title)

    valid = []
    for citation in citations:
        excerpt = normalise(citation.excerpt)
        if excerpt and any(excerpt in text for text in texts.get(citation.doc_id, [])):
            # The title comes from the source, not the model, so it cannot carry injected text.
            valid.append(Citation(doc_id=citation.doc_id, title=titles[citation.doc_id], excerpt=citation.excerpt))
    return CitationCheck(valid=valid, dropped=len(citations) - len(valid))
