"""Application settings resolved from environment variables."""

import json
import warnings
from datetime import datetime
from typing import Annotated, Any

from pydantic import AliasChoices, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# A list setting read from the environment. pydantic-settings would JSON-decode
# it (and crash the boot on "a.com,b.com"); NoDecode hands the raw string to
# Settings.parse_env_list, which accepts a JSON array or a comma-separated list.
EnvList = Annotated[list[str], NoDecode]

LIST_SETTINGS = (
    "cors_origins",
    "owner_emails",
    "signup_domain_limit_exempt",
    "trusted_proxies",
    "superadmin_user_ids",
    "connector_redirect_uris",
    "pii_exclusions",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REMEMBRA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Ignore unknown env vars so retired settings (e.g. leftover
        # REMEMBRA_STRIPE_* secrets in a deployed environment) never break boot.
        extra="ignore",
        # Aliased settings (e.g. typesafe_api_key <- TYPESAFE_API_KEY) can also
        # be passed by field name in code and tests.
        populate_by_name=True,
    )

    # -----------------------------------------------------------------------
    # Server
    # -----------------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8787
    debug: bool = False
    log_level: str = "info"
    build_sha: str | None = Field(
        None,
        description="Optional build identifier (git SHA) surfaced in /health for deployment verification.",
    )
    static_dir: str | None = Field(None, description="Directory for static files (dashboard UI). Set to enable serving.")

    # CORS
    cors_origins: EnvList = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:8787",
            "https://app.remembra.dev",
            "https://remembra.dev",
        ],
        description="Allowed CORS origins. Set to ['*'] only for development.",
    )
    cors_filter_localhost_in_production: bool = Field(
        True,
        description="Automatically remove localhost origins when debug=False",
    )

    # -----------------------------------------------------------------------
    # Qdrant (vector store)
    # -----------------------------------------------------------------------
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "memories"

    # -----------------------------------------------------------------------
    # Relational / metadata store
    # -----------------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///remembra.db"

    # -----------------------------------------------------------------------
    # Embeddings
    # -----------------------------------------------------------------------
    embedding_provider: str = Field(
        "openai",
        description="openai | azure_openai | cohere | ollama | voyage | jina",
    )
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    openai_api_key: str | None = None
    ollama_url: str = "http://localhost:11434"

    # Additional embedding provider keys
    cohere_api_key: str | None = None
    voyage_api_key: str | None = None
    jina_api_key: str | None = None

    # Azure OpenAI
    azure_openai_api_key: str | None = None
    azure_openai_endpoint: str = Field(
        "",
        description="Azure OpenAI endpoint (https://{resource}.openai.azure.com)",
    )
    azure_openai_deployment: str = Field(
        "",
        description="Azure OpenAI deployment name for embeddings",
    )
    azure_openai_api_version: str = "2024-02-01"

    # -----------------------------------------------------------------------
    # LLM models. Which setting drives which task (logged once at startup as
    # ``llm_task_models``):
    #   * extraction_model  -> fact extraction, consolidation (also the
    #     sleep-time pass), entity matching, conversation ingest. Always sent
    #     to the OpenAI API, so it must be an OpenAI model.
    #   * llm_provider      -> the entity-extraction backend only.
    #   * llm_model         -> entity extraction only, and only when
    #     extraction_model does not fit llm_provider (e.g. a Claude model for
    #     llm_provider=anthropic). It never changes fact extraction.
    # -----------------------------------------------------------------------
    llm_provider: str = Field(
        "openai",
        description="Entity-extraction backend: openai | anthropic | ollama. Fact extraction, consolidation, "
        "entity matching and conversation ingest always use OpenAI.",
    )
    llm_model: str = Field(
        "gpt-4o-mini",
        description="Entity-extraction model, used only when extraction_model does not fit llm_provider "
        "(e.g. claude-haiku-4-5 with llm_provider=anthropic). Does not affect fact extraction or consolidation.",
    )
    llm_base_url: str | None = Field(None, description="Not read yet: every OpenAI call uses the default API URL.")
    anthropic_api_key: str | None = None

    # -----------------------------------------------------------------------
    # Intelligent Extraction (Week 4)
    # -----------------------------------------------------------------------
    smart_extraction_enabled: bool = Field(True, description="Enable LLM-powered fact extraction")
    extraction_model: str = Field(
        "gpt-4o-mini",
        description="OpenAI model for fact extraction, consolidation (incl. sleep-time), entity matching and "
        "conversation ingest; also entity extraction when llm_provider=openai. Must be an OpenAI model.",
    )
    extraction_max_facts: int = Field(
        25,
        ge=1,
        description="Maximum facts kept per extraction chunk. Truncation is logged and flagged, never silent.",
    )
    extraction_chunk_chars: int = Field(
        8000,
        ge=500,
        description="Long inputs are split into chunks of about this many characters before extraction.",
    )
    consolidation_threshold: float = Field(
        0.6,
        description=(
            "Minimum vector similarity for an existing memory to be considered a "
            "consolidation candidate (duplicate / supersede) for a new fact."
        ),
    )
    consolidation_candidate_limit: int = Field(5, ge=1, le=20, description="Max consolidation candidates per fact")
    supersede_min_confidence: float = Field(
        0.7,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum decision confidence for the LLM consolidator to retire (supersede) "
            "an existing memory. Below it the new fact is added and the pair is flagged."
        ),
    )
    grounding_action: str = Field(
        "drop",
        description=(
            "What to do with an extracted fact that is not supported by its source text: "
            "'drop' (default; reported in the store response) or 'flag' (store with verified=false)."
        ),
    )

    # -----------------------------------------------------------------------
    # TypeSafe / Jev decisions (UPG-5)
    # -----------------------------------------------------------------------
    typesafe_api_key: str | None = Field(
        None,
        description="TypeSafe API key. Enables Jev decisions (shadow mode by default).",
        validation_alias=AliasChoices("REMEMBRA_TYPESAFE_API_KEY", "TYPESAFE_API_KEY"),
    )
    typesafe_mode: str | None = Field(
        None,
        description=(
            "off | shadow | enforce. Unset = 'shadow' when a TypeSafe key is configured, "
            "'off' otherwise. shadow: the LLM decides, Jev is logged to decision_log. "
            "enforce: Jev decides, with LLM fallback on any Jev error."
        ),
    )
    typesafe_base_url: str = Field("https://api.typesafe.ai", description="TypeSafe API base URL")
    typesafe_model: str = Field("jev-latest", description="TypeSafe model name")
    typesafe_timeout: float = Field(2.0, gt=0, description="Per-request TypeSafe timeout in seconds")
    typesafe_usd_per_request: float = Field(
        0.0005,
        ge=0,
        description=(
            "Dollar cost metered per TypeSafe request (smart credits on writes; free-breaker spend on recalls). "
            "Set it to your TypeSafe contract price."
        ),
    )
    typesafe_supersede_threshold: float = Field(
        0.85, ge=0.0, le=1.0, description="Enforce mode: min P(supersedes) to retire an existing memory"
    )
    typesafe_duplicate_threshold: float = Field(
        0.85, ge=0.0, le=1.0, description="Enforce mode: min P(duplicate) to skip a fact as already known"
    )
    typesafe_grounding_threshold: float = Field(
        0.5, ge=0.0, le=1.0, description="Enforce mode: min grounding probability for a fact to count as supported"
    )
    typesafe_entity_match_threshold: float = Field(
        0.8, ge=0.0, le=1.0, description="Enforce mode: min coreference probability to merge an entity mention"
    )
    typesafe_intent_threshold: float = Field(
        0.7, ge=0.0, le=1.0, description="Enforce mode: min Jev confidence to apply its recall query-intent mode"
    )

    # -----------------------------------------------------------------------
    # Store reliability
    # -----------------------------------------------------------------------
    idempotency_ttl_hours: int = Field(24, ge=1, description="How long a completed Idempotency-Key is remembered")
    store_pending_on_embedding_failure: bool = Field(
        True,
        description=(
            "When the embedding provider fails (quota, outage, open circuit), keep the fact in SQLite + FTS, "
            "queue it for the re-embedding worker and answer status='pending' instead of failing the store"
        ),
    )
    store_time_budget_seconds: float = Field(
        45.0,
        gt=0,
        description=(
            "Overall budget for one store. Past it, optional steps degrade: extraction stores verbatim, "
            "consolidation adds without a model decision, embedding is deferred to the pending queue"
        ),
    )
    idempotency_inflight_timeout_seconds: int = Field(
        300,
        ge=10,
        description="An in-flight Idempotency-Key older than this is treated as abandoned and may be retried",
    )
    batch_store_concurrency: int = Field(4, ge=1, le=16, description="Parallel item stores in POST /memories/batch")

    # -----------------------------------------------------------------------
    # Lossless Memory (provenance-grade fidelity)
    # -----------------------------------------------------------------------
    enable_source_records: bool = Field(
        True,
        description=(
            "Preserve the verbatim original content as an immutable source record "
            "whenever extraction derives facts from it. Derived facts carry a "
            "source_id receipt pointing back to the exact original text."
        ),
    )
    fact_verification_threshold: float = Field(
        0.5,
        description=(
            "Minimum content-word overlap between a derived fact and its source "
            "text for the fact to be marked verified=true. Facts below the "
            "threshold are stored but flagged verified=false (possible drift or "
            "hallucination in extraction)."
        ),
    )
    async_enrichment: bool = Field(
        False,
        description=(
            "When true, store() persists the verbatim source record and returns "
            "immediately; fact extraction/consolidation run in the background. "
            "Cuts store latency to a single embed call, but the store response "
            "no longer includes derived facts (they appear once enrichment "
            "completes)."
        ),
    )

    # -----------------------------------------------------------------------
    # Entity Resolution (Week 5)
    # -----------------------------------------------------------------------
    enable_entity_resolution: bool = Field(True, description="Enable entity extraction and resolution")
    entity_matching_threshold: float = Field(0.6, description="Minimum confidence for entity matching")

    # -----------------------------------------------------------------------
    # Advanced Retrieval (Week 6)
    # -----------------------------------------------------------------------
    # Hybrid Search (FTS5 + Vector)
    enable_hybrid_search: bool = Field(True, description="Enable FTS5 BM25 keyword search alongside vector search")
    hybrid_alpha: float = Field(0.4, description="Weight for keyword (BM25) in hybrid fusion. Research default: 0.4")

    # Reranking (CrossEncoder)
    enable_reranking: bool = Field(True, description="Enable CrossEncoder reranking for improved accuracy")
    rerank_model: str = Field("cross-encoder/ms-marco-MiniLM-L-6-v2", description="HuggingFace model for reranking")
    rerank_top_k: int = Field(20, description="Rerank top K results from hybrid search")
    rerank_min_logit: float | None = Field(
        None,
        description=(
            "Absolute CrossEncoder cutoff: candidates whose raw logit is below it are dropped "
            "(ms-marco models: > 0 relevant, < -5 clearly irrelevant). None = keep all"
        ),
    )

    # Recall behaviour (RET stream)
    recall_keyword_fallback: bool = Field(
        True,
        description="If the query cannot be embedded, answer from keyword + graph search with degraded='keyword_only'",
    )
    recall_max_candidates: int = Field(
        500, ge=10, description="Upper bound on vector hits scanned per recall while filling the requested limit"
    )
    recall_dedup_similarity: float = Field(
        0.9, ge=0.0, le=1.0, description="Word-overlap (Jaccard) at or above which a lower-ranked result is a near-duplicate"
    )
    ranking_feedback_weight: float = Field(
        0.05, ge=0.0, le=1.0, description="Weight of net helpful/unhelpful feedback in recall ranking"
    )
    status_stale_days: float = Field(7.0, gt=0, description="Age after which a status memory is flagged stale")
    checkpoint_stale_days: float = Field(2.0, gt=0, description="Age after which a checkpoint memory is flagged stale")

    # Graph-Aware Retrieval
    enable_graph_retrieval: bool = Field(True, description="Enable entity graph traversal during recall")
    graph_max_depth: int = Field(2, description="Maximum depth for entity relationship traversal")
    graph_max_entities: int = Field(100, description="Maximum related entities to return from graph traversal")
    graph_max_memories: int = Field(500, description="Maximum memory IDs to collect from graph traversal")

    # Context Optimization
    context_max_tokens: int = Field(4000, description="Maximum tokens in recall context output")
    context_include_metadata: bool = Field(True, description="Include timestamps and relevance in context")

    # Relevance Ranking
    ranking_semantic_weight: float = Field(0.6, description="Weight for semantic similarity in ranking")
    ranking_recency_weight: float = Field(0.15, description="Weight for recency boost in ranking")
    ranking_entity_weight: float = Field(0.15, description="Weight for entity match boost in ranking")
    ranking_keyword_weight: float = Field(0.1, description="Weight for keyword match boost in ranking")
    ranking_recency_decay_days: float = Field(30.0, description="Half-life in days for recency decay")

    # -----------------------------------------------------------------------
    # Features
    # -----------------------------------------------------------------------
    enable_temporal_decay: bool = True
    default_ttl_days: int | None = None
    # memory_type="checkpoint" stores get this TTL unless the caller sets ttl/expires_at (AGT-5).
    checkpoint_default_ttl: str = "7d"
    max_memories_per_recall: int = 10
    recall_score_threshold: float = 0.70

    # Strict Mode for expired memory references (v0.12)
    strict_mode: bool = Field(
        False,
        description="When enabled, writes to expired memory refs return HTTP 410 GONE. "
        "Forces agents to re-acquire context instead of silently creating orphan memories.",
    )

    # -----------------------------------------------------------------------
    # Conflict Resolution
    # -----------------------------------------------------------------------
    conflict_detection_enabled: bool = Field(
        True,
        description="Track conflicts detected during memory consolidation",
    )
    conflict_strategy: str = Field(
        "update",
        description="Default resolution strategy: update | version | flag",
    )

    # -----------------------------------------------------------------------
    # Cloud / SaaS
    # -----------------------------------------------------------------------
    cloud_enabled: bool = Field(
        False,
        description="Enable cloud features (billing, usage metering, plan enforcement)",
    )
    owner_emails: EnvList = Field(
        default_factory=list,
        description="Email addresses that get automatic Enterprise access (owner bypass)",
    )
    memory_cap_notice_effective_at: datetime | None = Field(
        None,
        description=(
            "When reduced memory caps (Free 25K->10K, legacy Pro 500K->250K, legacy Team 2M->600K) take "
            "effect. Unset = the previous caps stay in force. Set it to the notice email date + 30 days."
        ),
    )

    # Smart-credit metering and cost protection
    free_breaker_enabled: bool = Field(
        True,
        description="Global free-tier circuit breaker: degrade ALL free enrichment once monthly free AI spend passes the budget",
    )
    free_breaker_min_usd: float = Field(50.0, ge=0, description="Free-tier AI budget floor per calendar month (USD)")
    free_breaker_revenue_pct: float = Field(
        0.20,
        ge=0,
        le=1,
        description="Free-tier AI budget as a share of last month's net paid revenue, when that revenue is known",
    )
    unverified_credit_cap_effective_at: datetime | None = Field(
        None,
        description=(
            "Free accounts CREATED at or after this time are held at 25 smart credits until their email is "
            "verified. Unset = the cap is off (set it once the dashboard verify-email flow is live); accounts "
            "created earlier are grandfathered."
        ),
    )
    annual_credit_upfront_months: int = Field(
        12,
        ge=1,
        le=12,
        description=(
            "Annual plans: months of credits available in the first subscription month; one more month's "
            "allowance is released each month after (12 = the whole yearly bank up front)."
        ),
    )
    credit_reservation_stale_minutes: int = Field(
        15,
        ge=1,
        description="Open credit reservations older than this are settled at their chunk minimum (lost worker/restart)",
    )
    enrichment_global_concurrency: int = Field(
        16, ge=1, description="Max enrichment jobs (extraction / entity resolution) running at once across all tenants"
    )
    enrichment_default_concurrency: int = Field(
        4, ge=1, description="Per-tenant enrichment concurrency when no plan applies (self-hosted)"
    )
    enrichment_max_pending_per_tenant: int = Field(
        200,
        ge=1,
        description="Queued + running enrichment jobs allowed per tenant; beyond it enrichment is skipped (memory still stored)",
    )

    # Signup hardening
    turnstile_secret: str | None = Field(
        None,
        description="Cloudflare Turnstile secret. When set, signup requires a valid Turnstile token (server-side siteverify).",
    )
    turnstile_verify_url: str = Field(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        description="Turnstile siteverify endpoint",
    )
    turnstile_site_key: str | None = Field(
        None,
        description=(
            "Cloudflare Turnstile SITE key (public). Exposed by GET /api/v1/auth/providers so the dashboard "
            "renders the widget on signup. Only published while turnstile_secret is also set."
        ),
    )

    # Sign in with GitHub / Google (dashboard social login)
    public_dashboard_url: str | None = Field(
        None,
        description=(
            "Public HTTPS origin of the dashboard, e.g. https://app.remembra.dev. Social sign-in only ever "
            "redirects here; required (with public_url) for any sign-in provider to be enabled."
        ),
    )
    github_client_id: str | None = Field(None, description="GitHub OAuth app client ID (Sign in with GitHub)")
    github_client_secret: str | None = Field(None, description="GitHub OAuth app client secret")
    google_client_id: str | None = Field(None, description="Google OAuth 2.0 web client ID (Sign in with Google)")
    google_client_secret: str | None = Field(None, description="Google OAuth 2.0 web client secret")
    signup_ip_rate_limit: str = Field("3/hour", description="Signups allowed per client IP /24 (IPv6: /56)")
    signup_attempt_ip_rate_limit: str = Field(
        "30/hour",
        description="Signup ATTEMPTS per client /24 before Turnstile runs (bounds siteverify calls; only with Turnstile on)",
    )
    signup_domain_rate_limit: str = Field("20/day", description="Signups allowed per email domain")
    signup_domain_limit_exempt: EnvList = Field(
        default_factory=lambda: [
            "gmail.com",
            "googlemail.com",
            "outlook.com",
            "hotmail.com",
            "live.com",
            "yahoo.com",
            "icloud.com",
            "me.com",
            "proton.me",
            "protonmail.com",
        ],
        description="Large mailbox providers exempt from the per-domain signup limit (the per-IP limit still applies)",
    )

    # -----------------------------------------------------------------------
    # Paddle (sole billing provider)
    # -----------------------------------------------------------------------
    paddle_api_key: str | None = Field(
        None,
        description="Paddle API key",
        validation_alias=AliasChoices("REMEMBRA_PADDLE_API_KEY", "PADDLE_API_KEY"),
    )
    paddle_client_token: str | None = Field(
        None,
        description="Paddle client-side token for checkout overlay",
        validation_alias=AliasChoices("REMEMBRA_PADDLE_CLIENT_TOKEN", "PADDLE_CLIENT_TOKEN"),
    )
    paddle_webhook_secret: str | None = Field(
        None,
        description="Paddle webhook signing secret",
        validation_alias=AliasChoices("REMEMBRA_PADDLE_WEBHOOK_SECRET", "PADDLE_WEBHOOK_SECRET"),
    )
    paddle_sandbox: bool = Field(
        False,
        description="Use Paddle sandbox environment",
        validation_alias=AliasChoices("REMEMBRA_PADDLE_SANDBOX", "PADDLE_SANDBOX"),
    )
    # Paddle price IDs for the 2026-09 catalog (pri_...). They are created in the
    # Paddle dashboard by the owner; a missing ID makes checkout for that plan
    # unavailable (clear 503) instead of guessing. The grandfathered $49 / $199
    # price IDs are fixed in remembra.cloud.paddle_config.
    paddle_price_solo_monthly: str | None = Field(None, description="Paddle price ID: Solo $12/mo")
    paddle_price_solo_annual: str | None = Field(None, description="Paddle price ID: Solo $120/yr")
    paddle_price_pro_monthly: str | None = Field(None, description="Paddle price ID: Pro $29/mo")
    paddle_price_pro_annual: str | None = Field(None, description="Paddle price ID: Pro $290/yr")
    paddle_price_team_seat_monthly: str | None = Field(None, description="Paddle price ID: Team $15/seat/mo (qty >= 3)")
    paddle_price_team_seat_annual: str | None = Field(None, description="Paddle price ID: Team $150/seat/yr (qty >= 3)")
    paddle_price_founding_annual: str | None = Field(None, description="Paddle price ID: Founding 100 Solo $108/yr")

    # -----------------------------------------------------------------------
    # Email (Resend)
    # -----------------------------------------------------------------------
    resend_api_key: str | None = Field(
        None,
        description="Resend API key for sending emails (welcome, password reset, etc.)",
        validation_alias=AliasChoices("REMEMBRA_RESEND_API_KEY", "RESEND_API_KEY"),
    )

    # -----------------------------------------------------------------------
    # Webhooks
    # -----------------------------------------------------------------------
    webhooks_enabled: bool = Field(
        False,
        description="Enable the webhook event system for memory lifecycle events",
    )
    webhook_timeout: float = Field(
        10.0,
        description="Timeout in seconds for webhook HTTP delivery",
    )
    webhook_max_retries: int = Field(
        3,
        description="Maximum delivery attempts before marking a webhook delivery as failed",
    )

    # -----------------------------------------------------------------------
    # Security & Authentication (Week 7)
    # -----------------------------------------------------------------------
    auth_enabled: bool = Field(True, description="Enable API key authentication (disable for development only)")
    auth_master_key: str | None = Field(None, description="Master key for admin operations (key management)")
    jwt_secret: str = Field(
        "remembra-jwt-secret-change-in-production", description="Secret key for JWT token signing (MUST change in production)"
    )
    jwt_expiration_hours: int = Field(
        24,  # 24 hours (OWASP recommendation: 1 day max for web sessions)
        description="JWT token expiration in hours",
    )

    # Rate Limiting
    rate_limit_enabled: bool = Field(True, description="Enable rate limiting")
    rate_limit_storage: str = Field("memory", description="Rate limit storage backend: 'memory' or 'redis://...'")
    trusted_proxies: EnvList = Field(
        default_factory=lambda: ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"],
        description=(
            "CIDRs of reverse proxies whose X-Forwarded-For / X-Real-IP headers are trusted. "
            "Forwarding headers from any other peer are ignored. Set to [] when the API is exposed directly."
        ),
    )
    trust_cloudflare_proxies: bool = Field(
        True,
        description=(
            "Treat Cloudflare's published edge ranges as trusted proxies: behind Cloudflare the client IP comes "
            "from CF-Connecting-IP (or the forwarded chain) instead of the edge IP. Only applies when the direct "
            "peer is already a trusted proxy."
        ),
    )
    superadmin_user_ids: EnvList = Field(
        default_factory=list,
        description="User IDs with platform superadmin access (in addition to verified owner_emails).",
    )
    secret_redaction_enabled: bool = Field(
        True,
        description="Redact credentials (API keys, tokens, private keys) from memory content on write and on read.",
    )

    # -----------------------------------------------------------------------
    # Remote MCP connector for the Claude apps and ChatGPT (OAuth 2.1)
    # -----------------------------------------------------------------------
    connector_enabled: bool = Field(
        False,
        description=(
            "Serve the remote MCP connector at /mcp with its OAuth 2.1 authorization server "
            "(/oauth/*, /.well-known/oauth-*). Requires auth_enabled and public_url."
        ),
    )
    public_url: str | None = Field(
        None,
        description=(
            "Public HTTPS origin of this API as clients reach it, e.g. https://api.remembra.dev. "
            "It is the OAuth issuer and the base of the connector resource URL (<public_url>/mcp). "
            "Never derived from request headers."
        ),
    )
    connector_redirect_uris: EnvList = Field(
        default_factory=list,
        description="Extra exact redirect URIs dynamic client registration accepts, on top of the built-in Claude/ChatGPT ones.",
    )
    connector_allow_loopback_redirects: bool = Field(
        True,
        description="Accept http://localhost / 127.0.0.1 / [::1] redirect URIs (any port) for local clients such as Claude Code.",
    )
    connector_access_token_ttl_seconds: int = Field(3600, ge=60, le=86400, description="Connector access-token lifetime.")
    connector_refresh_token_ttl_days: int = Field(30, ge=1, le=365, description="Connector refresh-token lifetime (sliding).")

    # Input Sanitization
    sanitization_enabled: bool = Field(True, description="Enable input sanitization and trust scoring")
    trust_score_threshold: float = Field(0.5, description="Content below this trust score is flagged as suspicious")

    # -----------------------------------------------------------------------
    # Security Hardening (Phase 2 - OWASP 2026)
    # -----------------------------------------------------------------------
    max_memory_content_length: int = Field(
        50000,
        description="Maximum content length per memory (50KB default)",
    )

    # Encryption at Rest (AES-256-GCM)
    encryption_key: str | None = Field(
        None,
        description="AES-256-GCM encryption key for memory content at rest. "
        'Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"',
    )

    # PII Detection (OWASP ASI06)
    pii_detection_enabled: bool = Field(
        True,
        description="Enable PII pattern detection in content",
    )
    pii_mode: str = Field(
        "redact",
        description="PII handling mode: 'detect' | 'redact' | 'block'",
    )
    pii_exclusions: EnvList = Field(
        default_factory=list,
        description="PII pattern types to exclude from detection",
    )

    # Anomaly Detection
    anomaly_detection_enabled: bool = Field(
        True,
        description="Enable memory acquisition anomaly detection",
    )
    anomaly_rate_threshold: int = Field(
        100,
        description="Max memories per hour before flagging anomaly",
    )

    # -----------------------------------------------------------------------
    # Tracing (OpenTelemetry)
    # -----------------------------------------------------------------------
    tracing_enabled: bool = Field(
        False,
        description="Enable OpenTelemetry tracing (requires opentelemetry packages)",
    )
    tracing_endpoint: str = Field(
        "http://localhost:4317",
        description="OTLP gRPC endpoint for trace export",
    )
    tracing_service_name: str = Field(
        "remembra",
        description="Service name in traces",
    )

    # -----------------------------------------------------------------------
    # Sleep-Time Compute (Phase 3)
    # -----------------------------------------------------------------------
    sleep_time_enabled: bool = Field(
        True,
        description="Enable background sleep-time consolidation",
    )
    sleep_time_trigger: str = Field(
        "interval",
        description="Trigger mode: 'interval' | 'event' | 'manual'",
    )
    sleep_time_interval_hours: float = Field(
        6.0,
        description="Hours between automatic consolidation runs",
    )
    sleep_time_event_threshold: int = Field(
        50,
        description="Run consolidation after every N ingestion events",
    )
    sleep_time_model: str | None = Field(
        None,
        description="Not read yet: the sleep-time consolidation pass uses extraction_model.",
    )

    # -----------------------------------------------------------------------
    # Reliability: provider failure handling, readiness, background work
    # (2026-09-25 REL remediation — see docs/DEPLOYING.md)
    # -----------------------------------------------------------------------
    embedding_timeout_seconds: float = Field(20.0, description="Per-request timeout for embedding provider HTTP calls")
    embedding_breaker_failure_threshold: int = Field(
        5, description="Consecutive 5xx/timeout/429 embedding failures before the circuit opens"
    )
    embedding_breaker_reset_seconds: float = Field(30.0, description="Seconds an opened circuit waits before one probe")
    provider_quota_reset_seconds: float = Field(
        900.0, description="Seconds a circuit stays open after a quota_exhausted error before probing again"
    )
    llm_timeout_seconds: float = Field(20.0, description="Per-request timeout for extraction/consolidation LLM calls")
    llm_max_retries: int = Field(1, description="OpenAI SDK retries for LLM calls (SDK default 2 burns quota)")
    readiness_probe_interval_seconds: float = Field(
        300.0, description="Minimum seconds between active embedding probes made by /health/ready"
    )
    pending_embeddings_worker_enabled: bool = Field(
        True, description="Run the background worker that re-embeds rows queued in pending_embeddings"
    )
    pending_embeddings_poll_seconds: float = Field(5.0, description="Pending-embedding worker poll interval")
    pending_embeddings_batch_size: int = Field(20, description="Rows claimed per worker iteration")
    pending_embeddings_max_attempts: int = Field(12, description="Retryable failures before a pending embedding is dead-lettered")
    recall_recent_pool: int = Field(
        10,
        description=(
            "For recency-intent recalls (debug mode), also add this many of the newest memories to the "
            "candidate pool so recent work surfaces even when it shares no wording with the query (0 disables)."
        ),
    )
    temporal_cleanup_enabled: bool = Field(
        False,
        description=(
            "Run the TTL cleanup loop (expired memories are archived, not deleted). Off by default "
            "until existing rows are audited: the pre-2026-09-25 consolidation bug (ING-5) could copy "
            "a short TTL onto permanent memories."
        ),
    )
    temporal_cleanup_interval_seconds: int = Field(3600, description="Seconds between TTL cleanup runs")
    pre_migration_backup: bool = Field(
        True,
        description=(
            "Copy the SQLite database (online backup API) before schema migrations run, once per build. "
            "Startup stops if the copy is needed and fails, so a deploy never migrates unprotected data."
        ),
    )
    pre_migration_backup_keep: int = Field(3, ge=1, le=50, description="Pre-migration backups to keep (newest first)")
    pre_migration_backup_dir: str | None = Field(
        None, description="Directory for pre-migration backups (default: a 'backups' folder next to the database)"
    )
    qdrant_init_retries: int = Field(5, description="Attempts to reach Qdrant at startup (exponential backoff)")
    reconcile_interval_hours: float = Field(
        24.0, description="Hours between report-only SQLite/Qdrant/FTS drift scans (0 disables)"
    )
    background_task_concurrency: int = Field(16, description="Max concurrently running tracked background tasks")
    alert_webhook_url: str | None = Field(
        None, description="POST JSON operator alerts here (e.g. first embedding quota_exhausted)"
    )
    alert_email: str | None = Field(None, description="Also email operator alerts here (requires email backend)")
    alert_cooldown_seconds: float = Field(3600.0, description="Minimum seconds between repeats of the same alert")
    metrics_token: str | None = Field(
        None, description="Bearer token required for GET /metrics. When unset, /metrics is disabled (404)."
    )

    @field_validator(*LIST_SETTINGS, mode="before")
    @classmethod
    def parse_env_list(cls, value: Any, info: ValidationInfo) -> Any:
        """Accept a JSON array, a comma-separated list or a bare value.

        An empty (or whitespace-only) value means "unset" and keeps the default:
        a Coolify variable left present but blank must neither crash the boot
        nor silently empty a list such as trusted_proxies.
        """
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            assert info.field_name is not None
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"{info.field_name}: invalid JSON array ({e.msg})") from e
        return [item.strip() for item in raw.split(",") if item.strip()]

    @field_validator("public_url", "memory_cap_notice_effective_at", "unverified_credit_cap_effective_at", mode="before")
    @classmethod
    def blank_is_unset(cls, value: Any) -> Any:
        """A variable present but empty (REMEMBRA_PUBLIC_URL=) means unset, not an invalid value."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def fill_build_sha(self) -> "Settings":
        """Fall back to the deploy platform's commit (Coolify sets SOURCE_COMMIT)
        when REMEMBRA_BUILD_SHA is unset or empty (REL-18)."""
        if not self.build_sha:
            import os

            object.__setattr__(self, "build_sha", os.environ.get("SOURCE_COMMIT") or None)
        return self

    @model_validator(mode="after")
    def check_security_settings(self) -> "Settings":
        """Warn about insecure settings in production and filter CORS origins."""
        if self.auth_enabled and not self.debug:
            # Check JWT secret
            if self.jwt_secret == "remembra-jwt-secret-change-in-production":
                warnings.warn(
                    "⚠️  SECURITY WARNING: Using default JWT secret in production! Set REMEMBRA_JWT_SECRET environment variable.",
                    UserWarning,
                    stacklevel=2,
                )

        if self.typesafe_mode is not None:
            mode = self.typesafe_mode.strip().lower()
            if mode not in {"off", "shadow", "enforce"}:
                raise ValueError("typesafe_mode must be one of: off, shadow, enforce")
            object.__setattr__(self, "typesafe_mode", mode)
        if self.grounding_action not in {"drop", "flag"}:
            raise ValueError("grounding_action must be 'drop' or 'flag'")

        if self.public_url is not None:
            from remembra.connector.policy import normalize_public_url

            object.__setattr__(self, "public_url", normalize_public_url(self.public_url))
        if self.public_dashboard_url is not None and not self.public_dashboard_url.strip():
            object.__setattr__(self, "public_dashboard_url", None)
        if self.public_dashboard_url is not None:
            from remembra.connector.policy import normalize_public_url

            try:
                object.__setattr__(self, "public_dashboard_url", normalize_public_url(self.public_dashboard_url))
            except ValueError as e:
                raise ValueError(str(e).replace("public_url", "public_dashboard_url")) from e
        if self.connector_enabled and not self.public_url:
            raise ValueError("connector_enabled requires public_url (e.g. REMEMBRA_PUBLIC_URL=https://api.example.com)")

        # Filter out localhost from CORS origins in production mode
        if not self.debug and self.cors_filter_localhost_in_production:
            # Use object.__setattr__ since model is frozen after validation
            filtered = [origin for origin in self.cors_origins if "localhost" not in origin and "127.0.0.1" not in origin]
            object.__setattr__(self, "cors_origins", filtered)

        return self

    @property
    def typesafe_effective_mode(self) -> str:
        """Resolved Jev mode: explicit setting, else shadow iff a key is configured."""
        if not self.typesafe_api_key:
            return "off"
        return self.typesafe_mode or "shadow"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
