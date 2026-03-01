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
