"""Synthetic contract review playbook corpus.

This playbook is INVENTED for this blueprint. It is generic legal-ops
language written for a reference prototype — it is not any real
company's playbook, legal advice, or wording, and it describes no
client engagement.

Positions are the company's standard ladder: **preferred** (ask for
this), **acceptable fallback** (can live with this), **review
required** (a human must look), **walk-away** (do not accept; escalate
to senior counsel). The fallback language quoted on entries is the
playbook's own proposed wording — drafts propose it, counsel decides.

To adapt the blueprint, replace this corpus with your own playbook
(and keep the mirror in ``data/sample/playbook.json`` in sync).
"""

from __future__ import annotations

PLAYBOOK: list[dict] = [
    {
        "id": "PB-LIA-01",
        "title": "Liability cap — preferred position",
        "clause_type": "liability_cap",
        "text": (
            "Preferred: each party's aggregate liability is capped at the fees paid or "
            "payable in the twelve months preceding the claim (or a fixed amount of at "
            "least 1,000,000), and the cap applies to both parties."
        ),
        "fallback_language": (
            "Each party's aggregate liability arising under or in connection with this "
            "Agreement shall not exceed the fees paid or payable in the twelve (12) "
            "months immediately preceding the event giving rise to the claim."
        ),
    },
    {
        "id": "PB-LIA-02",
        "title": "Liability cap — acceptable fallback",
        "clause_type": "liability_cap",
        "text": (
            "Acceptable fallback: a stated cap below the preferred level is acceptable "
            "when it is mutual and expressed as a fixed amount or a fees multiple. Ask "
            "for the preferred wording first; record any fallback accepted."
        ),
        "fallback_language": (
            "Each party's aggregate liability shall not exceed the greater of the fees "
            "paid in the preceding twelve (12) months or USD 250,000."
        ),
    },
    {
        "id": "PB-LIA-03",
        "title": "Liability — uncapped is a walk-away",
        "clause_type": "liability_cap",
        "text": (
            "Walk-away: unlimited or uncapped liability for either party is never "
            "accepted. Escalate to senior counsel and propose the preferred cap "
            "language. Do not characterise uncapped liability as a negotiable point."
        ),
        "fallback_language": (
            "Each party's aggregate liability arising under or in connection with this "
            "Agreement shall not exceed the fees paid or payable in the twelve (12) "
            "months immediately preceding the event giving rise to the claim."
        ),
    },
    {
        "id": "PB-IND-01",
        "title": "Indemnity — preferred position",
        "clause_type": "indemnity",
        "text": (
            "Preferred: indemnities are mutual and proportionate. The counterparty "
            "indemnifies us against third-party claims that its materials, services, "
            "or intellectual property infringe third-party rights."
        ),
        "fallback_language": (
            "Each party shall indemnify the other against third-party claims arising "
            "from its own negligence, wilful misconduct, or infringement of "
            "third-party intellectual property rights."
        ),
    },
    {
        "id": "PB-IND-02",
        "title": "Indemnity — one-way and negligence coverage",
        "clause_type": "indemnity",
        "text": (
            "Review required: a one-way indemnity running only against us requires "
            "counsel review. Walk-away escalation: an indemnity that makes us "
            "responsible for the counterparty's own negligence is never accepted as "
            "drafted — propose the mutual wording."
        ),
        "fallback_language": (
            "Neither party shall be required to indemnify the other for claims "
            "resulting from the indemnified party's own negligence or wilful "
            "misconduct."
        ),
    },
    {
        "id": "PB-IP-01",
        "title": "IP ownership — preferred position",
        "clause_type": "ip_ownership",
        "text": (
            "Preferred: each party retains ownership of its intellectual property, "
            "including pre-existing and background IP. The counterparty receives a "
            "licence to use deliverables for its internal business purposes."
        ),
        "fallback_language": (
            "Each party retains all right, title and interest in its pre-existing "
            "intellectual property. Deliverables are licensed to the receiving party "
            "for its internal business purposes on a non-exclusive basis."
        ),
    },
    {
        "id": "PB-IP-02",
        "title": "IP assignment — walk-away",
        "clause_type": "ip_ownership",
        "text": (
            "Walk-away: assignment of all intellectual property — including "
            "pre-existing or background IP — to the counterparty is never accepted. "
            "Escalate to senior counsel. Assignment narrowly limited to bespoke "
            "deliverables created and paid for under a statement of work may be an "
            "acceptable fallback with counsel approval."
        ),
        "fallback_language": (
            "Ownership of deliverables created specifically for and paid for by the "
            "counterparty vests in the counterparty on payment in full; all other "
            "intellectual property remains with its owner."
        ),
    },
    {
        "id": "PB-CON-01",
        "title": "Confidentiality — preferred position",
        "clause_type": "confidentiality",
        "text": (
            "Preferred: confidentiality obligations are mutual, cover both parties' "
            "confidential information, and last no longer than five years from "
            "disclosure (trade secrets excepted)."
        ),
        "fallback_language": (
            "Each party shall hold the other's Confidential Information in confidence "
            "for five (5) years from the date of disclosure."
        ),
    },
    {
        "id": "PB-CON-02",
        "title": "Confidentiality — one-way is a fallback, perpetual is review",
        "clause_type": "confidentiality",
        "text": (
            "Acceptable fallback: one-way confidentiality may be accepted where we "
            "are the receiving party and the scope is narrow — propose mutual wording "
            "first. Review required: obligations lasting longer than five years, or "
            "stated to be perpetual, need counsel review."
        ),
        "fallback_language": (
            "The obligations in this section apply to Confidential Information "
            "disclosed by either party and survive for five (5) years from "
            "disclosure."
        ),
    },
    {
        "id": "PB-TERM-01",
        "title": "Term and termination — preferred position",
        "clause_type": "term_termination",
        "text": (
            "Preferred: either party may terminate for convenience on no more than "
            "ninety days' written notice. Auto-renewal is acceptable when the "
            "renewal notice window is ninety days or shorter."
        ),
        "fallback_language": (
            "Either party may terminate this Agreement for convenience on ninety "
            "(90) days' prior written notice."
        ),
    },
    {
        "id": "PB-TERM-02",
        "title": "Term and termination — lock-in needs review",
        "clause_type": "term_termination",
        "text": (
            "Review required: no termination for convenience, or an auto-renewal "
            "notice window longer than 180 days, locks us in — counsel must review. "
            "A renewal notice window between 91 and 180 days is an acceptable "
            "fallback when termination for convenience exists."
        ),
        "fallback_language": (
            "This Agreement renews for successive twelve-month terms unless either "
            "party gives notice of non-renewal at least ninety (90) days before the "
            "end of the then-current term."
        ),
    },
    {
        "id": "PB-PAY-01",
        "title": "Payment terms — preferred position",
        "clause_type": "payment_terms",
        "text": (
            "Preferred: invoices are payable net 30, and in no case later than "
            "net 45. Late-payment interest, if any, does not exceed 1 percent "
            "per month."
        ),
        "fallback_language": (
            "Undisputed invoices are payable within forty-five (45) days of the "
            "invoice date."
        ),
    },
    {
        "id": "PB-PAY-02",
        "title": "Payment terms — fallbacks and review triggers",
        "clause_type": "payment_terms",
        "text": (
            "Acceptable fallback: net 60 where the counterparty is the customer "
            "and the amounts are material. Review required: terms beyond net 60, "
            "full payment in advance, or late fees above 1.5 percent per month."
        ),
        "fallback_language": (
            "Undisputed invoices are payable within forty-five (45) days of the "
            "invoice date; interest on overdue undisputed amounts accrues at "
            "1 percent per month or the maximum lawful rate, whichever is lower."
        ),
    },
    {
        "id": "PB-LAW-01",
        "title": "Governing law — preferred position",
        "clause_type": "governing_law",
        "text": (
            "Preferred: the laws of the State of Delaware or the State of New York, "
            "with venue in courts of that state."
        ),
        "fallback_language": (
            "This Agreement is governed by the laws of the State of Delaware, "
            "without regard to its conflict-of-laws principles."
        ),
    },
    {
        "id": "PB-LAW-02",
        "title": "Governing law — fallbacks and review triggers",
        "clause_type": "governing_law",
        "text": (
            "Acceptable fallback: the law of another US state where the "
            "counterparty is headquartered. Review required: foreign governing "
            "law, foreign venue, or mandatory arbitration seated outside the "
            "United States."
        ),
        "fallback_language": (
            "This Agreement is governed by the laws of the State of New York, and "
            "the parties consent to the exclusive jurisdiction of its state and "
            "federal courts."
        ),
    },
    {
        "id": "PB-DP-01",
        "title": "Data protection — preferred position",
        "clause_type": "data_protection",
        "text": (
            "Preferred: the agreement incorporates a data processing addendum with "
            "security measures proportionate to the data, sub-processor notice, "
            "and breach notification to us within 72 hours of discovery."
        ),
        "fallback_language": (
            "Each party shall notify the other without undue delay and in any event "
            "within seventy-two (72) hours of becoming aware of a personal-data "
            "breach affecting the other party's data."
        ),
    },
    {
        "id": "PB-DP-02",
        "title": "Data protection — absence is critical",
        "clause_type": "data_protection",
        "text": (
            "An agreement that involves personal data but contains no data "
            "protection terms at all is a critical gap: flag the absence, escalate, "
            "and request that the counterparty's data processing addendum be "
            "attached as an exhibit before signature."
        ),
        "fallback_language": (
            "The parties shall comply with applicable data protection law and the "
            "data processing addendum attached as an exhibit to this Agreement."
        ),
    },
    {
        "id": "PB-WAR-01",
        "title": "Warranties — preferred position",
        "clause_type": "limitation_of_warranty",
        "text": (
            "Preferred: warranties are limited to professional performance for no "
            "more than twelve months, with re-performance or refund as the "
            "exclusive remedy. Extended warranty or support obligations beyond "
            "twelve months require counsel review."
        ),
        "fallback_language": (
            "Warranties apply for twelve (12) months from acceptance; the exclusive "
            "remedy is re-performance or, if re-performance is not commercially "
            "practicable, a refund of fees for the affected deliverable."
        ),
    },
    {
        "id": "PB-ASG-01",
        "title": "Assignment — preferred position",
        "clause_type": "assignment",
        "text": (
            "Preferred: neither party may assign the agreement without the other's "
            "prior written consent, not to be unreasonably withheld. Assignment to "
            "a successor in a sale of all or substantially all of a party's business "
            "may be an acceptable fallback with notice."
        ),
        "fallback_language": (
            "Neither party may assign this Agreement without the prior written "
            "consent of the other party, except to a successor in connection with a "
            "sale of all or substantially all of its business, on written notice."
        ),
    },
    {
        "id": "PB-ASG-02",
        "title": "Assignment — free assignment needs review",
        "clause_type": "assignment",
        "text": (
            "Review required: free assignment by the counterparty — including on a "
            "change of control — without consent or notice. We should retain the "
            "right to object to assignment to a competitor."
        ),
        "fallback_language": (
            "The counterparty shall give at least thirty (30) days' written notice "
            "of any assignment, including by change of control, and we may terminate "
            "on notice if the assignee is a competitor."
        ),
    },
    {
        "id": "PB-GEN-01",
        "title": "Review process — drafts are for counsel, never conclusions",
        "clause_type": "other",
        "text": (
            "Every output of the review process is a draft for counsel review. A "
            "first-pass review flags positions against this playbook; it is not a "
            "legal conclusion, it does not approve a contract, and nothing is agreed "
            "with the counterparty until counsel says so in writing."
        ),
        "fallback_language": "",
    },
    {
        "id": "PB-GEN-02",
        "title": "Missing standard clauses — absence is a finding",
        "clause_type": "other",
        "text": (
            "Absence is a finding: a services agreement with no liability cap or no "
            "data protection terms is not 'silent', it is unprotected. Flag missing "
            "standard clauses, name the exhibit or addendum to request, and never "
            "treat absence as acceptance."
        ),
        "fallback_language": "",
    },
]

PLAYBOOK_BY_ID = {entry["id"]: entry for entry in PLAYBOOK}
