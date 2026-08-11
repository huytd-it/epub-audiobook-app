"""Two independent chunking levels: chapters->patches, and patch text->TTS-sized chunks."""
from __future__ import annotations

import re

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")


def group_into_patches(chapter_count: int, patch_size: int = 10) -> list[tuple[int, int]]:
    """Return a list of (chapter_start, chapter_end) inclusive ranges, sequential, last one
    may be smaller than patch_size."""
    if chapter_count <= 0:
        return []
    ranges = []
    for start in range(0, chapter_count, patch_size):
        end = min(start + patch_size - 1, chapter_count - 1)
        ranges.append((start, end))
    return ranges


def _split_paragraph_into_sentences(paragraph: str) -> list[str]:
    sentences = _SENTENCE_BOUNDARY_RE.split(paragraph)
    return [s.strip() for s in sentences if s.strip()]


def _hard_split(piece: str, max_chars: int) -> list[str]:
    """Last-resort split at word boundaries for a sentence longer than max_chars.

    Without this a paragraph carrying no sentence-ending punctuation would leave the
    chunker as one oversized chunk, and TTS backends reject it.
    """
    words = piece.split(" ")
    parts: list[str] = []
    buffer = ""
    for word in words:
        candidate = f"{buffer} {word}".strip()
        if len(candidate) <= max_chars:
            buffer = candidate
            continue
        if buffer:
            parts.append(buffer)
        # A single word longer than the limit still has to be cut somewhere.
        while len(word) > max_chars:
            parts.append(word[:max_chars])
            word = word[max_chars:]
        buffer = word
    if buffer:
        parts.append(buffer)
    return parts


def split_into_tts_chunks(text: str, max_chars: int = 400) -> list[str]:
    """Greedily pack paragraphs/sentences into chunks no longer than max_chars,
    never splitting mid-sentence."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
            continue
        for sentence in _split_paragraph_into_sentences(paragraph):
            if len(sentence) <= max_chars:
                pieces.append(sentence)
            else:
                pieces.extend(_hard_split(sentence, max_chars))

    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if not buffer:
            buffer = piece
        elif len(buffer) + 1 + len(piece) <= max_chars:
            buffer = f"{buffer} {piece}"
        else:
            chunks.append(buffer)
            buffer = piece
    if buffer:
        chunks.append(buffer)

    return chunks
