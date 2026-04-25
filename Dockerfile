# Remembra - Production Dockerfile
# Multi-stage build for minimal image size

# =============================================================================
# Stage 1: Build Dashboard
# =============================================================================
FROM node:20-alpine AS dashboard-builder

WORKDIR /app/dashboard

# Install dependencies
COPY dashboard/package*.json ./
RUN npm ci

# Build dashboard
COPY dashboard/ ./
RUN npm run build

# =============================================================================
# Stage 2: Python Dependencies
# =============================================================================
FROM python:3.11-slim AS python-builder

WORKDIR /app

# Install build dependencies (including cryptography deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[server,encryption,cloud]"

# =============================================================================
# Stage 3: Production Image
# =============================================================================
FROM python:3.11-slim AS production

LABEL org.opencontainers.image.title="Remembra"
LABEL org.opencontainers.image.description="AI Memory Layer - Self-hosted"
LABEL org.opencontainers.image.url="https://github.com/remembra-ai/remembra"
LABEL org.opencontainers.image.vendor="Remembra"

# Optional build identifier (e.g. git SHA) surfaced via /health for deployment verification.
ARG REMEMBRA_BUILD_SHA=""

# Install runtime dependencies for cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r remembra && useradd -r -g remembra remembra

WORKDIR /app

# Copy Python environment
COPY --from=python-builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy dashboard build
COPY --from=dashboard-builder /app/dashboard/dist /app/static

# Copy application code
COPY src/ ./src/

# Create data directory
RUN mkdir -p /data && chown -R remembra:remembra /data

# Environment variables
ENV REMEMBRA_HOST=0.0.0.0
ENV REMEMBRA_PORT=8787
ENV REMEMBRA_DATABASE_URL=sqlite:////data/remembra.db
ENV REMEMBRA_QDRANT_URL=http://localhost:6333
ENV REMEMBRA_STATIC_DIR=/app/static
ENV REMEMBRA_BUILD_SHA=${REMEMBRA_BUILD_SHA}
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8787

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/health')" || exit 1

# Switch to non-root user
USER remembra

# Run the application
CMD ["python", "-m", "remembra.main"]
