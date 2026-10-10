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
| facts | Document posture counted in code: position counts, worst position, absence detection (missing standard clauses; liability cap + data protection are critical absences) | code |
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
terms" in the counterparty draft), `no_materiality_minimising` (no
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
