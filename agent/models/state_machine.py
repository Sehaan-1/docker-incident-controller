from __future__ import annotations

from agent.models.incident import IncidentStatus


class IncidentStateMachine:
    """
    Single source of truth for legal incident status transitions.

    Transitions are defined as: {from_status: frozenset[to_status, ...]}

    Terminal states (RESOLVED, FAILED, NEEDS_HUMAN) map to empty frozensets —
    any transition out of them raises InvalidTransitionError.
    """

    _TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
        IncidentStatus.OPEN: frozenset(
            {
                IncidentStatus.PLANNED,
                IncidentStatus.NEEDS_HUMAN,
            }
        ),
        IncidentStatus.PLANNED: frozenset(
            {
                IncidentStatus.IN_PROGRESS,
                IncidentStatus.NEEDS_HUMAN,
            }
        ),
        IncidentStatus.IN_PROGRESS: frozenset(
            {
                IncidentStatus.RESOLVED,
                IncidentStatus.FAILED,
                IncidentStatus.NEEDS_HUMAN,
            }
        ),
        # FAILED is retryable — the worker may re-queue it as OPEN up to MAX_ATTEMPTS times,
        # or escalate to NEEDS_HUMAN once the attempt budget is exhausted.
        IncidentStatus.FAILED: frozenset({IncidentStatus.OPEN, IncidentStatus.NEEDS_HUMAN}),
        # Terminal states — no outgoing transitions.
        IncidentStatus.RESOLVED: frozenset(),
        IncidentStatus.NEEDS_HUMAN: frozenset(),
    }

    @classmethod
    def can_transition(cls, from_status: IncidentStatus, to_status: IncidentStatus) -> bool:
        """Return True iff the transition from_status → to_status is legal."""
        return to_status in cls._TRANSITIONS.get(from_status, frozenset())

    @classmethod
    def assert_can_transition(cls, from_status: IncidentStatus, to_status: IncidentStatus) -> None:
        """Raise InvalidTransitionError if the transition is not legal."""
        if not cls.can_transition(from_status, to_status):
            raise InvalidTransitionError(
                f"cannot transition from {from_status.value!r} to {to_status.value!r}"
            )

    @classmethod
    def actionable_statuses(cls) -> list[IncidentStatus]:
        """
        Return all statuses that have at least one outgoing transition
        (i.e. statuses the worker loop should actively process).
        """
        return [
            status
            for status, targets in cls._TRANSITIONS.items()
            if targets  # non-empty frozenset → not terminal
        ]


class InvalidTransitionError(ValueError):
    """Raised when a requested incident status transition is not permitted."""
