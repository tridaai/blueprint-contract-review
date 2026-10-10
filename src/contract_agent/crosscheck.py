"""Rules-vs-LLM clause cross-check — the resolution policy.

Every clause is reviewed twice, independently:

- the deterministic playbook rules (``clause_review.py``), and
- in provider mode, the LLM, which is never shown the rule verdict.

This module resolves each pair into the single verdict the rest of the
pipeline uses. The policy is explicit and deliberately narrow (the
same policy as the reference blueprint family, plus one
contract-specific escalation):

1. **Agreement** (same position): the rule verdict stands — identical
   conclusion, and the rule path is the auditable one.
2. **Disagreement, rules strong**: the rule verdict stands
   (``rules_authoritative``) — except the disagreement itself is a
   finding: a clause the two readers placed differently is escalated
   to at least ``review_required`` (``escalated_disagreement``) so a
   human reads it, whatever the rules said.
3. **Disagreement, rules weak, LLM confident**: when the rules' own
   confidence is below ``RULE_WEAK_CONFIDENCE`` AND the LLM confidence
   is at least ``LLM_ADOPT_CONFIDENCE``, the LLM position is adopted
   (``llm_adopted``) — the case rules are known to miss: a clause whose
   substance no keyword anticipated. The adoption is flagged on the
   verdict so counsel can see exactly why it is not the rule output.

Everything else — no LLM configured (default mode), a provider error,
an unusable reply — resolves to the rule verdict alone
(``rules_only``). Adoption never touches computed facts: posture,
counts, and absences come from ``facts.py`` regardless of which path
wins.
"""

from __future__ import annotations

from .schemas import POSITION_SEVERITY, ClauseVerdict

RULE_WEAK_CONFIDENCE = 0.85
LLM_ADOPT_CONFIDENCE = 0.85


def resolve_clause_verdict(rule: ClauseVerdict, llm: dict | None) -> ClauseVerdict:
    """Apply the resolution policy to one clause's (rule, LLM) pair."""
    if llm is None:
        return rule

    llm_position = str(llm.get("position") or "")
    try:
        llm_confidence = float(llm.get("confidence") or 0.0)
    except (TypeError, ValueError):
        llm_confidence = 0.0
    if llm_position not in POSITION_SEVERITY:
        rule.resolution = "rules_only"
        return rule

    final = rule.model_copy()
    final.llm_position = llm_position
    final.llm_confidence = llm_confidence

    if llm_position == rule.rule_position:
        final.resolution = "agree"
        return final

    if rule.confidence < RULE_WEAK_CONFIDENCE and llm_confidence >= LLM_ADOPT_CONFIDENCE:
        final.position = llm_position
        final.confidence = llm_confidence
        final.resolution = "llm_adopted"
        rationale = str(llm.get("rationale") or "").strip()
        final.rationale = (
            f"{rationale} (Adopted from the LLM cross-check: the playbook rules "
            f"placed this clause '{rule.rule_position}' at confidence "
            f"{rule.confidence}, so this adoption is flagged for counsel.)"
            if rationale
            else final.rationale
        )
        return final

    # Disagreement with strong rules: rules stay authoritative, and the
    # disagreement escalates the clause to at least review_required.
    final.resolution = "rules_authoritative"
    if POSITION_SEVERITY[final.position] < POSITION_SEVERITY["review_required"]:
        final.position = "review_required"
        final.resolution = "escalated_disagreement"
        final.escalated = True
        final.rationale = (
            f"{final.rationale} The LLM cross-check disagreed "
            f"('{llm_position}' at confidence {llm_confidence}); the "
            "disagreement itself escalates this clause for counsel review."
        )
    return final
