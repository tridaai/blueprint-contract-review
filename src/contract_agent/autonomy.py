"""Autonomy recommendation: should reviews like this skip the gate?

After every run the pipeline prints a recommendation: whether this
run *would have been* eligible for auto-triage (no counsel gate)
under a conservative policy — and the reasons either way. It is a
recommendation only: nothing in this blueprint acts on it. The
counsel gate is unconditional; this exists so an adopting team can
see, case by case, what a future autonomy policy would have to be
comfortable with before they ever consider one.

Eligibility is deliberately narrow — ALL of:

- every clause at ``preferred`` or ``acceptable_fallback`` and no
  ``walk_away`` anywhere;
- no missing standard clauses (nothing absent to not-see);
- the package is complete (no exhibits or signature pages missing);
- guardrails passed on the first draft (no repair was needed);
- no clause verdict was escalated by a rules/LLM disagreement;
- the independent reviewer did not block.
"""

from __future__ import annotations

from .schemas import (
    AutonomyRecommendation,
    ClauseVerdict,
    ContractFacts,
    ReviewResult,
    Validation,
)


def recommend_autonomy(
    *,
    verdicts: list[ClauseVerdict],
    facts: ContractFacts,
    validation: Validation,
    repair_attempted: bool,
    review: ReviewResult | None,
    reviewer_blocked: bool,
) -> AutonomyRecommendation:
    reasons: list[str] = []
    blockers: list[str] = []

    bad = [v for v in verdicts if v.position in {"review_required", "walk_away"}]
    if not bad:
        reasons.append("every clause is at preferred or acceptable_fallback")
    else:
        blockers.append(
            "clause(s) at review_required/walk_away: "
            + ", ".join(f"{v.clause_id} ({v.position})" for v in bad)
        )

    if not facts.missing_standard_clauses:
        reasons.append("no standard clauses are missing")
    else:
        blockers.append(
            "missing standard clauses: " + ", ".join(facts.missing_standard_clauses)
        )

    if facts.package_complete:
        reasons.append("the package is complete")
    else:
        blockers.append("the package is incomplete (exhibits or signature pages missing)")

    if validation.passed:
        reasons.append("guardrails passed")
    else:
        blockers.append("guardrails failed")

    if repair_attempted:
        blockers.append("the draft needed a repair pass")
    else:
        reasons.append("no repair pass was needed")

    escalated = [v.clause_id for v in verdicts if v.escalated]
    if escalated:
        blockers.append(
            "clause verdict(s) escalated by a rules/LLM disagreement: "
            + ", ".join(escalated)
        )
    else:
        reasons.append("no clause verdict was escalated by a cross-check disagreement")

    if reviewer_blocked:
        blockers.append("the independent reviewer blocked the draft")
    elif review is not None and review.enabled:
        reasons.append("the independent reviewer did not block")

    return AutonomyRecommendation(
        eligible_for_auto_triage=not blockers,
        reasons=reasons,
        blockers=blockers,
    )
