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
| options | Per flagged clause, options scored in code (position fit + posture fit + package completeness + a capped history term from the counterparty profile) | code |
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
agreement's acceptance window). The information request names the
missing exhibit by family (`facts.exhibit_missing_checks`), so a
missing order form is never requested as a missing DPA.

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
merged view. One honest limit: the retrieval corpus snapshots
the shipped playbook when a retriever is built, so activated
entries join keyword/semantic retrieval on the next rebuild —
citations and tiers see them immediately.

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

**Deadline dispatch sweep** (`ContractService.dispatch_obligation_sweep`).
Per-run `obligation_due` events announce deadlines at analysis
time; the sweep is the scheduled counterpart. It walks every
stored contract against an explicit clock, emits one
`obligation_due` event per due deadline (marked
`via: deadline_sweep`), and returns a dispatch report. Report-only
by design — delivery is the event consumer's job. Exposed as a
service method, `POST /obligations/sweep`, and the CLI's
`--sweep-obligations` flag.

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
