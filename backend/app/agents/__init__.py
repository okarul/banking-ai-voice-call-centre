"""The agent layer: deciding who answers a caller turn, and in what words.

Everything here is deterministic Python. No language model is called, and the
agents reach banking data only through the registry, which passes the session
id straight to the tool layer and its authorization guards.

    handle_turn -> SupervisorAgent -> guard -> domain agent -> registry -> tool
"""

from app.agents.account_agent import AccountServicesAgent
from app.agents.base import Agent, AgentResponse
from app.agents.intents import (
    Action,
    Classification,
    Domain,
    Intent,
    classify,
    parse_type_reply,
)
from app.agents.loan_agent import LoanServicesAgent
from app.agents.registry import (
    TOOLS,
    ToolError,
    ToolSpec,
    dispatch,
    tool_names,
    tool_schemas,
)
from app.agents.router import handle_turn
from app.agents.supervisor import SupervisorAgent, supervisor

__all__ = [
    "Agent",
    "AgentResponse",
    "AccountServicesAgent",
    "LoanServicesAgent",
    "SupervisorAgent",
    "supervisor",
    "handle_turn",
    "Action",
    "Classification",
    "Domain",
    "Intent",
    "classify",
    "parse_type_reply",
    "TOOLS",
    "ToolError",
    "ToolSpec",
    "dispatch",
    "tool_names",
    "tool_schemas",
]
