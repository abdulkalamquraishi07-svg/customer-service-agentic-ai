"""Errors shared by the agent service and its helpers (kept separate to avoid import cycles)."""


class RequestRejected(Exception):
    """Invalid input or session misuse. Message is safe to return to the caller."""
