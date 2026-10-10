"""Typed schemas for the contract review agent.

Every boundary in the pipeline is a Pydantic model: the contract that
arrives, the clause units the segmenter cuts in code, the per-clause
extractions and playbook verdicts, the document-level assessment, the
drafts, and the final result that waits at counsel's gate.

The model never invents the numbers on these objects — clause verdicts
come from deterministic rules over the playbook matrix (see
``clause_review.py``), the document posture is counted in code (see
``facts.py``), and the model only does language work over them.

Intake is messy in the real world, so normalisation happens here, at
the boundary: contract-type and document-type aliases are folded to a
small vocabulary and odd values are coerced instead of rejected.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Intake normalisation
# ---------------------------------------------------------------------------

CONTRACT_TYPE_ALIASES = {
    "msa": "msa",
    "master_service_agreement": "msa",
    "master_services_agreement": "msa",
    "master agreement": "msa",
    "sow": "sow",
    "statement_of_work": "sow",
    "statement of work": "sow",
    "work_order": "sow",
    "nda": "nda",
    "non_disclosure_agreement": "nda",
    "nondisclosure_agreement": "nda",
    "confidentiality_agreement": "nda",
    "amendment": "amendment",
    "addendum": "amendment",
    "change_order": "amendment",
    "order_form": "order_form",
    "order form": "order_form",
    "purchase_order": "order_form",
    "po": "order_form",
    "dpa": "dpa",
    "data_processing_addendum": "dpa",
    "data processing addendum": "dpa",
    "data_processing_agreement": "dpa",
    "data processing agreement": "dpa",
    "data_protection_addendum": "dpa",
    "employment": "employment",
    "employment_agreement": "employment",
    "employment agreement": "employment",
    "employment_contract": "employment",
    "employment contract": "employment",
    "offer_letter": "employment",
    "offer letter": "employment",
    "employment_offer_letter": "employment",
    "employment offer letter": "employment",
}

CONTRACT_TYPES = {"msa", "sow", "nda", "amendment", "order_form", "dpa", "employment"}

DOC_TYPE_ALIASES = {
    "agreement": "agreement",
    "contract": "agreement",
    "main_agreement": "agreement",
    "draft": "agreement",
    "exhibit": "exhibit",
    "annex": "exhibit",
    "appendix": "exhibit",
    "schedule": "schedule",
    "signature_page": "signature_page",
    "signature_pages": "signature_page",
    "execution_page": "signature_page",
    "cover_email": "cover_email",
    "email": "cover_email",
    "redline": "redline",
    "prior_version": "redline",
}

DOC_TYPES = {"agreement", "exhibit", "schedule", "signature_page", "cover_email", "redline"}


def _fold(raw: str) -> tuple[str, str]:
    key = " ".join(str(raw or "").strip().lower().split())
    return key, key.replace(" ", "_")


def normalize_contract_type(raw: str) -> str:
    key, key_us = _fold(raw)
    return CONTRACT_TYPE_ALIASES.get(key) or CONTRACT_TYPE_ALIASES.get(key_us) or key_us


def normalize_doc_type(raw: str) -> str:
    key, key_us = _fold(raw)
    return DOC_TYPE_ALIASES.get(key) or DOC_TYPE_ALIASES.get(key_us) or key_us


def _coerce_field_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


class DocumentInput(BaseModel):
    doc_type: str
    document_id: str = ""
    raw_text: str = ""
    fields: dict[str, str] = Field(default_factory=dict)

    doc_type_provided: str = ""
    doc_type_flagged: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            raw = str(data.get("doc_type", ""))
            norm = normalize_doc_type(raw)
            data["doc_type_provided"] = raw
            data["doc_type"] = norm
            data["doc_type_flagged"] = norm not in DOC_TYPES
            fields = data.get("fields") or {}
            if isinstance(fields, dict):
                data["fields"] = {str(k): _coerce_field_value(v) for k, v in fields.items()}
        return data


class ContractInput(BaseModel):
    contract_id: str
    doc_type: str = "msa"  # msa | sow | nda | amendment | order_form | dpa | employment
    supersedes: str = ""  # contract_id of the previous negotiation round this draft replaces
    matter: str = ""  # the matter this contract belongs to: chains sharing a counterparty + matter tag form one matter (matter_brief.py)
    title: str = ""
    counterparty_name: str = ""
    our_role: str = ""  # vendor | customer | partner — the side we review from
    sender_name: str = ""
    cover_email_text: str = ""  # untrusted: counterparty-authored
    notes: str = ""
    received_at: str = ""  # ISO datetime/date
    contract_text: str = ""  # convenience: agreement text when not carried in documents
    documents: list[DocumentInput] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_type(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if data.get("doc_type"):
                data["doc_type"] = normalize_contract_type(str(data["doc_type"]))
        return data

    def agreement_text(self) -> str:
        """The text to segment: the agreement document's text wins;
        ``contract_text`` is the fallback for single-text intake."""
        for doc in self.documents:
            if doc.doc_type == "agreement" and doc.raw_text.strip():
                return doc.raw_text
        return self.contract_text


# ---------------------------------------------------------------------------
# Clause segmentation (code only)
# ---------------------------------------------------------------------------

CLAUSE_TYPES = [
    "liability_cap",
    "indemnity",
    "ip_ownership",
    "confidentiality",
    "term_termination",
    "payment_terms",
    "governing_law",
    "data_protection",
    # The standalone-DPA family (doc_type "dpa"): a data processing
    # addendum reviewed as the primary document gets its own clause
    # matrix — the processor machinery an MSA's single data
    # protection clause only points at.
    "subprocessor_terms",
    "data_transfer",
    "audit_rights",
    "data_deletion",
    # The employment family (doc_type "employment"): an offer letter
    # or employment agreement reviewed as the primary document gets
    # its own clause matrix — restrictive covenants, invention
    # assignment, dispute machinery, and the at-will core.
    "non_compete",
    "ip_assignment",
    "arbitration",
    "employment_terms",
    "limitation_of_warranty",
    "assignment",
    "other",
]


class ClauseUnit(BaseModel):
    clause_id: str  # C-01, C-02, ... in document order
    ref: str = ""  # numbering/heading label as written, e.g. "7.2" or "ARTICLE V"
    heading: str = ""
    text: str = ""
    document_id: str = ""


# ---------------------------------------------------------------------------
# Per-clause extraction
# ---------------------------------------------------------------------------

class ClauseExtraction(BaseModel):
    clause_id: str
    clause_type: str = "other"
    type_confidence: float = 0.0
    type_source: str = "rules"  # rules | llm
    signals: dict[str, Any] = Field(default_factory=dict)  # typed facts found in the clause
    key_facts: list[str] = Field(default_factory=list)
    discrepancies: list[str] = Field(default_factory=list)  # rules-vs-LLM disagreements
    note: str = ""


# ---------------------------------------------------------------------------
# Per-clause playbook verdict + cross-check
# ---------------------------------------------------------------------------

POSITIONS = ["preferred", "acceptable_fallback", "review_required", "walk_away"]

POSITION_SEVERITY = {name: idx for idx, name in enumerate(POSITIONS)}


class ClauseVerdict(BaseModel):
    clause_id: str
    clause_type: str = "other"
    position: str = "review_required"
    confidence: float = 0.0
    rationale: str = ""
    rule_position: str = ""
    llm_position: Optional[str] = None
    llm_confidence: Optional[float] = None
    resolution: str = "not_run"  # agree | rules_authoritative | llm_adopted | rules_only | not_run | escalated_disagreement
    escalated: bool = False  # a disagreement pushed this clause to at least review_required
    playbook_refs: list[str] = Field(default_factory=list)
    fallback_language: str = ""  # proposed fallback wording from the playbook, when relevant


class ClauseDiff(BaseModel):
    """One flagged clause, side by side: what the counterparty wrote
    vs the fallback language the playbook proposes. Assembled in code
    (``clause_diffs.py``) — neither side is model-rewritten."""

    clause_id: str
    ref: str = ""
    heading: str = ""
    clause_type: str = "other"
    position: str = ""
    counterparty_text: str = ""
    playbook_fallback: str = ""  # "" = the playbook proposes none; counsel drafts
    playbook_refs: list[str] = Field(default_factory=list)
    rationale: str = ""
    # Fallback tiers (fallback_tiers.py): which concession the packet
    # proposes for this clause, chosen in code from the round number
    # and the chain's re-trade flags. 0 = no proposal (preferred).
    proposed_tier: int = 0  # 1 = first fallback | 2 = walk-away line
    tier_name: str = ""
    tier_language: str = ""  # the language of the proposed tier
    tier_reason: str = ""
    pattern_escalated: bool = False  # a reflection pattern (below) drove this clause to the walk-away line


class ProposedEdit(BaseModel):
    """One concrete edit the packet proposes for a flagged clause
    (``proposed_edits.py``): a tracked-changes style instruction —
    replace this sentence with that language, insert this language
    after that sentence, replace the clause wholesale, or counsel
    drafts — computed in code by aligning the clause diff's
    tier-appropriate fallback language against the counterparty's
    own sentences. The model may phrase fallback language upstream;
    the assignment of language to sentences is code's, and every
    edit is a draft for counsel, never a sent redline."""

    clause_id: str = ""
    clause_type: str = "other"
    position: str = ""
    tier: int = 0  # the ClauseDiff tier this edit carries (0 = no tier proposal)
    kind: str = ""  # replace_sentence | insert_after | replace_clause | counsel_draft
    original_sentence: str = ""  # the counterparty sentence to replace ("" for inserts / counsel-draft)
    anchor_sentence: str = ""  # for insert_after: the sentence the new language follows
    replacement_text: str = ""  # the proposed language ("" for counsel_draft)
    alignment: str = ""  # sentence | clause_fallback | none — how the pairing was made
    note: str = ""


EDIT_OUTCOMES = ["accepted", "modified", "rejected"]


class EditOutcome(BaseModel):
    """Counsel's per-edit decision on one proposed edit, recorded on
    the record (round 6): the packet-level gate decision says whether
    the packet may go out; this says, edit by edit, what counsel
    settled with the counterparty — accepted as proposed, modified
    (``modified_text`` carries the language actually settled on, when
    counsel records it), or rejected. Outcomes are bookkeeping for
    the negotiation: they decide nothing by themselves, but the next
    round's reflection check reads the accepted/modified ones (see
    ``EditReflection``)."""

    edit_index: int  # position in the result's proposed_edits list
    clause_id: str = ""  # snapshot from the edit, for display robustness
    clause_type: str = "other"
    kind: str = ""
    outcome: str = ""  # accepted | modified | rejected
    note: str = ""  # counsel's note ("agreed on the call, 12 Oct")
    modified_text: str = ""  # the settled language, when outcome == modified
    marked_by: str = ""
    marked_at: str = ""  # ISO timestamp


class EditOutcomeInput(BaseModel):
    """One outcome as submitted by counsel (API / gate payload)."""

    edit_index: int
    outcome: str  # accepted | modified | rejected
    note: str = ""
    modified_text: str = ""


class EditReflection(BaseModel):
    """One accepted/modified edit from the previous round, checked
    against the follow-up round's draft text (``edit_reflection.py``):
    did the language counsel agreed actually land in the new draft?

    The check is textual and says so: the agreed language is
    checked sentence by sentence against the aligned clause of the
    new draft, each sentence measured on its NEW content (the words
    the edit would change relative to the prior draft).
    ``reflected`` = the agreed sentences are there;
    ``agreed_not_reflected`` = they are not — counsel agreed this on
    the call and the draft does not show it, the classic
    agreed-but-not-reflected trap; ``not_checkable`` = there is no
    agreed language on record to check (a counsel-draft edit, or
    language with no measurable content). Counsel judges substance;
    this flags where to look."""

    clause_type: str = "other"
    edit_index: int = -1  # index into the PREVIOUS round's proposed_edits
    prior_clause_id: str = ""
    clause_id: str = ""  # the aligned clause in this round ("" when the clause is gone)
    kind: str = ""
    outcome: str = ""  # accepted | modified
    status: str = ""  # reflected | agreed_not_reflected | not_checkable
    coverage: Optional[float] = None  # share of the agreed language's checkable sentences that landed, 0..1
    detail: str = ""


class WalkAwayOutcome(BaseModel):
    """One walk-away line a decided packet proposed, tracked to its
    outcome (``walkaway.py``, round 9). Captured on the decision
    record at the moment counsel decides a contract whose packet
    proposed tier-2 lines: the line itself, the position the clause
    sat at when counsel decided, and the decision. The outcome then
    resolves against the FOLLOW-UP round — the counterparty's next
    draft is the evidence of whether the line held:

    ``held`` = the follow-up places the clause at an acceptable
    position (preferred or acceptable_fallback): the final text is
    at or above the line. ``softened`` = review_required: off the
    walk-away text, short of an acceptable position. ``dropped`` =
    still walk_away: the final text sits beyond the line the packet
    proposed. ``clause_removed`` = the clause is gone from the
    follow-up — the line was neither held nor tested. ``pending`` =
    no follow-up round is in the store yet. Counsel's decision is
    the anchor; the follow-up draft is the evidence — the concession
    profile reads these as decision evidence, not verdict movement.
    """

    clause_id: str = ""
    clause_type: str = "other"
    line_language: str = ""  # the walk-away line the packet proposed
    position_at_decision: str = ""  # the clause's position when counsel decided
    decision: str = ""  # approved | rejected — the decision that captured this line
    decided_by: str = ""
    captured_at: str = ""  # ISO timestamp of the decision
    status: str = "pending"  # pending | held | softened | dropped | clause_removed
    final_position: str = ""  # the follow-up round's position for the clause
    resolved_in: str = ""  # the follow-up round's contract_id, once resolved
    detail: str = ""
    # Who moved the line before it went out (round 10, walkaway.py's
    # classify_line_origin): the join with the decision's own edit
    # outcomes. sent_intact = counsel accepted the line's edits as
    # proposed; modified_by_counsel / rejected_by_counsel = counsel
    # changed or dropped the line at their own desk; unmarked = no
    # per-edit outcomes on record; no_edit_on_record = the packet
    # proposed the line without a concrete tier-2 edit. The status
    # says what the counterparty's draft did; the origin says what
    # counsel's desk did first — together they say where a lost
    # line was actually lost.
    origin: str = ""
    origin_detail: str = ""


class ReflectionPattern(BaseModel):
    """A clause type whose agreed edits keep not landing
    (``reflection_patterns.py``): counsel agreed an edit on this
    clause type, the follow-up draft did not show it, counsel agreed
    the edit again on the next round, and that draft did not show it
    either — flagged ``agreed_not_reflected`` in TWO consecutive
    follow-ups. One miss is a drafting oversight; the same ask
    missing twice running is a pattern, and the packet answers it
    differently: the clause's proposed tier escalates to the
    walk-away line (``fallback_tiers.py``), and the brief names the
    pattern with its round history. Keyed by clause type — the same
    ask, re-agreed round after round, is what counsel experiences as
    one edit that never lands."""

    clause_type: str = "other"
    flagged_in: list[str] = Field(default_factory=list)  # follow-up contract ids that raised the flag, oldest → newest
    agreed_in: list[str] = Field(default_factory=list)  # the rounds whose agreed edits those follow-ups checked
    flags: int = 0  # total agreed_not_reflected flags for this type across the chain
    detail: str = ""


# ---------------------------------------------------------------------------
# Negotiation-round diff (computed in code — verdict movement)
# ---------------------------------------------------------------------------

class ClauseMovement(BaseModel):
    """One clause aligned across two negotiation rounds: where its
    playbook position stood in the superseded round and where it
    stands now. ``movement`` is the severity comparison — positions
    only ever move along the ladder in ``POSITIONS``."""

    clause_type: str = "other"
    heading: str = ""
    prior_clause_id: str = ""  # "" when the clause is new in this round
    clause_id: str = ""  # "" when the clause was removed in this round
    prior_ref: str = ""
    ref: str = ""
    prior_position: str = ""  # "" when new
    position: str = ""  # "" when removed
    movement: str = ""  # improved | worsened | unchanged | new | removed


class RoundDiff(BaseModel):
    """Verdict movement between this contract and the round it
    supersedes (``round_diff.py``): what the last negotiation round
    actually won, clause by clause, plus the counts."""

    previous_contract_id: str = ""
    contract_id: str = ""
    movements: list[ClauseMovement] = Field(default_factory=list)
    improved: int = 0
    worsened: int = 0
    unchanged: int = 0
    new_clauses: int = 0
    removed_clauses: int = 0
    summary: list[str] = Field(default_factory=list)


class RoundPosition(BaseModel):
    """One round's seat in a clause's movement history: the position
    the clause held in that round (``""`` when the clause was absent
    from that round's draft)."""

    contract_id: str = ""
    clause_id: str = ""
    position: str = ""


class ClauseHistory(BaseModel):
    """One clause tracked across a whole negotiation chain
    (``round_diff.build_round_chain``): its position in every round,
    the movement at each step, and whether it was RE-TRADED — it
    improved in an earlier round and worsened again later. A re-trade
    is a finding, not a footnote: counsel settled that ground once
    and the counterparty took it back."""

    clause_type: str = "other"
    heading: str = ""
    track: list[RoundPosition] = Field(default_factory=list)
    movements: list[str] = Field(default_factory=list)  # one per round-to-round step
    cumulative_movement: str = ""  # improved | worsened | unchanged | new | removed
    re_traded: bool = False
    re_trade_detail: str = ""


class RoundChain(BaseModel):
    """Verdict movement across an entire negotiation chain — every
    round from the first draft to this one, not just the last step.
    Attached when the chain has three or more rounds (with two, the
    pairwise ``RoundDiff`` already says everything)."""

    contract_ids: list[str] = Field(default_factory=list)  # oldest → newest
    rounds: int = 0
    histories: list[ClauseHistory] = Field(default_factory=list)
    re_trades: int = 0
    re_traded_clause_types: list[str] = Field(default_factory=list)
    cumulative_improved: int = 0
    cumulative_worsened: int = 0
    cumulative_unchanged: int = 0
    rounds_to_no_walk_away: Optional[int] = None  # 1-based; None = never reached
    summary: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Obligations register (extracted in code — dates validated/derived)
# ---------------------------------------------------------------------------

class Obligation(BaseModel):
    """One dated or recurring duty the contract creates, with its
    source clause. Built in code from extraction signals + clause
    text (``obligations.py``): day/hour counts are the code-extracted
    signal values, and a ``date`` is either quoted from the clause or
    derived by date arithmetic (``computed`` says which)."""

    kind: str  # renewal | notice_period | payment | reporting | breach_notice | confidentiality_term | term
    description: str = ""
    detail: str = ""  # the operative value as written, e.g. "120 days' written notice"
    days: Optional[int] = None
    hours: Optional[int] = None
    date: Optional[str] = None  # ISO date: explicit in the clause, or computed
    computed: bool = False  # True when `date` was derived (e.g. renewal date − notice days)
    source_clause_id: str = ""
    source_ref: str = ""


# ---------------------------------------------------------------------------
# Exhibit consistency (cross-document checks, in code)
# ---------------------------------------------------------------------------

class ExhibitCheck(BaseModel):
    """One promise the agreement makes about an attached exhibit,
    checked against the exhibit's own text (``exhibit_review.py``).
    ``status``: consistent | mismatch (the exhibit says something
    different) | missing_in_exhibit (the exhibit is silent on it) |
    exhibit_missing (the promised exhibit is not in the package)."""

    name: str  # breach_notice_window | subprocessor_list | order_form_fees_total | order_form_payment_terms | order_form_term | sow_deliverables | sow_delivery_dates | sow_acceptance_period
    expectation: str = ""
    exhibit_document_id: str = ""  # "" when no DPA exhibit is attached
    promised: str = ""  # the value as the clause states it
    found: str = ""  # the value as the exhibit states it ("" when silent)
    status: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# Computed document facts (code only — never the model)
# ---------------------------------------------------------------------------

class ContractFacts(BaseModel):
    clause_count: int = 0
    clauses_by_type: dict[str, int] = Field(default_factory=dict)
    positions_count: dict[str, int] = Field(default_factory=dict)
    worst_position: str = "preferred"
    posture: str = "aligned"  # aligned | negotiable | escalation_required
    missing_standard_clauses: list[str] = Field(default_factory=list)
    critical_absences: list[str] = Field(default_factory=list)
    unheaded_units: int = 0
    discrepancy_count: int = 0
    discrepancies: list[str] = Field(default_factory=list)
    exhibits_referenced: list[str] = Field(default_factory=list)
    exhibits_missing: list[str] = Field(default_factory=list)
    exhibit_mismatches: list[str] = Field(default_factory=list)  # failed exhibit-check names
    exhibit_missing_checks: list[str] = Field(default_factory=list)  # failed names whose status is exhibit_missing
    exhibit_absent: bool = False  # a promised exhibit is not in the package at all
    signature_pages_present: bool = False
    package_complete: bool = True
    contract_age_days: int = 0


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

class RetrievedPlaybook(BaseModel):
    playbook_id: str
    title: str = ""
    text: str = ""
    clause_type: str = ""
    fallback_language: str = ""  # tier 1: the entry's proposed fallback wording
    walk_away_line: str = ""  # tier 2: the entry's final-position line, when it carries one
    score: float = 0.0
    sources: list[str] = Field(default_factory=list)  # retrieval provenance: keyword / semantic

    @property
    def policy_id(self) -> str:  # convenience alias for shared code paths
        return self.playbook_id

    @property
    def guideline_id(self) -> str:  # convenience alias (engine code carried over)
        return self.playbook_id


# ---------------------------------------------------------------------------
# Assessment / options / drafts / checks
# ---------------------------------------------------------------------------

class Assessment(BaseModel):
    summary: str = ""
    risk_factors: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    source: str = "template"  # llm | template
    note: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


# Alias: parts of the shared engine refer to this step as the diagnosis.
Diagnosis = Assessment


class ClauseOption(BaseModel):
    clause_id: str = ""
    kind: str  # accept_as_preferred | propose_fallback_language | escalate_to_senior_counsel | request_counterparty_info
    title: str = ""
    description: str = ""
    score: float = 0.0
    recommended: bool = False
    # The score, decomposed (options.py): position + posture + info +
    # history points. ``history`` is the concession-history term —
    # how this counterparty has actually moved on this clause type —
    # capped small enough that it can never dominate the
    # playbook-driven terms; it is shown, never hidden in the total.
    breakdown: dict[str, float] = Field(default_factory=dict)


# Alias used by shared engine code.
RecoveryOption = ClauseOption


class Drafts(BaseModel):
    counterparty_response: str = ""
    review_packet: str = ""
    subject: str = ""


class VerifyResult(BaseModel):
    grounded: bool = True
    issues: list[str] = Field(default_factory=list)
    source: str = "checklist"  # llm | checklist
    note: str = ""


class ReviewResult(BaseModel):
    verdict: str = "pass"  # pass | concerns | block
    findings: list[str] = Field(default_factory=list)
    source: str = "checklist"  # llm | checklist
    enabled: bool = True


class GuardrailCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class Validation(BaseModel):
    passed: bool = True
    checks: list[GuardrailCheck] = Field(default_factory=list)


class InformationRequest(BaseModel):
    needed: bool = False
    missing_items: list[str] = Field(default_factory=list)
    message: str = ""


class AutonomyRecommendation(BaseModel):
    eligible_for_auto_triage: bool = False
    reasons: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class RunTelemetry(BaseModel):
    backend: str = "mock"
    model: str = ""
    model_calls: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    latency_seconds: float = 0.0
    budget: Optional[dict[str, Any]] = None


class TraceStep(BaseModel):
    name: str
    title: str = ""
    details: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Final result
# ---------------------------------------------------------------------------

class AgentResult(BaseModel):
    contract_id: str
    status: str = "awaiting_counsel"  # awaiting_counsel | approved | rejected
    counterparty_name: str = ""
    doc_type: str = ""
    clauses: list[ClauseUnit] = Field(default_factory=list)
    extractions: list[ClauseExtraction] = Field(default_factory=list)
    facts: ContractFacts = Field(default_factory=ContractFacts)
    injection_flags: list[str] = Field(default_factory=list)
    clause_verdicts: list[ClauseVerdict] = Field(default_factory=list)
    clause_diffs: list[ClauseDiff] = Field(default_factory=list)
    proposed_edits: list[ProposedEdit] = Field(default_factory=list)
    edit_outcomes: list[EditOutcome] = Field(default_factory=list)  # counsel's per-edit decisions (recorded post-analysis)
    walk_away_outcomes: list[WalkAwayOutcome] = Field(default_factory=list)  # walk-away lines this packet proposed, captured at counsel's decision and resolved against the follow-up round
    edit_reflection: list[EditReflection] = Field(default_factory=list)  # prior round's accepted edits, checked against this draft
    reflection_patterns: list[ReflectionPattern] = Field(default_factory=list)  # clause types flagged not-reflected in two consecutive follow-ups
    round_diff: Optional[RoundDiff] = None  # set when intake named a superseded round
    round_chain: Optional[RoundChain] = None  # set when the chain has 3+ rounds
    exhibit_checks: list[ExhibitCheck] = Field(default_factory=list)
    obligations: list[Obligation] = Field(default_factory=list)
    playbook: list[RetrievedPlaybook] = Field(default_factory=list)
    vector_store: str = ""
    assessment: Assessment = Field(default_factory=Assessment)
    options: list[ClauseOption] = Field(default_factory=list)
    drafts: Optional[Drafts] = None
    verification: Optional[VerifyResult] = None
    review: Optional[ReviewResult] = None
    reviewer_blocked: bool = False
    validation: Optional[Validation] = None
    repair_attempted: bool = False
    repaired: bool = False
    repair_attempts: int = 0
    original_validation: Optional[Validation] = None
    information_request: Optional[InformationRequest] = None
    needs_information: bool = False
    autonomy: AutonomyRecommendation = Field(default_factory=AutonomyRecommendation)
    telemetry: Optional[RunTelemetry] = None
    trace: list[TraceStep] = Field(default_factory=list)
    decision: Optional[str] = None  # approved | rejected
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None
    decided_at: Optional[str] = None  # ISO timestamp of counsel's decision
    dispatch_status: Optional[str] = None  # sent | failed | None
    external_action_taken: bool = False
    error: Optional[str] = None


class AnalyzeRequest(BaseModel):
    contract: ContractInput


class DecisionRequest(BaseModel):
    actor: Optional[str] = None
    approver: Optional[str] = None
    reviewer: Optional[str] = None
    reason: Optional[str] = None
    # Per-edit outcomes recorded together with an approval — counsel
    # settles the packet and marks what was agreed in one motion.
    edit_outcomes: Optional[list[EditOutcomeInput]] = None

    def actor_name(self) -> Optional[str]:
        return self.actor or self.approver or self.reviewer


class EditOutcomesRequest(BaseModel):
    """Counsel marking per-edit outcomes on a stored record, outside
    (or ahead of) the packet-level gate decision."""

    actor: Optional[str] = None
    approver: Optional[str] = None
    reviewer: Optional[str] = None
    outcomes: list[EditOutcomeInput] = Field(default_factory=list)

    def actor_name(self) -> Optional[str]:
        return self.actor or self.approver or self.reviewer


class AssignRequest(BaseModel):
    """Queue ownership: an operator (``actor``) records which member
    of counsel's team (``assignee``) holds an awaiting contract.
    Assignment is bookkeeping — it never decides anything."""

    actor: Optional[str] = None
    assignee: Optional[str] = None


# ---------------------------------------------------------------------------
# Playbook proposals (coverage-driven — the playbook stays human-owned)
# ---------------------------------------------------------------------------

class PlaybookProposalEntry(BaseModel):
    """One candidate playbook entry inside a proposal: the same shape
    as a shipped ``playbook_data`` entry, drafted in code from the
    nearest covered clause type's family (``playbook_proposals.py``).
    It is text counsel reviews, edits, and only then activates —
    until activation it exists nowhere but the proposal record."""

    id: str  # PROP-<TYPE>-NN — never collides with shipped PB-* ids
    title: str = ""
    clause_type: str = ""
    text: str = ""
    fallback_language: str = ""
    walk_away_line: str = ""


class PlaybookProposal(BaseModel):
    """A drafted playbook addition for a clause type that keeps
    arriving flagged with no fallback language to offer — a coverage
    gap in the playbook, surfaced by ``playbook_coverage`` and
    drafted by ``playbook_proposals.py``. Stored as PROPOSED;
    counsel activates or dismisses it in the console. Activation —
    a human act, recorded with the actor — is the only way a
    proposal's entries join the active playbook
    (``playbook_registry.py``); the agent never activates its own
    drafts, and a dismissed proposal is never re-drafted. An
    activated proposal may later be RETIRED by counsel, with a
    reason: the lifecycle closes, the entries leave the playbook,
    and the effectiveness record is preserved."""

    proposal_id: str
    clause_type: str = ""
    status: str = "proposed"  # proposed | activated | dismissed | retired
    based_on_type: str = ""  # the covered clause type whose family shaped the draft
    based_on_entry_ids: list[str] = Field(default_factory=list)
    basis: str = ""  # why that donor: the stated similarity rule's reading
    entries: list[PlaybookProposalEntry] = Field(default_factory=list)
    flagged: int = 0  # stored verdicts that flagged this type
    without_fallback: int = 0  # ...and had no fallback language to offer
    example_contract_ids: list[str] = Field(default_factory=list)
    created_at: str = ""
    decided_by: str = ""
    decided_at: str = ""
    decision_note: str = ""
    # Retirement (round 10): an ACTIVATED proposal counsel later
    # withdraws, with the reason on record. The entries leave the
    # active playbook; the proposal — and its effectiveness record —
    # stay, so what the entry did while live remains auditable.
    retired_by: str = ""
    retired_at: str = ""
    retire_reason: str = ""


class ProposalDecisionRequest(BaseModel):
    """Counsel's decision on a playbook proposal (activate/dismiss).
    The actor is required: activating a playbook entry is a human
    decision with a name on it, like every gate in this blueprint."""

    actor: Optional[str] = None
    note: Optional[str] = None
