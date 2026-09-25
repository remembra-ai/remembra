"""Remembra Relay: every agent leaves a trail when it stops; any agent picks it up.

- :mod:`remembra.relay.identity` — location-independent project fingerprints.
- :mod:`remembra.relay.handoff`  — deterministic handoff rendering + grounding.
- :mod:`remembra.relay.facts`    — git / transcript fact gathering (client side).
- :mod:`remembra.relay.cli`      — the ``remembra-relay`` command.
- :mod:`remembra.relay.adapters` — per-agent hook wiring used by ``connect``.

Everything under this package except ``handoff`` is importable with the SDK
dependencies only (stdlib + httpx), so the CLI runs from a bare install.
"""
