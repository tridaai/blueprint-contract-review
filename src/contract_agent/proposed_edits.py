"""Proposed edits: the packet's fallback language, mapped onto the
counterparty's own sentences.

A clause diff shows counsel two texts side by side — the clause as
written and the fallback the playbook proposes. Turning that into a
redline still meant re-drafting by hand. This module closes the gap
with a tracked-changes style edit script per flagged clause:

- ``replace_sentence`` — this counterparty sentence comes out, this
  fallback sentence goes in its place;
- ``insert_after`` — fallback language with no counterpart sentence
  is inserted after the sentence it belongs behind;
- ``replace_clause`` — when no sentence pairing is strong enough,
  the honest fallback: replace the clause wholesale and let counsel
  apply it, rather than pretend a precision the alignment does not
  have;
- ``counsel_draft`` — the playbook proposes no language for this
  clause at all, so there is nothing to map; counsel drafts.

The mapping is computed in code: sentences are paired by content-word
overlap (Jaccard over word sets with stopwords removed), greedily,
each counterparty sentence used at most once. The model may phrase
fallback language upstream (playbook entries are human-authored; the
verdict carries them); WHICH sentence each line of language replaces
is decided here, never by the model. Every edit is labelled
draft-for-counsel on every surface that renders it — the agent
proposes edits, it never sends redlines.
"""

from __future__ import annotations

import re

from .schemas import ClauseDiff, ProposedEdit

# Below this content-word overlap, a pairing is a guess — the edit
# degrades to a clause-level replacement instead.
ALIGNMENT_THRESHOLD = 0.15

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
    "by", "with", "under", "this", "that", "it", "its", "is", "are",
    "be", "shall", "will", "may", "not", "no", "any", "all", "each",
    "party", "parties", "agreement",
}

_WORD = re.compile(r"[a-z0-9]+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    """Split clause text into sentences worth aligning. Fragments
    are dropped when they are labels rather than operative language:
    a fragment counts as a sentence when it ends with terminal
    punctuation, or runs to six words or more unpunctuated (a long
    operative line that simply lacks its full stop). Short
    unpunctuated fragments — headings, numbering stubs — are neither
    and never become alignment targets."""
    sentences = []
    for part in _SENTENCE_SPLIT.split(text or ""):
        part = " ".join(part.split()).strip()
        words = len(part.split())
        if words >= 6 or (words >= 3 and part.endswith((".", "!", "?"))):
            sentences.append(part)
    return sentences


def _content_words(sentence: str) -> set[str]:
    return {
        word
        for word in _WORD.findall(sentence.lower())
        if word not in _STOPWORDS and len(word) > 2
    }


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_proposed_edits(diffs: list[ClauseDiff]) -> list[ProposedEdit]:
    """The edit script for a result's clause diffs, in diff order.

    The language mapped is the diff's TIER language (the concession
    the packet actually proposes — first fallback or walk-away line),
    falling back to the playbook fallback when a tier carries none.
    """
    edits: list[ProposedEdit] = []
    for diff in diffs:
        edits.extend(_edits_for_diff(diff))
    return edits


def _edits_for_diff(diff: ClauseDiff) -> list[ProposedEdit]:
    base = {
        "clause_id": diff.clause_id,
        "clause_type": diff.clause_type,
        "position": diff.position,
        "tier": diff.proposed_tier,
    }
    language = (diff.tier_language or diff.playbook_fallback or "").strip()
    if not language:
        return [
            ProposedEdit(
                **base,
                kind="counsel_draft",
                alignment="none",
                note=(
                    "the playbook proposes no fallback language for this "
                    "clause — there is no edit to map; counsel drafts the "
                    "ask. Draft for counsel review."
                ),
            )
        ]
    clause_sentences = split_sentences(diff.counterparty_text)
    language_sentences = split_sentences(language) or [language]
    if not clause_sentences:
        return [
            ProposedEdit(
                **base,
                kind="replace_clause",
                original_sentence="",
                replacement_text=language,
                alignment="clause_fallback",
                note=(
                    "no counterparty clause text was captured to align "
                    "against — counsel applies the proposed language at "
                    "clause level. Draft for counsel review."
                ),
            )
        ]
    clause_words = [_content_words(s) for s in clause_sentences]
    used: set[int] = set()
    pairs: list[tuple[int, str]] = []  # (clause sentence idx, language sentence)
    unpaired: list[str] = []
    for sentence in language_sentences:
        words = _content_words(sentence)
        best_idx, best_score = -1, 0.0
        for idx, candidate in enumerate(clause_words):
            if idx in used:
                continue
            score = _overlap(words, candidate)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx >= 0 and best_score >= ALIGNMENT_THRESHOLD:
            used.add(best_idx)
            pairs.append((best_idx, sentence))
        else:
            unpaired.append(sentence)
    if not pairs:
        return [
            ProposedEdit(
                **base,
                kind="replace_clause",
                original_sentence=(diff.counterparty_text or "").strip(),
                replacement_text=language,
                alignment="clause_fallback",
                note=(
                    "sentence-level alignment was too weak to pair the "
                    "proposed language with individual sentences — the "
                    "edit is stated at clause level for counsel to "
                    "apply. Draft for counsel review."
                ),
            )
        ]
    edits: list[ProposedEdit] = []
    for idx, sentence in sorted(pairs):
        edits.append(
            ProposedEdit(
                **base,
                kind="replace_sentence",
                original_sentence=clause_sentences[idx],
                replacement_text=sentence,
                alignment="sentence",
                note=(
                    "paired with the counterparty sentence it most "
                    "closely matches, by content-word overlap. "
                    "Draft for counsel review."
                ),
            )
        )
    if unpaired:
        anchor_idx = max(idx for idx, _ in pairs)
        edits.append(
            ProposedEdit(
                **base,
                kind="insert_after",
                anchor_sentence=clause_sentences[anchor_idx],
                replacement_text=" ".join(unpaired),
                alignment="sentence",
                note=(
                    "proposed language with no counterpart sentence in "
                    "the clause as written — inserted after the last "
                    "paired sentence. Draft for counsel review."
                ),
            )
        )
    return edits
