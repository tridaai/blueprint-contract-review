"""Information-request flow: when a contract package is incomplete.

A package that references exhibits it does not attach (or omits its
signature pages) cannot be fully reviewed: the computed facts say
exactly what is missing, so the honest next step is asking the
counterparty for it. This module composes that request — the
missing-items list is computed by code (``facts.py``), never invented
by the model; in provider mode the model only words the message.

The request is composed, never sent — like every output here it stops
at counsel's gate, and leaves only if counsel approves it and routes
it through their own system.
"""

from __future__ import annotations

from .model_backends import DraftContext
from .schemas import ContractFacts, ContractInput, InformationRequest


def missing_items(facts: ContractFacts) -> list[str]:
    """The counterparty-facing missing-items list, computed from the facts."""
    items: list[str] = []
    for exhibit in facts.exhibits_missing:
        items.append(f"Exhibit {exhibit}, which the agreement references but the package does not include")
    if not facts.signature_pages_present:
        items.append("the signature pages for the agreement")
    return items


def needs_information(facts: ContractFacts, items: list[str]) -> bool:
    """The trigger: an incomplete package with something concrete to ask for."""
    return not facts.package_complete and bool(items)


def template_message(contract: ContractInput, items: list[str]) -> str:
    lines = [
        f"Re: contract {contract.contract_id} — {contract.counterparty_name} "
        f"({contract.doc_type})",
        "",
        "Thank you for the draft. Before our counsel can complete the review, we still need:",
        *[f"- {item}" for item in items],
        "",
        "Once we have these, the review will be completed in full. This "
        "message requests information only — nothing in it agrees to, "
        "accepts, or approves any term.",
    ]
    return "\n".join(lines)


def build_information_request(
    *,
    contract: ContractInput,
    facts: ContractFacts,
    backend,
) -> InformationRequest | None:
    """Compose the request when the trigger fires; None otherwise.

    In provider mode the LLM words the message over the computed
    items; the template is the fallback and the default-mode path.
    """
    items = missing_items(facts)
    if not needs_information(facts, items):
        return None
    message = template_message(contract, items)
    compose = getattr(backend, "compose_information_request", None)
    if compose is not None:
        context = DraftContext(
            contract_id=contract.contract_id,
            counterparty_name=contract.counterparty_name,
            doc_type=contract.doc_type,
            missing_items=items,
        )
        try:
            llm_message = compose(context)
        except Exception:  # the request never fails the run
            llm_message = None
        if llm_message:
            message = llm_message
    return InformationRequest(needed=True, missing_items=items, message=message)
