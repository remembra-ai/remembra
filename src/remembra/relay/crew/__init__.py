"""Crew mode local runtime (WP-9, spec §8.1): the hook gate, crewd, the CLI and their helpers.

* :mod:`~remembra.relay.crew.gate` — ``crew-gate.py``: the per-tool and per-turn hook gate and the git
  gates. Stdlib only; vendored under ``~/.remembra/crew/bin`` together with
  :mod:`remembra.crew.gatecore`, :mod:`remembra.crew.schemas`, :mod:`~remembra.relay.crew.outbox`
  and :mod:`~remembra.relay.crew.snapshot`.
* :mod:`~remembra.relay.crew.crewd` — ``remembra-crewd``: the supervised per-user daemon.
* :mod:`~remembra.relay.crew.cli` — ``remembra-crew``.
* :mod:`~remembra.relay.crew.arbiter`, :mod:`~remembra.relay.crew.baton`,
  :mod:`~remembra.relay.crew.fence`, :mod:`~remembra.relay.crew.detector`,
  :mod:`~remembra.relay.crew.zonescompile`: the local arbiter, baton refs, the read-only fence,
  the transcript limit detector and the zones.yml compiler.
* Installers (WP-10, spec §8.2–§8.4): :mod:`~remembra.relay.crew.install` (``remembra-crew
  connect``), :mod:`~remembra.relay.crew.verify` (the adapter round trip) and
  :mod:`~remembra.relay.crew.githooks` (git gates with Husky/lefthook chaining).

This package ``__init__`` imports nothing, so the vendored gate loads only what it needs.
"""
