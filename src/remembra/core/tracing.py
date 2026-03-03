"""Optional OpenTelemetry tracing integration.

This module provides distributed tracing support for production observability.
Tracing is a soft dependency - if OpenTelemetry packages aren't installed,
tracing is silently disabled with no errors.

Enable with:
    REMEMBRA_TRACING_ENABLED=true
    REMEMBRA_TRACING_ENDPOINT=http://localhost:4317  # OTLP collector
    REMEMBRA_TRACING_SERVICE_NAME=remembra

Install tracing packages with:
    pip install remembra[tracing]
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from remembra.config import Settings

logger = structlog.get_logger()


def setup_tracing(settings: "Settings") -> None:
    """Initialize OpenTelemetry tracing if enabled and packages available.
    
    This function:
    1. Checks if tracing is enabled in settings
    2. Attempts to import OpenTelemetry packages
    3. Configures the tracer provider with OTLP exporter
    4. Silently degrades if packages aren't installed
    
    Args:
        settings: Application settings with tracing configuration
    """
    if not settings.tracing_enabled:
        return
    
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        
        resource = Resource.create({"service.name": settings.tracing_service_name})
        provider = TracerProvider(resource=resource)
        
        exporter = OTLPSpanExporter(endpoint=settings.tracing_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        
        trace.set_tracer_provider(provider)
        
        logger.info(
            "opentelemetry_tracing_initialized",
            endpoint=settings.tracing_endpoint,
            service=settings.tracing_service_name,
        )
        
    except ImportError:
        logger.warning(
            "opentelemetry_packages_not_installed",
            hint="pip install opentelemetry-api opentelemetry-sdk "
                 "opentelemetry-exporter-otlp opentelemetry-instrumentation-fastapi",
        )


def instrument_app(app) -> None:
    """Instrument FastAPI app if OpenTelemetry is available.
    
    Automatically adds tracing middleware to capture:
    - HTTP request/response spans
    - Route information
    - Status codes and timing
    
    Args:
        app: FastAPI application instance
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        logger.info("fastapi_instrumented_for_tracing")
    except ImportError:
        # OpenTelemetry not installed, silently skip
        pass
    except Exception as e:
        logger.warning("fastapi_instrumentation_failed", error=str(e))
