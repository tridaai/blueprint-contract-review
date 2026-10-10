"""Retrieval behind a small interface — the LlamaIndex-style layer.

Three implementations ship in the repo, selected with the ``RETRIEVER``
environment variable (see :func:`get_retriever`):

- ``keyword`` (default) — transparent token overlap, deterministic, offline.
- ``semantic`` — embeds the playbook corpus and the query with an embeddings
  API and ranks by cosine similarity. Embeddings come from the local
  Ollama server when ``MODEL_BACKEND=ollama``, otherwise from OpenAI
  (or an OpenAI-compatible endpoint via ``OPENAI_BASE_URL``): Anthropic
  has no embeddings API, so ``MODEL_BACKEND=anthropic`` still needs
  ``OPENAI_API_KEY`` for this retriever — the failure message says
  exactly that when the key is missing.
- ``hybrid`` — runs both, merges the candidate pools, and applies an
  explicit rerank step: reciprocal-rank score fusion in code (see
  :func:`rerank_fused`). This is score-fusion reranking, not a
  cross-encoder — the repo ships no cross-encoder and claims none.

Vectors live in **pgvector**, in the same PostgreSQL that holds the
records: the corpus embeddings are written to ``playbook_embeddings``
(``migrations/0002``) on first use and ranked in SQL by cosine
distance. With no ``DATABASE_URL`` configured (the offline demo, the
test suite), the semantic retriever falls back to in-memory cosine
over freshly embedded vectors behind the same interface — the
``vector_store`` attribute on the result says which one served, and
it is recorded on every run. Production deployments can equally swap
in LlamaIndex over their own store; the agent only depends on the
``Retriever`` protocol, so that swap touches one file, not the graph.
"""

from __future__ import annotations

import math
import re

from .config import env_float, env_str, load_dotenv
from .errors import translate_construction_error, translate_provider_error
from .model_backends import DEFAULT_TIMEOUT_SECONDS, _missing_sdk_error
from .playbook_data import PLAYBOOK
from .schemas import RetrievedPlaybook

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "was",
    "by", "with", "when", "if", "it", "its", "are", "be", "at", "as",
}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS]


# The Retriever protocol is declared in ports.py (the seam registry);
# it is re-exported here so existing imports keep working.
from .ports import Retriever  # noqa: E402,F401


class KeywordRetriever:
    """Transparent keyword-overlap retriever over the playbook corpus.

    Deliberately simple and deterministic so evals and tests are stable
    offline. Score = shared tokens normalised by playbook length.
    """

    name = "keyword"

    def __init__(self, playbooks: list[dict[str, str]] | None = None) -> None:
        self._playbooks = playbooks if playbooks is not None else PLAYBOOK
        self._index = [
            (p, set(_tokens(f"{p['title']} {p['text']}"))) for p in self._playbooks
        ]

    def retrieve(self, query: str, top_k: int = 3) -> list[RetrievedPlaybook]:
        query_tokens = set(_tokens(query))
        if not query_tokens:
            return []
        scored: list[RetrievedPlaybook] = []
        for playbook, tokens in self._index:
            overlap = len(query_tokens & tokens)
            if overlap == 0:
                continue
            score = round(overlap / math.sqrt(len(tokens)), 4)
            scored.append(
                RetrievedPlaybook(
                    playbook_id=playbook["id"],
                    title=playbook["title"],
                    text=playbook["text"],
                    clause_type=playbook.get("clause_type", ""),
                    fallback_language=playbook.get("fallback_language", ""),
                    walk_away_line=playbook.get("walk_away_line", ""),
                    score=score,
                    sources=["keyword"],
                )
            )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class SemanticRetriever:
    """Cosine-similarity retriever over the playbook corpus, via embeddings.

    The corpus is embedded once (lazily, on first use) and cached; each
    query is embedded per call. Same return shape as the keyword
    retriever: ``RetrievedPlaybook`` snippets with similarity scores.
    """

    name = "semantic"

    def __init__(
        self,
        playbooks: list[dict[str, str]] | None = None,
        *,
        mode_label: str = "semantic",
    ) -> None:
        self._playbooks = playbooks if playbooks is not None else PLAYBOOK
        # The RETRIEVER value this instance serves ("semantic", or
        # "hybrid" when the hybrid retriever owns it) — error messages
        # name the value the operator actually set, never a sibling mode.
        self._mode_label = mode_label
        (
            self._client,
            self._model,
            self._embeddings_backend,
            self._embeddings_base_url,
        ) = self._build_embeddings_client()
        self._corpus_vectors: list[list[float]] | None = None
        # Which store actually serves queries: "pgvector" when
        # DATABASE_URL points at the production Postgres, else "memory"
        # (in-repo cosine over freshly embedded vectors — the offline
        # demo / test fallback).
        self.vector_store = "memory"
        self._pg_ready = False
        self._pg_checked = False

    def _build_embeddings_client(self):
        """Resolve the embeddings client, failing loudly when unusable.

        Embeddings follow the configured stack:

        - ``MODEL_BACKEND=ollama`` → embeddings from the local Ollama
          server (``OLLAMA_EMBEDDING_MODEL``, default ``nomic-embed-text``)
          — fully local, no cloud key.
        - Otherwise → OpenAI (or an OpenAI-compatible endpoint via
          ``OPENAI_BASE_URL``), regardless of the drafting backend:
          Anthropic offers no embeddings API. That is stated, not hidden:
          with ``MODEL_BACKEND=anthropic`` (or ``mock``) an
          ``OPENAI_API_KEY`` is still required, and the error says so.

        Returns (client, model, provider_name, base_url).
        """
        load_dotenv()
        backend = (env_str("MODEL_BACKEND") or "mock").lower()
        timeout = env_float("LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        if backend == "ollama":
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise _missing_sdk_error("ollama (embeddings)", "OpenAI") from exc
            base_url = env_str("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            try:
                client = OpenAI(
                    api_key="ollama",  # placeholder — Ollama ignores it
                    base_url=base_url,
                    timeout=timeout,
                    max_retries=0,
                )
            except Exception as exc:  # construction failures are translated too
                raise translate_construction_error(
                    exc,
                    backend=f"ollama embeddings (RETRIEVER={self._mode_label})",
                    base_url=base_url,
                ) from exc
            return client, env_str("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"), "ollama", base_url
        if not env_str("OPENAI_API_KEY"):
            hint = (
                f" MODEL_BACKEND={backend} has no embeddings API to use instead —"
                " drafting and embeddings are separate providers here."
                if backend != "openai"
                else ""
            )
            raise RuntimeError(
                f"RETRIEVER={self._mode_label} needs embeddings, which run through OpenAI, "
                "but OPENAI_API_KEY is not set." + hint
                + " Add OPENAI_API_KEY=<your key> to the .env file in the repo root "
                "(copy .env.example to .env) or export it, set MODEL_BACKEND=ollama "
                "to embed locally with Ollama, or set RETRIEVER=keyword."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise _missing_sdk_error("openai (embeddings)", "OpenAI") from exc
        client_kwargs: dict = {
            "api_key": env_str("OPENAI_API_KEY"),
            "timeout": timeout,
            "max_retries": 0,
        }
        base_url = env_str("OPENAI_BASE_URL")
        if base_url:
            client_kwargs["base_url"] = base_url
        model = env_str("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        try:
            client = OpenAI(**client_kwargs)
        except Exception as exc:  # construction failures are translated too
            raise translate_construction_error(
                exc,
                backend=f"openai embeddings (RETRIEVER={self._mode_label})",
                base_url=base_url or "https://api.openai.com/v1",
            ) from exc
        return (client, model, "openai", base_url or "https://api.openai.com/v1")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self._client.embeddings.create(model=self._model, input=texts)
        except Exception as exc:
            raise translate_provider_error(
                exc,
                backend=f"{self._embeddings_backend} embeddings (RETRIEVER={self._mode_label})",
                base_url=self._embeddings_base_url,
            ) from exc
        ordered = sorted(response.data, key=lambda d: getattr(d, "index", 0))
        return [list(d.embedding) for d in ordered]

    def _pg_connect(self):
        import psycopg

        return psycopg.connect(env_str("DATABASE_URL"))

    def _ensure_pgvector(self) -> bool:
        """Lazily prepare the pgvector store, embedding the corpus once.

        Returns True when pgvector serves this retriever: DATABASE_URL
        is set, the migrations have run, and every corpus entry has an
        embedding written by the CURRENT model (a model switch
        re-embeds rather than mixing vector spaces). Any failure —
        no database configured, driver missing, server unreachable —
        leaves the in-memory fallback serving, with ``vector_store``
        recording that pgvector did not serve.
        """
        if self._pg_checked:
            return self._pg_ready
        self._pg_checked = True
        if not env_str("DATABASE_URL"):
            return False
        try:
            from .migrations import run_migrations

            with self._pg_connect() as conn:
                run_migrations(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT policy_id, model FROM playbook_embeddings"
                    )
                    existing = {row[0]: row[1] for row in cur.fetchall()}
                stale = [
                    p for p in self._playbooks
                    if existing.get(p["id"]) != self._model
                ]
                if stale:
                    texts = [f"{p['title']} {p['text']}" for p in stale]
                    vectors = self._embed(texts)
                    with conn.cursor() as cur:
                        for playbook, vector in zip(stale, vectors):
                            literal = "[" + ",".join(repr(float(v)) for v in vector) + "]"
                            cur.execute(
                                """
                                INSERT INTO playbook_embeddings (policy_id, model, embedding, updated_at)
                                VALUES (%s, %s, %s::vector, now())
                                ON CONFLICT (policy_id) DO UPDATE
                                SET model = EXCLUDED.model,
                                    embedding = EXCLUDED.embedding,
                                    updated_at = now()
                                """,
                                (playbook["id"], self._model, literal),
                            )
                    conn.commit()
            self._pg_ready = True
            self.vector_store = "pgvector"
        except Exception:  # a broken store must not break retrieval
            self._pg_ready = False
        return self._pg_ready

    def _retrieve_pgvector(self, query: str, top_k: int) -> list[RetrievedPlaybook]:
        query_vector = self._embed([query])[0]
        literal = "[" + ",".join(repr(float(v)) for v in query_vector) + "]"
        by_id = {p["id"]: p for p in self._playbooks}
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT policy_id, 1 - (embedding <=> %s::vector) AS similarity
                    FROM playbook_embeddings
                    WHERE model = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (literal, self._model, literal, min(top_k, len(self._playbooks))),
                )
                rows = cur.fetchall()
        retrieved: list[RetrievedPlaybook] = []
        for playbook_id, similarity in rows:
            playbook = by_id.get(playbook_id)
            if playbook is None:  # foreign id in a shared store — skip it
                continue
            retrieved.append(
                RetrievedPlaybook(
                    playbook_id=playbook_id,
                    title=playbook["title"],
                    text=playbook["text"],
                    clause_type=playbook.get("clause_type", ""),
                    fallback_language=playbook.get("fallback_language", ""),
                    walk_away_line=playbook.get("walk_away_line", ""),
                    score=round(float(similarity), 4),
                    sources=["semantic"],
                )
            )
        return retrieved

    def retrieve(self, query: str, top_k: int = 3) -> list[RetrievedPlaybook]:
        if self._ensure_pgvector():
            return self._retrieve_pgvector(query, top_k)
        if self._corpus_vectors is None:
            self._corpus_vectors = self._embed(
                [f"{p['title']} {p['text']}" for p in self._playbooks]
            )
        query_vector = self._embed([query])[0]
        scored = [
            RetrievedPlaybook(
                playbook_id=playbook["id"],
                title=playbook["title"],
                text=playbook["text"],
                clause_type=playbook.get("clause_type", ""),
                fallback_language=playbook.get("fallback_language", ""),
                walk_away_line=playbook.get("walk_away_line", ""),
                score=round(_cosine(query_vector, vector), 4),
                sources=["semantic"],
            )
            for playbook, vector in zip(self._playbooks, self._corpus_vectors)
        ]
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]


def rerank_fused(
    keyword_results: list[RetrievedPlaybook],
    semantic_results: list[RetrievedPlaybook],
    top_k: int = 3,
    *,
    rrf_k: int = 60,
) -> list[RetrievedPlaybook]:
    """The hybrid rerank step: reciprocal-rank score fusion, in code.

    Keyword scores (token overlap) and semantic scores (cosine) live on
    incomparable scales, so the merge never compares them directly.
    Each list contributes ``1 / (rrf_k + rank)`` per playbook; the fused
    score is the sum across lists, and playbooks found by both retrievers
    rank above single-list finds at similar ranks. The returned score
    IS the fused score — labelled as such via ``retrieval`` ("keyword",
    "semantic", or "keyword+semantic"). This is score-fusion reranking:
    transparent and deterministic, not a cross-encoder model.
    """
    fused: dict[str, dict] = {}
    for source, results in (("keyword", keyword_results), ("semantic", semantic_results)):
        for rank, result in enumerate(results):
            entry = fused.setdefault(
                result.playbook_id, {"playbook": result, "score": 0.0, "sources": []}
            )
            entry["score"] += 1.0 / (rrf_k + rank + 1)
            entry["sources"].append(source)
            if result.score > entry["playbook"].score:
                entry["playbook"] = result
    merged = sorted(
        fused.values(), key=lambda e: (-e["score"], e["playbook"].playbook_id)
    )
    reranked: list[RetrievedPlaybook] = []
    for entry in merged[:top_k]:
        sources = [s for s in ("keyword", "semantic") if s in entry["sources"]]
        reranked.append(
            entry["playbook"].model_copy(
                update={"score": round(entry["score"], 4), "sources": sources}
            )
        )
    return reranked


class HybridRetriever:
    """Keyword + semantic retrieval with an explicit rerank step.

    Pipeline: both retrievers produce a candidate pool → the pools are
    merged by playbook ID → :func:`rerank_fused` reranks by reciprocal-rank
    fusion → the top-k are returned and cited exactly like the other
    retrievers' results. ``last_stats`` records the pool sizes of the
    most recent call so the trace can show merge → rerank honestly.
    """

    name = "hybrid"

    def __init__(self, playbooks: list[dict[str, str]] | None = None) -> None:
        self._keyword = KeywordRetriever(playbooks)
        self._semantic = SemanticRetriever(playbooks, mode_label="hybrid")
        self.last_stats: dict[str, int] = {}

    @property
    def vector_store(self) -> str:
        """The store serving the semantic half (pgvector | memory)."""
        return self._semantic.vector_store

    def retrieve(self, query: str, top_k: int = 3) -> list[RetrievedPlaybook]:
        pool = top_k * 2
        keyword_results = self._keyword.retrieve(query, top_k=pool)
        semantic_results = self._semantic.retrieve(query, top_k=pool)
        self.last_stats = {
            "keyword_pool": len(keyword_results),
            "semantic_pool": len(semantic_results),
        }
        return rerank_fused(keyword_results, semantic_results, top_k=top_k)


def get_retriever(name: str | None = None) -> Retriever:
    """Select the retriever by name or the RETRIEVER env var (default: keyword).

    Loads the repo-root ``.env`` first (real environment variables win), so
    every surface — API, CLI, traced demo — selects the same way.
    """
    load_dotenv()
    selected = (name or env_str("RETRIEVER") or "keyword").lower()
    if selected == "keyword":
        return KeywordRetriever()
    if selected == "semantic":
        return SemanticRetriever()
    if selected == "hybrid":
        return HybridRetriever()
    raise ValueError(
        f"Unknown RETRIEVER: {selected!r} (expected keyword | semantic | hybrid)"
    )
