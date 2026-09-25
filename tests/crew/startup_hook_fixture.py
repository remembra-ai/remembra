"""A module that registers a crew startup hook at import (used by test_crew_startup via HOOK_MODULES)."""

from remembra.crew import startup

CALLS: list[str] = []


async def _start(app, rt):
    CALLS.append("fixture.start")
    rt.extras["fixture_saw_bus"] = rt.bus is not None


async def _stop(app, rt):
    CALLS.append("fixture.stop")


startup.add_hook("test.fixture", order=35, start=_start, stop=_stop)
