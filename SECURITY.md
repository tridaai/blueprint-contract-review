# Security

## Prototype — not hardened for exposure

This blueprint is a **reference prototype**. Optional API-key auth exists
(`API_KEY`), but it is a shared-secret gate for local demos, not an
identity system.

- Run it locally or inside a network you control.
- **Do not expose it to the public internet** as-is. A production
  deployment needs authenticated access, per-client data isolation,
  rate limiting, and secrets management beyond environment variables.

## Data

- All bundled data is synthetic. Never put a real contract, a real
  counterparty's terms, or any privileged legal material into this
  prototype, and never include real data in issues, pull requests,
  logs, or screenshots.
- Contract text is treated as untrusted input throughout: it is
  screened for prompt injection at ingest, flagged sentences are
  fenced out of prompts and drafts, and the guardrail layer — not the
  model — decides what may reach the review packet and the
  counterparty response. The agent never states a legal conclusion
  and never approves a contract; drafts stop at counsel's gate.
- The code reads optional LLM API keys from environment variables. The
  application loads a `.env` file from the repo root at startup (real
  environment variables take precedence); `.env.example` lists every
  variable. `.env` is git-ignored — never commit a filled-in one.
- In the production topology, records live in PostgreSQL and
  documents in S3-compatible object storage. Apply your
  organisation's database/object-store access controls, encryption,
  and retention policies before any real use.

## Reporting a concern

If you find a security concern in this repository, please report it
privately through Trida AI's contact route at
https://trida.ai/intake rather than opening a public issue.
