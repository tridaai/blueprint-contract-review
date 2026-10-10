"""Guardrails as code: deterministic checks on every draft.

These run on the review packet and the counterparty response draft in
every mode — template, LLM, and repaired drafts alike — and a failed
blocking check makes the draft unapprovable (the service layer refuses
approval and the API returns 422).

The rules encode the blueprint's core promise: **the agent flags and
explains; it never states a legal conclusion, never agrees to or
accepts any term, never pronounces a clause acceptable, never
promises an outcome, and never characterises how material the
requested changes are.** A counterparty's own words
can violate them — a cover email pressing "confirm you accept all
terms today" is reported pressure, not an instruction, so the
injection screen leaves it flagged-but-present in the source; when
a drafter pastes it into the response draft, these checks are what
stop it. The adversarial samples demonstrate exactly that path.

Every check here is pattern-based — deterministic families of
phrasings, not semantic understanding — and each says so. The
materiality check is the clearest example: it catches a minimising
adjective tied to a change noun, in either order; a drafter determined
to minimise in other words would still be caught by counsel, the
verify step, and the reviewer — the guardrail is one layer, honestly
labelled.
"""

from __future__ import annotations

import re

from .schemas import ContractInput, Drafts, GuardrailCheck, Validation

# Affirmative legal-conclusion language. Each match is checked against
# a negation window (see _affirmative): "not a legal conclusion" is the
# required label, not a violation.
_CONCLUSION_PATTERNS = [
    r"\bthis contract is (legally )?(binding|enforceable|valid|sound)\b",
    r"\b(contract|agreement) is approved\b",
    r"\bwe (have )?approved (the|this) (contract|agreement)\b",
    r"\bapproved for (signature|execution)\b",
    r"\bsafe to sign\b",
    r"\blegally binding (contract|agreement|document)\b",
    r"\bthe contract is compliant\b",
    r"\bwe conclude\b[^.]{0,40}\b(enforceable|binding|compliant)\b",
]

# Acceptability pronouncements — subtler than agreement language, and
# caught live by the LLM judge (a response letter calling a cap
# "acceptable as drafted"). The letter PROPOSES; whether any clause —
# even a preferred one — is acceptable is counsel's decision, and the
# draft may not pre-decide it in either direction of politeness.
_ACCEPTABILITY_PATTERNS = [
    r"\bacceptable as drafted\b",
    r"\bacceptable as (it )?(stands|written)\b",
    r"\bacceptable in its current form\b",
    r"\bacceptable to us\b",
    r"\b(this|the|that) (clause|draft|wording|language|provision|position) is acceptable\b",
    r"\bclause [\w.-]+ is acceptable\b",
    r"\bwe find (this|the clause|it|clause [\w.-]+)\b[^.]{0,30}\bacceptable\b",
    r"\bno objection to (clause|section|§)\b",
]


def find_acceptability_hit(text: str) -> str | None:
    """First affirmative acceptability pronouncement in ``text`` (the
    matched phrase), or None. Pattern-based, like every check here: a
    phrasing family, not a semantic detector."""
    for pattern in _ACCEPTABILITY_PATTERNS:
        hit = _affirmative(pattern, text or "")
        if hit:
            return hit
    return None


# Affirmative agreement/acceptance language — the counterparty draft
# proposes changes; it must never accept anything.
_AGREEMENT_PATTERNS = [
    r"\bwe (hereby )?agree\b",
    r"\bwe (hereby )?accept\b",
    r"\bi agree\b",
    r"\bhereby agrees to\b",
    r"\baccept (all|these|the) terms\b",
    r"\bconfirm\w* (that )?(we|you) accept\b",
    r"\byou accept (all|these|the)\b",
    r"\bagreed and accepted\b",
    r"\bwe are (happy|pleased) to (accept|agree)\b",
    r"\bterms are accepted\b",
]

_NEGATION = re.compile(r"\b(not|never|no|n't|nor|nothing)\b", re.IGNORECASE)

# Materiality characterisations of the requested changes. Counsel
# judges whether a change is material; the drafter lists the changes
# factually. An LLM judge caught live drafts calling requested changes
# "technical and routine" — language that quietly pre-decides the
# negotiation. The family below binds a minimising adjective to a
# change noun, in either order ("minor changes", "the changes are
# technical and routine"), plus bare minimisers ("only a minor ...").
# The noun list deliberately excludes clause nouns: quoted playbook
# fallback language stays safe ("the standard clause", "standard
# position") — the characterisation is the violation, not the word.
_MINIMISER = (
    r"(?:routine|minor|technical|simple|small|standard|administrative|"
    r"clerical|trivial|cosmetic)"
)
_CHANGE_NOUN = r"(?:changes?|amendments?|revisions?|requests?|edits?|modifications?)"
_MATERIALITY_PATTERNS = [
    rf"\b{_MINIMISER}\b[^.]{{0,40}}?\b{_CHANGE_NOUN}\b",
    rf"\b{_CHANGE_NOUN}\b[^.]{{0,40}}?\b{_MINIMISER}\b",
    r"\b(?:only|just|merely)\s+(?:a\s+)?(?:minor|technical|routine|small|simple|administrative)\b",
]


def find_materiality_hit(text: str) -> str | None:
    """First affirmative materiality characterisation of the requested
    changes in ``text`` (the matched phrase), or None. Pattern-based:
    a phrasing family, not a semantic detector."""
    for pattern in _MATERIALITY_PATTERNS:
        hit = _affirmative(pattern, text or "")
        if hit:
            return hit
    return None


def _affirmative(pattern: str, text: str) -> str | None:
    """First match of ``pattern`` not preceded by a negation within
    30 characters — returns the matched phrase, or None."""
    for match in re.finditer(pattern, text, re.IGNORECASE):
        window = text[max(0, match.start() - 30):match.start()]
        if _NEGATION.search(window):
            continue
        return match.group(0)
    return None


_PROMISE_PATTERNS = [
    # Outcome promises — the agent may never promise a decision.
    r"\bwe (promise|guarantee)\b",
    r"\bguaranteed (approval|acceptance|signature|execution)\b",
    r"\bwill be (signed|executed|approved|accepted)\b",
    r"\bwe will (sign|execute|approve|accept)\b",
    # Time commitments: a promise about WHEN something will happen.
    # (A bare "within 72 hours" is NOT matched: in a contract, notice
    # periods are clause facts — "breach notice within 72 hours" is a
    # position to report, never a promise by us.)
    r"\bwill (arrive|be sent|be returned|be ready|be provided|be delivered|reach you)\b[^.]{0,40}\b(within|in|by)\b[^.]{0,20}\b(hours?|days?|weeks?|tomorrow|eod|end of day|end of week)\b",
    r"\bwe will\b[^.]{0,40}\bwithin (the next )?\d+ (hours?|days?|weeks?)\b",
    r"\b(decision|signed copy|executed copy|answer) (within|in|by)\b[^.]{0,20}\b(\d+|tomorrow|end of)\b",
]

_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PASSPORT = re.compile(r"\b[A-Z]\d{8}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _find_card(text: str) -> str | None:
    for match in re.finditer(r"\b(?:\d[ -]?){13,19}\b", text):
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return match.group(0)
    return None


def validate_drafts(
    contract: ContractInput,
    drafts: Drafts,
    citations: list[str],
    flagged_refs: list[list[str]] | None = None,
) -> Validation:
    """Run all guardrail checks over the drafts. ``passed`` is the
    conjunction — every check here is blocking."""
    response = drafts.counterparty_response or ""
    packet = drafts.review_packet or ""
    combined = f"{drafts.subject}\n{response}\n{packet}"
    checks: list[GuardrailCheck] = []

    present = contract.contract_id in combined
    checks.append(
        GuardrailCheck(
            name="references_contract_id",
            passed=present,
            detail=(
                f"contract id {contract.contract_id} referenced"
                if present
                else f"drafts never reference contract {contract.contract_id}"
            ),
        )
    )

    conclusion_hit = next(
        (hit for hit in (_affirmative(p, combined) for p in _CONCLUSION_PATTERNS) if hit),
        None,
    )
    checks.append(
        GuardrailCheck(
            name="no_legal_conclusion",
            passed=conclusion_hit is None,
            detail=(
                "no draft states a legal conclusion"
                if conclusion_hit is None
                else f"legal conclusion found: '{conclusion_hit}' — the agent flags positions; counsel concludes"
            ),
        )
    )

    agreement_hit = next(
        (hit for hit in (_affirmative(p, response) for p in _AGREEMENT_PATTERNS) if hit),
        None,
    )
    checks.append(
        GuardrailCheck(
            name="no_agreement_language",
            passed=agreement_hit is None,
            detail=(
                "the counterparty draft agrees to nothing"
                if agreement_hit is None
                else f"agreement language found in the counterparty draft: '{agreement_hit}' — the draft proposes changes only"
            ),
        )
    )

    acceptability_hit = find_acceptability_hit(response)
    checks.append(
        GuardrailCheck(
            name="no_acceptability_pronouncement",
            passed=acceptability_hit is None,
            detail=(
                "the counterparty draft pronounces no clause acceptable"
                if acceptability_hit is None
                else f"acceptability pronouncement found in the counterparty draft: '{acceptability_hit}' — "
                "the draft proposes changes; whether a clause is acceptable is counsel's call"
            ),
        )
    )

    materiality_hit = find_materiality_hit(combined)
    checks.append(
        GuardrailCheck(
            name="no_materiality_minimising",
            passed=materiality_hit is None,
            detail=(
                "neither draft characterises the requested changes' materiality"
                if materiality_hit is None
                else f"materiality characterisation found: '{materiality_hit}' — "
                "list the changes factually; whether they are material is counsel's call"
            ),
        )
    )

    promise_hit = next(
        (m.group(0) for p in _PROMISE_PATTERNS if (m := re.search(p, combined, re.IGNORECASE))),
        None,
    )
    checks.append(
        GuardrailCheck(
            name="no_prohibited_promises",
            passed=promise_hit is None,
            detail=(
                "no outcome or timing promises"
                if promise_hit is None
                else f"prohibited promise found: '{promise_hit}'"
            ),
        )
    )

    pii_hit = None
    if _SSN.search(combined):
        pii_hit = "what looks like a US Social Security number"
    elif _find_card(combined):
        pii_hit = "what looks like a payment card number (Luhn-valid)"
    elif _PASSPORT.search(combined):
        pii_hit = "what looks like a passport number"
    checks.append(
        GuardrailCheck(
            name="no_pii_in_draft",
            passed=pii_hit is None,
            detail=(
                "no SSN / card / passport patterns in the drafts"
                if pii_hit is None
                else f"PII in draft: {pii_hit} — personal identifiers never belong in drafts"
            ),
        )
    )

    checks.append(
        GuardrailCheck(
            name="playbook_citations_present",
            passed=bool(citations),
            detail=(
                f"playbook citations present: {citations}"
                if citations
                else "no playbook citations — every review must cite the playbook"
            ),
        )
    )

    # Every flagged clause must carry at least one of its playbook
    # refs in the packet — a verdict without a citation is an opinion.
    uncited: list[int] = []
    for index, refs in enumerate(flagged_refs or []):
        if refs and not any(f"[{ref}]" in packet for ref in refs):
            uncited.append(index)
    checks.append(
        GuardrailCheck(
            name="flagged_clauses_cited",
            passed=not uncited,
            detail=(
                "every flagged clause carries a playbook citation in the packet"
                if not uncited
                else f"flagged clause(s) at index {uncited} lack a playbook citation in the packet"
            ),
        )
    )

    next_step = "next step" in response.lower()
    checks.append(
        GuardrailCheck(
            name="states_next_step",
            passed=next_step,
            detail=(
                "the counterparty draft states the next step"
                if next_step
                else "the counterparty draft never states the next step"
            ),
        )
    )

    return Validation(passed=all(c.passed for c in checks), checks=checks)
