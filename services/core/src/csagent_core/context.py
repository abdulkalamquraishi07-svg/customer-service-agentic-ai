"""Caller context: the ONLY source of identity for services and tools.

It is built from verified JWT claims (or an internal service identity), never from
model output or request bodies.
"""

from dataclasses import dataclass, field

CUSTOMERS = "customers"
SUPPORT_AGENTS = "support-agents"
APPROVERS = "approvers"
SYSTEM = "system"  # internal workflow identity (e.g. Step Functions task), never a user


@dataclass(frozen=True)
class CallerContext:
    subject: str  # Cognito `sub`
    groups: frozenset[str] = field(default_factory=frozenset)
    request_id: str = "unknown"

    @property
    def customer_id(self) -> str:
        """For customer callers the subject IS the customer id."""
        return self.subject

    def has_group(self, group: str) -> bool:
        return group in self.groups
