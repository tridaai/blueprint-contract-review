"""Exhibit consistency review: does the exhibit contain what the
clause promises?

A data protection clause that says "breach notice within 24 hours,
per the Data Processing Addendum attached as Exhibit B" is making a
claim about another document. Reviewing the clause alone cannot
tell whether the claim is true — the exhibit has to be opened and
checked. That is what this module does, in code, the same way clause
absences are checked: the agreement's promises become expectations,
the exhibit's text either satisfies them or it does not, and every
outcome is a named status on the result.

Checks are generated only when there is something cross-document to
check:

- a DPA exhibit (or schedule) is attached — then the data
  protection clause's commitments are verified against it; or
- the clause explicitly ties itself to an exhibit ("attached as
  Exhibit B") that is not in the package — then each promised item
  is reported ``exhibit_missing``, the exhibit analogue of a missing
  standard clause.

A clause that merely references a DPA ("in accordance with the Data
Processing Addendum between the parties") with no exhibit attached
and none promised generates no checks — the DPA's absence there is
already the clause verdict's business, and this module does not
double-flag it.
"""

from __future__ import annotations

import re

from .schemas import ClauseExtraction, ContractInput, ExhibitCheck

_DPA_DOC = re.compile(
    r"data processing|data protection addendum|\bDPA\b|personal data", re.I
)
_BULLET_LINE = re.compile(r"^\s*[-*•]\s+\S+", re.M)
_NUMBERED_LINE = re.compile(r"^\s*\d+[.)]\s+(.+)$", re.M)
_SUBPROCESSOR = re.compile(r"sub-?processors?", re.I)
_WITHIN_HOURS = re.compile(r"within\s+(\d+)\s*hours", re.I)
_WITHIN_DAYS = re.compile(r"within\s+(\d+)\s*(?:business\s+)?days", re.I)


def _dpa_exhibit_promised(clause_text: str) -> bool:
    """The clause ties its promises to an attached exhibit: a DPA
    reference together with exhibit/annex language."""
    lower = clause_text.lower()
    if "data processing addendum" not in lower and "dpa" not in lower:
        return False
    return "exhibit" in lower or "annex" in lower or "attached hereto" in lower


def _find_dpa_document(contract: ContractInput):
    """The attached exhibit/schedule that purports to be the DPA."""
    for doc in contract.documents:
        if doc.doc_type in {"exhibit", "schedule"} and doc.raw_text.strip():
            if _DPA_DOC.search(doc.raw_text):
                return doc
    return None


def _breach_window_hours(exhibit_text: str) -> int | None:
    """The breach-notification window an exhibit commits to, in hours.

    Only sentences that talk about breach notification count — an
    unrelated "within 30 days" elsewhere in the exhibit is not a
    breach commitment. Days convert at 24 hours, the same convention
    the extractor uses for clause signals."""
    for sentence in re.split(r"(?<=[.!?])\s+", exhibit_text):
        lower = sentence.lower()
        if "breach" not in lower or not any(
            word in lower for word in ("notif", "inform", "alert")
        ):
            continue
        m = _WITHIN_HOURS.search(sentence)
        if m:
            return int(m.group(1))
        m = _WITHIN_DAYS.search(sentence)
        if m:
            return int(m.group(1)) * 24
    return None


def _has_subprocessor_list(exhibit_text: str) -> bool:
    """A list is a list: the exhibit names its sub-processors as
    items, not as a passing mention. Bullets count directly; a
    numbered line counts only when it is a single short item — a
    numbered section paragraph ("3. Breach notification. The
    Provider shall notify...") is prose, not a list entry."""
    if len(_BULLET_LINE.findall(exhibit_text)) >= 2:
        return True
    items = [
        text
        for text in _NUMBERED_LINE.findall(exhibit_text)
        if ". " not in text and len(text) <= 80
    ]
    return len(items) >= 2


def build_exhibit_checks(
    contract: ContractInput,
    units,
    extractions: list[ClauseExtraction],
) -> list[ExhibitCheck]:
    """Cross-document fact checks between the data protection
    clause(s) and the DPA exhibit. Deterministic; returns an empty
    list when the contract makes no exhibit-tied promises."""
    unit_text = {u.clause_id: u.text or "" for u in units}
    expectations: list[tuple[str, str, str]] = []  # (name, expectation, promised)
    for extraction in extractions:
        if extraction.clause_type != "data_protection":
            continue
        text = unit_text.get(extraction.clause_id, "")
        signals = extraction.signals or {}
        hours = signals.get("breach_notice_hours")
        promised = _dpa_exhibit_promised(text)
        exhibit = _find_dpa_document(contract)
        if exhibit is None and not promised:
            continue  # nothing cross-document to check (see module docstring)
        if isinstance(hours, int) and hours > 0:
            expectations.append((
                "breach_notice_window",
                "The data protection clause promises breach notification "
                f"within {hours} hours; the DPA exhibit must commit to "
                "no longer.",
                f"{hours} hours",
            ))
        if _SUBPROCESSOR.search(text):
            expectations.append((
                "subprocessor_list",
                "The data protection clause relies on the DPA exhibit "
                "for the list of sub-processors; the exhibit must "
                "contain that list.",
                "sub-processor list in the exhibit",
            ))

    exhibit = _find_dpa_document(contract)
    checks: list[ExhibitCheck] = []
    seen: set[str] = set()
    for name, expectation, promised in expectations:
        if name in seen:
            continue
        seen.add(name)
        if exhibit is None:
            checks.append(ExhibitCheck(
                name=name,
                expectation=expectation,
                promised=promised,
                status="exhibit_missing",
                detail=(
                    "The clause ties this promise to an attached data "
                    "processing addendum, but no DPA exhibit is in the "
                    "package — there is nothing to check the promise "
                    "against."
                ),
            ))
            continue
        if name == "breach_notice_window":
            promised_hours = int(promised.split()[0])
            found_hours = _breach_window_hours(exhibit.raw_text)
            if found_hours is None:
                status, found, detail = (
                    "missing_in_exhibit", "",
                    "The DPA exhibit states no breach-notification window "
                    "at all — the clause's promise is not carried into it.",
                )
            elif found_hours <= promised_hours:
                status, found, detail = (
                    "consistent", f"{found_hours} hours",
                    f"The exhibit commits to breach notification within "
                    f"{found_hours} hours, inside the {promised_hours} "
                    "hours the clause promises.",
                )
            else:
                status, found, detail = (
                    "mismatch", f"{found_hours} hours",
                    f"The exhibit commits to breach notification within "
                    f"{found_hours} hours, but the clause promises "
                    f"{promised_hours} — the exhibit contradicts the "
                    "clause it is attached to.",
                )
        else:  # subprocessor_list
            if not _SUBPROCESSOR.search(exhibit.raw_text):
                status, found, detail = (
                    "missing_in_exhibit", "",
                    "The DPA exhibit never mentions sub-processors — "
                    "the list the clause relies on is not there.",
                )
            elif _has_subprocessor_list(exhibit.raw_text):
                status, found, detail = (
                    "consistent", "sub-processor list present",
                    "The exhibit names its sub-processors as a list, "
                    "as the clause relies on it doing.",
                )
            else:
                status, found, detail = (
                    "missing_in_exhibit", "sub-processors mentioned, no list",
                    "The exhibit mentions sub-processors but lists "
                    "none — a mention is not the list the clause "
                    "relies on.",
                )
        checks.append(ExhibitCheck(
            name=name,
            expectation=expectation,
            exhibit_document_id=exhibit.document_id or exhibit.doc_type,
            promised=promised,
            found=found,
            status=status,
            detail=detail,
        ))
    return checks
