"""Remembra Crew mode: coordination of several AI agents working on one project.

The contracts every other crew module builds on live in :mod:`remembra.crew.schemas`
(stdlib only, so the vendored hook gate and the CLI can import them without the
server extras). The shared client-side reducer is :mod:`remembra.crew.reducer`.
Human-readable specs are in ``docs/crew/``.
"""
