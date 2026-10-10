"""Edit reflection: did the agreed language land in the next draft?

Counsel marks proposed edits accepted / modified / rejected on a
round's record (``EditOutcome``). When the counterparty's next draft
arrives, the open question is not only "what moved?" — it is "the
edits we AGREED last round: are they in this text?" A point conceded
on a call and then missing from the drafting is the oldest trap in
negotiation, and it is invisible to a verdict diff: positions can
improve for other reasons while the specific agreed words never
appear.

The check is computed in code and deliberately textual. For each
accepted (or modified) edit of the previous round it takes the
agreed language — counsel's ``modified_text`` when recorded, else
the edit's proposed replacement — and checks it sentence by
sentence against the aligned clause of the new draft. A sentence
counts as landed when at least half of its NEW content words
appear in the new clause: the words the sentence would change
relative to the prior draft, so boilerplate both drafts share
cannot inflate the score. One class of words is never left to the
coverage vote: OPERATIVE tokens — figures (45 days is not 30 days)
and capitalised names mid-sentence (New York is not Delaware). A
sentence whose operative tokens do not all appear in the new
clause has not landed, however much of its frame the drafter kept:
adopting the sentence's shape with a different state named is
precisely the substitution this check exists to catch. An edit is
``reflected`` when at least half its checkable sentences landed,
``agreed_not_reflected`` below that — counsel agreed this edit,
but the new draft does not show it. Edits with no agreed language
on record (a ``counsel_draft`` edit), or no checkable content, are
``not_checkable`` — the module says what it cannot check rather
than guessing.

The verdict movement (``round_diff.py``) sits next to this on every
surface: reflection says the WORDS are or are not there; counsel
judges whether the substance landed.
"""

from __future__ import annotations

import re

from .proposed_edits import _content_words
from .round_diff import align_clauses
from .schemas import AgentResult, EditReflection

# The check splits the agreed language on semicolons as well as
# sentence punctuation: playbook language joins independent
# propositions with "; " (the net-days term; the interest rider),
# and each proposition lands or fails on its own.
_CLAUSE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")


def _agreed_sentences(text: str) -> list[str]:
    """The agreed language as checkable propositions: fragments of
    five words or more (shorter fragments are connectives, not
    terms). Falls back to the whole text when nothing splits."""
    sentences = []
    for part in _CLAUSE_SPLIT.split(text or ""):
        part = " ".join(part.split()).strip()
        if len(part.split()) >= 5:
            sentences.append(part)
    if not sentences and text.strip():
        sentences.append(text.strip())
    return sentences

# A sentence counts as landed when at least this share of its new
# content words appears in the new draft's clause; an edit counts
# as reflected when at least this share of its sentences landed.
# Half is the honest line in both cases: below it, more of what was
# agreed is missing than present, whatever paraphrase the drafter
# chose.
REFLECTION_COVERAGE_THRESHOLD = 0.5


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,%/-]*")


def _operative_tokens(sentence: str) -> list[str]:
    """The tokens in an agreed sentence that carry its substance:
    figures (any token containing a digit) and capitalised names —
    tokens capitalised mid-sentence, where capitalisation marks a
    proper name rather than a sentence opening. Returned in the
    sentence's own casing, in order, de-duplicated."""
    tokens = _TOKEN.findall(sentence)
    operative: list[str] = []
    for position, token in enumerate(tokens):
        has_digit = any(ch.isdigit() for ch in token)
        capitalised_name = position > 0 and token[:1].isupper()
        if (has_digit or capitalised_name) and token not in operative:
            operative.append(token)
    return operative


def _sentence_landed(
    sentence: str, prior_words: set[str], current_words: set[str], current_text: str
) -> tuple[bool | None, list[str]]:
    """Whether one agreed sentence landed in the new clause text.

    Returns ``(landed, missing_operative)``; ``landed`` is None when
    the sentence carries no checkable content (every word is a
    stopword or too short to measure). Operative tokens are checked
    first and exactly: one missing figure or name fails the
    sentence outright, and the missing tokens are reported."""
    lowered = current_text.lower()
    missing = [
        token
        for token in _operative_tokens(sentence)
        if not re.search(
            rf"(?<![a-z0-9]){re.escape(token.lower().strip('.,'))}(?![a-z0-9])", lowered
        )
    ]
    if missing:
        return False, missing
    sentence_words = _content_words(sentence)
    novel = sentence_words - prior_words
    checked = novel if novel else sentence_words
    if not checked:
        return None, []
    landed = len(checked & current_words) / len(checked) >= REFLECTION_COVERAGE_THRESHOLD
    return landed, []


def build_edit_reflection(
    prior: AgentResult, current: AgentResult
) -> list[EditReflection]:
    """Check the prior round's accepted/modified edits against the
    current round's text. ``prior`` carries the outcomes; ``current``
    is the follow-up draft's result. Rejected edits are not checked
    — nothing was agreed, so nothing is owed in the text."""
    outcomes = {
        outcome.edit_index: outcome
        for outcome in prior.edit_outcomes
        if outcome.outcome in ("accepted", "modified")
    }
    if not outcomes:
        return []
    alignment = dict(align_clauses(prior, current))
    prior_units = {u.clause_id: u for u in prior.clauses}
    current_units = {u.clause_id: u for u in current.clauses}

    reflections: list[EditReflection] = []
    for index in sorted(outcomes):
        outcome = outcomes[index]
        if index < 0 or index >= len(prior.proposed_edits):
            continue  # an outcome for an edit that no longer exists
        edit = prior.proposed_edits[index]
        base = {
            "clause_type": edit.clause_type,
            "edit_index": index,
            "prior_clause_id": edit.clause_id,
            "kind": edit.kind,
            "outcome": outcome.outcome,
        }
        agreed = (
            outcome.modified_text.strip()
            if outcome.outcome == "modified" and outcome.modified_text.strip()
            else edit.replacement_text.strip()
        )
        if edit.kind == "counsel_draft" or not agreed:
            reflections.append(
                EditReflection(
                    **base,
                    clause_id=alignment.get(edit.clause_id, ""),
                    status="not_checkable",
                    detail=(
                        "no agreed language is on record for this edit "
                        "(counsel-draft edit, or the settled text was "
                        "not recorded) — there is nothing to check the "
                        "new draft against."
                    ),
                )
            )
            continue
        current_clause_id = alignment.get(edit.clause_id, "")
        if not current_clause_id:
            reflections.append(
                EditReflection(
                    **base,
                    status="agreed_not_reflected",
                    coverage=0.0,
                    detail=(
                        "the clause itself is absent from the new "
                        "round's draft — the agreed language has "
                        "nowhere to appear."
                    ),
                )
            )
            continue
        prior_words = _content_words(
            prior_units[edit.clause_id].text if edit.clause_id in prior_units else ""
        )
        current_text = current_units[current_clause_id].text
        current_words = _content_words(current_text)
        sentences = _agreed_sentences(agreed)
        landed = 0
        checkable = 0
        missing_operative: list[str] = []
        for sentence in sentences:
            verdict, missing = _sentence_landed(
                sentence, prior_words, current_words, current_text
            )
            if verdict is None:
                continue
            checkable += 1
            if verdict:
                landed += 1
            else:
                for token in missing:
                    if token not in missing_operative:
                        missing_operative.append(token)
        if checkable == 0:
            reflections.append(
                EditReflection(
                    **base,
                    clause_id=current_clause_id,
                    status="not_checkable",
                    detail=(
                        "the agreed language carries no checkable "
                        "content words — nothing to measure against "
                        "the new draft."
                    ),
                )
            )
            continue
        coverage = landed / checkable
        operative_note = (
            f" Not carried into the new draft: {', '.join(missing_operative)}."
            if missing_operative
            else ""
        )
        if coverage >= REFLECTION_COVERAGE_THRESHOLD:
            reflections.append(
                EditReflection(
                    **base,
                    clause_id=current_clause_id,
                    status="reflected",
                    coverage=round(coverage, 3),
                    detail=(
                        f"{landed} of {checkable} agreed sentence(s) "
                        f"appear in the new draft's {current_clause_id} "
                        "— the agreed language landed."
                    ),
                )
            )
        else:
            reflections.append(
                EditReflection(
                    **base,
                    clause_id=current_clause_id,
                    status="agreed_not_reflected",
                    coverage=round(coverage, 3),
                    detail=(
                        f"only {landed} of {checkable} agreed "
                        f"sentence(s) appear in the new draft's "
                        f"{current_clause_id} — counsel agreed this "
                        "edit, but the new draft does not show it. "
                        "Agreed, but not reflected." + operative_note
                    ),
                )
            )
    return reflections
