"""Renewal-certificate evidence: a renewal is checked against
the terms that required the insurance, not the terms that
arrived with it.

The single-round certificate checks (``exhibit_review.py``)
read a certificate against its own round's agreement. That is
the right question at signing and the wrong question at
renewal: by the time a renewal certificate arrives, the current
draft may restate the insurance clause at whatever the renewal
happens to carry — professional liability quietly lowered,
an endorsement quietly dropped — and each round then agrees
with itself while the cover the original bargain required
evaporates. Nobody ever compares the renewal with the original
requirement set. This module does:

- a renewal has arrived when the current round's certificate
  runs past the prior round's certificate — cover was extended,
  which is what a renewal IS;
- the renewal certificate is then read against the ORIGINAL
  (prior round's) requirement set: the coverage limits the
  original clause required, the endorsements it required, the
  counterparty it required as named insured;
- anything the original required that the renewal no longer
  evidences is a drop, and drops make the check a ``mismatch``
  — against the original clause, in the open. A renewal that
  evidences the original set in full is ``consistent``, and it
  is that evidence — not the mere arrival of a new certificate —
  on which the renewal watch (``certificates.py``) closes: a
  failed renewal leaves the watch open on the last relied-upon
  expiry, flagged as failed on evidence.

Both rounds must carry a certificate and the prior round's
agreement must state a requirement set; without either, there
is nothing cross-round to check and the single-round checks
own the record.

Every requirement the renewal is judged against carries its
provenance (round 13): the round that set it and the clause
that states it — "set by CTR-2029 §7 (Insurance)" — named on
the check's detail, per requirement. On a long chain the
requirement set a renewal must evidence may have been imposed
across several rounds (an amendment that raised a limit in
round 2 is not the root round's bargain), and counsel reading
a failed renewal should see which round's clause each failed
requirement came from, not a blended "original terms".

The requirement set itself is composed across the whole
chain (round 14): walking the supersedes chain from the root
to the prior round, each requirement is taken from the round
that LAST SET it. A round sets a coverage limit when it first
states it or states it at a different amount — an amendment
that raises (or deliberately lowers) a limit moves the
requirement and its provenance to that round, while a
restatement that merely carries the standing amount leaves
both where they were. Endorsement requirements, once any
round imposes them, persist — a later round's silence does
not quietly un-require what the bargain required — and the
named-insured term is attributed to the round that imposed
the certificate regime. So a short amendment round that
restates nothing cannot thin the set the renewal is judged
against, and a limit raised in round 2 and merely carried by
round 3's restatement is judged — and cited — from round 2.
"""

from __future__ import annotations

from .exhibit_review import (
    _CERT_DOC,
    _COVERAGE_KEYS,
    _certificate_expiry,
    _coverage_amounts,
    _endorsement_present,
    _find_schedule_document,
    _fmt_money,
    _named_insured,
    _normalise_name,
)
from .schemas import ContractInput, ExhibitCheck


def _requirement_provenance(
    prior: ContractInput, required_limits: dict[str, float]
) -> dict[str, str]:
    """Where each of the prior round's insurance requirements was
    set, as a line counsel can follow: ``CTR-2029 §7 (Insurance)``.

    The prior agreement is cut with the pipeline's own segmenter,
    and each requirement — a coverage limit, an endorsement, the
    named-insured term — is placed in the clause whose text
    states it. A limit is placed at the clause stating the amount
    the requirement actually is: where two clauses state the
    same cover (the root clause and a later uplift), the
    requirement is the higher statement, and its provenance is
    the clause that set it there — an amendment is judged from
    the amendment, not the root. A requirement no single clause
    states (carried by the agreement as a whole) is attributed
    to the round's agreement, plainly, rather than to a clause
    it does not live in."""
    from .segmentation import segment_text

    units = segment_text(prior.agreement_text())

    def place(unit) -> str:
        if unit is None:
            return f"{prior.contract_id} (agreement)"
        if unit.ref:
            label = f"{prior.contract_id} §{unit.ref}"
            return f"{label} ({unit.heading})" if unit.heading else label
        if unit.heading:
            return f"{prior.contract_id} — {unit.heading}"
        return f"{prior.contract_id} (agreement)"

    provenance: dict[str, str] = {}
    for key, required in required_limits.items():
        unit = next(
            (
                u for u in units
                if _coverage_amounts(u.text).get(key) == required
            ),
            None,
        ) or next(
            (u for u in units if key in _coverage_amounts(u.text)),
            None,
        )
        if unit is not None:
            provenance[key] = place(unit)
    for phrase in ("additional insured", "waiver of subrogation"):
        unit = next(
            (u for u in units if phrase in (u.text or "").lower()),
            None,
        )
        if unit is not None:
            provenance[phrase] = place(unit)
    cert_unit = next(
        (
            u for u in units
            if "certificate of insurance" in (u.text or "").lower()
            or "named insured" in (u.text or "").lower()
        ),
        None,
    )
    if cert_unit is not None:
        provenance["named_insured"] = place(cert_unit)
    return provenance


def _compose_requirement_set(
    chain: list[ContractInput],
) -> tuple[dict[str, float], dict[str, ContractInput]]:
    """The effective insurance requirement set across a chain of
    rounds (oldest → newest), and the round that set each part
    of it.

    A round SETS a coverage limit when it first states the
    cover or states it at an amount different from the standing
    composed value: an amendment moves the requirement; a
    restatement at the standing amount merely carries it, and
    the setter stays the earlier round. Endorsements are
    sticky: the first round whose agreement requires one sets
    it, and no later round's silence removes it. The
    named-insured term is set by the first round whose
    agreement carries the certificate regime's language."""
    required: dict[str, float] = {}
    setters: dict[str, ContractInput] = {}
    for round_ in chain:
        agreement = round_.agreement_text()
        lower = agreement.lower()
        for key, amount in _coverage_amounts(agreement).items():
            if key not in required or required[key] != amount:
                required[key] = amount
                setters[key] = round_
        for phrase in ("additional insured", "waiver of subrogation"):
            if phrase in lower and phrase not in setters:
                setters[phrase] = round_
        if "named_insured" not in setters and (
            "certificate of insurance" in lower or "named insured" in lower
        ):
            setters["named_insured"] = round_
    return required, setters


def _composed_provenance(
    required_limits: dict[str, float],
    setters: dict[str, ContractInput],
) -> dict[str, str]:
    """Per-requirement provenance for a composed set: each
    requirement placed by the round-13 placement rule inside
    the round that SET it, so a requirement imposed by an
    amendment cites the amendment's clause, not the prior
    round's restatement of it."""
    provenance: dict[str, str] = {}
    by_setter: dict[str, tuple[ContractInput, list[str]]] = {}
    for key, setter in setters.items():
        by_setter.setdefault(setter.contract_id, (setter, []))[1].append(key)
    for setter, keys in by_setter.values():
        limits = {k: required_limits[k] for k in keys if k in required_limits}
        placed = _requirement_provenance(setter, limits)
        for key in keys:
            if key in placed:
                provenance[key] = placed[key]
    return provenance


def build_certificate_renewal(
    prior: ContractInput,
    current: ContractInput,
    chain: list[ContractInput] | None = None,
) -> list[ExhibitCheck]:
    """The renewal-evidence check between two consecutive rounds
    of a chain, or an empty list when no renewal arrived or there
    is no requirement set to check it against. The requirement
    set is composed across ``chain`` — every round from the root
    to ``prior``, oldest first (the service walks the supersedes
    links); without a chain, the prior round alone composes it,
    which is the pre-round-14 reading."""
    prior_cert = _find_schedule_document(prior, _CERT_DOC)
    current_cert = _find_schedule_document(current, _CERT_DOC)
    if prior_cert is None or current_cert is None:
        return []
    prior_expiry = _certificate_expiry(prior_cert.raw_text)
    current_expiry = _certificate_expiry(current_cert.raw_text)
    if prior_expiry is None or current_expiry is None:
        return []
    if current_expiry <= prior_expiry:
        return []  # cover was not extended — no renewal arrived

    # The requirement set, composed across the whole chain:
    # each requirement from the round that last set it — an
    # amendment's uplift survives a later round that restates
    # nothing, and a restated amount is cited from the round
    # that set it, not the round that carried it.
    rounds = list(chain) if chain else []
    if not rounds or rounds[-1].contract_id != prior.contract_id:
        rounds.append(prior)
    required_limits, setters = _compose_requirement_set(rounds)
    additional_required = "additional insured" in setters
    waiver_required = "waiver of subrogation" in setters
    if not required_limits and not additional_required and not waiver_required:
        return []  # the chain's terms required nothing checkable
    labels = {key: label for key, label, _ in _COVERAGE_KEYS}
    counterparty = prior.counterparty_name or current.counterparty_name or ""
    # Provenance: which round, and which clause in it, set each
    # requirement — named on the findings below, per requirement,
    # so a failed renewal shows its evidence chain.
    provenance = _composed_provenance(required_limits, setters)

    def set_by(key: str) -> str:
        return f" (set by {provenance[key]})" if key in provenance else ""

    requirement_lines: list[str] = []
    if counterparty:
        requirement_lines.append(
            f"named insured {counterparty}{set_by('named_insured')}"
        )
    for key in sorted(required_limits):
        requirement_lines.append(
            f"{labels[key]} at {_fmt_money(required_limits[key])}{set_by(key)}"
        )
    for required_flag, phrase in (
        (additional_required, "additional insured"),
        (waiver_required, "waiver of subrogation"),
    ):
        if required_flag:
            requirement_lines.append(
                f"the {phrase} endorsement{set_by(phrase)}"
            )

    cert_text = current_cert.raw_text
    cert_lower = cert_text.lower()
    cert_limits = _coverage_amounts(cert_text)
    drops: list[str] = []
    if counterparty:
        found_name = _named_insured(cert_text)
        if not found_name:
            drops.append(
                f"it names no insured at all, where the original "
                f"terms require {counterparty} to be named"
                f"{set_by('named_insured')}"
            )
        elif _normalise_name(found_name) != _normalise_name(counterparty):
            drops.append(
                f"it names {found_name} as the insured, not "
                f"{counterparty} (the named insured the original "
                f"terms require{set_by('named_insured')})"
            )
    for key in sorted(required_limits):
        required = required_limits[key]
        stated = cert_limits.get(key)
        if stated is None:
            drops.append(
                f"{labels[key]} cover — required at "
                f"{_fmt_money(required)} under the original terms"
                f"{set_by(key)} — is not stated on it at all"
            )
        elif stated < required:
            drops.append(
                f"{labels[key]} cover stands at "
                f"{_fmt_money(stated)}, below the "
                f"{_fmt_money(required)} the original terms require"
                f"{set_by(key)}"
            )
    for required_flag, phrase in (
        (additional_required, "additional insured"),
        (waiver_required, "waiver of subrogation"),
    ):
        if required_flag and _endorsement_present(cert_lower, phrase) is not True:
            drops.append(
                f"the {phrase} endorsement the original terms "
                f"require{set_by(phrase)} is not shown on it"
            )

    doc_id = current_cert.document_id or current_cert.doc_type
    expectation = (
        "A renewal certificate is evidence against the terms "
        "that required the insurance — the requirement set "
        "composed across the chain, each requirement from the "
        "round that last set it — not against whatever terms "
        "arrived with the renewal. Limits or endorsements "
        "quietly dropped at renewal are a mismatch against the "
        "clause that set them, not a fresh consistent."
    )
    promised = (
        f"the original requirement set (certificate to "
        f"{prior_expiry.isoformat()}) renewed in full"
    )
    if drops:
        return [ExhibitCheck(
            name="insurance_certificate_renewal",
            expectation=expectation,
            exhibit_document_id=doc_id,
            promised=promised,
            found=(
                f"renewal to {current_expiry.isoformat()} — "
                f"{len(drops)} original requirement(s) not evidenced"
            ),
            status="mismatch",
            detail=(
                f"The renewal certificate extends cover to "
                f"{current_expiry.isoformat()}, but read against "
                f"the original terms it does not renew them in "
                f"full: {'; '.join(drops)}. The current round's "
                "own checks read the certificate against the "
                "current draft and may pass it; the cover the "
                "original bargain required is nevertheless no "
                "longer evidenced, and the renewal watch stays "
                "open on the earlier expiry until a certificate "
                "evidences the original set."
            ),
        )]
    return [ExhibitCheck(
        name="insurance_certificate_renewal",
        expectation=expectation,
        exhibit_document_id=doc_id,
        promised=promised,
        found=(
            f"renewal to {current_expiry.isoformat()} evidences "
            "the original requirement set in full"
        ),
        status="consistent",
        detail=(
            f"The renewal certificate extends cover from "
            f"{prior_expiry.isoformat()} to "
            f"{current_expiry.isoformat()} and evidences every "
            "requirement the original terms set: "
            f"{'; '.join(requirement_lines)}. The renewal watch "
            "on the earlier expiry closes on this evidence: the "
            "cover counsel relied on is the cover now in force."
        ),
    )]
