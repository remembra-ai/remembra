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
    # Hybrid Search (FTS5 + Vector)
    enable_hybrid_search: bool = Field(
        True,
        description="Enable FTS5 BM25 keyword search alongside vector search"
    )
    hybrid_alpha: float = Field(
        0.4,
        description="Weight for keyword (BM25) in hybrid fusion. Research default: 0.4"
    )
    
    # Reranking (CrossEncoder)
    enable_reranking: bool = Field(
        True,
        description="Enable CrossEncoder reranking for improved accuracy"
    )
    rerank_model: str = Field(
        "cross-encoder/ms-marco-MiniLM-L-6-v2",
        description="HuggingFace model for reranking"
    )
    rerank_top_k: int = Field(
        20,
        description="Rerank top K results from hybrid search"
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
