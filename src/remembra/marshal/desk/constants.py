"""The desk's hard caps and fixed values (the owner's decisions for v1; spec M3 "Costs and caps")."""

from __future__ import annotations

# Per question (one "turn").
MAX_TOOL_CALLS = 4  # reads per question, a context pre-read included
MAX_MODEL_CALLS = 5
MAX_TOKENS = 600  # output tokens per model call
TEMPERATURE = 0.2
ASK_TIMEOUT_S = 45.0
TOOL_TIMEOUT_S = 10.0

# What the client may send (nothing is stored server-side).
MAX_QUESTION_CHARS = 1000  # Unicode code points, after strip()
MAX_HISTORY_TURNS = 6
MAX_HISTORY_ANSWER_CHARS = 600

# Money: every ask reserves its whole budget up front; the ledger is in integer micro-dollars.
ASK_BUDGET_USD = 0.02
ASK_RESERVE_MICRO = 20_000
RESERVATION_TTL_S = 300  # a hold older than this was left by a crashed ask: it expires at its full reserve
PRUNE_AFTER_DAYS = 35
PRUNE_BATCH = 500

# What a tool result may weigh in the model's context.
TOOL_RESULT_MAX_CHARS = 4000

# The stream.
KEEPALIVE_S = 10.0

# Links an answer may carry, and where "ask a person" goes.
ALLOWED_LINK_HOSTS: tuple[str, ...] = ("remembra.dev", "docs.remembra.dev", "app.remembra.dev")
CONTACT_URL = "https://remembra.dev/contact"
FALLBACK_TEXT = "That's all I can confirm."
