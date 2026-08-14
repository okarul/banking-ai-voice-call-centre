"""Database access functions.

Plain queries only: no agent logic, no voice logic, no authorization rules.
Every function takes an explicit SQLAlchemy Session so callers control the
transaction and the functions stay easy to test.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import Account, Customer, Loan, Transaction


def get_customer_by_customer_id(session: Session, customer_id: str) -> Customer | None:
    """Return the customer with this public customer_id, or None."""
    return session.scalar(select(Customer).where(Customer.customer_id == customer_id))


def get_accounts_for_customer(session: Session, customer_id: str) -> list[Account]:
    """Return all accounts for a customer. Empty list if the customer is unknown."""
    stmt = (
        select(Account)
        .join(Customer)
        .where(Customer.customer_id == customer_id)
        .order_by(Account.id)
    )
    return list(session.scalars(stmt))


def get_account_by_type(
    session: Session, customer_id: str, account_type: str
) -> Account | None:
    """Return the customer's account of this type, or None.

    The account type is matched case-insensitively ("savings" == "Savings").
    """
    stmt = (
        select(Account)
        .join(Customer)
        .where(
            Customer.customer_id == customer_id,
            func.lower(Account.account_type) == account_type.lower(),
        )
        .order_by(Account.id)
    )
    return session.scalars(stmt).first()


def get_recent_transactions_for_account(
    session: Session, account_id: int, limit: int = 5
) -> list[Transaction]:
    """Return the most recent transactions for an account, newest first."""
    stmt = (
        select(Transaction)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def get_loans_for_customer(session: Session, customer_id: str) -> list[Loan]:
    """Return all loans for a customer. Empty list if the customer is unknown."""
    stmt = (
        select(Loan)
        .join(Customer)
        .where(Customer.customer_id == customer_id)
        .order_by(Loan.id)
    )
    return list(session.scalars(stmt))


def get_loan_by_type(session: Session, customer_id: str, loan_type: str) -> Loan | None:
    """Return the customer's loan of this type, or None."""
    stmt = (
        select(Loan)
        .join(Customer)
        .where(
            Customer.customer_id == customer_id,
            func.lower(Loan.loan_type) == loan_type.lower(),
        )
        .order_by(Loan.id)
    )
    return session.scalars(stmt).first()


def count_customers(session: Session) -> int:
    """Total number of customers. Used by the development database check."""
    return session.scalar(select(func.count()).select_from(Customer)) or 0
