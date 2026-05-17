from __future__ import annotations

import pytest

from agent.models.incident import IncidentStatus
from agent.models.state_machine import IncidentStateMachine, InvalidTransitionError


# ---------------------------------------------------------------------------
# Fixtures: parametrized valid and invalid transitions
# ---------------------------------------------------------------------------

VALID_TRANSITIONS = [
    (IncidentStatus.OPEN, IncidentStatus.PLANNED),
    (IncidentStatus.OPEN, IncidentStatus.NEEDS_HUMAN),
    (IncidentStatus.PLANNED, IncidentStatus.IN_PROGRESS),
    (IncidentStatus.PLANNED, IncidentStatus.NEEDS_HUMAN),
    (IncidentStatus.IN_PROGRESS, IncidentStatus.RESOLVED),
    (IncidentStatus.IN_PROGRESS, IncidentStatus.FAILED),
    (IncidentStatus.IN_PROGRESS, IncidentStatus.NEEDS_HUMAN),
    # FAILED is retryable — can go back to OPEN or escalate to NEEDS_HUMAN.
    (IncidentStatus.FAILED, IncidentStatus.OPEN),
    (IncidentStatus.FAILED, IncidentStatus.NEEDS_HUMAN),
]

INVALID_TRANSITIONS = [
    # Skip-forward / backwards
    (IncidentStatus.OPEN, IncidentStatus.RESOLVED),
    (IncidentStatus.OPEN, IncidentStatus.IN_PROGRESS),
    (IncidentStatus.OPEN, IncidentStatus.FAILED),
    (IncidentStatus.PLANNED, IncidentStatus.OPEN),
    (IncidentStatus.PLANNED, IncidentStatus.RESOLVED),
    (IncidentStatus.PLANNED, IncidentStatus.FAILED),
    # Terminal → anything
    (IncidentStatus.RESOLVED, IncidentStatus.OPEN),
    (IncidentStatus.RESOLVED, IncidentStatus.PLANNED),
    (IncidentStatus.RESOLVED, IncidentStatus.IN_PROGRESS),
    # FAILED → RESOLVED and FAILED → IN_PROGRESS are still illegal.
    (IncidentStatus.FAILED, IncidentStatus.RESOLVED),
    (IncidentStatus.FAILED, IncidentStatus.IN_PROGRESS),
    (IncidentStatus.NEEDS_HUMAN, IncidentStatus.PLANNED),
    (IncidentStatus.NEEDS_HUMAN, IncidentStatus.IN_PROGRESS),
    (IncidentStatus.NEEDS_HUMAN, IncidentStatus.OPEN),
]


# ---------------------------------------------------------------------------
# Step 3A tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("from_status,to_status", VALID_TRANSITIONS)
def test_valid_transitions(from_status: IncidentStatus, to_status: IncidentStatus) -> None:
    assert IncidentStateMachine.can_transition(from_status, to_status)


@pytest.mark.parametrize("from_status,to_status", INVALID_TRANSITIONS)
def test_invalid_transitions_are_rejected(
    from_status: IncidentStatus, to_status: IncidentStatus
) -> None:
    assert not IncidentStateMachine.can_transition(from_status, to_status)


@pytest.mark.parametrize("from_status,to_status", INVALID_TRANSITIONS)
def test_assert_can_transition_raises_on_invalid(
    from_status: IncidentStatus, to_status: IncidentStatus
) -> None:
    with pytest.raises(InvalidTransitionError):
        IncidentStateMachine.assert_can_transition(from_status, to_status)


@pytest.mark.parametrize("from_status,to_status", VALID_TRANSITIONS)
def test_assert_can_transition_does_not_raise_on_valid(
    from_status: IncidentStatus, to_status: IncidentStatus
) -> None:
    # Must not raise
    IncidentStateMachine.assert_can_transition(from_status, to_status)


# ---------------------------------------------------------------------------
# Step 3C helper — actionable_statuses
# ---------------------------------------------------------------------------


def test_actionable_statuses_contains_all_non_terminal_statuses() -> None:
    actionable = IncidentStateMachine.actionable_statuses()
    assert set(actionable) == {
        IncidentStatus.OPEN,
        IncidentStatus.PLANNED,
        IncidentStatus.IN_PROGRESS,
        # FAILED is now retryable, so it is actionable.
        IncidentStatus.FAILED,
    }


def test_actionable_statuses_excludes_terminal_statuses() -> None:
    actionable = set(IncidentStateMachine.actionable_statuses())
    for terminal in (
        IncidentStatus.RESOLVED,
        IncidentStatus.NEEDS_HUMAN,
    ):
        assert terminal not in actionable


def test_invalid_transition_error_message_is_descriptive() -> None:
    with pytest.raises(InvalidTransitionError, match="RESOLVED.*OPEN"):
        IncidentStateMachine.assert_can_transition(IncidentStatus.RESOLVED, IncidentStatus.OPEN)
