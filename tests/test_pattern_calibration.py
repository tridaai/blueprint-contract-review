"""Pattern-term calibration (round 10): the reflection-pattern
score term is a stated guess of six points. This report measures
it against the stored record — where it was applied, whether it
ever changed which option the packet recommended, and what
counsel did with the escalations on decided records. A type whose
term was applied repeatedly and decided nothing is flagged a
calibration candidate FOR COUNSEL; the report itself moves
nothing."""

from __future__ import annotations

from contract_agent.options import PATTERN_TERM_CAP
from contract_agent.pattern_calibration import pattern_calibration
from contract_agent.schemas import (
    AgentResult,
    ClauseOption,
    ClauseVerdict,
    EditOutcome,
    ProposedEdit,
)
from contract_agent.service import ContractService
from contract_agent.store import ApprovalRecord, InMemoryStore
from helpers import contract


def _option(clause_id, kind, score, pattern=0.0, recommended=False) -> ClauseOption:
    return ClauseOption(
        clause_id=clause_id,
        kind=kind,
        score=score,
        breakdown={"position": score - pattern, "pattern": pattern},
        recommended=recommended,
    )


def _record(cid, options, verdicts=(), **result_kwargs) -> ApprovalRecord:
    return ApprovalRecord(
        result=AgentResult(
            contract_id=cid,
            options=list(options),
            clause_verdicts=list(verdicts),
            **result_kwargs,
        )
    )


def _verdict(clause_id="C-2", clause_type="payment_terms") -> ClauseVerdict:
    return ClauseVerdict(clause_id=clause_id, clause_type=clause_type, position="review_required")


def _row(report, clause_type="payment_terms") -> dict:
    return next(r for r in report["clause_types"] if r["clause_type"] == clause_type)


# ---------------------------------------------------------------------------
# Applied, decisive, and the difference between them
# ---------------------------------------------------------------------------


def test_term_that_flips_the_recommendation_is_decisive():
    # Escalate scores 82 on the playbook terms; the competing
    # proposal scores 85. The six-point term carries escalation to
    # 88 and the recommendation — remove it and the outcome flips.
    records = [
        _record(
            "CTR-CAL-1",
            [
                _option("C-2", "escalate_to_senior_counsel", 88.0, pattern=PATTERN_TERM_CAP, recommended=True),
                _option("C-2", "propose_fallback_language", 85.0),
            ],
            [_verdict()],
        )
    ]
    report = pattern_calibration(records)
    row = _row(report)
    assert row["applications"] == 1
    assert row["decisive"] == 1
    assert row["applied_not_decisive"] == 0
    assert row["calibration_candidate"] is False
    assert report["term_cap"] == PATTERN_TERM_CAP


def test_term_that_only_pads_a_win_is_not_decisive():
    # Escalation wins 82–76 on the playbook terms alone; the term
    # rides along to 88 and changes nothing.
    records = [
        _record(
            "CTR-CAL-2",
            [
                _option("C-2", "escalate_to_senior_counsel", 88.0, pattern=PATTERN_TERM_CAP, recommended=True),
                _option("C-2", "propose_fallback_language", 76.0),
            ],
            [_verdict()],
        )
    ]
    row = _row(pattern_calibration(records))
    assert row["applications"] == 1
    assert row["decisive"] == 0
    assert row["applied_not_decisive"] == 1


def test_term_on_an_option_counsel_never_sees_recommended_is_not_decisive():
    records = [
        _record(
            "CTR-CAL-3",
            [
                _option("C-1", "escalate_to_senior_counsel", 94.0, recommended=True),
                _option("C-2", "escalate_to_senior_counsel", 70.0, pattern=PATTERN_TERM_CAP),
            ],
            [_verdict("C-1", "liability_cap"), _verdict()],
        )
    ]
    report = pattern_calibration(records)
    assert _row(report)["decisive"] == 0
    # The liability option carried no term: it is not an application,
    # and its type earns no row in the report at all.
    assert [r["clause_type"] for r in report["clause_types"]] == ["payment_terms"]


def test_options_without_the_term_are_not_applications():
    records = [
        _record(
            "CTR-CAL-4",
            [_option("C-2", "escalate_to_senior_counsel", 82.0, recommended=True)],
            [_verdict()],
        )
    ]
    report = pattern_calibration(records)
    assert report["applications"] == 0
    assert report["clause_types"] == []
    assert report["calibration_candidates"] == []


# ---------------------------------------------------------------------------
# The candidate rule: applied enough, decisive never
# ---------------------------------------------------------------------------


def _padding_record(cid: str) -> ApprovalRecord:
    return _record(
        cid,
        [
            _option("C-2", "escalate_to_senior_counsel", 88.0, pattern=PATTERN_TERM_CAP, recommended=True),
            _option("C-2", "propose_fallback_language", 76.0),
        ],
        [_verdict()],
    )


def test_repeatedly_indecisive_term_becomes_a_calibration_candidate():
    report = pattern_calibration([_padding_record("CTR-CAL-5"), _padding_record("CTR-CAL-6")])
    row = _row(report)
    assert row["applications"] == 2
    assert row["contracts"] == 2
    assert row["decisive"] == 0
    assert row["calibration_candidate"] is True
    assert report["calibration_candidates"] == ["payment_terms"]
    assert "never changed" in row["detail"]
    # The report proposes; it does not reach for the cap.
    assert report["term_cap"] == PATTERN_TERM_CAP


def test_a_single_application_is_below_the_candidate_threshold():
    report = pattern_calibration([_padding_record("CTR-CAL-7")])
    assert _row(report)["calibration_candidate"] is False
    assert report["calibration_candidates"] == []


# ---------------------------------------------------------------------------
# Follow-through: what counsel did with the escalation, on decided records
# ---------------------------------------------------------------------------


def _decided_record(cid: str, outcome: str | None) -> ApprovalRecord:
    edits = [
        ProposedEdit(clause_id="C-2", clause_type="payment_terms", tier=2, kind="replace_clause")
    ]
    outcomes = (
        [EditOutcome(edit_index=0, clause_id="C-2", clause_type="payment_terms", outcome=outcome)]
        if outcome
        else []
    )
    return _record(
        cid,
        [
            _option("C-2", "escalate_to_senior_counsel", 88.0, pattern=PATTERN_TERM_CAP, recommended=True),
            _option("C-2", "propose_fallback_language", 76.0),
        ],
        [_verdict()],
        decision="approved",
        proposed_edits=edits,
        edit_outcomes=outcomes,
    )


def test_follow_through_reads_the_tier_two_edit_outcomes():
    report = pattern_calibration(
        [
            _decided_record("CTR-CAL-8", "accepted"),
            _decided_record("CTR-CAL-9", "rejected"),
            _decided_record("CTR-CAL-10", None),
            # An undecided record with the term: an application, but
            # no follow-through to read.
            _padding_record("CTR-CAL-11"),
        ]
    )
    row = _row(report)
    assert row["applications"] == 4
    assert row["decided_applications"] == 3
    assert row["sent_by_counsel"] == 1
    assert row["rejected_by_counsel"] == 1
    assert row["unmarked"] == 1


# ---------------------------------------------------------------------------
# End to end: a real pattern chain reaches the report (and /metrics)
# ---------------------------------------------------------------------------

_NET90 = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Payment Terms
The Customer shall pay all invoices within ninety (90) days of the invoice date.
"""


def test_real_chain_applications_are_counted_and_metrics_carry_the_headline():
    from contract_agent.schemas import EditOutcomeInput

    svc = ContractService(store=InMemoryStore(), checkpointer=False)

    def agree(result) -> None:
        index = next(
            i for i, e in enumerate(result.proposed_edits)
            if e.clause_type == "payment_terms"
        )
        svc.record_edit_outcomes(
            result.contract_id,
            [EditOutcomeInput(edit_index=index, outcome="accepted")],
            actor="C. Ounsel",
        )

    r1 = svc.analyze(contract(_NET90, contract_id="CTR-CAL-E1"))
    agree(r1)
    r2 = svc.analyze(contract(_NET90, contract_id="CTR-CAL-E2", supersedes="CTR-CAL-E1"))
    agree(r2)
    svc.analyze(contract(_NET90, contract_id="CTR-CAL-E3", supersedes="CTR-CAL-E2"))
    svc.analyze(contract(_NET90, contract_id="CTR-CAL-E4", supersedes="CTR-CAL-E3"))
    report = svc.pattern_calibration()
    row = _row(report)
    # Round 4 was scored with the pattern in force (round 3's
    # options predate it): exactly one application, on the payment
    # clause's escalation, and it padded a win the playbook terms
    # had already decided — applied, honestly not decisive.
    assert row["applications"] == 1
    assert row["decisive"] == 0
    metrics = svc.metrics()
    assert metrics["pattern_calibration"]["applications"] == 1
    assert metrics["pattern_calibration"]["calibration_candidates"] == []
