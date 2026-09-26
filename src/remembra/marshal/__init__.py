"""Remembra Marshal: says where the baton dropped between your agents, from evidence, and changes nothing.

M1 is rules only (no model): ``remembra-relay doctor``, the MCP tools
``remembra_doctor`` / ``remembra_setup`` / ``remembra_help``, and the
"You still need to" list at the end of ``remembra-relay connect``.

- :mod:`remembra.marshal.signals` reads this machine (and, optionally, the
  user's trail with GETs only);
- :mod:`remembra.marshal.rules` turns signals into findings;
- :mod:`remembra.marshal.render` prints them as the exchange slip;
- :mod:`remembra.marshal.commands` holds every command Marshal may suggest;
- :mod:`remembra.marshal.setup_plan`, :mod:`remembra.marshal.knowledge` and
  :mod:`remembra.marshal.todo` back ``remembra_setup``, ``remembra_help`` and
  connect's to-do list.

Standard library and httpx only, so ``pipx run`` on a base install works.
"""

RULESET_VERSION = "1"

__all__ = ["RULESET_VERSION"]
