"""Clause segmentation — in code, never by the model.

Contracts arrive as one long text (an OCR layer or a PDF extraction
upstream has already produced text; this blueprint consumes text). The
segmenter cuts the text into numbered clause units at headings and
numbering boundaries, so every clause is addressable: the extractor,
the playbook review, the packet, and counsel all point at the same
``clause_id``.

Segmentation is deliberately simple and deterministic: heading lines
(numbered headings like ``7.2 Data Protection``, ``Section 4`` /
``ARTICLE V`` banners, and all-caps heading lines) start a new unit;
everything else accumulates into the current unit. Text before the
first heading becomes a preamble unit. The same text always cuts the
same way.
"""

from __future__ import annotations

import re

from .schemas import ClauseUnit, ContractInput

_NUMBERED = re.compile(
    r"^\s*(?P<ref>\d+(?:\.\d+)*)\.?\s+(?P<heading>[A-Z][A-Za-z0-9 ,/&()'\-]{2,70})\s*$"
)
_SECTION = re.compile(
    r"^\s*(?P<kind>SECTION|Section|ARTICLE|Article)\s+"
    r"(?P<ref>[IVXLC]+|\d+(?:\.\d+)*)\s*[-—.:]?\s*(?P<heading>.*)$"
)
_ALLCAPS = re.compile(r"^\s*(?P<heading>[A-Z][A-Z0-9 ,/&()'\-]{3,70})\s*$")


def _is_heading(line: str) -> tuple[str, str] | None:
    """Return (ref, heading) when a line starts a new clause unit."""
    m = _SECTION.match(line)
    if m:
        return m.group("ref"), m.group("heading").strip()
    m = _NUMBERED.match(line)
    if m and not line.rstrip().endswith((".", ";", ",")):
        return m.group("ref"), m.group("heading").strip()
    m = _ALLCAPS.match(line)
    if m:
        heading = m.group("heading").strip()
        letters = sum(ch.isalpha() for ch in heading)
        if letters >= 4 and heading == heading.upper():
            return "", heading.title()
    return None


def segment_text(text: str, document_id: str = "") -> list[ClauseUnit]:
    """Cut contract text into clause units in document order."""
    units: list[ClauseUnit] = []
    ref = ""
    heading = ""
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if not body and not heading:
            return
        units.append(
            ClauseUnit(
                clause_id=f"C-{len(units) + 1:02d}",
                ref=ref,
                heading=heading,
                text=body,
                document_id=document_id,
            )
        )

    for line in (text or "").splitlines():
        found = _is_heading(line)
        if found is not None:
            flush()
            ref, heading = found
            buffer = []
        else:
            buffer.append(line)
    flush()
    return units


def segment_contract(contract: ContractInput) -> list[ClauseUnit]:
    """Segment the contract's agreement document (or contract_text)."""
    doc_id = ""
    for doc in contract.documents:
        if doc.doc_type == "agreement" and doc.raw_text.strip():
            doc_id = doc.document_id
            break
    return segment_text(contract.agreement_text(), document_id=doc_id)
