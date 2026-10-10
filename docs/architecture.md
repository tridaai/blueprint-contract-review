# Architecture — Contract Review Agent

## Provenance and stance

This blueprint follows the structural conventions of modern agent
blueprints — provider endpoint profiles (including NVIDIA NIM),
an in-repo eval harness, guardrails as code, per-run observability,
and decomposable components behind ports. Those are *patterns*. Every
line shipped here — code, documentation, sample contracts, playbook
content — is original Trida AI work, written for this repository.
Patterns informed by NVIDIA's blueprint conventions; implementation
and content are original. Nothing is copied from NVIDIA repositories
(or anyone else's), regardless of what their licenses would allow.

## The pipeline

A LangGraph state graph, sixteen nodes, one direction: a contract
goes in, a counsel-ready packet comes out, and the run pauses at a
checkpointed human gate. No node sends, signs, agrees, or files
anything.

```
ingest → extract → screen → classify → facts
  → fan-out [retrieve ‖ discrepancies ‖ history]
  → assess → options → draft → verify → review → validate
  → (bounded repair → draft) → gate → approval_gate (interrupt)
```

| Stage | What happens | Who decides |
| --- | --- | --- |
| ingest | Contract types normalized (msa/sow/nda/amendment/order_form/dpa + aliases); text segmented into numbered clause units in code | code |
| extract | Per-clause type + structured facts (cap months/amounts, net days, notice periods, governing law, DPA, assignment) with confidence; optional LLM extraction cross-check; discrepancies recorded, never fatal | rules + LLM check |
| screen | Injection screening: counterparty text is untrusted. AI-directed instructions are flagged and fenced out of prompts/drafts; the clause is still reviewed on substance | code |
| classify | Per-clause playbook position from the deterministic matrix, cross-checked by an LLM per clause; disagreements escalate to ≥ review_required; resolution recorded per verdict | rules, LLM advises |
| facts | Document posture counted in code: position counts, worst position, absence detection (missing standard clauses; liability cap + data protection are critical absences); exhibit consistency checks (the DPA exhibit's text vs the clause's promises); the obligations register | code |
| retrieve | Playbook entries by keyword / semantic / hybrid (RRF fusion); query built from clause content | code |
| assess | Risk summary composed over evidence; with a provider, a bounded tool loop (`search_playbook`, `clause_facts`, `counterparty_history`); template fallback offline | LLM over evidence |
| options | Per flagged clause, options scored in code (position fit + posture fit + package completeness + a capped history term from the counterparty profile + a capped pattern term from the chain's reflection patterns) | code |
| draft | Review packet (clause table: verdict, citation, rationale, fallback language) + counterparty response asserting nothing as agreed | LLM / template |
| verify | Grounding checklist (citations must be retrieved playbook ids; figures and foreign ids flagged) + optional LLM critique | code + LLM |
| review | Independent reviewer — a second pass, not the drafter; can block | LLM / checklist |
| validate | Guardrails as code (below); bounded repair loop redrafts with the failures as feedback | code |
| gate | Autonomy recommendation (printed only) + clarification flow (missing exhibits/signature pages → composed info request, never sent) | code |
| approval_gate | `interrupt()` at the checkpoint; counsel approves/rejects with a reason; the reason feeds the store and teaches later runs | **counsel** |

### Guardrails (code, not prompts)

`no_legal_conclusion` (no "legally binding / safe to sign /
approved"), `no_agreement_language` (no "we agree / we accept these
terms" in the counterparty draft), `no_acceptability_pronouncement`
(no "acceptable as drafted" / "acceptable to us" in the counterparty
draft — added after an LLM judge caught a live letter pronouncing a
cap acceptable, pre-deciding counsel's call), `no_materiality_minimising` (no
"technical and routine" / "minor changes" characterisations of the
requested changes — a pattern family added after an LLM judge caught
live drafts minimising; materiality is counsel's judgement),
`no_prohibited_promises`,
`no_pii_in_draft` (SSN / Luhn-checked card / passport patterns),
`playbook_citations_present`, `flagged_clauses_cited` (every flagged
clause's playbook ref must appear in the packet),
`references_contract_id`, `states_next_step`. A draft that fails
cannot be approved; repair is bounded and the original failures are
kept on the record.

### Cross-check resolution policy

Rules are the spine. When the LLM agrees, the rule position stands.
When it disagrees: a high-confidence rule position escalates the
clause to `review_required` (`escalated_disagreement`) rather than
silently adopting either side; a weak rule position adopts a
high-confidence LLM position. Every verdict carries
`rule_position`, `llm_position`, `resolution`, and `escalated` so
counsel can see exactly how the final position was reached.

### Across documents and across rounds

A set of service-level capabilities sits on top of the per-contract
pipeline; all are computed in code from stored records, and all are
original to this blueprint.

**Round chains and re-trades** (`round_diff.py`). A pairwise diff
answers what the last round won. With three or more rounds in the
store, the service walks the `supersedes` chain and threads each
clause type through consecutive pairwise alignments: a movement
history per clause, cumulative first-to-latest movement, and
re-trade detection — an `improved` step followed by a later
`worsened` step is a clause that was won and then given back, the
finding no single diff can see. A worsening followed by a recovery
is explicitly not a re-trade.

**Tiered fallbacks** (`fallback_tiers.py`). Each clause type's
fallback language is tier 1; the type's designated playbook entry
also carries a `walk_away_line` — tier 2, the final position. The
packet's proposal is chosen by rule: walk-away verdicts propose
tier 2 directly, later rounds propose tier 2 (the first fallback
did not close the clause), and re-traded clause types propose
tier 2 in any round. The proposal (tier, language, reason) rides on
the clause diff into the packet and the console.

**Exhibit consistency** (`exhibit_review.py`). A clause that ties
its promises to an attached exhibit makes a claim about another
document. The data protection clause's commitments — the breach
window from its extractor signals, the sub-processor list it relies
on — become expectations checked against the DPA exhibit's own
text: `consistent`, `mismatch`, `missing_in_exhibit`, or
`exhibit_missing`. Failures land in the computed facts like clause
absences: the posture floors at negotiable, a promised-but-absent
exhibit makes the package incomplete and is requested by name, and
autonomy triage is blocked. Checks are generated only when there
is something cross-document to check, so a DPA merely referenced
between the parties is not double-flagged. Two more families run
on the same status model: **order forms** (the fees total the
payment clause relies on must be stated; payment terms or a term
the form restates are checked against the agreement's — a
restatement that differs is a `mismatch`, silence asserts nothing
and generates no check) and **statements of work** (the SOW must
list the deliverables and state the delivery dates the agreement
relies on; an acceptance period it states is checked against the
agreement's acceptance window). A fourth family (round 8) checks
**certificates of insurance**: when the agreement states the
insurance the counterparty must carry — coverage types with
minimum limits (parsed per coverage phrase, so two coverages in
one sentence keep their own amounts), a certificate to be
furnished, additional-insured or waiver-of-subrogation
endorsements — the certificate exhibit is checked on each
statement: the named insured must be the counterparty itself (a
differently-suffixed entity is a `mismatch`, not a near-miss),
each required coverage must be stated at or above its limit
(absent cover is `missing_in_exhibit`, short cover a `mismatch`),
the certificate must be in force (unexpired at receipt, and
covering a stated initial term), and required endorsements must
be shown, not merely mentioned to be withheld. The information request names the
missing exhibit by family (`facts.exhibit_missing_checks`), so a
missing order form is never requested as a missing DPA.

**Sub-processor list exhibits** (`exhibit_review.py`, round 10).
A fifth family reads a DPA's own annex — the list of
sub-processors the processor engages — against the data-processing
terms that govern it. A list is operative: it is where engagements
actually change, so its contents are checked, not just its
existence. Three checks, generated only when a list annex is
attached and the governing clause states the machinery:
`subprocessor_notice_period` (the notice period the clause
promises for changes to the list must be carried by the annex that
maintains it — absent there, `missing_in_exhibit`; shorter there,
a `mismatch`), `subprocessor_authorised_list` (where the clause
authorises sub-processors by name, every processor the list
engages must be among them — one that is not is undeclared,
however tidy the list looks), and `subprocessor_transfer_terms`
(where the transfer clause confines processing to the EEA/EU, no
listed processor may state a location outside it; a list that
states no locations cannot be checked and reports
`missing_in_exhibit`). Samples CTR-2030 (an annex that breaks all
three of its own DPA's terms) and CTR-2031 (an annex that honours
them) pin the family in the CTR-2023/2024 lineage.

**Prior-invention schedule exhibits** (`exhibit_review.py`,
round 11). A sixth family, in the employment lineage: an
invention assignment that excludes "inventions listed on the
schedule of prior inventions" makes the schedule load-bearing —
the employee's pre-existing work stays theirs only if the
schedule exists and names it. Two checks, generated when an
`ip_assignment` clause relies on such a schedule or one is
attached: `prior_invention_schedule` (the schedule must be in
the package and list inventions — promised but absent is
`exhibit_missing`, attached but empty `missing_in_exhibit`) and
`prior_invention_exclusion` (read back against an attached,
non-empty schedule: an assignment clause that never excludes
the scheduled inventions would swallow them — a `mismatch`).
CTR-2025's clean offer letter now shows the first finding,
honestly: its exclusion points at a schedule nobody attached.
Samples CTR-2032 (schedule attached, assignment swallows it)
and CTR-2033 (schedule attached and excluded — clean) pin the
family, with eval cases X13–X15.

**Severance arithmetic** (`exhibit_review.py`, round 12). A
seventh family, also in the employment lineage: a letter that
states a severance formula and a package that states a number
must agree, and the arithmetic is code's, never the model's.
The formula (months, or weeks converted) comes from the
`employment_terms` extraction's structured signal or the
clause text; the salary basis is read from the package (per
month, per annum, or a bare base-salary figure read as
annual); the stated figure is the severance sentence's own
lump sum — never the salary sentence's number, which would
make the check compare the basis against itself. Months ×
monthly basis is compared against the stated figure at a $1
tolerance: agreement is `consistent`; disagreement a
`mismatch` naming both figures and the gap; a formula and a
figure with **no salary basis anywhere** is
`missing_in_exhibit` — the arithmetic is unverifiable, which
is itself the finding. A formula with no stated figure is
checked by computing it: the check reports the computed
amount in place of a quoted one, and says that is what it
did. Samples CTR-2034 (3 months × $186,000/yr = the stated
$46,500) and CTR-2035 (4 months × $240,000/yr = $80,000
computed against a schedule stating $60,000 — a $20,000 gap)
pin the family, with eval cases X16–X17.

**Sub-processor list drift** (`subprocessor_drift.py`, round
11). The annex is a living list, so the single-round checks are
joined by a cross-round one: consecutive rounds of a chain have
their annexes diffed like clauses are — processors **added**,
**removed**, or **moved** (same processor, different stated
location) — and an addition is read against the notice clock:
it first appears in this round, so if this round was received
fewer days after the previous round than the notice period the
clause promises, the promised notice cannot have run — a
finding (`mismatch`), not just a change. Changes that break
nothing are still reported, as `consistent` with the movement
spelled out: drift is information, the notice violation is the
contradiction. The check rides on the result separately from
the single-round exhibit checks (attached by the service after
the run, like the round diff — the comparison needs the prior
round's stored package) and surfaces in the brief's exhibit
findings and the console's exhibit table.

**Prior-invention schedule drift** (`prior_invention_drift.py`,
round 12). The schedule gets the drift treatment too, and here
the direction of movement is the substance: an **addition**
only widens the record of what predates the employment
(reported, `consistent`), but a **removal** — read where the
assignment ties its exclusion to the schedule — means the
invention that left the schedule is no longer excluded: as
the chain stands, the employee assigns it away. A removal is
a `mismatch`, a transfer effected by an edit to an annex and
named as one; a schedule that vanishes entirely is the
loudest removal of all. Where the current assignment excludes
pre-employment inventions generally rather than via the
schedule, a removal changes no exclusion and is reported as
information. Like the other cross-round checks it rides on
the result separately and surfaces in the brief and console.

**Certificate renewal evidence** (`certificate_renewal.py`,
round 12). A renewal certificate is a cross-round fact too:
when a later round's certificate extends cover past the prior
round's expiry, the renewal is read against the **original**
round's requirement set — the limits, the named insured, the
endorsements the original clause required — not against
whatever terms arrived with the renewal. That asymmetry is
the point: a renewal round whose draft restates the clause
at the thinner cover its new certificate happens to carry
passes every single-round check while the bargained cover
quietly evaporates, and only the comparison against the
original terms catches it. Any original requirement the
renewal does not evidence is named as a drop: a `mismatch`;
full evidence is `consistent`, and the two samples pin both
readings — CTR-2036 renews CTR-2029 in full, CTR-2037 is the
quiet drop (professional liability $1,000,000 → $250,000, the
waiver gone, its own checks all green).

**Proposed edits** (`proposed_edits.py`). The clause diff pairs two
texts; the edit script turns the pair into instructions counsel
can paste. Each flagged clause's tier language is aligned against
the clause's own sentences in code — content-word (Jaccard)
overlap, greedy, each sentence used at most once — producing
`replace_sentence` edits for paired sentences, an `insert_after`
for language with no counterpart, a `replace_clause` when no
pairing clears the alignment threshold (stated honestly as a
clause-level edit rather than dressed up as sentence precision),
and `counsel_draft` when the playbook proposes no language. The
model may phrase language upstream; which sentence it replaces is
code's decision. Edits ride on the result, the approval webhook,
and the console, labelled draft-for-counsel; when the service
refines the diffs from the full chain, the edits are rebuilt from
the refined diffs so the two never disagree.

**Edit outcomes and reflection** (`edit_reflection.py`,
`ContractService.record_edit_outcomes`). The gate decision is
packet-level; negotiations settle edit by edit, so counsel records
a per-edit outcome on the record — accepted / modified / rejected,
a note, and for a modified edit the settled language — standalone
or carried with the approval (recorded before dispatch, so the
routed packet carries them). When a follow-up round is analysed,
the reflection check reads the superseded round's accepted/modified
edits and tests them against the new draft's text: the agreed
language is split into propositions and each is measured on its
novel content words (the words it would change relative to the
prior clause — shared boilerplate cannot inflate the score), with
one class held to an exact standard: operative tokens (figures,
capitalised names mid-sentence) must all appear, so adopting a
sentence's frame with a different state or figure named still
fails. An edit lands `reflected`, is flagged
`agreed_not_reflected`, or is `not_checkable` when no agreed
language is on record — the module states what it cannot check.
Outcomes marked after the follow-up was analysed refresh its
stored reflection, so the check never goes stale against late
bookkeeping. The verdict diff and the reflection answer different
questions — positions vs the specific agreed words — and ride side
by side on the result, the brief, and the webhook.

**Reflection patterns** (`reflection_patterns.py`,
`fallback_tiers.propose_tier(reflection_escalated=...)`).
Reflection is per-edit; a pattern is per clause type across the
chain. For each type, the two most recent *measurable* reflection
checks in the chain (reflected / agreed-not-reflected verdicts —
an uncheckable edit neither builds nor breaks the streak) are
read in chain order: if both are agreed-not-reflected and no
later check resolved the type, the type carries a pattern — a
concession agreed in one round, missing from the next draft, then
agreed and missing again. A flagged type's proposed tier
escalates to the walk-away line where the playbook carries one
(the pattern reason outranks the position and re-trade reasons),
the clause diff records `pattern_escalated`, the result carries
the patterns with their round history (flagged in / agreed in),
and the brief names them under their own heading with a
"New reflection pattern" delta line. Late-marked outcomes refresh
the stored patterns of every affected follow-up, computed from the
chain's records rather than the arrival record alone.

**Queue SLA and ownership** (`ContractService.queue` / `assign`).
The counsel queue is a worklist, not a report: each awaiting item
carries an assignee recorded on the record (who holds it, assigned
by whom, when — bookkeeping that decides nothing) and an SLA state
computed in code against an injectable clock: a budget in business
days by worst-position tier (`walk_away` 2, `review_required` 5,
`acceptable_fallback` 10, `preferred` none; `QUEUE_SLA_*` env
overrides), business days waited (Mon–Fri in (received, today]),
and a breach flag. Breaches sort ahead of posture. `/metrics`
aggregates the state (awaiting / breached / unassigned /
unassigned-breached), which is the number legal ops manages to.

**SLA breach sweep** (`ContractService.dispatch_sla_breach_sweep`).
The queue's breach flag waits for someone to look; the sweep is
its scheduled counterpart, in the deadline sweep's shape. It
walks the queue against an injectable clock and, for each contract
past budget, emits one `sla_breach` event (tier, budget vs waited,
owner — `unassigned` stated outright) and, when
`ACTION_WEBHOOK_URL` is configured, a signed webhook on the
approval channel's scheme (`dispatch_sla_breach_webhook`; the two
dispatchers share one signed-POST helper). Dedupe is a stamp on
the record (`sla_breach_notified_at`): first observation only,
however often the sweep runs, and `/metrics` counts the stamps as
`breach_events_emitted`. Exposed as a service method,
`POST /queue/sla-sweep`, the CLI's `--sweep-sla`, and a console
button.

**Counterparty concession profiles** (`concessions.py`). The
chains also aggregate sideways, per counterparty: records are
threaded into chains by their `supersedes` links, chain histories
come from `build_round_chain`, and per clause type the profile
counts contested chains closed better than they opened
(`concedes`) vs closed no better (`holds`), with re-trades,
improved/worsened step counts, and first/final position tallies —
plus rounds-to-no-walk-away per chain and counsel's decision
counts. The tendency label is a stated counting rule
(conceded > held, held > conceded, equal = mixed, never contested
= uncontested), so the profile is auditable back to the records.
It surfaces as `GET /counterparties/{name}/profile` and a console
panel, and its strongest lines (headline, concedes, holds with
re-trades first, capped at six) are injected into the service's
history dict as `concession_lines`, which
`assessment.memory_evidence_lines` renders as evidence — the next
review of that counterparty is informed by how they have actually
moved, as priors beside the posture memory and counsel feedback.

**Walk-away line acceptance tracking** (`walkaway.py`). The
profile's verdict movement says how drafts moved; it does not say
whether the positions counsel actually authorised as final — the
walk-away lines — survived. So counsel's decision now captures
them: deciding a contract whose clause diffs propose tier 2
records a `WalkAwayOutcome` per line on the decision record (the
line's language, the clause's position at decision time, the
decision itself), and each outcome resolves against the **follow-up
round** — the counterparty's next draft is the evidence. At an
acceptable position (preferred / acceptable_fallback) the final
text is at or above the line: `held`. At `review_required` it came
off the walk-away text but stopped short of acceptable:
`softened`. Still at `walk_away`: `dropped`. Absent from the
follow-up: `clause_removed`. With no follow-up in the store the
outcome stays `pending`; resolution is a pure function of the
stored chain, re-run for the whole chain whenever a new round is
analysed, so late arrivals resolve old records without counsel
touching them. The concession profile counts the outcomes —
proposed / held / softened / dropped / pending, overall and per
clause type — as `walk_away_lines`, speaks them as an evidence
line, and the console shows both the profile tallies and each
decided packet's lines with their resolutions.

**Who drops the line?** (`walkaway.py`, `concessions.py`, round
10). A resolved outcome says what the counterparty's draft did
with a line; it does not say what counsel's own desk did with it
first. Each captured line is therefore joined to the decision
record's per-edit outcomes for the edits that carried it (the
clause's tier-2 edits): `sent_intact` (every edit accepted as
proposed), `modified_by_counsel` (counsel settled their own
language for the line before it went out), `rejected_by_counsel`
(counsel rejected at least one of the line's edits — the line as
proposed never reached the counterparty), `unmarked`, or
`no_edit_on_record`. The origin is stamped at capture and
re-classified at every resolution, so outcomes marked late never
leave a stale origin blaming the wrong desk. The profile
aggregates held rates by origin (`walk_away_lines.by_origin`) and,
when both sides have resolved lines, speaks the contrast — lines
sent intact against lines counsel modified or rejected first, and
which desk the losses concentrate at. That is the calibration
fact behind every future tier policy: a line the counterparty
keeps moving and a line counsel keeps giving up look identical in
the outcome counts until the origin is joined.

**Line provenance in the brief** (`brief.py`, round 11). The
origin join is read back where counsel can still act on it: the
negotiation brief gains a **Line provenance** section ahead of
its proposed edits, warning — per decided line — when counsel's
own desk modified the line before it went out (with what the
follow-up then did with the settled language), when counsel
rejected the line's edit outright (the line never reached the
counterparty; read its outcome with that provenance), and when
a line went out intact and the counterparty moved it anyway
(provenance clean; the movement is theirs). Where most of a
clause type's decided lines in the chain were counsel-modified
and the current packet again proposes the playbook's line for
that type, the brief names the desk's own pattern while the
choice is still counsel's. New warnings also surface in the
since-the-last-brief delta.

**Negotiation brief** (`brief.py`). One markdown document per
chain, assembled in code from the stored results — never
model-composed, so every figure traces to a record: the
round-by-round chain table (posture, worst position, walk-away and
flagged counts), cumulative movement with re-trades called out,
open positions grouped by proposed tier with their language, the
proposed edits with counsel's per-edit outcomes, the reflection
flags, exhibit findings, and the obligations register. Exported
through the object-store port under
`contracts/<id>/negotiation-brief.md` and served as a download by
`GET /contracts/{id}/brief`; labelled draft for counsel review.
Exports are a lineage, not an overwrite: the service retains each
export under a versioned key and stores its source digest
(`brief_source_digest` — clause positions, edits keyed by language
hash, chain-wide outcomes, obligations, re-trade and
reflection-flag counts) in an index keyed by the chain's root
contract. A new export diffs against the previous export's digest,
whichever round it covered, and the brief leads with "Since the
last brief" (`brief_delta_lines`): new round, decision recorded,
clauses moved / appeared / disappeared, edits proposed anew,
outcomes recorded, obligations opened or closed — or, honestly,
that nothing material changed.

**Matter brief** (`matter_brief.py`). A matter is wider than a
chain: several chains sharing one counterparty and one matter tag
(intake metadata, `ContractInput.matter`). The matter brief
assembles across them, reading each chain's latest round for the
current state: a chain table (rounds, latest status and decision,
worst position, open walk-away lines), the open positions of every
chain combined and grouped by proposed tier, the reflection
patterns anywhere in the matter, the exhibit findings across all
the documents, and every obligation in the matter sorted by
deadline — dated first, in due order, undated duties after. The
export keeps the chain brief's lineage discipline under
`matters/<counterparty>--<matter>/`: versioned keys, the canonical
latest copy, and a retained source digest per export
(`matter_source_digest` — chains keyed by root with their latest
rounds, decisions, open walk-away counts, exhibit failures,
obligations), so the next export leads with "Since the last matter
brief" (`matter_delta_lines`): chains added or advanced, decisions
recorded, walk-away lines opened or closed, obligations opened or
closed, exhibit failures appearing or clearing. Served by
`ContractService.matter_brief` and `GET /matters/brief`; an
untagged contract is in no matter, and an unknown pair is a 404,
not an empty document.

**History-aware option scoring** (`options.py`). The concession
profile also feeds the options node as a scoring term — small,
explicit, capped. For a flagged clause whose type the counterparty
**concedes**, `propose_fallback_language` gains points; for a
type they **hold**, or have **re-traded** in any chain,
`escalate_to_senior_counsel` gains them. The term is one number
per option (`HISTORY_TERM_CAP`, 6 of 100), lands in the option's
`breakdown` beside the position, posture, and information terms
(the components sum to the score), and is test-pinned to order
close calls without ever outweighing the playbook terms. No
profile, no tendencies, no term: cold counterparties score exactly
as before.

**Pattern-aware option scoring** (`options.py`,
`reflection_patterns.py`). Round 8 adds the sharper sibling term.
A clause type under an active **reflection pattern** in the
contract's own chain — counsel agreed its edit, the follow-up did
not show it, counsel agreed again, and that follow-up did not show
it either — already escalates the clause's proposed tier to the
walk-away line; the options now tell the same story.
`pattern_adjustment` adds `PATTERN_TERM_CAP` (6 of 100, the same
size as the history term) to `escalate_to_senior_counsel` only,
in its own `pattern` breakdown component, separately capped from
the history term so both cousin signals stay legible. The service
computes the set from the stored rounds before the run
(`_reflection_pattern_types`, walking the supersedes links), so
the scorer reads the patterns in force when drafting begins: a
pattern that forms only on this round's own reflection moves this
round's tiers at once, and this term from the next round onward —
stated, not backdated. Patterns are chain-scoped: another chain
of the same counterparty does not leak in.

**Pattern-term calibration** (`pattern_calibration.py`, round
10). The pattern term's size — six points — is a stated guess,
and a guess never measured becomes a fact nobody chose. The
calibration report audits the term against the stored record,
per clause type: how many scored options it was **applied** to;
how many it **decided** (the option it rode on is the packet's
recommendation, and without the term it would not have been);
and, on decided records, what counsel did with the escalations —
the clause's walk-away-line edits settled, rejected at counsel's
desk, or never marked. A clause type whose term has been applied
at least twice and decided nothing is flagged a **calibration
candidate** — for counsel, at `GET /options/pattern-calibration`
with the headline in `/metrics`. The report proposes; it changes
nothing. The cap moves only when a human moves it, with this
evidence on the table — the playbook-proposal discipline applied
to the scorer itself.

**Acting on a candidate** (`calibration_store.py`,
`migrations/0004_pattern_term_suspensions.sql`, round 11). The
loop closes the way the proposals' loop does: counsel may
**suspend** the pattern term for a flagged clause type — a named
act requiring a reason, both stamped on the suspension record —
and from the next analysis the scorer does not apply the term
to that type (the service filters the chain's pattern types
through the active suspensions before scoring). The calibration
report then reads the type as **suspended**, naming who set it
aside, when, and why, instead of re-flagging it as a candidate
every run; the historical facts the act was taken on are
reported as they stand. The suspension is reversible by a
recorded **lift**, with both acts kept on the one record, so why
the scorer treats a type as it does is always answerable. The
cap itself never moves — only its application to one type is
set aside. Suspensions live in their own store, PostgreSQL in
production, and surface at
`GET /options/pattern-calibration/suspensions`, in the report,
in `/metrics`, and in the console's calibration panel.

A suspension may carry a **review-by date** (round 12): a
set-aside is a decision with a shelf life, taken on one chain's
evidence and not meant to be inherited forever unexamined. The
report reads each dated suspension against today —
`scheduled`, `due` (inside `REVIEW_WINDOW_DAYS`, 30), or
`overdue` — names the position on the suspended row ("the
set-aside is stale until counsel re-confirms or lifts it"),
and collects the due and overdue types in
`suspensions_due_review`; the same alerts ride the `/queue`
payload, since the queue is where counsel works. An overdue
suspension still binds the scorer: staleness is counsel's
prompt to re-confirm (lift and re-suspend) or lift — never the
agent quietly restoring a term counsel set aside.

**Playbook proposals** (`playbook_proposals.py`,
`proposal_store.py`, `playbook_registry.py`,
`migrations/0003_playbook_proposals.sql`). The playbook is
counsel's document, so its gaps are counsel's work queue. The
coverage analytics count, per clause type, verdicts whose cited
entries carry no fallback language; a type at or above
`MIN_GAP_FLAGS` becomes a generation candidate.
`draft_playbook_proposal` shapes a three-entry family (position,
fallback, walk-away line) from the **nearest covered type** —
deterministic by shared name tokens, then family size, then
name — recording its basis on the proposal. Language is carried
from the donor only when a name token is shared ("related");
otherwise the draft is a structure-only scaffold that says
plainly counsel drafts the substance. Proposals persist in their
own store (Postgres in production, an in-memory double in tests)
as PROPOSED with their evidence; generation is idempotent in
effect — a type already proposed, in ANY state, is never drafted
twice, so a dismissed gap stays dismissed. Activation is a named
counsel act on the API or the console panel; it registers the
proposal's entries into the active playbook (a process-local
overlay over the shipped corpus, re-registered from the store
after a restart), after which verdict citation resolution, the
missing-fallback enrichment, tier walk-away lines, the mock
assessment's playbook text, and the coverage counts all read the
merged view. Retrieval reads the same merged view: retrievers
snapshot the active playbook at construction, and the service
refreshes its retriever (`refresh()`) the moment a proposal is
activated — keyword corpora rebuild in place, semantic corpora
drop their cached vectors (the pgvector stale-entry pass embeds
the new entries), hybrid refreshes both halves — so an entry
counsel activates at 9:00 answers queries at 9:01. A retriever
built over an explicitly pinned corpus keeps that corpus;
refresh is a no-op there.

**Proposal effectiveness** (`proposal_effectiveness.py`).
Activation was where the record went quiet; now it is measured.
Every analysis stamps its record (`ApprovalRecord.analyzed_at`),
and an activated proposal's effectiveness is computed from the
stored verdicts issued at or after its activation stamp: per
entry, the verdicts citing it (off `playbook_refs`) and their
positions; per clause type, all verdicts in the window, how many
cited the new entries, how many arrived flagged, and how many
still carried **no fallback language** — the gap recurring, which
is the activation failing in the only way that matters. The view
rides on the proposal record (`GET /playbook/proposals`), at
`GET /playbook/proposals/{id}/effectiveness`, and in `/metrics`
beside the coverage analytics that started the loop. It is honest
in both directions: a structure-only activation (the "other"
scaffold, whose entries carry no language for counsel to have
written yet) reports the gap still recurring rather than dressing
the decision up, and records saved before the stamp existed are
counted as unstamped, never silently backdated into a window.

**Proposal retirement** (`ContractService.retire_playbook_proposal`,
round 10). Activation is no longer a one-way door. Counsel may
retire an activated proposal — a named act requiring a reason,
both stamped on the proposal beside the activation record they do
not rewrite — and the lifecycle closes: proposed → activated →
retired. The entries leave the active playbook and retrieval at
once (the registry drops them and the retriever refreshes, the
mirror image of activation), and a fresh process does not
re-register them: the store's status is the durable fact. The
proposal is never deleted. Its effectiveness view keeps measuring
the activation → retirement window exactly as before, and gains a
post-retirement window: verdicts for the clause type since the
entries left, and whether the coverage gap the activation had
closed has **re-opened** — the honest price of retiring, stated
on the record rather than discovered at the next flagged contract.

**Standalone DPA family** (`doc_type: "dpa"`). A data processing
addendum arriving as the primary document is normalised from its
names (data processing addendum/agreement, data protection
addendum) and measured on its own absence matrix in
`STANDARD_CLAUSES["dpa"]` — data protection, sub-processor terms,
transfers, audit rights, deletion, confidentiality — with four
clause types of its own (`subprocessor_terms`, `data_transfer`,
`audit_rights`, `data_deletion`), each with extractor signals
(objection window in days, transfer mechanisms named, audit
excluded vs direct vs reports-only, deletion at whose choice,
indefinite retention), deterministic verdict rules, a two-entry
playbook family (PB-SUB/TRF/AUD/DEL), tier lines, and an
obligations-register entry for the objection window. The samples
carry a clean DPA (CTR-2023, aligned) and an adversarial one
(CTR-2024: silent sub-processor changes, mechanism-free
transfers, audit excluded, indefinite retention — plus an
instruction planted in the transfer clause, screened and flagged
while the clause is still judged on its substance).

**Employment family** (`doc_type: "employment"`). An offer letter
or employment agreement arriving as the primary document is
normalised from its names (employment agreement/contract, offer
letter) and measured on its own absence matrix in
`STANDARD_CLAUSES["employment"]` — the at-will core, restrictive
covenants, invention assignment, dispute machinery,
confidentiality — with four clause types of its own, each with
extractor signals, deterministic verdict rules, a two-entry
playbook family, and tier lines: `non_compete` (duration in
months, geographic bound, competitor bound, solicitation-only;
over twenty-four months or any-business/nationwide scope is a
walk-away), `ip_assignment` (assignment reaching prior inventions
or unbounded own-time work is a walk-away; course-of-employment
and business-related, with prior inventions excluded, is
preferred), `arbitration` (mandatory requires binding/exclusive
framing — a bare agreement to arbitrate is voluntary; one-sided
machinery with a class waiver is a walk-away, a waiver with no
statutory carve-out needs review, mutual arbitration with
carve-outs is the acceptable fallback), and `employment_terms`
(at-will stated and preserved is preferred; only-for-cause or
guaranteed-term promises, and severance beyond twelve months,
need review). The samples carry a clean offer letter (CTR-2025 —
preferred across the matrix, at the arbitration fallback) and an
adversarial executive draft (CTR-2026: a thirty-six-month
worldwide covenant, an assignment reaching prior inventions, a
one-sided arbitration clause — plus an instruction planted in
the assignment clause, screened and flagged while the clauses are
judged on their substance). The DPA and employment families
together prove the pattern: a document type earns its own
matrix, signals, playbook family, samples, and eval cases, or it
is reviewed on a matrix that was written for another document.

**Deadline dispatch sweep** (`ContractService.dispatch_obligation_sweep`).
Per-run `obligation_due` events announce deadlines at analysis
time; the sweep is the scheduled counterpart. It walks every
stored contract against an explicit clock, emits one
`obligation_due` event per due deadline (marked
`via: deadline_sweep`), and returns a dispatch report. Report-only
by design — delivery is the event consumer's job. Exposed as a
service method, `POST /obligations/sweep`, and the CLI's
`--sweep-obligations` flag.

**Certificate renewal watch** (`certificates.py`). The exhibit
checks judge a certificate of insurance at review time; the cover
then expires mid-term and nothing notices. The watch reads the
stored checks: a contract **relies** on a certificate when its
`insurance_certificate_expiry` check came back `consistent` (one
per chain, from the chain's latest round — a renewal certificate
arriving as a later round supersedes the earlier one), and the
recorded expiry is compared against the sweep clock. Relied-upon
certificates expiring within the watch window
(`CERTIFICATE_EXPIRY_WATCH_DAYS`, default 90 — the watch's own
window, not the obligations window; overdue included) draw one
`certificate_expiring` event per sweep and a section of the
dispatch report; `GET /certificates/expiring` serves the same view
on its own. Certificates whose checks failed are deliberately
**not** watched: they are already findings on the record, and the
watch exists for the cover nobody had a reason to doubt. The
sample CTR-2029 is the demonstration — fully consistent at
review, expiring 2026-12-31, flagged by an autumn sweep.

**Closing the watch takes evidence** (round 12). A renewal
supersedes the earlier certificate for the watch only when the
renewal check (`certificate_renewal.py`, above) evidences the
original requirement set in full: a passing renewal moves the
watch to the new expiry (the row is marked `renewed`), while a
failed one leaves the watch open on the last relied-upon
expiry, with the failed renewal named on the row
(`renewal_evidence_failed`, `renewal_contract_id`) and the
sweep still announcing the original date. Arrival is not
evidence; the check is — CTR-2036's renewal closes CTR-2029's
watch, CTR-2037's does not.

## Production topology

The application container is stateless. All state lives in services:

```
            ┌────────────┐        ┌──────────────────────────────┐
 clients ──▶│  api (this │───────▶│ PostgreSQL (pgvector image)  │
            │  repo,     │        │  · contract_records (JSONB)  │
            │  non-root) │        │  · LangGraph checkpoints     │
            └─────┬──────┘        │  · playbook_embeddings       │
                  │               └──────────────────────────────┘
                  │               ┌──────────────────────────────┐
                  └──────────────▶│ S3-compatible object store   │
                                  │  contracts/<id>/… documents  │
                                  │  + drafts (MinIO in compose) │
                                  └──────────────────────────────┘
```

- **PostgreSQL is the system of record** — the decisions store and
  the LangGraph checkpointer (ecosystem `PostgresSaver`) behind the
  `Store` port, and pgvector for playbook embeddings behind the
  `Retriever` port. Schema changes are numbered SQL files in
  `migrations/`, applied by a small runner at startup; no ORM.
- **Object storage port** — documents and drafts are archived under
  object keys, never local paths (`objectstore.py`; a compact
  stdlib SigV4 client, path-style for MinIO, virtual-host for AWS).
- **Runtime posture** — multi-stage Dockerfile, non-root user,
  `/health` (liveness) and `/readiness` (database check, 503 when
  down), structured JSON logs with per-request IDs, env-only
  configuration, graceful shutdown. Compose runs this same topology
  locally — it is not a toy mode.
- **Ports everywhere** (`ports.py`): model backend, retriever,
  store, checkpointer, object store, event sink. `wiring.py`
  assembles them from the environment.
- **Worklist surfaces** — the store doubles as the operations
  queue: `/queue` (awaiting contracts sorted by posture severity,
  worst position, then age), `/obligations/due` (register deadlines
  across contracts; runs also emit `obligation_due` stream events),
  and `/metrics` playbook-coverage analytics (entry firing counts,
  clause types lacking fallback language, per-counterparty position
  distribution). All are pure reads over stored results — no new
  state.
- **Negotiation rounds** — intake may name a superseded round
  (`supersedes`); the service then aligns clauses across the two
  rounds in code (`round_diff.py`: by clause type, heading match
  before document order) and attaches the verdict movement to the
  new result. The pipeline itself stays single-contract; the diff
  is a store-level composition of two finished reviews.

In-memory implementations of the store/checkpointer/vector index
exist **only as test doubles** inside the test suite, so tests stay
hermetic; Postgres integration tests are gated on `DATABASE_URL` and
skip without it. They are never a documented runtime option.

## Resilience and budgets

Provider calls retry only on transient `ProviderError`s, and only
for idempotent nodes (extract, classify, assess, options, verify,
review) — drafting and the gate never retry blindly. A per-run
token budget degrades later provider steps to their template paths
rather than failing the run; telemetry (calls, tokens, estimated
cost, latency, budget state) rides on every result.

## What the agent will never do

State a legal conclusion; approve or sign a contract; send redlines
or any message to a counterparty; treat counterparty text as
instructions. Those are invariants enforced by the guardrail layer
and the gate design, not by prompt wording.
