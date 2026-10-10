# Trida AI Blueprint — Contract Review Agent
#
# Multi-stage: the builder resolves the pinned dependency set into a
# venv; the runtime image carries only the venv + the package, runs as
# a non-root user, and is stateless — records and checkpoints live in
# PostgreSQL, documents in S3 (see docker-compose.yml).
#
# NOTE: this image definition is maintained in-repo but has NOT been
# build-verified in CI (labelled build-unverified, like every Trida
# blueprint). Verify locally with: docker build -t contract-review .
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml constraints.txt README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -c constraints.txt ".[llm]"

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Trida AI Blueprint — Contract Review Agent" \
      org.opencontainers.image.description="Reference prototype: first-pass contract review against a playbook, stopping at counsel's gate. Build-unverified." \
      trida.blueprint="contract-review" \
      trida.build_verified="false"

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY src ./src
COPY evals ./evals
COPY migrations ./migrations
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

# Graceful shutdown: uvicorn handles SIGTERM by finishing in-flight
# requests; the process holds no state of its own.
CMD ["uvicorn", "contract_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
