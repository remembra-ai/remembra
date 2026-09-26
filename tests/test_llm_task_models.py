"""Which environment variable drives which LLM task (gap analysis section 11).

``REMEMBRA_EXTRACTION_MODEL`` drives fact extraction, consolidation, entity
matching and conversation ingest. ``REMEMBRA_LLM_MODEL`` (the only model
variable docker-compose.prod.yml sets) is only the entity-extraction fallback.
Startup logs the model of every task once (``llm_task_models``) and warns when
the extraction model is not an OpenAI model.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from remembra.config import Settings
from remembra.extraction.entities import is_openai_model
from remembra.services.memory import MemoryService
from tests._ingest_fakes import HashEmbeddings, MemQdrant, close_ingest_dbs, make_db  # noqa: F401


@pytest.fixture()
def fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The anthropic SDK is an optional extra; the Anthropic extractor only needs its client class."""
    module = types.ModuleType("anthropic")

    class AsyncAnthropic:
        def __init__(self, api_key: str | None = None) -> None:
            self.api_key = api_key

    module.AsyncAnthropic = AsyncAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)


async def _service(tmp_path: Path, **overrides: Any) -> MemoryService:
    settings = Settings(openai_api_key="t", typesafe_mode="off", **overrides)
    db = await make_db(tmp_path)
    return MemoryService(settings=settings, qdrant=MemQdrant(), db=db, embeddings=HashEmbeddings())  # type: ignore[arg-type]


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e.get("event") == name]


async def test_defaults_run_every_task_on_gpt_4o_mini(tmp_path) -> None:
    service = await _service(tmp_path)
    with capture_logs() as logs:
        models = service.log_llm_task_models()
    assert models == {
        "fact_extraction": "openai:gpt-4o-mini",
        "consolidation": "openai:gpt-4o-mini",
        "entity_matching": "openai:gpt-4o-mini",
        "conversation_ingest": "openai:gpt-4o-mini",
        "entity_extraction": "openai:gpt-4o-mini",
    }
    [event] = _events(logs, "llm_task_models")
    assert event["fact_extraction"] == "openai:gpt-4o-mini"
    assert event["log_level"] == "info"
    assert _events(logs, "extraction_model_not_openai") == []


async def test_llm_model_alone_changes_no_task_with_the_openai_provider(tmp_path) -> None:
    """The production compose shape: only REMEMBRA_LLM_MODEL is set."""
    service = await _service(tmp_path, llm_model="gpt-5.4-nano")
    models = service.llm_task_models()
    assert set(models.values()) == {"openai:gpt-4o-mini"}


async def test_extraction_model_drives_extraction_consolidation_matching_and_ingest(tmp_path) -> None:
    service = await _service(tmp_path, extraction_model="gpt-5.4-nano", llm_model="gpt-4.1-mini")
    models = service.llm_task_models()
    assert models == {
        "fact_extraction": "openai:gpt-5.4-nano",
        "consolidation": "openai:gpt-5.4-nano",
        "entity_matching": "openai:gpt-5.4-nano",
        "conversation_ingest": "openai:gpt-5.4-nano",
        "entity_extraction": "openai:gpt-5.4-nano",
    }
    # The objects that make the calls agree with what is logged.
    assert service.extractor.config.model == "gpt-5.4-nano"
    assert service.consolidator.model == "gpt-5.4-nano"
    assert service.entity_matcher.model == "gpt-5.4-nano"


async def test_anthropic_provider_uses_llm_model_for_entity_extraction_only(tmp_path, fake_anthropic) -> None:
    service = await _service(tmp_path, llm_provider="anthropic", llm_model="claude-haiku-4-5")
    with capture_logs() as logs:
        models = service.log_llm_task_models()
    assert models["entity_extraction"] == "anthropic:claude-haiku-4-5"
    assert {k: v for k, v in models.items() if k != "entity_extraction"} == {
        "fact_extraction": "openai:gpt-4o-mini",
        "consolidation": "openai:gpt-4o-mini",
        "entity_matching": "openai:gpt-4o-mini",
        "conversation_ingest": "openai:gpt-4o-mini",
    }
    assert _events(logs, "extraction_model_not_openai") == []


async def test_claude_extraction_model_is_flagged_at_startup(tmp_path, fake_anthropic) -> None:
    """The old .env.example advice: a Claude extraction model is sent to OpenAI and rejected there."""
    service = await _service(tmp_path, llm_provider="anthropic", extraction_model="claude-sonnet-4-5")
    with capture_logs() as logs:
        models = service.log_llm_task_models()
    assert models["fact_extraction"] == "openai:claude-sonnet-4-5"
    assert models["entity_extraction"] == "anthropic:claude-sonnet-4-5"
    [warning] = _events(logs, "extraction_model_not_openai")
    assert warning["log_level"] == "warning"
    assert warning["extraction_model"] == "claude-sonnet-4-5"


async def test_ollama_provider_reports_its_model(tmp_path) -> None:
    service = await _service(tmp_path, llm_provider="ollama", llm_model="llama3.1")
    assert service.llm_task_models()["entity_extraction"] == "ollama:llama3.1"
    assert service.llm_task_models()["fact_extraction"] == "openai:gpt-4o-mini"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-4o-mini", True),
        ("GPT-6-Luna", True),
        ("o4-mini", True),
        ("ft:gpt-4o-mini:acme::abc123", True),
        ("claude-sonnet-4-5", False),
        ("llama3.1", False),
        ("", False),
        (None, False),
    ],
)
def test_is_openai_model(model: str | None, expected: bool) -> None:
    assert is_openai_model(model) is expected


async def test_app_startup_logs_task_models_once(tmp_path, monkeypatch) -> None:
    import remembra.config
    import remembra.main as main
    from qdrant_client import AsyncQdrantClient

    from remembra.storage.qdrant import QdrantStore

    monkeypatch.setenv("REMEMBRA_DATABASE_URL", f"sqlite:///{tmp_path / 'boot.db'}")
    monkeypatch.setenv("REMEMBRA_OPENAI_API_KEY", "t")
    monkeypatch.setenv("REMEMBRA_EMBEDDING_DIMENSIONS", "4")
    monkeypatch.setenv("REMEMBRA_QDRANT_COLLECTION", "models_boot")
    monkeypatch.setenv("REMEMBRA_DEBUG", "true")
    monkeypatch.setenv("REMEMBRA_SLEEP_TIME_ENABLED", "false")
    monkeypatch.setenv("REMEMBRA_TYPESAFE_MODE", "off")
    monkeypatch.setenv("REMEMBRA_EXTRACTION_MODEL", "gpt-6-luna")
    monkeypatch.setenv("REMEMBRA_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(remembra.config, "_settings", None)
    local = AsyncQdrantClient(location=":memory:")

    async def local_client(self):  # noqa: ANN001
        return local

    monkeypatch.setattr(QdrantStore, "_get_client", local_client)
    # Boot reconfigures structlog, which would detach capture_logs.
    monkeypatch.setattr(main, "configure_logging", lambda level: None)
    app = main.create_app()
    with capture_logs() as logs:
        async with app.router.lifespan_context(app):
            pass
    [event] = _events(logs, "llm_task_models")
    assert event["fact_extraction"] == "openai:gpt-6-luna"
    assert event["consolidation"] == "openai:gpt-6-luna"
    assert event["entity_matching"] == "openai:gpt-6-luna"
    assert event["conversation_ingest"] == "openai:gpt-6-luna"
    assert event["entity_extraction"] == "openai:gpt-6-luna"
    assert _events(logs, "extraction_model_not_openai") == []
    monkeypatch.setattr(remembra.config, "_settings", None)


def test_env_example_only_suggests_openai_extraction_models() -> None:
    """.env.example used to suggest claude-sonnet-4-5 / llama3.1 as the extraction model."""
    import re

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    suggested = re.findall(r"^#?\s*REMEMBRA_EXTRACTION_MODEL=(\S+)", text, flags=re.MULTILINE)
    assert suggested, "the example should show REMEMBRA_EXTRACTION_MODEL"
    assert all(is_openai_model(m) for m in suggested), suggested
    # Provider keys are only read with the REMEMBRA_ prefix.
    assert not re.search(r"^(OPENAI|ANTHROPIC|VOYAGE|JINA)_API_KEY=", text, flags=re.MULTILINE)
