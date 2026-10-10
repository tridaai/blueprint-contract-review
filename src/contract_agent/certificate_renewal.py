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


def build_certificate_renewal(
    prior: ContractInput, current: ContractInput
) -> list[ExhibitCheck]:
    """The renewal-evidence check between two consecutive rounds
    of a chain, or an empty list when no renewal arrived or there
    is no original requirement set to check it against."""
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

    # The original requirement set: the prior round's agreement.
    agreement = prior.agreement_text()
    lower = agreement.lower()
    required_limits = _coverage_amounts(agreement)
    additional_required = "additional insured" in lower
    waiver_required = "waiver of subrogation" in lower
    if not required_limits and not additional_required and not waiver_required:
        return []  # the original terms required nothing checkable
    labels = {key: label for key, label, _ in _COVERAGE_KEYS}
    counterparty = prior.counterparty_name or current.counterparty_name or ""

    cert_text = current_cert.raw_text
    cert_lower = cert_text.lower()
    cert_limits = _coverage_amounts(cert_text)
    drops: list[str] = []
    if counterparty:
        found_name = _named_insured(cert_text)
        if not found_name:
            drops.append("it names no insured at all")
        elif _normalise_name(found_name) != _normalise_name(counterparty):
            drops.append(
                f"it names {found_name} as the insured, not "
                f"{counterparty}"
            )
    for key in sorted(required_limits):
        required = required_limits[key]
        stated = cert_limits.get(key)
        if stated is None:
            drops.append(
                f"{labels[key]} cover — required at "
                f"{_fmt_money(required)} under the original terms "
                "— is not stated on it at all"
            )
        elif stated < required:
            drops.append(
                f"{labels[key]} cover stands at "
                f"{_fmt_money(stated)}, below the "
                f"{_fmt_money(required)} the original terms require"
            )
    for required_flag, phrase in (
        (additional_required, "additional insured"),
        (waiver_required, "waiver of subrogation"),
    ):
        if required_flag and _endorsement_present(cert_lower, phrase) is not True:
            drops.append(
                f"the {phrase} endorsement the original terms "
                "require is not shown on it"
            )

    doc_id = current_cert.document_id or current_cert.doc_type
    expectation = (
        "A renewal certificate is evidence against the terms "
        "that required the insurance — the original round's "
        "requirement set — not against whatever terms arrived "
        "with the renewal. Limits or endorsements quietly "
        "dropped at renewal are a mismatch against the original "
        "clause, not a fresh consistent."
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
            "requirement the original terms set — the same "
            "limits, the same endorsements, the same named "
            "insured. The renewal watch on the earlier expiry "
            "closes on this evidence: the cover counsel relied "
            "on is the cover now in force."
        ),
    )]
