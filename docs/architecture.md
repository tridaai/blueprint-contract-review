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
| ingest | Contract types normalized (msa/sow/nda/amendment/order_form + aliases); text segmented into numbered clause units in code | code |
| extract | Per-clause type + structured facts (cap months/amounts, net days, notice periods, governing law, DPA, assignment) with confidence; optional LLM extraction cross-check; discrepancies recorded, never fatal | rules + LLM check |
| screen | Injection screening: counterparty text is untrusted. AI-directed instructions are flagged and fenced out of prompts/drafts; the clause is still reviewed on substance | code |
| classify | Per-clause playbook position from the deterministic matrix, cross-checked by an LLM per clause; disagreements escalate to ≥ review_required; resolution recorded per verdict | rules, LLM advises |
| facts | Document posture counted in code: position counts, worst position, absence detection (missing standard clauses; liability cap + data protection are critical absences); exhibit consistency checks (the DPA exhibit's text vs the clause's promises); the obligations register | code |
| retrieve | Playbook entries by keyword / semantic / hybrid (RRF fusion); query built from clause content | code |
| assess | Risk summary composed over evidence; with a provider, a bounded tool loop (`search_playbook`, `clause_facts`, `counterparty_history`); template fallback offline | LLM over evidence |
| options | Per flagged clause, options scored in code (position fit + posture fit + package completeness) | code |
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

Four service-level capabilities sit on top of the per-contract
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
between the parties is not double-flagged.

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
