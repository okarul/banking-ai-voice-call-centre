"""The controlled tool surface exposed to the agent layer.

This is the only place an agent may reach a banking tool from, and it exists to
make one guarantee enforceable in a single file:

    the caller of a tool never chooses whose data is read

`session_id` is supplied by `dispatch` from its own argument, never from the
`arguments` mapping, and no schema declares a `session_id` or `customer_id`
property. An argument the schema does not declare is rejected outright rather
than forwarded, so a caller — today a test, later a language model emitting a
function call — cannot smuggle an identity in beside the real parameters.

    caller -> dispatch(name, session_id, arguments) -> tool -> guard -> session
"""

from dataclasses import dataclass
from typing import Callable

from app.sessions import SessionManager
from app.sessions import session_manager as default_manager
from app.tools import accounts, loans

# Names that must never be accepted from a caller's arguments. They are already
# absent from every schema; this list makes the intent explicit and is asserted
# by the tests.
FORBIDDEN_ARGUMENTS = frozenset({"session_id", "customer_id", "manager"})


class ToolError:
    """Reasons a dispatch can fail before the tool itself is reached."""

    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    UNKNOWN_ARGUMENT = "UNKNOWN_ARGUMENT"
    INVALID_ARGUMENT_TYPE = "INVALID_ARGUMENT_TYPE"


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool and the JSON Schema describing its arguments."""

    name: str
    description: str
    function: Callable[..., dict]
    parameters: dict

    def schema(self) -> dict:
        """Function-calling style description, safe to hand to any caller."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _object_schema(properties: dict) -> dict:
    """A JSON Schema object that accepts only the listed properties."""
    return {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


_ACCOUNT_TYPE_PROPERTY = {
    "type": ["string", "null"],
    "description": (
        "Which account, e.g. 'Savings' or 'Current'. Omit when the customer "
        "did not say; the tool asks for it when more than one account exists."
    ),
}

_LOAN_TYPE_PROPERTY = {
    "type": ["string", "null"],
    "description": (
        "Which loan, e.g. 'Home Loan', 'Personal Loan' or 'Car Loan'. Omit "
        "when the customer did not say."
    ),
}

TOOL_SPECS = (
    ToolSpec(
        name="get_account_balance",
        description="Available balance for the authenticated customer's account.",
        function=accounts.get_account_balance,
        parameters=_object_schema({"account_type": _ACCOUNT_TYPE_PROPERTY}),
    ),
    ToolSpec(
        name="get_account_details",
        description="Account type, masked number, balance and status.",
        function=accounts.get_account_details,
        parameters=_object_schema({"account_type": _ACCOUNT_TYPE_PROPERTY}),
    ),
    ToolSpec(
        name="get_recent_transactions",
        description="Most recent transactions for one account, newest first.",
        function=accounts.get_recent_transactions,
        parameters=_object_schema(
            {
                "account_type": _ACCOUNT_TYPE_PROPERTY,
                "limit": {
                    "type": "integer",
                    "minimum": accounts.MIN_TRANSACTION_LIMIT,
                    "maximum": accounts.MAX_TRANSACTION_LIMIT,
                    "description": (
                        f"How many transactions to return "
                        f"({accounts.MIN_TRANSACTION_LIMIT}-"
                        f"{accounts.MAX_TRANSACTION_LIMIT}). Defaults to "
                        f"{accounts.DEFAULT_TRANSACTION_LIMIT}."
                    ),
                },
            }
        ),
    ),
    ToolSpec(
        name="get_loan_balance",
        description="Outstanding balance for the authenticated customer's loan.",
        function=loans.get_loan_balance,
        parameters=_object_schema({"loan_type": _LOAN_TYPE_PROPERTY}),
    ),
    ToolSpec(
        name="get_loan_details",
        description="Loan balance, interest rate, next instalment and maturity.",
        function=loans.get_loan_details,
        parameters=_object_schema({"loan_type": _LOAN_TYPE_PROPERTY}),
    ),
    ToolSpec(
        name="get_next_instalment",
        description="Amount and date of the next instalment due on a loan.",
        function=loans.get_next_instalment,
        parameters=_object_schema({"loan_type": _LOAN_TYPE_PROPERTY}),
    ),
)

TOOLS = {spec.name: spec for spec in TOOL_SPECS}

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
    "object": dict,
    "array": list,
}


def tool_schemas() -> list[dict]:
    """Every tool description, in registration order."""
    return [spec.schema() for spec in TOOL_SPECS]


def tool_names() -> list[str]:
    """Every registered tool name, in registration order."""
    return [spec.name for spec in TOOL_SPECS]


def _failure(reason: str, **extra) -> dict:
    """Structured failure, matching the shape the tool layer returns."""
    return {"success": False, "reason": reason, **extra}


def _type_matches(value, declared) -> bool:
    """Check a value against a JSON Schema `type`, which may be a list.

    Booleans are rejected where an integer is declared: Python treats `True` as
    `1`, and a limit of `True` is a caller error, not a request for one row.
    """
    names = declared if isinstance(declared, list) else [declared]
    for name in names:
        expected = _JSON_TYPES.get(name)
        if expected is None:
            continue
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, expected):
            return True
    return False


def validate_arguments(spec: ToolSpec, arguments: dict) -> dict | None:
    """Return a failure dict if the arguments are not acceptable, else None.

    Range checks are deliberately left to the tool itself, which already
    validates them and owns the reason codes. This function only rejects what
    would otherwise reach the tool as an unexpected keyword or a wrong type.
    """
    properties = spec.parameters["properties"]

    for name, value in arguments.items():
        if name not in properties:
            # Covers the forbidden names too: none of them is ever declared.
            return _failure(
                ToolError.UNKNOWN_ARGUMENT,
                tool=spec.name,
                argument=name,
                accepted_arguments=sorted(properties),
            )
        if not _type_matches(value, properties[name].get("type", "string")):
            return _failure(
                ToolError.INVALID_ARGUMENT_TYPE,
                tool=spec.name,
                argument=name,
                expected_type=properties[name].get("type"),
            )

    return None


def dispatch(
    tool_name: str,
    session_id: str,
    arguments: dict | None = None,
    *,
    manager: SessionManager = default_manager,
) -> dict:
    """Run one registered tool for one session.

    The session id comes from this function's own parameter. Anything the
    caller put in `arguments` is checked against the tool's schema first, so an
    identity-shaped argument is rejected rather than reaching the tool.
    """
    spec = TOOLS.get(tool_name)
    if spec is None:
        return _failure(
            ToolError.UNKNOWN_TOOL,
            tool=tool_name,
            available_tools=tool_names(),
        )

    arguments = dict(arguments or {})
    error = validate_arguments(spec, arguments)
    if error is not None:
        return error

    return spec.function(session_id, **arguments, manager=manager)
