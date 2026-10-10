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

Fallbacks are tiered (``fallback_tiers.py``): an entry's
``fallback_language`` is tier 1, the first fallback; the designated
entry for each clause type also carries a ``walk_away_line`` — tier
2, the final position, beyond which the playbook's answer is to walk
away. Which tier a packet proposes is decided in code from the round
number and the negotiation chain, never by the model.

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
        "walk_away_line": (
            "Final position on liability: aggregate liability must be capped, and the "
            "twelve (12) months' fees cap above is the last position we can put. An "
            "uncapped clause ends the negotiation."
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
        "walk_away_line": (
            "Final position on indemnity: indemnification is mutual and fault-based "
            "only. A one-way indemnity, or indemnifying a party against the "
            "consequences of its own negligence, ends the negotiation."
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
        "walk_away_line": (
            "Final position on intellectual property: pre-existing intellectual "
            "property is never assigned. An assignment of pre-existing tools, methods "
            "or know-how ends the negotiation."
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
        "walk_away_line": (
            "Final position on confidentiality: obligations survive no more than five "
            "(5) years from disclosure. Perpetual confidentiality ends the "
            "negotiation."
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
        "walk_away_line": (
            "Final position on term: the agreement must be exitable — termination for "
            "convenience must exist, and no notice window may exceed one hundred "
            "eighty (180) days. A lock-in with no convenience termination ends the "
            "negotiation."
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
        "walk_away_line": (
            "Final position on payment: net sixty (60) days is the outer limit for "
            "undisputed invoices. Full prepayment, terms beyond net 60, or late fees "
            "above 1.5 percent per month end the negotiation."
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
        "walk_away_line": (
            "Final position on governing law: a United States governing law and "
            "exclusive jurisdiction as above. Foreign governing law ends the "
            "negotiation."
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
        "walk_away_line": (
            "Final position on data protection: a data processing addendum must be "
            "incorporated, with breach notification within seventy-two (72) hours. "
            "Processing personal data with no DPA ends the negotiation."
        ),
    },
    {
        "id": "PB-SUB-01",
        "title": "Sub-processors — preferred position (standalone DPA)",
        "clause_type": "subprocessor_terms",
        "text": (
            "Preferred (standalone DPA): the processor gives at least thirty "
            "days' prior written notice of any new or replacement "
            "sub-processor, the controller may object within that window, "
            "and a change needing the controller's prior written consent is "
            "stronger still. The current sub-processor list is maintained "
            "and available to the controller."
        ),
        "fallback_language": (
            "The Processor shall give the Controller at least thirty (30) "
            "days' prior written notice of any intended addition or "
            "replacement of a sub-processor, and the Controller may object "
            "in writing within that period; the Processor shall not proceed "
            "with a change while a reasonable objection remains unresolved."
        ),
    },
    {
        "id": "PB-SUB-02",
        "title": "Sub-processors — silent changes are a walk-away (standalone DPA)",
        "clause_type": "subprocessor_terms",
        "text": (
            "Review required: a sub-processor clause that states no notice, "
            "objection, or consent machinery at all. Walk-away: the "
            "processor may engage or replace sub-processors at its sole "
            "discretion or without notice — personal data would flow to "
            "parties the controller never saw. Escalate to senior counsel."
        ),
        "fallback_language": (
            "The Processor shall give the Controller at least thirty (30) "
            "days' prior written notice of any intended addition or "
            "replacement of a sub-processor, and the Controller may object "
            "in writing within that period; the Processor shall not proceed "
            "with a change while a reasonable objection remains unresolved."
        ),
        "walk_away_line": (
            "Final position on sub-processors: no sub-processor may be added "
            "or replaced without prior written notice and a thirty (30) day "
            "objection window. Changes at the processor's sole discretion "
            "end the negotiation."
        ),
    },
    {
        "id": "PB-TRF-01",
        "title": "International transfers — preferred position (standalone DPA)",
        "clause_type": "data_transfer",
        "text": (
            "Preferred (standalone DPA): transfers of personal data to a "
            "third country rest on a recognised mechanism in place before "
            "the transfer begins — standard contractual clauses, an "
            "adequacy decision, or an equivalent instrument. Processing "
            "that stays within the EEA/EU, and an outright prohibition on "
            "third-country transfers, are equally preferred."
        ),
        "fallback_language": (
            "The Processor shall not transfer personal data to a third "
            "country unless a valid transfer mechanism is in place — the "
            "European Commission's standard contractual clauses, an "
            "adequacy decision, or another safeguard recognised by "
            "applicable data protection law — and shall notify the "
            "Controller before any new transfer begins."
        ),
    },
    {
        "id": "PB-TRF-02",
        "title": "International transfers — no mechanism is a walk-away (standalone DPA)",
        "clause_type": "data_transfer",
        "text": (
            "Walk-away: personal data may be transferred to third "
            "countries with no transfer mechanism in place — the transfer "
            "itself would be unlawful, not merely unfavourable. Escalate "
            "to senior counsel and propose the mechanism language. A "
            "promise of 'appropriate safeguards' with no mechanism named "
            "is an acceptable fallback only with counsel approval."
        ),
        "fallback_language": (
            "The Processor shall not transfer personal data to a third "
            "country unless a valid transfer mechanism is in place — the "
            "European Commission's standard contractual clauses, an "
            "adequacy decision, or another safeguard recognised by "
            "applicable data protection law — and shall notify the "
            "Controller before any new transfer begins."
        ),
        "walk_away_line": (
            "Final position on transfers: no transfer of personal data to "
            "a third country without a valid transfer mechanism in place "
            "before the transfer begins. A transfer with no mechanism ends "
            "the negotiation."
        ),
    },
    {
        "id": "PB-AUD-01",
        "title": "Audit rights — preferred position (standalone DPA)",
        "clause_type": "audit_rights",
        "text": (
            "Preferred (standalone DPA): the controller (or its designated "
            "auditor) holds a direct audit right over the facilities, "
            "systems, and records relevant to the processing, on "
            "reasonable notice. Acceptable fallback: assurance through "
            "current third-party audit reports and certifications (SOC 2, "
            "ISO 27001) where a direct right is refused — with a direct "
            "right reviving on a personal-data breach."
        ),
        "fallback_language": (
            "The Controller (or its designated auditor) may, on at least "
            "thirty (30) days' written notice and no more than once per "
            "year absent a personal-data breach, audit the Processor's "
            "facilities, systems, and records relevant to the processing, "
            "during business hours and subject to reasonable "
            "confidentiality duties."
        ),
    },
    {
        "id": "PB-AUD-02",
        "title": "Audit rights — exclusion is a walk-away (standalone DPA)",
        "clause_type": "audit_rights",
        "text": (
            "Walk-away: the addendum excludes audit altogether (no right "
            "to audit, inspection barred, a self-issued summary stated to "
            "be conclusive). A controller who cannot verify the processing "
            "cannot answer for it. Escalate to senior counsel and propose "
            "the audit language."
        ),
        "fallback_language": (
            "The Controller (or its designated auditor) may, on at least "
            "thirty (30) days' written notice and no more than once per "
            "year absent a personal-data breach, audit the Processor's "
            "facilities, systems, and records relevant to the processing, "
            "during business hours and subject to reasonable "
            "confidentiality duties."
        ),
        "walk_away_line": (
            "Final position on audit: the Controller must hold a real audit "
            "right — direct audit on reasonable notice, with reports alone "
            "acceptable only as a fallback. A clause that excludes audit "
            "ends the negotiation."
        ),
    },
    {
        "id": "PB-DEL-01",
        "title": "Return and deletion — preferred position (standalone DPA)",
        "clause_type": "data_deletion",
        "text": (
            "Preferred (standalone DPA): when the processing ends, the "
            "processor returns all personal data to the controller or "
            "securely deletes it, AT THE CONTROLLER'S CHOICE, and "
            "certifies the deletion in writing; backup copies follow "
            "within a stated short period. Deletion at the processor's "
            "choice is an acceptable fallback."
        ),
        "fallback_language": (
            "On termination or expiry of the processing, the Processor "
            "shall, at the Controller's choice, return all personal data "
            "to the Controller or securely delete it, and shall certify "
            "the deletion in writing; backup copies shall be deleted "
            "within thirty (30) days thereafter."
        ),
    },
    {
        "id": "PB-DEL-02",
        "title": "Return and deletion — keeping the data is a walk-away (standalone DPA)",
        "clause_type": "data_deletion",
        "text": (
            "Review required: personal data may be retained indefinitely "
            "beyond the engagement, or on terms the clause does not state. "
            "Walk-away: the processor refuses, or is not obliged, to "
            "delete the data — the controller's data would outlive the "
            "relationship on the processor's say-so. Escalate to senior "
            "counsel and propose the return-or-delete language."
        ),
        "fallback_language": (
            "On termination or expiry of the processing, the Processor "
            "shall, at the Controller's choice, return all personal data "
            "to the Controller or securely delete it, and shall certify "
            "the deletion in writing; backup copies shall be deleted "
            "within thirty (30) days thereafter."
        ),
        "walk_away_line": (
            "Final position on exit: personal data is returned or deleted "
            "at the Controller's choice when the processing ends, and the "
            "deletion is certified. Keeping the data indefinitely, or "
            "refusing to delete it, ends the negotiation."
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
        "walk_away_line": (
            "Final position on warranties: a warranty period of at least twelve (12) "
            "months from acceptance, with re-performance or refund as the remedy. A "
            "complete disclaimer of warranties ends the negotiation."
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
        "walk_away_line": (
            "Final position on assignment: no assignment, including by change of "
            "control, without notice and — outside a bona fide successor — consent. "
            "Free assignability ends the negotiation."
        ),
    },
    {
        "id": "PB-NCT-01",
        "title": "Restrictive covenants — preferred position (employment)",
        "clause_type": "non_compete",
        "text": (
            "Preferred (employment): a non-compete runs no more than twelve "
            "months after employment ends and is bounded — to the businesses "
            "that actually compete with us, or to a stated geography (a named "
            "state, or a radius around where the employee worked). A "
            "non-solicitation covenant alone (customers, recruits) is "
            "preferred over any non-compete. The covenant protects a real "
            "interest; it does not park a career."
        ),
        "fallback_language": (
            "For twelve (12) months following the end of the Employee's "
            "employment, the Employee shall not be employed by, or provide "
            "services to, a business that competes with the Company within "
            "the state in which the Employee was employed."
        ),
    },
    {
        "id": "PB-NCT-02",
        "title": "Restrictive covenants — overbroad is a walk-away (employment)",
        "clause_type": "non_compete",
        "text": (
            "Review required: a non-compete longer than twelve months. "
            "Walk-away: a covenant longer than twenty-four months, or one "
            "barring work in ANY business in ANY capacity across the whole "
            "country (or world) — scope no legitimate interest supports, "
            "and the clause most likely to be struck down or read against "
            "us. Escalate to senior counsel and propose the bounded "
            "wording."
        ),
        "fallback_language": (
            "For twelve (12) months following the end of the Employee's "
            "employment, the Employee shall not be employed by, or provide "
            "services to, a business that competes with the Company within "
            "the state in which the Employee was employed."
        ),
        "walk_away_line": (
            "Final position on restrictive covenants: no more than twelve "
            "(12) months, bounded to competing businesses and a stated "
            "geography. A longer or unbounded covenant ends the negotiation."
        ),
    },
    {
        "id": "PB-IPA-01",
        "title": "Invention assignment — preferred position (employment)",
        "clause_type": "ip_assignment",
        "text": (
            "Preferred (employment): the employee assigns inventions "
            "conceived in the course of employment and relating to the "
            "company's business. Inventions conceived before the employment "
            "are excluded — listed on a schedule of prior inventions when "
            "the employee has them — and work done entirely on the "
            "employee's own time and resources, unrelated to the business, "
            "stays theirs."
        ),
        "fallback_language": (
            "The Employee hereby assigns to the Company all inventions "
            "conceived or reduced to practice in the course of the "
            "Employee's employment and relating to the Company's business. "
            "Inventions conceived before the employment are excluded, and "
            "no assignment is made of inventions developed entirely on the "
            "Employee's own time and resources that do not relate to the "
            "Company's business."
        ),
    },
    {
        "id": "PB-IPA-02",
        "title": "Invention assignment — reaching backwards is a walk-away (employment)",
        "clause_type": "ip_assignment",
        "text": (
            "Walk-away: an assignment that reaches inventions conceived "
            "BEFORE the employment began, with no prior-invention carve-out "
            "— or that claims everything the employee makes on their own "
            "time with no limit to the company's business. The company "
            "cannot own what predates it, and a clause that tries invites "
            "the whole assignment to be challenged. Escalate to senior "
            "counsel and propose the bounded wording."
        ),
        "fallback_language": (
            "The Employee hereby assigns to the Company all inventions "
            "conceived or reduced to practice in the course of the "
            "Employee's employment and relating to the Company's business. "
            "Inventions conceived before the employment are excluded, and "
            "no assignment is made of inventions developed entirely on the "
            "Employee's own time and resources that do not relate to the "
            "Company's business."
        ),
        "walk_away_line": (
            "Final position on invention assignment: assignment is limited "
            "to inventions conceived in the course of employment and "
            "relating to the Company's business; prior inventions are "
            "excluded. An assignment reaching prior or unrelated "
            "own-time inventions ends the negotiation."
        ),
    },
    {
        "id": "PB-ARB-01",
        "title": "Dispute resolution — preferred position (employment)",
        "clause_type": "arbitration",
        "text": (
            "Preferred (employment): no mandatory arbitration — disputes "
            "stay in court unless both sides agree otherwise after a "
            "dispute arises. Acceptable fallback: binding arbitration "
            "that is MUTUAL, with carve-outs for injunctive relief and "
            "for statutory claims the law does not allow to be waived."
        ),
        "fallback_language": (
            "Any dispute arising out of the Employee's employment shall be "
            "resolved by binding arbitration, at either party's election. "
            "Nothing in this section prevents either party from seeking "
            "injunctive relief in court, or from bringing statutory claims "
            "that cannot lawfully be submitted to arbitration."
        ),
    },
    {
        "id": "PB-ARB-02",
        "title": "Dispute resolution — one-sided machinery is a walk-away (employment)",
        "clause_type": "arbitration",
        "text": (
            "Review required: a class, collective, or representative "
            "action waiver with no carve-out for statutory claims — the "
            "waiver's reach is a counsel call. Walk-away: arbitration "
            "that binds only the employee (with the waiver on top) while "
            "the company keeps its own claims in court — one-sided "
            "machinery, and the shape most likely to be held "
            "unenforceable. Escalate to senior counsel."
        ),
        "fallback_language": (
            "Any dispute arising out of the Employee's employment shall be "
            "resolved by binding arbitration, at either party's election. "
            "Nothing in this section prevents either party from seeking "
            "injunctive relief in court, or from bringing statutory claims "
            "that cannot lawfully be submitted to arbitration."
        ),
        "walk_away_line": (
            "Final position on dispute resolution: any arbitration must "
            "be mutual, with statutory and injunctive carve-outs. "
            "Arbitration binding only the employee ends the negotiation."
        ),
    },
    {
        "id": "PB-EMP-01",
        "title": "Employment terms — at-will preserved (employment)",
        "clause_type": "employment_terms",
        "text": (
            "Preferred (employment): employment is at will, stated "
            "plainly — either side may end it at any time, with or "
            "without cause — and nothing else in the letter takes that "
            "back. Severance, where offered, is stated in months of base "
            "salary; garden leave, where used, runs over the notice "
            "period and is paid."
        ),
        "fallback_language": (
            "The Employee's employment is at will: either the Employee or "
            "the Company may terminate the employment at any time, with "
            "or without cause or notice. Nothing in this letter creates a "
            "promise of employment for any period."
        ),
    },
    {
        "id": "PB-EMP-02",
        "title": "Employment terms — promised security needs review (employment)",
        "clause_type": "employment_terms",
        "text": (
            "Review required: a letter that promises what at-will "
            "employment cannot — termination only for cause, a guaranteed "
            "or 'permanent' term — whatever the at-will boilerplate "
            "elsewhere says, because the promise is what gets quoted back. "
            "Severance beyond twelve months of salary is likewise a "
            "counsel call. Escalate; either the promise comes out or the "
            "term is made deliberate."
        ),
        "fallback_language": (
            "The Employee's employment is at will: either the Employee or "
            "the Company may terminate the employment at any time, with "
            "or without cause or notice. Nothing in this letter creates a "
            "promise of employment for any period."
        ),
        "walk_away_line": (
            "Final position on employment terms: employment is at will "
            "and the letter says so without contradiction. A guaranteed "
            "term or for-cause-only promise ends the negotiation until "
            "counsel makes it deliberate."
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
