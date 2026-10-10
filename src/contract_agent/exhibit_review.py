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

Two more families follow the same discipline (round 5):

- **Order forms.** When the payment clause ties the fees to an order
  form / schedule, the order form must state the total it charges
  (``order_form_fees_total``). When the order form restates payment
  terms or a term of its own, those restatements are checked against
  the agreement's (``order_form_payment_terms``, ``order_form_term``)
  — an order form that quietly moves Net 30 to Net 60, or a 12-month
  term to 24, contradicts the agreement it is issued under. A silent
  order form asserts nothing, so silence alone generates no check:
  the agreement's terms govern.
- **Statements of work.** When the agreement ties deliverables to
  statements of work, an attached SOW must actually list its
  deliverables and state delivery dates (``sow_deliverables``,
  ``sow_delivery_dates``), and an acceptance period it states is
  checked against the agreement's acceptance window
  (``sow_acceptance_period``).

A fourth family follows the same discipline (round 8):

- **Certificates of insurance.** When the agreement states the
  insurance the counterparty must carry — coverage types with
  minimum limits, a certificate to be furnished, additional-insured
  or waiver-of-subrogation endorsements — the certificate exhibit
  is checked against every one of those statements
  (``insurance_named_insured``, ``insurance_limits_<coverage>``,
  ``insurance_certificate_expiry``, ``insurance_additional_insured``,
  ``insurance_waiver_of_subrogation``). A certificate naming a
  different legal entity, carrying half the required limit, or
  already expired does not become true by being attached.

A fifth family follows the same discipline (round 10):

- **Sub-processor lists.** A DPA's own annex — the list of
  sub-processors the processor engages — is checked against the
  data-processing terms that govern it: the notice period the
  sub-processor clause promises for changes to the list must be
  carried by the annex that maintains the list
  (``subprocessor_notice_period``); every processor the list names
  must be one the clause authorises (``subprocessor_authorised_list``
  — a processor appearing in the annex but nowhere in the clause's
  authorisation is undeclared, however tidy the list looks); and
  where the transfer clause confines processing to a region, no
  listed processor may sit outside it
  (``subprocessor_transfer_terms``). The list is the operative
  document in a DPA — it is where engagements actually change — so
  it is checked like one.

A sixth family follows the same discipline (round 11):

- **Prior-invention schedules.** An employment agreement's
  invention assignment that excludes "inventions listed on the
  schedule of prior inventions" makes the schedule load-bearing:
  the employee's pre-existing work stays theirs only if the
  schedule exists and names it. When the clause relies on such a
  schedule, the schedule must be in the package and must list
  inventions (``prior_invention_schedule`` — promised but absent
  is ``exhibit_missing``, the exhibit analogue of a missing
  clause). And when a schedule IS attached and lists inventions,
  the assignment clause is read back against it
  (``prior_invention_exclusion``): an assignment that never
  excludes the scheduled inventions would swallow them — the
  schedule becomes a list of things the employee is about to
  assign away.

A seventh family follows the same discipline (round 12):

- **Severance arithmetic.** An offer letter's severance promise
  is usually a formula — so many months of base salary — while
  the compensation schedule attached to the same letter often
  states the severance as a lump sum. The two are the same
  promise in different units, and they do not always agree: the
  schedule is frequently drafted from last year's salary. The
  formula is applied, in code, to the salary basis the package
  states (``severance_arithmetic``): the computed figure must be
  the figure the package states. A formula whose salary basis is
  stated nowhere cannot be computed at all, and a lump sum that
  cannot be checked is reported, not waved through.
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
_ORDER_FORM_DOC = re.compile(r"order form", re.I)
_SOW_DOC = re.compile(r"statement of work|\bSOW\b", re.I)
_MONEY = re.compile(r"(?:USD|US\$|\$|£|€)\s?\d[\d,]*(?:\.\d{2})?")
_NET_DAYS = re.compile(r"net\s*\(?(\d+)\)?", re.I)
_DAYS_COUNT = re.compile(r"\(?(\d+)\)?\s*(?:business\s+)?days", re.I)
_MONTHS_COUNT = re.compile(r"\(?(\d+)\)?\s*(months?|years?)", re.I)
_DATE_STATED = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}\b"
)


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


_BULLET_ITEM = re.compile(r"^\s*[-*•]\s+(.+)$", re.M)


def _list_items(text: str) -> list[str]:
    """The list-shaped items a document carries: bullet lines, plus
    numbered lines that are single short items — a numbered section
    paragraph ("3. Breach notification. The Provider shall
    notify...") is prose, not a list entry."""
    items = [entry.strip() for entry in _BULLET_ITEM.findall(text)]
    items += [
        entry.strip()
        for entry in _NUMBERED_LINE.findall(text)
        if ". " not in entry and len(entry) <= 80
    ]
    return items


def _list_item_count(text: str) -> int:
    """How many list-shaped items a document carries (see
    ``_list_items``)."""
    return len(_list_items(text))


def _has_subprocessor_list(exhibit_text: str) -> bool:
    """A list is a list: the exhibit names its sub-processors as
    items, not as a passing mention."""
    return _list_item_count(exhibit_text) >= 2


def _find_schedule_document(contract: ContractInput, marker: re.Pattern):
    """The attached exhibit/schedule whose text matches ``marker``."""
    for doc in contract.documents:
        if doc.doc_type in {"exhibit", "schedule"} and doc.raw_text.strip():
            if marker.search(doc.raw_text):
                return doc
    return None


def _months_near(text: str, anchor: str) -> int | None:
    """A duration in months stated in a sentence containing
    ``anchor`` (years convert at 12). None when no such sentence
    states one."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if anchor not in sentence.lower():
            continue
        m = _MONTHS_COUNT.search(sentence)
        if m:
            value = int(m.group(1))
            return value * 12 if m.group(2).startswith("year") else value
    return None


def _acceptance_days(text: str) -> int | None:
    """The acceptance window a text states, in days: a sentence
    about accepting (deliverables, work) that carries a day count."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        lower = sentence.lower()
        if "accept" not in lower:
            continue
        m = _DAYS_COUNT.search(sentence)
        if m:
            return int(m.group(1))
    return None


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
    checks.extend(_order_form_checks(contract, units, extractions))
    checks.extend(_sow_checks(contract, units, extractions))
    checks.extend(_insurance_certificate_checks(contract, units, extractions))
    checks.extend(_subprocessor_list_checks(contract, units, extractions))
    checks.extend(_prior_invention_checks(contract, units, extractions))
    checks.extend(_severance_checks(contract, units, extractions))
    return checks


# ---------------------------------------------------------------------------
# Order forms: the schedule must charge what the agreement says it charges
# ---------------------------------------------------------------------------


def _order_form_total(text: str) -> str:
    """The total an order form states: a money amount on a line that
    says "total". "" when the document states amounts but no total,
    or no amounts at all (the caller distinguishes via the text)."""
    for line in text.splitlines():
        if "total" in line.lower():
            m = _MONEY.search(line)
            if m:
                return m.group(0)
    return ""


def _order_form_checks(
    contract: ContractInput,
    units,
    extractions: list[ClauseExtraction],
) -> list[ExhibitCheck]:
    """Order-form checks: generated from the payment clause's tie to
    the order form, and from whatever the order form itself asserts."""
    unit_text = {u.clause_id: u.text or "" for u in units}
    payment = next(
        (e for e in extractions if e.clause_type == "payment_terms"), None
    )
    if payment is None:
        return []
    clause_text = unit_text.get(payment.clause_id, "")
    lower = clause_text.lower()
    fees_tied = "order form" in lower or (
        "schedule" in lower and ("fee" in lower or "charge" in lower)
    )
    doc = _find_schedule_document(contract, _ORDER_FORM_DOC)
    checks: list[ExhibitCheck] = []
    doc_id = (doc.document_id or doc.doc_type) if doc else ""

    if fees_tied:
        expectation = (
            "The payment clause sets the fees by the order form; the "
            "order form must state the total it charges."
        )
        if doc is None:
            checks.append(ExhibitCheck(
                name="order_form_fees_total",
                expectation=expectation,
                promised="fees total in the order form",
                status="exhibit_missing",
                detail=(
                    "The payment clause ties the fees to an order form, "
                    "but no order form is in the package — the amount "
                    "payable cannot be checked against anything."
                ),
            ))
        else:
            total = _order_form_total(doc.raw_text)
            if total:
                checks.append(ExhibitCheck(
                    name="order_form_fees_total",
                    expectation=expectation,
                    exhibit_document_id=doc_id,
                    promised="fees total in the order form",
                    found=total,
                    status="consistent",
                    detail=(
                        f"The order form states a total of {total}, as "
                        "the payment clause relies on it doing."
                    ),
                ))
            else:
                checks.append(ExhibitCheck(
                    name="order_form_fees_total",
                    expectation=expectation,
                    exhibit_document_id=doc_id,
                    promised="fees total in the order form",
                    found="amounts itemised, no total" if _MONEY.search(doc.raw_text) else "",
                    status="missing_in_exhibit",
                    detail=(
                        "The order form itemises amounts but states no "
                        "total — the figure the payment clause relies "
                        "on is not there to check."
                        if _MONEY.search(doc.raw_text)
                        else "The order form states no fees at all — "
                        "the total the payment clause relies on is "
                        "not there."
                    ),
                ))
    if doc is None:
        return checks

    # Restatements: only checked when the order form makes them —
    # silence asserts nothing and the agreement's terms govern.
    net_days = (payment.signals or {}).get("net_days")
    stated_net = _NET_DAYS.search(doc.raw_text)
    if isinstance(net_days, int) and net_days > 0 and stated_net:
        stated = int(stated_net.group(1))
        if stated == net_days:
            status, detail = (
                "consistent",
                f"The order form's Net {stated} matches the payment "
                f"clause's Net {net_days}.",
            )
        else:
            status, detail = (
                "mismatch",
                f"The order form states Net {stated}, but the payment "
                f"clause states Net {net_days} — the order form "
                "contradicts the agreement it is issued under.",
            )
        checks.append(ExhibitCheck(
            name="order_form_payment_terms",
            expectation=(
                "An order form that restates payment terms must restate "
                "the agreement's; the agreement governs where they differ."
            ),
            exhibit_document_id=doc_id,
            promised=f"Net {net_days}",
            found=f"Net {stated}",
            status=status,
            detail=detail,
        ))

    term = next(
        (e for e in extractions if e.clause_type == "term_termination"), None
    )
    if term is not None:
        msa_months = _months_near(unit_text.get(term.clause_id, ""), "initial term")
        form_months = _months_near(doc.raw_text, "term")
        if msa_months and form_months:
            if form_months == msa_months:
                status, detail = (
                    "consistent",
                    f"The order form's {form_months}-month term matches "
                    f"the agreement's initial term of {msa_months} months.",
                )
            else:
                status, detail = (
                    "mismatch",
                    f"The order form states a term of {form_months} "
                    f"months, but the agreement's initial term is "
                    f"{msa_months} months — the order form runs longer "
                    "than the agreement it is issued under."
                    if form_months > msa_months
                    else f"The order form states a term of {form_months} "
                    f"months, but the agreement's initial term is "
                    f"{msa_months} months.",
                )
            checks.append(ExhibitCheck(
                name="order_form_term",
                expectation=(
                    "An order form that states its own term must state "
                    "the agreement's initial term; it cannot extend "
                    "the agreement by itself."
                ),
                exhibit_document_id=doc_id,
                promised=f"{msa_months} months",
                found=f"{form_months} months",
                status=status,
                detail=detail,
            ))
    return checks


# ---------------------------------------------------------------------------
# Statements of work: deliverables and dates the agreement relies on
# ---------------------------------------------------------------------------


def _sow_checks(
    contract: ContractInput,
    units,
    extractions: list[ClauseExtraction],
) -> list[ExhibitCheck]:
    """SOW checks: generated when the agreement ties deliverables to
    statements of work, checked against the attached SOW document."""
    unit_text = {u.clause_id: u.text or "" for u in units}
    tied_text = ""
    for extraction in extractions:
        text = unit_text.get(extraction.clause_id, "")
        lower = text.lower()
        if ("statement of work" in lower or "sow" in lower) and "deliverab" in lower:
            tied_text = text
            break
    doc = _find_schedule_document(contract, _SOW_DOC)
    if not tied_text and doc is None:
        return []
    doc_id = (doc.document_id or doc.doc_type) if doc else ""
    promised = "attached" in tied_text.lower() and (
        "exhibit" in tied_text.lower() or "schedule" in tied_text.lower()
    )
    checks: list[ExhibitCheck] = []

    deliverables_expectation = (
        "The agreement ties the deliverables to each statement of "
        "work; the SOW must list the deliverables it commits to."
    )
    dates_expectation = (
        "The agreement relies on each statement of work to state its "
        "delivery dates; the SOW must carry at least one explicit date."
    )
    if doc is None:
        if not promised:
            return []
        for name, expectation, promised_value in (
            ("sow_deliverables", deliverables_expectation, "deliverables list in the SOW"),
            ("sow_delivery_dates", dates_expectation, "delivery dates in the SOW"),
        ):
            checks.append(ExhibitCheck(
                name=name,
                expectation=expectation,
                promised=promised_value,
                status="exhibit_missing",
                detail=(
                    "The agreement ties this to a statement of work "
                    "attached as an exhibit/schedule, but no SOW is in "
                    "the package — there is nothing to check against."
                ),
            ))
        return checks

    if _list_item_count(doc.raw_text) >= 2 or re.search(
        r"^\s*deliverable\b", doc.raw_text, re.I | re.M
    ):
        checks.append(ExhibitCheck(
            name="sow_deliverables",
            expectation=deliverables_expectation,
            exhibit_document_id=doc_id,
            promised="deliverables list in the SOW",
            found="deliverables listed",
            status="consistent",
            detail="The SOW lists its deliverables as items, as the agreement relies on it doing.",
        ))
    else:
        checks.append(ExhibitCheck(
            name="sow_deliverables",
            expectation=deliverables_expectation,
            exhibit_document_id=doc_id,
            promised="deliverables list in the SOW",
            found="deliverables mentioned, no list" if "deliverab" in doc.raw_text.lower() else "",
            status="missing_in_exhibit",
            detail=(
                "The SOW mentions deliverables but lists none — a "
                "mention is not the list the agreement relies on."
                if "deliverab" in doc.raw_text.lower()
                else "The SOW names no deliverables at all — the list "
                "the agreement relies on is not there."
            ),
        ))

    date_match = _DATE_STATED.search(doc.raw_text)
    if date_match:
        checks.append(ExhibitCheck(
            name="sow_delivery_dates",
            expectation=dates_expectation,
            exhibit_document_id=doc_id,
            promised="delivery dates in the SOW",
            found=date_match.group(0),
            status="consistent",
            detail=(
                f"The SOW states explicit delivery dates (first: "
                f"{date_match.group(0)}), as the agreement relies on."
            ),
        ))
    else:
        checks.append(ExhibitCheck(
            name="sow_delivery_dates",
            expectation=dates_expectation,
            exhibit_document_id=doc_id,
            promised="delivery dates in the SOW",
            found="",
            status="missing_in_exhibit",
            detail=(
                "The SOW states no delivery date anywhere — the "
                "schedule the agreement relies on is not there."
            ),
        ))

    msa_days = _acceptance_days(contract.agreement_text())
    sow_days = _acceptance_days(doc.raw_text)
    if msa_days and sow_days:
        if sow_days == msa_days:
            status, detail = (
                "consistent",
                f"The SOW's {sow_days}-day acceptance period matches "
                f"the agreement's {msa_days} days.",
            )
        else:
            status, detail = (
                "mismatch",
                f"The SOW states an acceptance period of {sow_days} "
                f"days, but the agreement allows {msa_days} — the SOW "
                "contradicts the agreement it is issued under.",
            )
        checks.append(ExhibitCheck(
            name="sow_acceptance_period",
            expectation=(
                "An SOW that states its own acceptance period must "
                "state the agreement's; the agreement governs."
            ),
            exhibit_document_id=doc_id,
            promised=f"{msa_days} days",
            found=f"{sow_days} days",
            status=status,
            detail=detail,
        ))
    return checks


# ---------------------------------------------------------------------------
# Certificates of insurance: the exhibit must prove the clause's insurance
# ---------------------------------------------------------------------------

_CERT_DOC = re.compile(r"certificate of insurance", re.I)

# Coverage families, by the phrases contracts and certificates use
# for them. The key names the check (insurance_limits_<key>).
_COVERAGE_KEYS: list[tuple[str, str, tuple[str, ...]]] = [
    ("general_liability", "commercial general liability",
     ("commercial general liability", "general liability")),
    ("professional_liability", "professional liability",
     ("professional liability", "errors and omissions", "errors & omissions")),
    ("workers_compensation", "workers' compensation",
     ("workers' compensation", "workers compensation", "worker's compensation")),
    ("automobile_liability", "automobile liability",
     ("commercial automobile", "automobile liability", "auto liability")),
    ("cyber_liability", "cyber liability",
     ("cyber liability", "network security liability", "cyber insurance")),
]

_MONEY_VALUE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|m)?\b", re.I)


def _money_value(text: str) -> float | None:
    m = _MONEY_VALUE.search(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if m.group(2):
        value *= 1_000_000
    return value


def _fmt_money(value: float) -> str:
    return f"${value:,.0f}" if value == int(value) else f"${value:,.2f}"


def _text_sentences(text: str) -> list[str]:
    """Sentences and lines: certificates are line-oriented, so a
    line break ends a statement the way a full stop does."""
    return [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+|\n", text or "")
        if s.strip()
    ]


def _coverage_amounts(text: str) -> dict[str, float]:
    """Coverage type → amount stated, read per coverage phrase: a
    sentence may carry two coverages with two amounts ("general
    liability … $2,000,000 … and professional liability … $1,000,000"),
    so each phrase's amount is the first money value between it and
    the next coverage phrase in the same sentence."""
    amounts: dict[str, float] = {}
    for sentence in _text_sentences(text):
        lower = sentence.lower()
        if "insurance" not in lower and "liability" not in lower:
            continue
        hits: list[tuple[int, str]] = []
        for key, _label, phrases in _COVERAGE_KEYS:
            for phrase in phrases:
                idx = lower.find(phrase)
                if idx >= 0:
                    hits.append((idx, key))
                    break
        hits.sort()
        for pos, (idx, key) in enumerate(hits):
            end = hits[pos + 1][0] if pos + 1 < len(hits) else len(sentence)
            amount = _money_value(sentence[idx:end])
            if amount is not None:
                amounts[key] = max(amount, amounts.get(key, 0.0))
    return amounts


def _named_insured(cert_text: str) -> str:
    for line in cert_text.splitlines():
        lower = line.lower()
        if "named insured" in lower or lower.strip().startswith("insured"):
            if ":" in line:
                return line.split(":", 1)[1].strip()
            return line.strip()
    return ""


def _normalise_name(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split())


_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def _parse_stated_date(raw: str):
    """Parse the three date shapes _DATE_STATED matches."""
    from datetime import date

    raw = raw.strip().strip(",")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        pass
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", raw)
    if m and m.group(1).lower() in _MONTH_NAMES:
        return date(int(m.group(3)), _MONTH_NAMES[m.group(1).lower()], int(m.group(2)))
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", raw)
    if m and m.group(2).lower() in _MONTH_NAMES:
        return date(int(m.group(3)), _MONTH_NAMES[m.group(2).lower()], int(m.group(1)))
    return None


def _certificate_expiry(cert_text: str):
    """The certificate's expiry: a date in a sentence about expiry,
    else the end of a stated policy period."""
    for sentence in _text_sentences(cert_text):
        if "expir" in sentence.lower():
            m = _DATE_STATED.search(sentence)
            if m:
                return _parse_stated_date(m.group(0))
    for sentence in _text_sentences(cert_text):
        if "policy period" in sentence.lower():
            dates = _DATE_STATED.findall(sentence)
            if dates:
                return _parse_stated_date(dates[-1])
    return None


def _add_months(d, months: int):
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    from datetime import date

    return date(year, month, day)


def _endorsement_present(cert_lower: str, phrase: str) -> bool | None:
    """True: the certificate carries the endorsement. False: it
    mentions the endorsement only to withhold it. None: silent."""
    if phrase not in cert_lower:
        return None
    for sentence in _text_sentences(cert_lower):
        if phrase in sentence and any(
            neg in sentence
            for neg in ("no ", "not ", "none", "excluded", "absent")
        ):
            return False
    return True


def _insurance_certificate_checks(
    contract: ContractInput,
    units,
    extractions: list[ClauseExtraction],
) -> list[ExhibitCheck]:
    """Insurance-certificate checks: generated from what the
    agreement states the counterparty must carry and furnish,
    checked against the attached certificate of insurance."""
    agreement_text = contract.agreement_text()
    lower = agreement_text.lower()
    required_limits = _coverage_amounts(agreement_text)
    certificate_promised = "certificate of insurance" in lower
    additional_required = "additional insured" in lower
    waiver_required = "waiver of subrogation" in lower
    if not required_limits and not certificate_promised:
        return []  # the agreement relies on no insurance exhibit

    cert = _find_schedule_document(contract, _CERT_DOC)
    doc_id = (cert.document_id or cert.doc_type) if cert else ""
    counterparty = contract.counterparty_name or ""
    labels = {key: label for key, label, _ in _COVERAGE_KEYS}

    # The expectations, in the order counsel reads a certificate:
    # who is insured, for how much, until when, with what endorsements.
    expectations: list[tuple[str, str, str]] = []
    if counterparty:
        expectations.append((
            "insurance_named_insured",
            "The certificate must name the counterparty to the "
            "agreement as the insured; insurance held by a different "
            "legal entity is not the counterparty's insurance.",
            counterparty,
        ))
    for key in sorted(required_limits):
        expectations.append((
            f"insurance_limits_{key}",
            f"The agreement requires {labels[key]} insurance of at "
            f"least {_fmt_money(required_limits[key])}; the "
            "certificate must state cover at or above that limit.",
            f"{labels[key]} ≥ {_fmt_money(required_limits[key])}",
        ))
    expectations.append((
        "insurance_certificate_expiry",
        "The certificate must be in force — unexpired at review, "
        "and, where the agreement states an initial term, covering "
        "that term.",
        "in force for the term",
    ))
    if additional_required:
        expectations.append((
            "insurance_additional_insured",
            "The agreement requires the customer to be named as an "
            "additional insured; the certificate must show the "
            "endorsement.",
            "additional insured endorsed",
        ))
    if waiver_required:
        expectations.append((
            "insurance_waiver_of_subrogation",
            "The agreement requires a waiver of subrogation; the "
            "certificate must show it.",
            "waiver of subrogation endorsed",
        ))

    if cert is None:
        return [
            ExhibitCheck(
                name=name,
                expectation=expectation,
                promised=promised,
                status="exhibit_missing",
                detail=(
                    "The agreement states this insurance requirement, "
                    "but no certificate of insurance is in the package "
                    "— the cover the clause relies on is unevidenced."
                ),
            )
            for name, expectation, promised in expectations
        ]

    cert_text = cert.raw_text
    cert_lower = cert_text.lower()
    cert_limits = _coverage_amounts(cert_text)
    checks: list[ExhibitCheck] = []
    for name, expectation, promised in expectations:
        if name == "insurance_named_insured":
            found_name = _named_insured(cert_text)
            if not found_name:
                status, found, detail = (
                    "missing_in_exhibit", "",
                    "The certificate names no insured at all — whose "
                    "cover it evidences cannot be told from it.",
                )
            elif _normalise_name(found_name) == _normalise_name(counterparty):
                status, found, detail = (
                    "consistent", found_name,
                    f"The certificate names {found_name}, the "
                    "counterparty to the agreement.",
                )
            else:
                status, found, detail = (
                    "mismatch", found_name,
                    f"The certificate names {found_name}, but the "
                    f"counterparty to the agreement is {counterparty} "
                    "— a different legal entity's insurance does not "
                    "evidence this counterparty's cover.",
                )
        elif name.startswith("insurance_limits_"):
            key = name.removeprefix("insurance_limits_")
            required = required_limits[key]
            stated = cert_limits.get(key)
            if stated is None:
                status, found, detail = (
                    "missing_in_exhibit", "",
                    f"The certificate states no {labels[key]} cover "
                    "at all — the cover the agreement requires is "
                    "not evidenced.",
                )
            elif stated >= required:
                status, found, detail = (
                    "consistent", _fmt_money(stated),
                    f"The certificate states {labels[key]} cover of "
                    f"{_fmt_money(stated)}, at or above the "
                    f"{_fmt_money(required)} the agreement requires.",
                )
            else:
                status, found, detail = (
                    "mismatch", _fmt_money(stated),
                    f"The certificate states {labels[key]} cover of "
                    f"{_fmt_money(stated)}, below the "
                    f"{_fmt_money(required)} the agreement requires.",
                )
        elif name == "insurance_certificate_expiry":
            from .facts import _parse_date as _parse_contract_date

            expiry = _certificate_expiry(cert_text)
            received = _parse_contract_date(contract.received_at)
            unit_text = {u.clause_id: u.text or "" for u in units}
            term = next(
                (e for e in extractions if e.clause_type == "term_termination"),
                None,
            )
            term_months = (
                _months_near(unit_text.get(term.clause_id, ""), "initial term")
                if term is not None
                else None
            )
            if expiry is None:
                status, found, detail = (
                    "missing_in_exhibit", "",
                    "The certificate states no expiry or policy "
                    "period — whether the cover is in force cannot "
                    "be told from it.",
                )
            elif received is not None and expiry < received:
                status, found, detail = (
                    "mismatch", expiry.isoformat(),
                    f"The certificate expired on {expiry.isoformat()}, "
                    f"before the agreement was received "
                    f"({received.isoformat()}) — the cover it "
                    "evidences is no longer in force.",
                )
            elif (
                received is not None
                and term_months
                and expiry < _add_months(received, term_months)
            ):
                status, found, detail = (
                    "mismatch", expiry.isoformat(),
                    f"The certificate expires on {expiry.isoformat()}, "
                    f"inside the agreement's initial term of "
                    f"{term_months} months from receipt — the cover "
                    "runs out before the term it supports.",
                )
            else:
                status, found, detail = (
                    "consistent", expiry.isoformat(),
                    f"The certificate is in force until "
                    f"{expiry.isoformat()}.",
                )
        else:  # endorsements
            phrase = (
                "additional insured"
                if name == "insurance_additional_insured"
                else "waiver of subrogation"
            )
            present = _endorsement_present(cert_lower, phrase)
            if present is True:
                status, found, detail = (
                    "consistent", f"{phrase} endorsed",
                    f"The certificate shows the {phrase} endorsement "
                    "the agreement requires.",
                )
            elif present is False:
                status, found, detail = (
                    "missing_in_exhibit", f"no {phrase} endorsed",
                    f"The certificate mentions {phrase} only to "
                    "withhold it — the endorsement the agreement "
                    "requires is not there.",
                )
            else:
                status, found, detail = (
                    "missing_in_exhibit", "",
                    f"The certificate says nothing about {phrase} — "
                    "the endorsement the agreement requires is not "
                    "evidenced.",
                )
        checks.append(ExhibitCheck(
            name=name,
            expectation=expectation,
            exhibit_document_id=doc_id,
            promised=promised,
            found=found,
            status=status,
            detail=detail,
        ))
    return checks


# ---------------------------------------------------------------------------
# Sub-processor lists: the DPA's own annex against the DPA's own terms
# ---------------------------------------------------------------------------

# The locations a transfer clause confining processing to the EEA/EU
# permits, by the names a list states: the EU member states plus
# the EEA EFTA states. The United Kingdom is deliberately absent —
# it is neither, whatever a list's drafter assumes.
_EEA_LOCATIONS = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus",
    "czech republic", "czechia", "denmark", "estonia", "finland",
    "france", "germany", "greece", "hungary", "iceland", "ireland",
    "italy", "latvia", "liechtenstein", "lithuania", "luxembourg",
    "malta", "netherlands", "norway", "poland", "portugal",
    "romania", "slovakia", "slovenia", "spain", "sweden",
}

_DAYS_IN_TEXT = re.compile(r"\(?(\d+)\)?\s*(?:business\s+)?days", re.I)


def _find_subprocessor_list_document(contract: ContractInput):
    """The attached exhibit/schedule that IS a sub-processor list:
    it talks about sub-processors and names them as list items."""
    for doc in contract.documents:
        if doc.doc_type in {"exhibit", "schedule"} and doc.raw_text.strip():
            if _SUBPROCESSOR.search(doc.raw_text) and _has_subprocessor_list(
                doc.raw_text
            ):
                return doc
    return None


def _item_name(item: str) -> str:
    """A list item's processor name: the text before its location
    parenthesis or its function gloss."""
    return re.split(r"\s+\(|\s+—\s+|\s+-\s+|,", item)[0].strip()


def _item_location(item: str) -> str:
    """A list item's stated location: the parenthesised text, when
    the item carries one ("Hostline GmbH (Germany) — hosting")."""
    m = re.search(r"\(([^)]+)\)", item)
    return m.group(1).strip() if m else ""


def _notice_days_in(text: str) -> int | None:
    """The notice period a text states, in days: a day count in a
    sentence that talks about giving notice. A day count elsewhere
    (a breach window, an objection deadline) is not a notice
    period."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        lower = sentence.lower()
        if "notice" not in lower and "notify" not in lower:
            continue
        m = _DAYS_IN_TEXT.search(sentence)
        if m:
            return int(m.group(1))
    return None


def _promised_notice(clause_text: str) -> tuple[bool, int | None]:
    """Whether the governing clause promises notice of sub-processor
    changes, and the period it promises when it states one. Only
    sentences that tie notice to sub-processor changes count — a
    breach-notification sentence is a different promise."""
    promised = False
    days: int | None = None
    for sentence in re.split(r"(?<=[.!?])\s+", clause_text):
        lower = sentence.lower()
        if "sub-processor" not in lower and "subprocessor" not in lower:
            continue
        if "notice" not in lower and "notify" not in lower:
            continue
        promised = True
        m = _DAYS_IN_TEXT.search(sentence)
        if m and days is None:
            days = int(m.group(1))
    return promised, days


def _authorised_subprocessors(clause_text: str) -> list[str]:
    """The sub-processors the governing clause itself authorises by
    name ("The sub-processors authorised by the Controller are
    Hostline GmbH and Postmark Systems Ltd"). Empty when the clause
    authorises a mechanism (notice, consent) rather than a set —
    then there is no set for the list to be checked against."""
    for sentence in re.split(r"(?<=[.!?])\s+", clause_text):
        lower = sentence.lower()
        if "sub-processor" not in lower and "subprocessor" not in lower:
            continue
        if not any(
            word in lower for word in ("authoris", "authoriz", "approved")
        ):
            continue
        m = re.search(r"(?:\bare\b|:)\s*(.+)$", sentence.strip())
        if not m:
            continue
        names = [
            part.strip(" .")
            for part in re.split(r",|\band\b", m.group(1))
        ]
        names = [name for name in names if name and name[0].isupper()]
        if names:
            return names
    return []


def _transfer_region(transfer_text: str) -> str:
    """The region the transfer clause confines processing to, in the
    clause's own words — "" when the clause states no geographic
    confinement (SCC/adequacy machinery is a mechanism, not a
    boundary a list can be measured against)."""
    lower = transfer_text.lower()
    prohibits = any(
        phrase in lower
        for phrase in ("shall not transfer", "must not transfer",
                       "may not transfer", "no transfer")
    )
    confines = any(
        phrase in lower
        for phrase in ("only within", "solely within", "takes place within",
                       "remain within", "stays within")
    )
    if not (prohibits or confines):
        return ""
    if "european economic area" in lower or "eea" in lower:
        return "the European Economic Area"
    if "european union" in lower or re.search(r"\beu\b", lower):
        return "the European Union"
    return ""


def _subprocessor_list_checks(
    contract: ContractInput,
    units,
    extractions: list[ClauseExtraction],
) -> list[ExhibitCheck]:
    """Sub-processor list checks: generated when a sub-processor
    list annex is attached and the agreement's own data-processing
    terms govern it — the sub-processor clause (a standalone DPA's
    own matrix row) or, in an MSA, the data protection clause's
    sub-processor sentences."""
    doc = _find_subprocessor_list_document(contract)
    if doc is None:
        return []
    unit_text = {u.clause_id: u.text or "" for u in units}
    sub = next(
        (e for e in extractions if e.clause_type == "subprocessor_terms"), None
    )
    clause_text = unit_text.get(sub.clause_id, "") if sub is not None else ""
    if not clause_text:
        dp = next(
            (e for e in extractions if e.clause_type == "data_protection"), None
        )
        if dp is not None:
            sentences = [
                s
                for s in re.split(
                    r"(?<=[.!?])\s+", unit_text.get(dp.clause_id, "")
                )
                if "sub-processor" in s.lower() or "subprocessor" in s.lower()
            ]
            clause_text = " ".join(sentences)
    if not clause_text:
        return []
    transfer = next(
        (e for e in extractions if e.clause_type == "data_transfer"), None
    )
    transfer_text = (
        unit_text.get(transfer.clause_id, "") if transfer is not None else ""
    )
    doc_id = doc.document_id or doc.doc_type
    items = _list_items(doc.raw_text)
    names = [_item_name(item) for item in items]
    checks: list[ExhibitCheck] = []

    notice_promised, promised_days = _promised_notice(clause_text)
    if notice_promised:
        found_days = _notice_days_in(doc.raw_text)
        promised = (
            f"{promised_days} days' notice of sub-processor changes"
            if promised_days
            else "notice of sub-processor changes"
        )
        expectation = (
            "The sub-processor clause promises notice of any change "
            "to the sub-processors engaged"
            + (
                f" at least {promised_days} days ahead"
                if promised_days
                else ""
            )
            + "; the annex that maintains the list must carry that "
            "notice period — the list is where changes take effect."
        )
        if found_days is None:
            status, found, detail = (
                "missing_in_exhibit", "",
                "The list annex states no notice period for changes "
                "to the list — "
                + (
                    f"the clause's promise of {promised_days} days' "
                    "notice has no machinery in the document that "
                    "maintains the list."
                    if promised_days
                    else "the notice the clause promises has no "
                    "period anywhere in the document that maintains "
                    "the list, so no change date can be checked "
                    "against it."
                ),
            )
        elif promised_days and found_days < promised_days:
            status, found, detail = (
                "mismatch", f"{found_days} days",
                f"The list annex gives {found_days} days' notice of "
                f"changes, but the clause promises {promised_days} — "
                "the annex shortens the notice the clause commits to.",
            )
        else:
            status, found, detail = (
                "consistent", f"{found_days} days",
                f"The list annex carries the notice period for "
                f"changes ({found_days} days), as the clause's "
                "notice promise requires.",
            )
        checks.append(ExhibitCheck(
            name="subprocessor_notice_period",
            expectation=expectation,
            exhibit_document_id=doc_id,
            promised=promised,
            found=found,
            status=status,
            detail=detail,
        ))

    authorised = _authorised_subprocessors(clause_text)
    if authorised:
        authorised_keys = {_normalise_name(name) for name in authorised}
        undeclared = [
            name for name in names if _normalise_name(name) not in authorised_keys
        ]
        expectation = (
            "The sub-processor clause authorises the sub-processors "
            "it names; every processor the list annex engages must "
            "be one of them — a processor the clause never authorised "
            "is undeclared, wherever the list puts it."
        )
        if undeclared:
            status, found, detail = (
                "mismatch", ", ".join(undeclared),
                f"The list engages {', '.join(undeclared)}, which "
                f"the clause's authorisation ({', '.join(authorised)}) "
                "does not name — undeclared sub-processor(s) in the "
                "annex the clause governs.",
            )
        else:
            status, found, detail = (
                "consistent", "every listed sub-processor authorised",
                "Every processor the list engages is named in the "
                "clause's authorisation.",
            )
        checks.append(ExhibitCheck(
            name="subprocessor_authorised_list",
            expectation=expectation,
            exhibit_document_id=doc_id,
            promised=", ".join(authorised),
            found=found,
            status=status,
            detail=detail,
        ))

    region = _transfer_region(transfer_text)
    if region:
        located = [(name, _item_location(item)) for name, item in zip(names, items)]
        outside = [
            (name, location)
            for name, location in located
            if location and _normalise_name(location) not in _EEA_LOCATIONS
        ]
        expectation = (
            f"The transfer clause confines processing to {region}; "
            "no sub-processor the list engages may sit outside it — "
            "engaging one is a transfer the clause does not permit."
        )
        if outside:
            listing = ", ".join(
                f"{name} ({location})" for name, location in outside
            )
            status, found, detail = (
                "mismatch", listing,
                f"The list engages {listing} — outside {region}, "
                "which the transfer clause confines processing to.",
            )
        elif all(location for _, location in located):
            status, found, detail = (
                "consistent", f"every listed sub-processor in {region}",
                f"Every listed sub-processor states a location in "
                f"{region}, as the transfer clause confines it.",
            )
        else:
            status, found, detail = (
                "missing_in_exhibit", "locations not stated for every listed sub-processor",
                "The list does not state a location for every "
                f"sub-processor it engages — the transfer clause's "
                f"confinement to {region} cannot be checked against "
                "a list that does not say where processing happens.",
            )
        checks.append(ExhibitCheck(
            name="subprocessor_transfer_terms",
            expectation=expectation,
            exhibit_document_id=doc_id,
            promised=f"processing confined to {region}",
            found=found,
            status=status,
            detail=detail,
        ))
    return checks


# ---------------------------------------------------------------------------
# Prior-invention schedules: the exclusion is only as good as the schedule
# ---------------------------------------------------------------------------

_PRIOR_INVENTION_DOC = re.compile(r"prior inventions?", re.I)

_EXCLUSION_WORDS = (
    "exclud",
    "not assign",
    "not be assigned",
    "shall not apply",
    "does not apply",
    "no assignment is made",
    "retained by",
    "remains the property",
    "carved out",
    "carve-out",
)


def _assignment_relies_on_schedule(clause_text: str) -> bool:
    """The assignment clause ties its prior-invention exclusion to
    a schedule: the exclusion reaches only what the schedule lists,
    so the schedule's contents decide what the employee keeps."""
    lower = clause_text.lower()
    return "prior invention" in lower and "schedule" in lower


def _assignment_excludes_scheduled(clause_text: str) -> bool:
    """The assignment clause actually excludes prior/scheduled
    inventions: a sentence tying prior inventions (or the schedule)
    to exclusion language. An assignment that speaks of prior
    inventions only to include them does not qualify."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n", clause_text):
        lower = sentence.lower()
        if "prior" not in lower and "schedule" not in lower:
            continue
        if any(word in lower for word in _EXCLUSION_WORDS):
            return True
    return False


def _prior_invention_checks(
    contract: ContractInput,
    units,
    extractions: list[ClauseExtraction],
) -> list[ExhibitCheck]:
    """Prior-invention schedule checks: generated when an
    ``ip_assignment`` clause relies on a schedule of prior
    inventions, or when such a schedule is attached — the schedule
    is checked for existing and listing, and the clause is checked
    for actually excluding what the schedule lists."""
    unit_text = {u.clause_id: u.text or "" for u in units}
    assignment = next(
        (e for e in extractions if e.clause_type == "ip_assignment"), None
    )
    if assignment is None:
        return []
    clause_text = unit_text.get(assignment.clause_id, "")
    doc = _find_schedule_document(contract, _PRIOR_INVENTION_DOC)
    relies = _assignment_relies_on_schedule(clause_text)
    if not relies and doc is None:
        return []
    doc_id = (doc.document_id or doc.doc_type) if doc else ""
    items = _list_items(doc.raw_text) if doc is not None else []
    names = [_item_name(item) for item in items]
    checks: list[ExhibitCheck] = []

    schedule_expectation = (
        "The invention assignment excludes the inventions listed "
        "on the schedule of prior inventions; the schedule must be "
        "in the package and must list the inventions it protects — "
        "an exclusion that points at a schedule nobody attached "
        "protects nothing."
    )
    if doc is None:
        checks.append(ExhibitCheck(
            name="prior_invention_schedule",
            expectation=schedule_expectation,
            promised="schedule of prior inventions",
            status="exhibit_missing",
            detail=(
                "The assignment clause excludes inventions listed "
                "on the schedule of prior inventions, but no such "
                "schedule is in the package — which inventions the "
                "exclusion covers cannot be checked, and an "
                "unattached schedule is easily lost before it is "
                "ever argued over."
            ),
        ))
        return checks
    if names:
        checks.append(ExhibitCheck(
            name="prior_invention_schedule",
            expectation=schedule_expectation,
            exhibit_document_id=doc_id,
            promised="schedule of prior inventions",
            found=f"{len(names)} prior invention(s) listed",
            status="consistent",
            detail=(
                f"The schedule of prior inventions is attached and "
                f"lists {len(names)} invention(s): "
                f"{', '.join(names)}."
            ),
        ))
    else:
        checks.append(ExhibitCheck(
            name="prior_invention_schedule",
            expectation=schedule_expectation,
            exhibit_document_id=doc_id,
            promised="schedule of prior inventions",
            found="schedule attached, no inventions listed",
            status="missing_in_exhibit",
            detail=(
                "A schedule of prior inventions is attached but "
                "lists no inventions — the exclusion the clause "
                "grants reaches an empty list."
            ),
        ))
        return checks

    exclusion_expectation = (
        "A schedule of prior inventions only protects what it "
        "lists if the assignment clause excludes the scheduled "
        "inventions; an assignment that never excludes them takes "
        "them with everything else."
    )
    if _assignment_excludes_scheduled(clause_text):
        checks.append(ExhibitCheck(
            name="prior_invention_exclusion",
            expectation=exclusion_expectation,
            exhibit_document_id=doc_id,
            promised="scheduled inventions excluded from the assignment",
            found="assignment excludes prior/scheduled inventions",
            status="consistent",
            detail=(
                "The assignment clause excludes the inventions the "
                "schedule lists — the schedule's contents stay the "
                "employee's, as the schedule exists to provide."
            ),
        ))
    else:
        checks.append(ExhibitCheck(
            name="prior_invention_exclusion",
            expectation=exclusion_expectation,
            exhibit_document_id=doc_id,
            promised="scheduled inventions excluded from the assignment",
            found=", ".join(names),
            status="mismatch",
            detail=(
                f"The schedule lists {', '.join(names)}, but the "
                "assignment clause never excludes the scheduled "
                "inventions — as drafted, the assignment would "
                "swallow the very inventions the schedule records "
                "as the employee's own."
            ),
        ))
    return checks


# ---------------------------------------------------------------------------
# Severance arithmetic: the formula, applied to the package's own numbers
# ---------------------------------------------------------------------------

_MONEY_AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{2})?)")
_SALARY_ANNUAL = re.compile(
    r"salary[^.\n]{0,60}?\$\s?(\d[\d,]*(?:\.\d{2})?)\s*"
    r"(?:per\s+(?:annum|year)|a\s+year|annually)",
    re.I,
)
_SALARY_BARE = re.compile(
    r"base\s+salary\s+of\s+\$\s?(\d[\d,]*(?:\.\d{2})?)", re.I
)
_SALARY_MONTHLY = re.compile(
    r"salary[^.\n]{0,60}?\$\s?(\d[\d,]*(?:\.\d{2})?)\s*per\s+month", re.I
)
_SEVERANCE_MONTHS_TEXT = re.compile(
    r"severance[^.\n]{0,80}?\(?(\d+)\)?\s*months?\s*of\s*(?:base\s+)?salary",
    re.I,
)
_SEVERANCE_WEEKS_TEXT = re.compile(
    r"severance[^.\n]{0,80}?\(?(\d+)\)?\s*weeks?\s*of\s*(?:base\s+)?salary",
    re.I,
)

# A stated lump sum and a computed product agree when they are
# within a dollar: the schedule's figures are round numbers, the
# product of months and a divided salary need not be.
_ARITHMETIC_TOLERANCE = 1.0


def _salary_basis_monthly(texts: list[str]) -> tuple[float | None, str]:
    """The package's salary basis as a monthly figure, and how the
    package states it. Offer letters state base salary annually
    (a bare "base salary of $X" in an offer letter is the annual
    figure — the convention the letter itself trades on); a
    per-month statement is taken as written."""
    for text in texts:
        m = _SALARY_MONTHLY.search(text)
        if m:
            value = float(m.group(1).replace(",", ""))
            return value, f"{_fmt_money(value)} per month"
    for text in texts:
        m = _SALARY_ANNUAL.search(text)
        if m:
            value = float(m.group(1).replace(",", ""))
            return value / 12, f"{_fmt_money(value)} per annum"
    for text in texts:
        m = _SALARY_BARE.search(text)
        if m:
            value = float(m.group(1).replace(",", ""))
            return value / 12, f"base salary of {_fmt_money(value)} (annual)"
    return None, ""


def _severance_formula_months(
    texts: list[str], extractions: list[ClauseExtraction]
) -> tuple[float | None, str]:
    """The severance formula as a number of months of salary, and
    the words the package states it in. The employment-terms
    extraction's signal is the primary read (it is the extractor's
    own parse of the clause); the text patterns cover a formula
    stated where the clause segmentation did not reach, and the
    weeks form."""
    for extraction in extractions:
        if extraction.clause_type != "employment_terms":
            continue
        months = (extraction.signals or {}).get("severance_months")
        if isinstance(months, int) and months > 0:
            return float(months), f"{months} months of base salary"
    for text in texts:
        m = _SEVERANCE_MONTHS_TEXT.search(text)
        if m:
            months = int(m.group(1))
            return float(months), f"{months} months of base salary"
    for text in texts:
        m = _SEVERANCE_WEEKS_TEXT.search(text)
        if m:
            weeks = int(m.group(1))
            return weeks * 12 / 52, f"{weeks} weeks of base salary"
    return None, ""


def _stated_severance_amount(texts: list[str]) -> tuple[float | None, str]:
    """A severance lump sum the package states, and the id of the
    document stating it is the caller's concern — here the amount
    and the sentence. A money figure inside a salary-basis
    statement is the basis, not a severance amount, and is skipped:
    the two live in different sentences in every package shape this
    family reads, and a sentence that states a salary is not
    stating a severance."""
    for text in texts:
        for sentence in _text_sentences(text):
            if "severance" not in sentence.lower():
                continue
            if _SALARY_ANNUAL.search(sentence) or _SALARY_MONTHLY.search(
                sentence
            ):
                continue
            m = _MONEY_AMOUNT.search(sentence)
            if m:
                return float(m.group(1).replace(",", "")), sentence.strip()
    return None, ""


def _severance_checks(
    contract: ContractInput,
    units,
    extractions: list[ClauseExtraction],
) -> list[ExhibitCheck]:
    """Severance arithmetic: generated for an employment package
    whose severance promise is a formula (months or weeks of base
    salary). The formula is computed against the salary basis the
    package states and compared with any severance lump sum the
    package states — in the letter itself or in an attached
    compensation schedule. A formula alone, with neither a basis
    nor a figure anywhere in the package, produces nothing
    checkable and generates no check. The package gate is the
    document type or an employment-terms clause: a letter that
    mixes its notice and severance sentences in one paragraph can
    segment either way, and the arithmetic does not care which
    label the paragraph wore."""
    if contract.doc_type != "employment" and not any(
        e.clause_type == "employment_terms" for e in extractions
    ):
        return []
    texts = [contract.agreement_text()] + [
        doc.raw_text
        for doc in contract.documents
        if doc.doc_type in {"schedule", "exhibit"}
    ]
    months, formula = _severance_formula_months(texts, extractions)
    if months is None:
        return []
    basis_monthly, basis_desc = _salary_basis_monthly(texts)
    stated, stated_sentence = _stated_severance_amount(texts)
    if basis_monthly is None and stated is None:
        return []  # a formula with no numbers anywhere is not arithmetic yet

    expectation = (
        "The severance promise is a formula — a period of base "
        "salary. Applied to the salary basis the package states, "
        "the formula must produce the severance the package "
        "states; a schedule drafted from a different salary "
        "contradicts the letter it is attached to."
    )
    promised = f"severance of {formula}"
    if basis_monthly is None:
        return [ExhibitCheck(
            name="severance_arithmetic",
            expectation=expectation,
            promised=promised,
            found=f"severance stated as {_fmt_money(stated)}",
            status="missing_in_exhibit",
            detail=(
                f"The letter promises severance of {formula} and "
                f"the package states a severance figure of "
                f"{_fmt_money(stated)}, but the salary basis the "
                "formula multiplies is stated nowhere in the "
                "package — the severance cannot be computed, so "
                "the stated figure cannot be checked. One of the "
                "two numbers counsel is being asked to approve is "
                "unverifiable as the package stands."
            ),
        )]
    computed = round(months * basis_monthly, 2)
    if stated is None:
        return [ExhibitCheck(
            name="severance_arithmetic",
            expectation=expectation,
            promised=promised,
            found=f"computed {_fmt_money(computed)}",
            status="consistent",
            detail=(
                f"The formula produces {_fmt_money(computed)}: "
                f"{formula} at the stated basis of {basis_desc}. "
                "No severance lump sum is stated anywhere in the "
                "package for the computation to contradict — the "
                "figure is computed in code from the package's "
                "own numbers, not quoted from it."
            ),
        )]
    if abs(computed - stated) <= _ARITHMETIC_TOLERANCE:
        status, detail = (
            "consistent",
            f"The formula produces {_fmt_money(computed)} — "
            f"{formula} at the stated basis of {basis_desc} — and "
            f"the package states severance of "
            f"{_fmt_money(stated)}. The schedule's figure is the "
            "letter's formula, computed.",
        )
    else:
        status, detail = (
            "mismatch",
            f"The formula produces {_fmt_money(computed)} — "
            f"{formula} at the stated basis of {basis_desc} — but "
            f"the package states severance of "
            f"{_fmt_money(stated)} (\"{stated_sentence}\"). The "
            "two numbers in the same package describe the same "
            "promise and disagree by "
            f"{_fmt_money(abs(computed - stated))}: one of them "
            "was drafted against a different salary, and which "
            "one governs is a dispute the package invites.",
        )
    return [ExhibitCheck(
        name="severance_arithmetic",
        expectation=expectation,
        promised=promised,
        found=f"severance stated as {_fmt_money(stated)}; "
              f"formula produces {_fmt_money(computed)}",
        status=status,
        detail=detail,
    )]
