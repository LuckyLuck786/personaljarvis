"""Text chunking for the RAG pipeline: split on paragraph/sentence
boundaries where possible, hard-split otherwise, with overlap so context
isn't lost at boundaries. Character-based sizing (~4 chars/token heuristic)
keeps this dependency-free."""

from __future__ import annotations

CHUNK_CHARS = 1400   # ≈ 350 tokens
OVERLAP_CHARS = 200


def chunk_text(text: str, chunk_chars: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_chars, len(text))
        if end < len(text):
            # prefer to break on a paragraph, then sentence, then space
            for sep in ("\n\n", ". ", "\n", " "):
                cut = text.rfind(sep, start + chunk_chars // 2, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks
