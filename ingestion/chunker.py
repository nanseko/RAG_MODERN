"""Paragraph/table-aware chunks with a local tokenizer and a hard token limit."""
import re
from functools import lru_cache
from pathlib import Path

from app.settings import ROOT


@lru_cache(maxsize=1)
def _tokenizer():
    p = ROOT / "models/qwen3-embedding-0.6b/tokenizer.json"
    if p.is_file():
        from tokenizers import Tokenizer
        return Tokenizer.from_file(str(p))
    return None


def _tokens(text):
    tok = _tokenizer()
    # Conservative fallback when the approved local tokenizer is absent.
    return len(tok.encode(text, add_special_tokens=False).ids) if tok else len(text)


def _split(text, limit):
    while text:
        if _tokens(text) <= limit:
            yield text
            break
        low, high = 1, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if _tokens(text[:mid]) <= limit:
                low = mid
            else:
                high = mid - 1
        cut = low
        boundary = max(text.rfind("\n", 0, cut), text.rfind(" ", 0, cut))
        if boundary > cut // 2:
            cut = boundary
        yield text[:cut].strip()
        text = text[cut:].lstrip()


def _blocks(text, limit):
    for block in re.split(r"\n\s*\n", text.strip()):
        if not block:
            continue
        lines = block.splitlines()
        if len(lines) >= 2 and "|" in lines[0] and re.fullmatch(r"[| :\-]+", lines[1]):
            header = "\n".join(lines[:2])
            current = header
            for row in lines[2:]:
                if _tokens(current + "\n" + row) > limit:
                    yield from _split(current, limit)
                    current = header
                if _tokens(header + "\n" + row) > limit:
                    yield from _split(header + "\n" + row, limit)
                    current = header
                else:
                    current += "\n" + row
            if current != header or len(lines) == 2:
                yield from _split(current, limit)
        else:
            yield from _split(block, limit)


def hybrid_chunk(sections, target_tokens=650, max_tokens=1000):
    if not 0 < target_tokens <= max_tokens:
        raise ValueError("invalid token limits")
    chunks = []
    for section in sections:
        text = section.get("text", "").strip()
        if not text:
            continue
        current = ""
        for block in _blocks(text, min(target_tokens, max_tokens)):
            joined = current + "\n\n" + block if current else block
            if current and _tokens(joined) > target_tokens:
                chunks.append({**section, "text": current})
                current = block
            else:
                current = joined
        if current:
            chunks.append({**section, "text": current})
    return chunks
