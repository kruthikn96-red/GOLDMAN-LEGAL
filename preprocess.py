"""Deterministic splitting of a policy document into per-rule chunks.

The LLM does the semantic work (interpreting "shall not exceed", conditions,
exceptions). Splitting is purely structural and uses regex on rule markers
that the policy formatting already provides ((a), (b), (i), (ii), etc.).

Doing this without an LLM call:
  - guarantees we never miss a subsection,
  - lets us attribute each output rule back to its exact source span,
  - keeps each LLM call small and focused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SECTION_RE = re.compile(r"SECTION\s+(\d+(?:\.\d+)*)\s+(.+?):", re.IGNORECASE)

# Matches "(a)", "(ii)", etc. at the start of a line.
RULE_MARKER_RE = re.compile(r"^[ \t]*\(([a-z]+)\)[ \t]+", re.MULTILINE | re.IGNORECASE)


@dataclass
class RuleChunk:
    rule_id: str        # e.g. "5.1.a"
    section: str        # e.g. "5.1"
    section_title: str  # e.g. "ELIGIBILITY CRITERIA"
    subsection: str     # e.g. "a"
    source_text: str    # whitespace-collapsed verbatim


def split_document(text: str) -> tuple[str, str, list[RuleChunk]]:
    """Split a document into (section, section_title, rule_chunks)."""
    sec = SECTION_RE.search(text)
    if not sec:
        raise ValueError("Could not find a 'SECTION X.Y TITLE:' header in input.")

    section = sec.group(1)
    section_title = sec.group(2).strip()
    body = text[sec.end():]

    markers = list(RULE_MARKER_RE.finditer(body))
    if not markers:
        raise ValueError(f"No rule markers '(a)' / '(i)' found under SECTION {section}.")

    chunks: list[RuleChunk] = []
    for i, m in enumerate(markers):
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(body)
        subsection = m.group(1).lower()
        raw = body[start:end].strip()
        cleaned = re.sub(r"\s+", " ", raw)
        chunks.append(
            RuleChunk(
                rule_id=f"{section}.{subsection}",
                section=section,
                section_title=section_title,
                subsection=subsection,
                source_text=cleaned,
            )
        )

    return section, section_title, chunks


if __name__ == "__main__":
    # Quick smoke test
    import sys
    from pathlib import Path

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples/sample1_eligibility.txt")
    section, title, chunks = split_document(path.read_text())
    print(f"Section {section}: {title}  ({len(chunks)} rules)")
    for c in chunks:
        print(f"  [{c.rule_id}] {c.source_text[:80]}{'...' if len(c.source_text) > 80 else ''}")
