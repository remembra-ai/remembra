"""The Marshal desk: a read-only copilot in the dashboard, on Remembra's own model (spec M3).

A dashboard login asks a question; the desk reads the user's own records
with fixed GET tools (in process, under a restricted principal for the same
user), asks the model for an answer that cites those reads, checks the answer
(:mod:`.validate`) and streams the reads, the answer and the cost to the
dashboard. The model has no write tool, spends from a platform budget that
never touches smart credits (:mod:`.budget`), and nothing is stored but
counts and dollars.

Server-only: nothing here is imported by :mod:`remembra.marshal` itself, so a
base install (``pipx run ... remembra-relay doctor``) never loads the model
client or the web framework.
"""
