# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Stage 1 – dependency builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools
RUN pip install --no-cache-dir hatchling

COPY pyproject.toml README.md ./
COPY src/ ./src/

# Build the wheel
RUN pip wheel --no-deps --wheel-dir /wheels .

# ---------------------------------------------------------------------------
# Stage 2 – runtime image
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Default to info; override with REMEMBRA_LOG_LEVEL
    REMEMBRA_LOG_LEVEL=info

# Install runtime deps (including the wheel we built)
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

WORKDIR /app

# Non-root user for security
RUN addgroup --system remembra && adduser --system --ingroup remembra remembra
USER remembra

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/health')"

CMD ["remembra"]
