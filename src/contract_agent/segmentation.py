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

Real counterparty paper is formatted every which way, and a missed
heading is not a cosmetic miss: the clause folds into its neighbour,
and the absence check then reports a clause that is present as
missing. So the heading test also accepts the common variants — a
heading ending in a period, carrying ``;`` ``:`` ``&`` or a curly
apostrophe, prefixed ``§``, wrapped in DOCX-export markup, written
inline (``1. Fees. Customer shall pay...``), or standing unnumbered
in Title Case — but only when the text reads as a title (short, no
verb), so a numbered sentence stays body text. And when headings cut
a multi-paragraph agreement into almost nothing, the paragraphs
become the units rather than one giant clause.
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
_ALLCAPS_LOOSE = re.compile(r"^\s*(?P<heading>[A-Z][A-Z0-9 ,/&()'\-;:]{3,70})\s*$")

# The variants: markup stripped first, then a numbered heading whose
# text must read as a title, or an inline heading followed by its body.
_MARKUP = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?(?P<inner>.*?)(?:\*\*|__)?\s*$")
_REF = r"(?:§+\s*)?(?P<ref>\d+(?:\.\d+)*)"
_NUMBERED_LOOSE = re.compile(rf"^\s*{_REF}\.?\s+(?P<heading>.+?)\s*$")
_INLINE = re.compile(rf"^\s*{_REF}\.?\s+(?P<heading>[^.]{{2,70}}?)\.\s+(?P<body>\S.*)$")
_NUMBERED_PARAGRAPH = re.compile(r"^(?P<ref>\d+(?:\.\d+)*)\.?\s+(?P<body>.*)$", re.S)

# Lines that look like headings but never start a clause: recital and
# execution boilerplate, and page markers left in pasted text.
_NOT_A_HEADING = re.compile(
    r"^(?:whereas|recitals?|now,? therefore|in witness whereof|page \d+(?: of \d+)?)\b",
    re.I,
)
_SIGNATURE_LABEL = re.compile(r"^\s*(?:By|Name|Title|Date|Signature|Signed)\s*:", re.I)
_ENTITY_SUFFIX = re.compile(
    r"\b(?:Inc|LLC|LLP|Ltd|Limited|plc|Corp|Corporation|Company|Co|GmbH|AG|AB|AS|SA|BV|NV|Pty)\.?$",
    re.I,
)

_CONNECTORS = {"a", "an", "and", "or", "of", "the", "to", "in", "on", "for", "by", "with", "under", "&"}
_VERBS = {"shall", "will", "may", "must", "is", "are", "be", "agrees", "agree", "means", "has", "have", "not"}

# Fallback trigger: no heading after the title across this many
# paragraphs means the headings were not recognised, not that the
# agreement has one clause.
_FALLBACK_MIN_PARAGRAPHS = 4


def _title_like(text: str, max_words: int = 8) -> bool:
    """A heading reads as a title: short, a capitalised first word, no
    verb, and either Title Case throughout or short sentence case
    ("Breach notification")."""
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
    if not words or len(words) > max_words or len(text) > 70:
        return False
    if not words[0][0].isupper() or any(w.lower() in _VERBS for w in words):
        return False
    if all(w[0].isupper() or w.lower() in _CONNECTORS for w in words):
        return True
    return len(words) <= 4


def _clean(line: str) -> str:
    """Strip DOCX-export markup and normalise curly apostrophes."""
    m = _MARKUP.match(line)
    return (m.group("inner") if m else line).replace("’", "'")


def _is_heading(line: str, loose: bool = True) -> tuple[str, str] | None:
    """Return (ref, heading) when a line starts a new clause unit.

    The shipped heading forms always count. The looser variants (a
    trailing period, ``;``/``:`` in the heading, sentence case) count
    only when ``loose`` — the line opens a paragraph. A heading starts
    a paragraph; a numbered list item inside a clause ("1. Monthly
    invoices.") follows the line that introduces it."""
    line = _clean(line)
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
    if not loose or _NOT_A_HEADING.match(line.strip()):
        return None
    m = _NUMBERED_LOOSE.match(line)
    if m:
        heading = m.group("heading").rstrip(".").strip()
        if not heading.endswith((";", ",")) and _title_like(heading):
            return m.group("ref"), heading
    stripped = line.rstrip().rstrip(".")
    m = _ALLCAPS_LOOSE.match(stripped)
    if m and not stripped.endswith(":"):
        heading = m.group("heading").strip()
        letters = sum(ch.isalpha() for ch in heading)
        if letters >= 4 and heading == heading.upper():
            return "", heading.title()
    return None


def _inline_heading(line: str) -> tuple[str, str, str] | None:
    """Return (ref, heading, body) for ``1. Fees. Customer shall pay...``."""
    m = _INLINE.match(_clean(line))
    # At least four letters: "1. Mr. Smith shall..." is not a heading "Mr".
    if m and sum(ch.isalpha() for ch in m.group("heading")) >= 4 \
            and _title_like(m.group("heading"), max_words=6):
        return m.group("ref"), m.group("heading").strip(), m.group("body")
    return None


def _unnumbered_heading(lines: list[str], i: int) -> tuple[str, str] | None:
    """A Title Case line standing alone at a paragraph's start, with
    text right under it: ``Limitation of Liability``."""
    if i > 0 and lines[i - 1].strip():
        return None
    if i + 1 >= len(lines) or not lines[i + 1].strip():
        return None
    text = _clean(lines[i]).strip()
    if ":" in text or text.endswith((".", ",", ";")) or not _title_like(text, max_words=6):
        return None
    # A signature block reads like a heading — a party's name alone on
    # a line, then "By:" — but it is not a clause.
    if _SIGNATURE_LABEL.match(lines[i + 1]) or _ENTITY_SUFFIX.search(text):
        return None
    if _NOT_A_HEADING.match(text):
        return None
    return "", text


def segment_text(text: str, document_id: str = "") -> list[ClauseUnit]:
    """Cut contract text into clause units in document order."""
    units: list[ClauseUnit] = []
    ref = ""
    heading = ""
    buffer: list[str] = []
    lines = (text or "").splitlines()

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

    for i, line in enumerate(lines):
        opens_paragraph = i == 0 or not lines[i - 1].strip()
        found = _is_heading(line, loose=opens_paragraph)
        inline = None if found or not opens_paragraph else _inline_heading(line)
        if found is None and inline is None:
            found = _unnumbered_heading(lines, i)
        if found is not None:
            flush()
            ref, heading = found
            buffer = []
        elif inline is not None:
            flush()
            ref, heading, first = inline
            buffer = [first]
        else:
            buffer.append(line)
    flush()
    return units


def _paragraph_units(text: str, document_id: str = "") -> list[ClauseUnit]:
    """One unit per paragraph; a leading number becomes the ref."""
    units: list[ClauseUnit] = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip()
        if not para:
            continue
        m = _NUMBERED_PARAGRAPH.match(para)
        ref, body = (m.group("ref").split(".")[0], m.group("body")) if m else ("", para)
        units.append(
            ClauseUnit(
                clause_id=f"C-{len(units) + 1:02d}",
                ref=ref,
                heading="",
                text=body.strip(),
                document_id=document_id,
            )
        )
    return units


def segment_contract(contract: ContractInput) -> list[ClauseUnit]:
    """Segment the contract's agreement document (or contract_text).

    When headings cut a multi-paragraph agreement into almost nothing,
    fall back to paragraphs: one giant clause would read as an
    agreement missing most of its standard clauses."""
    doc_id = ""
    for doc in contract.documents:
        if doc.doc_type == "agreement" and doc.raw_text.strip():
            doc_id = doc.document_id
            break
    text = contract.agreement_text()
    units = segment_text(text, document_id=doc_id)
    paragraphs = [p for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    # The first unit may carry the document's title as its heading;
    # any heading after it means the headings were found, however few.
    headed_after_title = sum(1 for u in units[1:] if u.heading or u.ref)
    if headed_after_title == 0 and len(paragraphs) >= _FALLBACK_MIN_PARAGRAPHS:
        return _paragraph_units(text, document_id=doc_id)
    return units
