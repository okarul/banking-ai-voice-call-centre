"""The shape every agent returns, and the protocol every agent implements.

An agent turn always produces something the caller can be told. A refusal, a
clarifying question and a successful answer are all AgentResponse values; the
`success` flag and `reason` distinguish them for callers that need to branch.

Nothing here reaches the database. Agents call the tool layer, which enforces
authorization; this module only carries the result.
"""

from dataclasses import dataclass, field
from typing import Protocol

from app.agents.intents import Classification, Domain, Intent
from app.sessions import SessionManager


@dataclass(frozen=True)
class AgentResponse:
    """One agent's reply to one caller turn.

    `speech` is the sentence a later voice layer will read out. `data` is the
    structured tool result behind it, already filtered by the tool layer, so it
    never contains a PIN, a hash or another customer's values.
    """

    agent: str
    intent: Intent
    domain: Domain
    speech: str
    success: bool
    reason: str | None = None
    data: dict = field(default_factory=dict)
    # True when the turn was refused purely because the session is not yet
    # authenticated, so the caller can be taken through identification.
    requires_authentication: bool = False

    def to_dict(self) -> dict:
        """Safe view for development endpoints and tests."""
        return {
            "agent": self.agent,
            "intent": self.intent.value,
            "domain": self.domain.value,
            "speech": self.speech,
            "success": self.success,
            "reason": self.reason,
            "requires_authentication": self.requires_authentication,
            "data": self.data,
        }


class Agent(Protocol):
    """A domain specialist the supervisor can hand a classified turn to."""

    name: str

    def handle(
        self,
        session_id: str,
        classification: Classification,
        *,
        manager: SessionManager,
    ) -> AgentResponse:
        """Answer one turn that has already been classified and authorized."""
        ...
