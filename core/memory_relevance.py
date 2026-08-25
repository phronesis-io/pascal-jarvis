"""Bounded local retrieval from warm memory for deterministic task DATA."""

from __future__ import annotations

import re
from pathlib import Path

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,6}")


def _tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(str(text or ""))
        if len(token) >= 2
    }


def relevant_warm_lines(
    memory_dir: str | Path,
    queries: list[str],
    *,
    max_lines: int = 12,
    max_chars: int = 3000,
) -> list[dict]:
    """Return exact, scored warm-memory lines relevant to the due intents.

    This is retrieval, not summarization: every returned snippet is verbatim
    and names its source file. At least two token overlaps are required so a
    generic word such as "meeting" cannot pull an unrelated personal note.
    """
    query_tokens = _tokens("\n".join(str(query or "") for query in queries))
    if not query_tokens:
        return []
    warm = Path(memory_dir) / "warm"
    ranked: list[tuple[int, float, str, int, str]] = []
    for path in warm.glob("*.md") if warm.is_dir() else []:
        if path.name == "_index.md" or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            modified = path.stat().st_mtime
        except OSError:
            continue
        for line_number, raw in enumerate(text.splitlines(), 1):
            line = " ".join(raw.split())
            if not line or line in {"---", "..."}:
                continue
            overlap = query_tokens & _tokens(line)
            if len(overlap) < 2:
                continue
            ranked.append((len(overlap), modified, path.name, line_number, line))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    results: list[dict] = []
    used = 0
    for score, _modified, filename, line_number, line in ranked:
        snippet = line[:400]
        cost = len(filename) + len(snippet) + 32
        if results and used + cost > max_chars:
            break
        results.append({
            "file": filename,
            "line": line_number,
            "text": snippet,
            "overlap": score,
        })
        used += cost
        if len(results) >= max_lines:
            break
    return results
