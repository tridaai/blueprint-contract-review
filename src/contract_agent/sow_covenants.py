"""SOW-vs-MSA covenant conflicts: an SOW's restrictive covenants
read against the master agreement it names (round 19).

A statement of work lives under a master agreement, and both
documents can carry restrictive covenants — that is where
conflicts hide. The MSA promises exclusivity for twelve months
past termination; the SOW under it quietly grants twenty-four,
over a wider scope. Each document read alone looks consistent;
the conflict exists only between them, and it surfaces at the
worst moment — when someone relies on the longer tail.

The reading needs nothing but the two stored registers and the
two texts:

* **Linkage is stated, never inferred.** The SOW's own text
  names its master ("…under that certain Master Services
  Agreement (CTR-2040)…"), and the master must be in the store.
  An SOW that names no master — or names one this service has
  never seen — draws no checks at all: there is no second
  document to conflict with, and inventing one would be worse
  than silence.
* **Durations compare by kind.** For each covenant kind
  (``obligations.ALL_COVENANT_KINDS``), the SOW's register entry
  is read against the master's. Both present with different
  tails is a conflict; both present with the same tail is a
  consistent reading, on record because it was checked.
* **Exclusivity scope compares too.** An appointment's scope —
  the services after "exclusive provider of" — is part of the
  bargain: the same tail over wider services is a wider
  restriction, and a mismatch. And where the master expressly
  engages its providers on a NON-exclusive basis while the SOW
  grants exclusivity, the conflict is named even though the
  master carries no exclusivity covenant to compare tails with.
* **Silence incorporates; it does not contradict.** A kind only
  one document carries is a consistent reading, stated as such:
  the master's covenant binds the SOW's work by incorporation,
  and the SOW's own covenant stands where the master is silent.
  The reading is recorded so "we checked" is a fact, not a
  recollection.
* **Precedence is read, not assumed.** Where either document
  states an order of precedence for a conflict ("in the event
  of any conflict … the Master Agreement shall control"), the
  conflict names the governing document in the stated language's
  own terms. Where neither states one, the check says so —
  which covenant governs is counsel's call. The check flags and
  explains; it never resolves the conflict into a legal
  conclusion, and the register keeps BOTH readings either way.

Like the drift checks, the comparison attaches AFTER the run
(it needs the master's stored register), so a conflict does not
move the SOW's posture: posture is the SOW against the
playbook; this is the SOW against its own master.
"""

from __future__ import annotations

import re

from .covenant_history import _LABELS as COVENANT_LABELS
from .obligations import ALL_COVENANT_KINDS
from .schemas import ContractInput, ExhibitCheck, Obligation

CHECK_NAME = "sow_covenant_conflict"

_CONTRACT_ID = re.compile(r"\bCTR-\d+\b")
_MASTER_WORDS = ("master services agreement", "master agreement")
_MONTHS = re.compile(r"(\d+)\s+months")
_CONTROL_WORDS = (
    "control", "controls", "prevail", "prevails", "govern", "governs",
)
_SCOPE = re.compile(
    r"exclusive\s+(?:provider|supplier)\s+of\s+([^.,;]+)", re.IGNORECASE
)


def agreement_text(contract: ContractInput) -> str:
    """The package's agreement-document text (the document the
    covenants and the master reference live in); every document's
    text when no single agreement document is marked."""
    docs = [d for d in contract.documents if d.doc_type == "agreement"]
    if not docs:
        docs = list(contract.documents)
    return "\n".join(d.raw_text or "" for d in docs)


def _sentences(text: str) -> list[str]:
    return [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", text or "")
        if s.strip()
    ]


def master_agreement_id(text: str) -> str:
    """The contract id of the master agreement an SOW names, or
    "" when it names none. The reference must be stated — a
    sentence that speaks of a master agreement AND carries a
    contract id — never inferred from counterparty, chain, or
    supersedes links: an SOW's master is a fact the document
    itself asserts."""
    for sentence in _sentences(text):
        low = sentence.lower()
        if any(word in low for word in _MASTER_WORDS):
            match = _CONTRACT_ID.search(sentence)
            if match:
                return match.group(0)
    return ""


def _precedence_in(text: str, own: str) -> str:
    """Which document one text's order-of-precedence sentence
    puts first in a conflict: "master", "sow", or "" when the
    text states none. Read in code from the stated language: a
    sentence that speaks of a conflict and carries a control
    word — and the winner is the document named in the CONTROL
    CLAUSE (the segment running up to the control word), never
    any mention in the sentence, because a precedence sentence
    names both documents ("…between this Agreement and any
    statement of work, this Agreement shall control"). "This
    Agreement" in the control clause is the document speaking
    (``own``): a master stating its own precedence names itself
    exactly that."""
    for sentence in _sentences(text):
        low = sentence.lower()
        if "conflict" not in low:
            continue
        match = re.search(
            r"\b(control|controls|prevail|prevails|govern|governs)\b", low
        )
        if not match:
            continue
        segment = low[: match.start()]
        cut = max(segment.rfind(","), segment.rfind(";"))
        segment = segment[cut + 1:]
        if "master" in segment:
            return "master"
        if "statement of work" in segment or re.search(r"\bsow\b", segment):
            return "sow"
        if "this agreement" in segment:
            return own
    return ""


def order_of_precedence(
    sow: ContractInput, master: ContractInput
) -> tuple[str, str]:
    """The stated order of precedence between the two documents:
    (winner, the contract id of the document that states it).
    The SOW is read first — it is the later document, and parties
    commonly restate precedence there — then the master. ("", "")
    when neither document states one."""
    winner = _precedence_in(agreement_text(sow), "sow")
    if winner:
        return winner, sow.contract_id
    winner = _precedence_in(agreement_text(master), "master")
    if winner:
        return winner, master.contract_id
    return "", ""


def _months(obligation: Obligation) -> int | None:
    match = _MONTHS.search(obligation.detail or "")
    return int(match.group(1)) if match else None


def _first(obligations: list[Obligation], kind: str) -> Obligation | None:
    return next((o for o in obligations if o.kind == kind), None)


def exclusivity_scope(text: str) -> str:
    """The services scope of an exclusivity appointment — what
    follows "exclusive provider of", up to the appointment's
    beneficiary ("…services to the Customer") — normalised for
    comparison, or "" when the text makes no such appointment."""
    match = _SCOPE.search(text or "")
    if not match:
        return ""
    scope = " ".join(match.group(1).lower().split())
    return re.sub(r"\s+(to|for)\s+the\s+.*$", "", scope)


def _non_exclusive_sentence(text: str) -> str:
    """A sentence expressly engaging providers on a non-exclusive
    basis, or "" — the scope guard matters: a non-exclusive
    LICENCE (of IP, of materials) is not an engagement term, so
    the sentence must speak of providers, engagements, or
    services before it can conflict with an exclusivity grant."""
    for sentence in _sentences(text):
        low = sentence.lower()
        if "non-exclusive" not in low:
            continue
        if any(
            word in low for word in ("provider", "engage", "services")
        ):
            return sentence.strip()
    return ""


def _precedence_sentence(winner: str, stated_in: str) -> str:
    if winner == "master":
        return (
            f" Order of precedence: {stated_in} states that in "
            "the event of a conflict the master agreement "
            "controls — the SOW's term does not displace the "
            "master's, and both readings stay on the register "
            "for counsel."
        )
    if winner == "sow":
        return (
            f" Order of precedence: {stated_in} states that in "
            "the event of a conflict the statement of work "
            "controls — the SOW's term governs this engagement "
            "over the master's general term, and both readings "
            "stay on the register for counsel."
        )
    return (
        " Neither document states an order of precedence for "
        "a conflict: which covenant governs is counsel's call, "
        "and both readings stay on the register until it is "
        "made."
    )


def build_sow_covenant_checks(
    sow: ContractInput,
    master: ContractInput,
    sow_obligations: list[Obligation],
    master_obligations: list[Obligation],
) -> list[ExhibitCheck]:
    """One check per covenant kind either document carries,
    the SOW's register entry read against the master's. A
    conflict is a ``mismatch`` naming both documents, both
    clause sources, and — where stated — the order of
    precedence; an agreement, an incorporation, or an SOW-only
    covenant is a ``consistent`` reading, on record because it
    was checked."""
    sow_text = agreement_text(sow)
    master_text = agreement_text(master)
    winner, stated_in = order_of_precedence(sow, master)
    checks: list[ExhibitCheck] = []
    for kind in ALL_COVENANT_KINDS:
        label = COVENANT_LABELS[kind]
        sow_entry = _first(sow_obligations, kind)
        master_entry = _first(master_obligations, kind)
        if sow_entry is None and master_entry is None:
            continue
        expectation = (
            f"the {label} covenant under master agreement "
            f"{master.contract_id}"
        )

        def reading(entry: Obligation, contract_id: str) -> str:
            return (
                f"{entry.detail} ({contract_id} "
                f"{entry.source_ref or entry.source_clause_id})"
            )

        if master_entry is not None and sow_entry is None:
            checks.append(ExhibitCheck(
                name=CHECK_NAME,
                expectation=expectation,
                promised=reading(master_entry, master.contract_id),
                found=f"the SOW states no {label} covenant",
                status="consistent",
                detail=(
                    f"The master agreement's {label} covenant "
                    f"({reading(master_entry, master.contract_id)}) "
                    f"binds the SOW's work by incorporation; "
                    f"{sow.contract_id} states no {label} covenant "
                    "of its own, so there is nothing to conflict. "
                    "Recorded so the incorporation was checked, "
                    "not assumed."
                ),
            ))
            continue
        if master_entry is None:
            assert sow_entry is not None
            if kind == "exclusivity":
                non_exclusive = _non_exclusive_sentence(master_text)
                if non_exclusive:
                    checks.append(ExhibitCheck(
                        name=CHECK_NAME,
                        expectation=expectation,
                        promised=(
                            f"engagements under {master.contract_id} "
                            "are non-exclusive"
                        ),
                        found=reading(sow_entry, sow.contract_id),
                        status="mismatch",
                        detail=(
                            f"Scope conflict: {master.contract_id} "
                            f"engages its providers on a non-exclusive "
                            f"basis (\"{non_exclusive}\"), while the "
                            f"SOW grants exclusivity — "
                            f"{reading(sow_entry, sow.contract_id)}. "
                            "The master carries no exclusivity "
                            "covenant to compare tails with; the "
                            "conflict is in the grant itself."
                            + _precedence_sentence(winner, stated_in)
                        ),
                    ))
                    continue
            checks.append(ExhibitCheck(
                name=CHECK_NAME,
                expectation=expectation,
                promised=(
                    f"the master agreement states no {label} covenant"
                ),
                found=reading(sow_entry, sow.contract_id),
                status="consistent",
                detail=(
                    f"{master.contract_id} carries no {label} "
                    f"covenant; the SOW's own covenant "
                    f"({reading(sow_entry, sow.contract_id)}) "
                    "stands on its own terms. Silence incorporates, "
                    "it does not contradict — recorded so the gap "
                    "was checked, not overlooked."
                ),
            ))
            continue
        # Both documents carry the kind: compare.
        assert sow_entry is not None and master_entry is not None
        conflicts: list[str] = []
        sow_months = _months(sow_entry)
        master_months = _months(master_entry)
        if (
            sow_months is not None
            and master_months is not None
            and sow_months != master_months
        ):
            conflicts.append(
                f"duration — the SOW runs the {label} covenant "
                f"{sow_months} months following termination of "
                f"the agreement, the master {master_months} months"
            )
        if kind == "exclusivity":
            sow_scope = exclusivity_scope(sow_text)
            master_scope = exclusivity_scope(master_text)
            if sow_scope and master_scope and sow_scope != master_scope:
                conflicts.append(
                    f"scope — the SOW's exclusivity covers "
                    f"\"{sow_scope}\", the master's covers "
                    f"\"{master_scope}\""
                )
        if conflicts:
            checks.append(ExhibitCheck(
                name=CHECK_NAME,
                expectation=expectation,
                promised=reading(master_entry, master.contract_id),
                found=reading(sow_entry, sow.contract_id),
                status="mismatch",
                detail=(
                    f"Covenant conflict ({'; '.join(conflicts)}). "
                    f"The master reads "
                    f"{reading(master_entry, master.contract_id)}; "
                    f"the SOW reads "
                    f"{reading(sow_entry, sow.contract_id)}."
                    + _precedence_sentence(winner, stated_in)
                ),
            ))
        else:
            checks.append(ExhibitCheck(
                name=CHECK_NAME,
                expectation=expectation,
                promised=reading(master_entry, master.contract_id),
                found=reading(sow_entry, sow.contract_id),
                status="consistent",
                detail=(
                    f"The SOW carries the {label} covenant at the "
                    f"master's own terms "
                    f"({reading(sow_entry, sow.contract_id)} "
                    f"against {reading(master_entry, master.contract_id)}"
                    f") — checked against the master, not just "
                    "read alone."
                ),
            ))
    return checks
