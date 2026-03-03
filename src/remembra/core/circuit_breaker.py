"""
Circuit Breaker Pattern.

Prevents cascading failures when external services are down.
Wraps calls to LLM APIs, Qdrant, and webhook deliveries.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Service is failing, requests fail fast
- HALF_OPEN: Testing if service recovered

Based on the Hystrix pattern.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import wraps
from typing import Any, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal - requests pass through
    OPEN = "open"          # Failing - requests fail fast
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitStats:
    """Statistics for a circuit breaker."""
    
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0  # Calls rejected due to open circuit
    last_failure: datetime | None = None
    last_success: datetime | None = None
    state_changes: int = 0


@dataclass
class CircuitBreakerConfig:
    """Configuration for a circuit breaker."""
    
    failure_threshold: int = 5  # Failures before opening
    success_threshold: int = 3  # Successes in half-open before closing
    reset_timeout: float = 60.0  # Seconds before half-open
    call_timeout: float = 30.0  # Timeout for individual calls


class CircuitBreaker:
    """
    Circuit breaker for external service calls.
    
    Usage:
        breaker = CircuitBreaker("openai", failure_threshold=5)
        
        @breaker.protect
        async def call_openai(prompt: str):
            return await openai.chat.completions.create(...)
        
        # Or wrap manually:
        result = await breaker.call(call_openai, prompt="hello")
    """
    
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 3,
        reset_timeout: float = 60.0,
        call_timeout: float = 30.0,
    ):
        self.name = name
        self.config = CircuitBreakerConfig(
            failure_threshold=failure_threshold,
            success_threshold=success_threshold,
            reset_timeout=reset_timeout,
            call_timeout=call_timeout,
        )
        
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: datetime | None = None
        self._lock = asyncio.Lock()
        
        self.stats = CircuitStats()
        
        log.info(
            "circuit_breaker_created",
            name=name,
            failure_threshold=failure_threshold,
            reset_timeout=reset_timeout,
        )
    
    @property
    def state(self) -> CircuitState:
        """Get current circuit state, checking for timeout transitions."""
        if self._state == CircuitState.OPEN:
            # Check if reset timeout has passed
            if self._last_failure_time:
                elapsed = (datetime.utcnow() - self._last_failure_time).total_seconds()
                if elapsed >= self.config.reset_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
        
        return self._state
    
    @property
    def is_closed(self) -> bool:
        return self.state == CircuitState.CLOSED
    
    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN
    
    async def call(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        Execute a function with circuit breaker protection.
        
        Args:
            func: Async function to call
            *args, **kwargs: Arguments to pass to func
            
        Returns:
            Result from func
            
        Raises:
            CircuitOpenError: If circuit is open
            Exception: If call fails and circuit is closed/half-open
        """
        self.stats.total_calls += 1
        
        # Check state
        current_state = self.state
        
        if current_state == CircuitState.OPEN:
            self.stats.rejected_calls += 1
            log.warning(
                "circuit_breaker_rejected",
                name=self.name,
                state="open",
            )
            raise CircuitOpenError(f"Circuit '{self.name}' is open")
        
        # Execute call with timeout
        try:
            result = await asyncio.wait_for(
                func(*args, **kwargs),
                timeout=self.config.call_timeout,
            )
            
            await self._on_success()
            return result
            
        except TimeoutError:
            await self._on_failure(TimeoutError(f"Call timed out after {self.config.call_timeout}s"))
            raise
            
        except Exception as e:
            await self._on_failure(e)
            raise
    
    async def _on_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            self.stats.successful_calls += 1
            self.stats.last_success = datetime.utcnow()
            
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            else:
                # Reset failure count on success in closed state
                self._failure_count = 0
    
    async def _on_failure(self, error: Exception) -> None:
        """Record a failed call."""
        async with self._lock:
            self.stats.failed_calls += 1
            self.stats.last_failure = datetime.utcnow()
            self._last_failure_time = datetime.utcnow()
            
            log.warning(
                "circuit_breaker_failure",
                name=self.name,
                error=str(error),
                failure_count=self._failure_count + 1,
            )
            
            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open goes back to open
                self._transition_to(CircuitState.OPEN)
            else:
                self._failure_count += 1
                if self._failure_count >= self.config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)
    
    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state
        self.stats.state_changes += 1
        
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._success_count = 0
        
        log.info(
            "circuit_breaker_state_change",
            name=self.name,
            from_state=old_state.value,
            to_state=new_state.value,
        )
    
    def protect(self, func: Callable[..., T]) -> Callable[..., T]:
        """
        Decorator to protect an async function with this circuit breaker.
        
        Usage:
            @breaker.protect
            async def my_function():
                ...
        """
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await self.call(func, *args, **kwargs)
        return wrapper
    
    def reset(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        self._transition_to(CircuitState.CLOSED)
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        log.info("circuit_breaker_reset", name=self.name)
    
    def get_status(self) -> dict[str, Any]:
        """Get current status and statistics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "config": {
                "failure_threshold": self.config.failure_threshold,
                "success_threshold": self.config.success_threshold,
                "reset_timeout": self.config.reset_timeout,
                "call_timeout": self.config.call_timeout,
            },
            "stats": {
                "total_calls": self.stats.total_calls,
                "successful_calls": self.stats.successful_calls,
                "failed_calls": self.stats.failed_calls,
                "rejected_calls": self.stats.rejected_calls,
                "state_changes": self.stats.state_changes,
                "last_failure": self.stats.last_failure.isoformat() if self.stats.last_failure else None,
                "last_success": self.stats.last_success.isoformat() if self.stats.last_success else None,
            },
        }


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open and rejecting calls."""
    pass


# ============================================================================
# Global Circuit Breakers
# ============================================================================

# Pre-configured circuit breakers for common services
_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(
    name: str,
    failure_threshold: int = 5,
    reset_timeout: float = 60.0,
) -> CircuitBreaker:
    """
    Get or create a named circuit breaker.
    
    Args:
        name: Breaker name (e.g., "openai", "qdrant", "webhook")
        failure_threshold: Failures before opening
        reset_timeout: Seconds before trying again
        
    Returns:
        CircuitBreaker instance
    """
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            reset_timeout=reset_timeout,
        )
    return _breakers[name]


def circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    reset_timeout: float = 60.0,
) -> Callable:
    """
    Decorator to protect a function with a circuit breaker.
    
    Usage:
        @circuit_breaker("openai")
        async def call_openai(prompt):
            ...
    """
    breaker = get_breaker(name, failure_threshold, reset_timeout)
    return breaker.protect
