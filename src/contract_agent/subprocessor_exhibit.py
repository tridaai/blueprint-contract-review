"""The sub-processor exhibit: a matter's lists, assembled.

The single-round checks (``exhibit_review.py``) read one
package's list annex against that package's own terms, and the
drift read (``subprocessor_drift.py``) follows one chain's list
across its rounds. Neither assembles the thing counsel asks for
when a matter carries more than one document with a list in it:
the exhibit — every sub-processor the matter's documents engage,
each entry as its document states it, with the document and
line it came from, in one place.

This module assembles it, in code, from the documents' own
list annexes (the round-10 list shape: bullet items in an
exhibit/schedule document), reading each chain's LATEST round —
the matter's current state, the matter brief's discipline.
Every entry carries its lineage: the contract, the chain's
root, the document, the annex's own heading, and the line as
written. A list item that yields no readable processor name,
and a prose line that engages a named party outside the list's
item shape, are carried as UNPARSED with their source line —
never dropped, and never guessed into structure: the exhibit
reports what each line could and could not give it.

The assembly also compares across documents: the same
sub-processor (matched on the normalised name the checks use)
appearing in two documents with different stated purposes or
different stated locations is a CONFLICT — a computed
comparison, both sources cited, no judgement attached. The
exhibit assembles what the documents say; it draws no legal
conclusion about adequacy, authorisation, or anything else —
those readings belong to the checks and to counsel.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from .exhibit_review import (
    _find_subprocessor_list_document,
    _item_location,
    _item_name,
    _list_items,
    _normalise_name,
)
from .schemas import ContractInput
from .store import ApprovalRecord

# A prose mention: the annex's own verb ("engages") tied to a
# company-shaped name — capitalised words ending in a legal-form
# suffix. Tight on purpose: a line that merely talks about
# sub-processors in general is not a mention of one.
_COMPANY_NAME = (
    r"[A-Z][\w&.'-]*(?:\s+(?:[A-Z][\w&.'-]*|of|the))*"
    r"\s+(?:GmbH|Ltd|Inc|LLC|AB|BV|NV|SA|AG|Pty|Oy|Srl|PLC|Corp)"
)
_PROSE_MENTION = re.compile(rf"\bengages?\b[^.\n]*?\b({_COMPANY_NAME})\b")


def _item_purpose(item: str) -> str:
    """A list item's stated purpose: the gloss after its name
    and location ("Hostline GmbH (Germany) — data hosting" →
    "data hosting"). The location parenthesis is set aside
    first, then the remainder splits on the same dash separators
    ``_item_name`` splits on. "" when the item carries no gloss —
    the exhibit records the absence, it does not fill it."""
    rest = re.sub(r"\([^)]*\)", "", item).strip()
    parts = re.split(r"\s+—\s+|\s+-\s+", rest, maxsplit=1)
    return parts[1].strip().rstrip(".") if len(parts) == 2 else ""


def _norm_value(value: str) -> str:
    """A stated purpose/location reduced for comparison:
    case, punctuation and spacing set aside, so "Data Hosting"
    and "data hosting" are the same statement and "data hosting"
    and "hosting of data" are — honestly — different ones."""
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (value or "").lower()).split())


def document_exhibit(contract: ContractInput) -> dict | None:
    """One contract's sub-processor list annex as exhibit
    entries, or None when the package carries no list annex.
    Entries carry the name, purpose and location AS STATED and
    their lineage; lines that look like sub-processor mentions
    but do not structure — a list item with no readable name, a
    prose line engaging a named party outside the item shape —
    are returned under ``unparsed`` with their source line."""
    doc = _find_subprocessor_list_document(contract)
    if doc is None:
        return None
    doc_id = doc.document_id or doc.doc_type
    lines = [line.strip() for line in doc.raw_text.splitlines() if line.strip()]
    section = lines[0] if lines else ""
    items = _list_items(doc.raw_text)
    entries: list[dict] = []
    unparsed: list[dict] = []
    for item in items:
        name = _item_name(item)
        if not name:
            unparsed.append(
                {
                    "line": item,
                    "reason": "a list item with no readable processor name",
                    "document_id": doc_id,
                }
            )
            continue
        entries.append(
            {
                "name": name,
                "key": _normalise_name(name),
                "purpose": _item_purpose(item),
                "location": _item_location(item),
                "line": item,
                "document_id": doc_id,
                "section": section,
            }
        )
    item_texts = set(items)
    for line in lines:
        if line in item_texts or line == section:
            continue
        if "following" in line.lower():
            continue  # the annex's lead-in names no one
        if _PROSE_MENTION.search(line):
            unparsed.append(
                {
                    "line": line,
                    "reason": (
                        "names a sub-processor in prose, outside the "
                        "list's item shape"
                    ),
                    "document_id": doc_id,
                }
            )
    return {
        "contract_id": contract.contract_id,
        "document_id": doc_id,
        "section": section,
        "entries": entries,
        "unparsed": unparsed,
    }


def build_subprocessor_exhibit(
    counterparty: str,
    matter: str,
    chains: list[list[ApprovalRecord]],
) -> dict:
    """Assemble the matter's sub-processor exhibit from its
    chains' latest rounds. Pure over the stored records: a
    record whose stored contract no longer validates, or whose
    package carries no list annex, contributes no document —
    the exhibit assembles from the facts the records carry.
    Conflicts are computed across documents: the same processor
    in two different contracts' lists with differing stated
    purposes, or differing stated locations, both sources
    cited. A processor restating itself inside one document is
    one source, not a conflict."""
    documents: list[dict] = []
    for chain in chains:
        latest = chain[-1]
        try:
            contract = ContractInput.model_validate(latest.contract or {})
        except ValidationError:
            continue
        assembled = document_exhibit(contract)
        if assembled is None:
            continue
        assembled["chain_root"] = chain[0].result.contract_id
        documents.append(assembled)

    processors: dict[str, dict] = {}
    for document in documents:
        for entry in document["entries"]:
            processor = processors.setdefault(
                entry["key"],
                {"key": entry["key"], "name": entry["name"], "appearances": []},
            )
            processor["appearances"].append(
                {
                    "contract_id": document["contract_id"],
                    "chain_root": document["chain_root"],
                    "document_id": entry["document_id"],
                    "section": entry["section"],
                    "purpose": entry["purpose"],
                    "location": entry["location"],
                    "line": entry["line"],
                }
            )

    conflicts: list[dict] = []
    for processor in processors.values():
        appearances = processor["appearances"]
        if len({a["contract_id"] for a in appearances}) < 2:
            continue  # one document cannot conflict with itself
        for field in ("purpose", "location"):
            values: dict[str, list[dict]] = {}
            for appearance in appearances:
                stated = appearance[field]
                if not stated:
                    continue  # silence is an absence, not a position
                values.setdefault(_norm_value(stated), []).append(appearance)
            if len(values) > 1:
                conflicts.append(
                    {
                        "name": processor["name"],
                        "field": field,
                        "values": [
                            {
                                "value": group[0][field],
                                "contract_id": group[0]["contract_id"],
                                "document_id": group[0]["document_id"],
                            }
                            for group in values.values()
                        ],
                    }
                )
    conflicts.sort(key=lambda c: (c["name"], c["field"]))

    unparsed = [
        {
            "contract_id": document["contract_id"],
            "chain_root": document["chain_root"],
            "document_id": item["document_id"],
            "line": item["line"],
            "reason": item["reason"],
        }
        for document in documents
        for item in document["unparsed"]
    ]
    return {
        "counterparty": counterparty,
        "matter": matter,
        "framing": (
            "Assembled in code from the list annexes on the matter's "
            "current rounds. Every entry traces to its document and "
            "line; the exhibit assembles what the documents say and "
            "draws no conclusion about adequacy or authorisation."
        ),
        "documents": documents,
        "processors": sorted(processors.values(), key=lambda p: p["name"]),
        "conflicts": conflicts,
        "unparsed": unparsed,
        "counts": {
            "documents": len(documents),
            "entries": sum(len(d["entries"]) for d in documents),
            "processors": len(processors),
            "conflicts": len(conflicts),
            "unparsed": len(unparsed),
        },
    }


def render_subprocessor_exhibit_text(data: dict) -> str:
    """The exhibit as readable text: the same assembly as the
    JSON rendering, so the two cannot drift."""
    lines = [
        f"Sub-processor exhibit — {data['counterparty']} / {data['matter']}",
        data["framing"],
        "",
        "Documents:",
    ]
    if data["documents"]:
        for document in data["documents"]:
            lines.append(
                f"- {document['contract_id']} (chain root "
                f"{document['chain_root']}) · {document['document_id']} "
                f"\"{document['section']}\""
            )
    else:
        lines.append("- none — no list annex on the matter's current rounds")
    lines += ["", "Sub-processors as the documents state them:"]
    if data["processors"]:
        for processor in data["processors"]:
            lines.append(f"- {processor['name']}")
            for appearance in processor["appearances"]:
                stated = " · ".join(
                    part
                    for part in (
                        appearance["purpose"],
                        appearance["location"],
                    )
                    if part
                )
                lines.append(
                    f"  - {appearance['contract_id']} · "
                    f"{appearance['document_id']}: {stated or 'as listed'} "
                    f"— \"{appearance['line']}\""
                )
    else:
        lines.append("- none listed")
    lines += ["", "Conflicts across documents:"]
    if data["conflicts"]:
        for conflict in data["conflicts"]:
            cited = " vs ".join(
                f"\"{value['value']}\" ({value['contract_id']}, "
                f"{value['document_id']})"
                for value in conflict["values"]
            )
            lines.append(
                f"- {conflict['name']} — {conflict['field']} differs: {cited}"
            )
    else:
        lines.append("- none — no processor is stated two ways across documents")
    lines += ["", "Unparsed mentions (carried, not structured):"]
    if data["unparsed"]:
        for item in data["unparsed"]:
            lines.append(
                f"- {item['contract_id']} · {item['document_id']}: "
                f"\"{item['line']}\" ({item['reason']})"
            )
    else:
        lines.append("- none")
    return "\n".join(lines)
