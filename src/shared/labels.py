"""Canonical ``agent:*`` GitHub label definitions shared across ingest, worker, and reviewer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

AGENT_LABEL_PREFIX = "agent:"


class AgentLabel(StrEnum):
    """State-machine labels managed by the agentic workflow."""

    PLAN = "agent:plan"
    READY = "agent:ready"
    IN_PROGRESS = "agent:in-progress"
    IN_REVIEW = "agent:in-review"
    CHANGES_REQUESTED = "agent:changes-requested"
    NEEDS_HUMAN = "agent:needs-human"
    PAUSED = "agent:paused"


@dataclass(frozen=True, slots=True)
class LabelSpec:
    """GitHub label specification for idempotent repository bootstrapping."""

    name: AgentLabel
    color: str
    description: str


AGENT_LABEL_SPECS: tuple[LabelSpec, ...] = (
    LabelSpec(
        name=AgentLabel.PLAN,
        color="1D76DB",
        description="Trigger agent to produce an implementation plan before coding",
    ),
    LabelSpec(
        name=AgentLabel.READY,
        color="0E8A16",
        description="Issue is ready for the coding agent to implement",
    ),
    LabelSpec(
        name=AgentLabel.IN_PROGRESS,
        color="FBCA04",
        description="Agent is actively working on this issue or PR",
    ),
    LabelSpec(
        name=AgentLabel.IN_REVIEW,
        color="5319E7",
        description="PR is awaiting automated or human code review",
    ),
    LabelSpec(
        name=AgentLabel.CHANGES_REQUESTED,
        color="D93F0B",
        description="Review requested changes; coding agent will iterate",
    ),
    LabelSpec(
        name=AgentLabel.NEEDS_HUMAN,
        color="B60205",
        description="Agent hit a blocker or max review rounds; human intervention needed",
    ),
    LabelSpec(
        name=AgentLabel.PAUSED,
        color="CFD3D7",
        description="Kill switch: ignore all agent webhooks for this issue or PR",
    ),
)

ALL_AGENT_LABELS: frozenset[str] = frozenset(label.value for label in AgentLabel)


def is_agent_label(label_name: str) -> bool:
    """Return True if the label belongs to the ``agent:*`` namespace."""
    return label_name.startswith(AGENT_LABEL_PREFIX)
