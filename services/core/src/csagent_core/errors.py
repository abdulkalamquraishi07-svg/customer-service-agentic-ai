"""Domain errors. Messages are safe to show to callers (no internal details)."""


class DomainError(Exception):
    code = "domain_error"


class NotFound(DomainError):
    """Also returned when a resource belongs to another customer (no existence leak)."""

    code = "not_found"


class Forbidden(DomainError):
    code = "forbidden"


class ValidationFailed(DomainError):
    code = "validation_failed"


class Conflict(DomainError):
    code = "conflict"


class ConcurrentModification(Conflict):
    code = "concurrent_modification"


class IdempotencyConflict(Conflict):
    """Same idempotency key reused with a different request payload."""

    code = "idempotency_conflict"


class InvalidTransition(Conflict):
    code = "invalid_transition"


class PolicyViolation(DomainError):
    code = "policy_violation"


class DependencyFailure(DomainError):
    """Downstream system (e.g. payment gateway) failed; safe to retry with the same key."""

    code = "dependency_failure"
