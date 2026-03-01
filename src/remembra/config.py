"""Application settings resolved from environment variables."""

from pydantic import Field
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
    embedding_provider: str = Field("openai", description="openai | cohere | ollama")
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    openai_api_key: str | None = None
    ollama_url: str = "http://localhost:11434"

    # -----------------------------------------------------------------------
    # LLM (extraction / recall synthesis)
    # -----------------------------------------------------------------------
    llm_provider: str = Field("openai", description="openai | ollama | anthropic")
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str | None = None

    # -----------------------------------------------------------------------
    # Intelligent Extraction (Week 4)
    # -----------------------------------------------------------------------
    smart_extraction_enabled: bool = Field(
        True, 
        description="Enable LLM-powered fact extraction"
    )
    extraction_model: str = Field(
        "gpt-4o-mini",
        description="Model for fact extraction and consolidation"
    )
    consolidation_threshold: float = Field(
        0.5,
        description="Similarity threshold for memory consolidation"
    )

    # -----------------------------------------------------------------------
    # Entity Resolution (Week 5)
    # -----------------------------------------------------------------------
    enable_entity_resolution: bool = Field(
        True,
        description="Enable entity extraction and resolution"
    )
    entity_matching_threshold: float = Field(
        0.6,
        description="Minimum confidence for entity matching"
    )

    # -----------------------------------------------------------------------
    # Advanced Retrieval (Week 6)
    # -----------------------------------------------------------------------
    # Hybrid Search
    enable_hybrid_search: bool = Field(
        True,
        description="Enable BM25 keyword search alongside vector search"
    )
    hybrid_semantic_weight: float = Field(
        0.7,
        description="Weight for semantic (vector) search in hybrid mode"
    )
    hybrid_keyword_weight: float = Field(
        0.3,
        description="Weight for keyword (BM25) search in hybrid mode"
    )
    
    # Graph-Aware Retrieval
    enable_graph_retrieval: bool = Field(
        True,
        description="Enable entity graph traversal during recall"
    )
    graph_max_depth: int = Field(
        2,
        description="Maximum depth for entity relationship traversal"
    )
    
    # Context Optimization
    context_max_tokens: int = Field(
        4000,
        description="Maximum tokens in recall context output"
    )
    context_include_metadata: bool = Field(
        True,
        description="Include timestamps and relevance in context"
    )
    
    # Relevance Ranking
    ranking_semantic_weight: float = Field(
        0.6,
        description="Weight for semantic similarity in ranking"
    )
    ranking_recency_weight: float = Field(
        0.15,
        description="Weight for recency boost in ranking"
    )
    ranking_entity_weight: float = Field(
        0.15,
        description="Weight for entity match boost in ranking"
    )
    ranking_keyword_weight: float = Field(
        0.1,
        description="Weight for keyword match boost in ranking"
    )
    ranking_recency_decay_days: float = Field(
        30.0,
        description="Half-life in days for recency decay"
    )

    # -----------------------------------------------------------------------
    # Advanced Retrieval (Week 6)
    # -----------------------------------------------------------------------
    hybrid_search_enabled: bool = Field(
        True,
        description="Enable hybrid BM25 + vector search"
    )
    hybrid_alpha: float = Field(
        0.4,
        description="Weight for keyword/BM25 search (1-alpha for semantic)"
    )
    rerank_enabled: bool = Field(
        False,
        description="Enable CrossEncoder reranking (requires sentence-transformers)"
    )
    rerank_model: str = Field(
        "cross-encoder/ms-marco-MiniLM-L-6-v2",
        description="CrossEncoder model for reranking"
    )
    default_max_tokens: int = Field(
        4000,
        description="Default max tokens for recall context"
    )
    graph_retrieval_enabled: bool = Field(
        True,
        description="Enable graph-aware entity retrieval"
    )
    graph_traversal_depth: int = Field(
        2,
        description="Max depth for entity relationship traversal"
    )

    # -----------------------------------------------------------------------
    # Features
    # -----------------------------------------------------------------------
    enable_temporal_decay: bool = True
    default_ttl_days: int | None = None
    max_memories_per_recall: int = 10
    recall_score_threshold: float = 0.70


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
