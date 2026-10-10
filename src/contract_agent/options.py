"""Options node logic: next steps per flagged clause, scored in code.

The four option kinds are the only paths a flagged clause can take
(``schemas.ClauseOption.kind``):

- ``accept_as_preferred`` — the clause already matches the playbook's
  preferred position; no action. (Only sensible where a clause was
  flagged by the cross-check, not by its position.)
- ``propose_fallback_language`` — send the playbook's fallback wording
  for this clause in the counterparty response.
- ``escalate_to_senior_counsel`` — the position is a walk-away or a
  judgement call; a senior lawyer decides.
- ``request_counterparty_info`` — an exhibit, schedule, or
  clarification is missing; ask before deciding.

In provider mode the LLM may propose the wording of the options; in
every mode their *kinds* are validated here and their scores are
computed by deterministic code (the model never does this arithmetic):

    score = 60 * position_fit + 40 * posture_fit + info_bonus

where ``position_fit`` ∈ [0, 1] is how well the kind matches the
clause's verdict, ``posture_fit`` how well it matches the computed
document posture, and ``info_bonus`` applies to information requests
while the package is incomplete. The recommended option is the
argmax — in practice, the next step for the worst clause.
"""

from __future__ import annotations

from .model_backends import DraftContext, clause_rows_text
from .schemas import ClauseOption, ClauseVerdict, ContractFacts, ContractInput

OPTION_KINDS = {
    "accept_as_preferred",
    "propose_fallback_language",
    "escalate_to_senior_counsel",
    "request_counterparty_info",
}

_TITLES = {
    "accept_as_preferred": "Accept the clause as matching the preferred position",
    "propose_fallback_language": "Propose the playbook fallback language",
    "escalate_to_senior_counsel": "Escalate the clause to senior counsel",
    "request_counterparty_info": "Request information from the counterparty",
}

_DESCRIPTIONS = {
    "accept_as_preferred": (
        "The clause matches the playbook's preferred position; no change "
        "is requested. Counsel still makes the final call at the gate."
    ),
    "propose_fallback_language": (
        "The counterparty response proposes the playbook's fallback "
        "wording for this clause; counsel approves the wording at the gate."
    ),
    "escalate_to_senior_counsel": (
        "The clause sits at or beyond a playbook walk-away or needs a "
        "judgement the first pass must not make; senior counsel decides."
    ),
    "request_counterparty_info": (
        "The clause cannot be decided until the counterparty supplies "
        "the missing exhibit, schedule, or clarification."
    ),
}

# How well each kind fits each clause position (0..1).
_POSITION_FIT: dict[str, dict[str, float]] = {
    "preferred": {
        "accept_as_preferred": 1.0,
        "propose_fallback_language": 0.2,
        "escalate_to_senior_counsel": 0.15,
        "request_counterparty_info": 0.1,
    },
    "acceptable_fallback": {
        "accept_as_preferred": 0.55,
        "propose_fallback_language": 0.9,
        "escalate_to_senior_counsel": 0.4,
        "request_counterparty_info": 0.3,
    },
    "review_required": {
        "accept_as_preferred": 0.05,
        "propose_fallback_language": 0.6,
        "escalate_to_senior_counsel": 0.9,
        "request_counterparty_info": 0.55,
    },
    "walk_away": {
        "accept_as_preferred": 0.0,
        "propose_fallback_language": 0.65,
        "escalate_to_senior_counsel": 1.0,
        "request_counterparty_info": 0.25,
    },
}

# How well each kind fits the computed document posture (0..1).
_POSTURE_FIT: dict[str, dict[str, float]] = {
    "aligned": {
        "accept_as_preferred": 1.0,
        "propose_fallback_language": 0.5,
        "escalate_to_senior_counsel": 0.2,
        "request_counterparty_info": 0.3,
    },
    "negotiable": {
        "accept_as_preferred": 0.4,
        "propose_fallback_language": 1.0,
        "escalate_to_senior_counsel": 0.7,
        "request_counterparty_info": 0.6,
    },
    "escalation_required": {
        "accept_as_preferred": 0.0,
        "propose_fallback_language": 0.6,
        "escalate_to_senior_counsel": 1.0,
        "request_counterparty_info": 0.3,
    },
}


def flagged_verdicts(verdicts: list[ClauseVerdict]) -> list[ClauseVerdict]:
    """Clauses that need a next step: anything not plainly preferred."""
    return [v for v in verdicts if v.position != "preferred" or v.escalated]


def template_proposals(verdicts: list[ClauseVerdict], facts: ContractFacts) -> list[dict]:
    """The deterministic proposal set: for each flagged clause, the
    plausible kinds (the natural path first). Scoring, not presence,
    picks the recommendation."""
    proposals: list[dict] = []
    for verdict in flagged_verdicts(verdicts):
        kinds: list[str] = []
        if verdict.position == "walk_away":
            kinds = ["escalate_to_senior_counsel", "propose_fallback_language"]
        elif verdict.position == "review_required":
            kinds = ["escalate_to_senior_counsel", "propose_fallback_language"]
        elif verdict.position == "acceptable_fallback":
            kinds = ["propose_fallback_language", "accept_as_preferred"]
        else:  # preferred but escalated by the cross-check
            kinds = ["escalate_to_senior_counsel", "accept_as_preferred"]
        if not facts.package_complete:
            kinds.append("request_counterparty_info")
        for kind in kinds:
            if kind == "propose_fallback_language" and not verdict.fallback_language:
                continue
            proposals.append(
                {
                    "clause_id": verdict.clause_id,
                    "kind": kind,
                    "title": _TITLES[kind],
                    "description": _DESCRIPTIONS[kind],
                }
            )
    return proposals


def score_option(kind: str, position: str, facts: ContractFacts) -> float:
    """The deterministic score for one option (see module docstring)."""
    position_fit = _POSITION_FIT.get(position, _POSITION_FIT["review_required"]).get(kind, 0.0)
    posture_fit = _POSTURE_FIT.get(facts.posture, _POSTURE_FIT["negotiable"]).get(kind, 0.0)
    info_bonus = 8.0 if kind == "request_counterparty_info" and not facts.package_complete else 0.0
    return round(60.0 * position_fit + 40.0 * posture_fit + info_bonus, 2)


def score_options(
    proposals: list[dict],
    verdicts: list[ClauseVerdict],
    facts: ContractFacts,
) -> list[ClauseOption]:
    """Validate kinds and clause IDs, score every proposal, mark the
    argmax recommended."""
    by_id = {v.clause_id: v for v in verdicts}
    scored: list[ClauseOption] = []
    seen: set[tuple[str, str]] = set()
    for proposal in proposals:
        kind = str(proposal.get("kind", "")).strip().lower()
        clause_id = str(proposal.get("clause_id", "")).strip()
        if kind not in OPTION_KINDS or clause_id not in by_id:
            continue
        if (clause_id, kind) in seen:
            continue
        seen.add((clause_id, kind))
        scored.append(
            ClauseOption(
                clause_id=clause_id,
                kind=kind,
                title=str(proposal.get("title") or _TITLES[kind])[:120],
                description=str(proposal.get("description") or _DESCRIPTIONS[kind])[:300],
                score=score_option(kind, by_id[clause_id].position, facts),
            )
        )
    if not scored and proposals:
        # Every proposal was invalid (unknown kind or clause) — fall
        # back to the template set. When nothing is flagged there is
        # legitimately nothing to propose: an empty list, not a loop.
        scored = score_options(template_proposals(verdicts, facts), verdicts, facts) \
            if template_proposals(verdicts, facts) else []
    if scored:
        best = max(scored, key=lambda o: o.score)
        for option in scored:
            option.recommended = option is best
        scored.sort(key=lambda o: o.score, reverse=True)
    return scored


def build_options(
    *,
    contract: ContractInput,
    verdicts: list[ClauseVerdict],
    clause_rows: list[dict],
    facts: ContractFacts,
    assessment_summary: str,
    playbook: list[dict],
    backend,
) -> tuple[list[ClauseOption], str]:
    """Propose + score the next-step options for the flagged clauses.

    In provider mode the LLM proposes the option wording (kinds and
    clause IDs validated, scores computed here); otherwise the
    template proposal set runs. Returns (options, note) — the note
    records a degraded LLM path.
    """
    propose = getattr(backend, "propose_options", None)
    if propose is None:
        return score_options(template_proposals(verdicts, facts), verdicts, facts), ""
    context = DraftContext(
        contract_id=contract.contract_id,
        counterparty_name=contract.counterparty_name,
        our_role=contract.our_role,
        doc_type=contract.doc_type,
        title=contract.title,
        posture=facts.posture,
        positions_summary=", ".join(
            f"{k}: {v}" for k, v in sorted(facts.positions_count.items())
        ) or "none",
        clause_rows=[r for r in clause_rows if r.get("position") != "preferred"],
        missing_clauses=facts.missing_standard_clauses,
        discrepancies=facts.discrepancies,
        cover_text="",
        assessment_summary=assessment_summary,
        playbook_details=playbook,
    )
    note = ""
    try:
        proposals = propose(context)
    except Exception as exc:  # options never fail the run
        proposals = None
        note = f"LLM option proposals failed ({exc}) — template proposals used"
    if not proposals:
        proposals = template_proposals(verdicts, facts)
        if not note:
            note = "LLM option proposals were unusable — template proposals used"
    return score_options(proposals, verdicts, facts), note
