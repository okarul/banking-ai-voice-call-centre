"""Banking service tools.

These are ordinary backend functions today. A later phase will expose them as
controlled tools to the agent layer.
"""

from app.tools.accounts import (
    DEFAULT_TRANSACTION_LIMIT,
    MAX_TRANSACTION_LIMIT,
    MIN_TRANSACTION_LIMIT,
    get_account_balance,
    get_account_details,
    get_recent_transactions,
)
from app.tools.loans import (
    get_loan_balance,
    get_loan_details,
    get_next_instalment,
)

__all__ = [
    "DEFAULT_TRANSACTION_LIMIT",
    "MAX_TRANSACTION_LIMIT",
    "MIN_TRANSACTION_LIMIT",
    "get_account_balance",
    "get_account_details",
    "get_recent_transactions",
    "get_loan_balance",
    "get_loan_details",
    "get_next_instalment",
]
