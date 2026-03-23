"""Application settings resolved from environment variables."""

import warnings

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REMEMBRA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # -----------------------------------------------------------------------
    # Server
    # -----------------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8787
    debug: bool = False
    log_level: str = "info"
    static_dir: str | None = Field(None, description="Directory for static files (dashboard UI). Set to enable serving.")

    # CORS
    cors_origins: list[str] = Field(
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
    # LLM (extraction / recall synthesis)
    # -----------------------------------------------------------------------
    llm_provider: str = Field("openai", description="openai | ollama | anthropic")
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str | None = None
    anthropic_api_key: str | None = None

    # -----------------------------------------------------------------------
    # Intelligent Extraction (Week 4)
    # -----------------------------------------------------------------------
    smart_extraction_enabled: bool = Field(True, description="Enable LLM-powered fact extraction")
    extraction_model: str = Field("gpt-4o-mini", description="Model for fact extraction and consolidation")
    consolidation_threshold: float = Field(0.5, description="Similarity threshold for memory consolidation")

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

    # Graph-Aware Retrieval
    enable_graph_retrieval: bool = Field(True, description="Enable entity graph traversal during recall")
    graph_max_depth: int = Field(2, description="Maximum depth for entity relationship traversal")

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
    owner_emails: list[str] = Field(
        default_factory=list,
        description="Email addresses that get automatic Enterprise access (owner bypass)",
    )
    stripe_secret_key: str | None = Field(
        None,
        description="Stripe secret key (sk_live_xxx or sk_test_xxx)",
        validation_alias=AliasChoices("REMEMBRA_STRIPE_SECRET_KEY", "STRIPE_SECRET_KEY"),
    )
    stripe_publishable_key: str | None = Field(
        None,
        description="Stripe publishable key (pk_live_xxx or pk_test_xxx)",
        validation_alias=AliasChoices("REMEMBRA_STRIPE_PUBLISHABLE_KEY", "STRIPE_PUBLISHABLE_KEY"),
    )
    stripe_webhook_secret: str | None = Field(
        None,
        description="Stripe webhook signing secret (whsec_xxx)",
        validation_alias=AliasChoices("REMEMBRA_STRIPE_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET"),
    )
    stripe_success_url: str = Field(
        "https://remembra.dev/dashboard?checkout=success",
        description="URL to redirect after successful Stripe checkout",
    )
    stripe_cancel_url: str = Field(
        "https://remembra.dev/pricing?checkout=cancelled",
        description="URL to redirect when user cancels Stripe checkout",
    )
    billing_portal_return_url: str = Field(
        "https://remembra.dev/dashboard",
        description="URL to return to after billing portal session",
    )

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
        "detect",
        description="PII handling mode: 'detect' | 'redact' | 'block'",
    )
    pii_exclusions: list[str] = Field(
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
        description="Model for background consolidation (uses cheaper model if set)",
    )

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

        # Filter out localhost from CORS origins in production mode
        if not self.debug and self.cors_filter_localhost_in_production:
            # Use object.__setattr__ since model is frozen after validation
            filtered = [origin for origin in self.cors_origins if "localhost" not in origin and "127.0.0.1" not in origin]
            object.__setattr__(self, "cors_origins", filtered)

        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
