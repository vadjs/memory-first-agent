"""Heading-aware markdown chunking (spec §7): ~800-token chunks, ~15% overlap,
section titles preserved as retrieval metadata."""

import re
from dataclasses import dataclass

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


@dataclass
class Chunk:
    text: str
    section: str


def _tokens(text: str) -> int:
    return len(_enc.encode(text, disallowed_special=()))


def _hard_split(text: str, target: int) -> list[str]:
    ids = _enc.encode(text, disallowed_special=())
    return [_enc.decode(ids[i : i + target]) for i in range(0, len(ids), target)]


def chunk_markdown(md: str, target_tokens: int = 800, overlap_tokens: int = 120) -> list[Chunk]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in md.splitlines():
        m = _HEADING.match(line)
        if m:
            sections.append((m.group(2).strip(), []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for section, lines in sections:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", "\n".join(lines)) if p.strip()]
        buffer: list[str] = []
        size = 0
        for para in paragraphs:
            n = _tokens(para)
            if n > int(target_tokens * 1.2):
                if buffer:
                    chunks.append(Chunk("\n\n".join(buffer), section))
                    buffer, size = [], 0
                chunks.extend(Chunk(piece, section) for piece in _hard_split(para, target_tokens))
                continue
            if size + n > target_tokens and buffer:
                chunks.append(Chunk("\n\n".join(buffer), section))
                # paragraph-boundary overlap: carry trailing paragraphs up to the budget
                carried: list[str] = []
                carried_size = 0
                for prev in reversed(buffer):
                    prev_n = _tokens(prev)
                    if carried_size + prev_n > overlap_tokens:
                        break
                    carried.insert(0, prev)
                    carried_size += prev_n
                buffer, size = carried, carried_size
            buffer.append(para)
            size += n
        if buffer:
            chunks.append(Chunk("\n\n".join(buffer), section))
    return chunks
