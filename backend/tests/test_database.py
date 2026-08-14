"""Phase 2 database tests.

These run against the local synthetic banking database. Seed it first::

    .\\.venv\\Scripts\\python.exe -m app.database.seed
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.database import repositories
from app.database.connection import get_session_factory
from app.database.models import Customer
from app.database.seed import DEMO_CUSTOMERS, seed_database
from app.main import app

DEMO_IDS = ["DEMO001", "DEMO002", "DEMO003", "DEMO004", "DEMO005"]


@pytest.fixture
def session():
    """A database session for a single test."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


def test_database_connection_succeeds(session):
    assert session.scalar(text("SELECT 1")) == 1


def test_demo001_exists(session):
    customer = repositories.get_customer_by_customer_id(session, "DEMO001")
    assert customer is not None
    assert customer.customer_id == "DEMO001"
    assert customer.name


def test_all_five_demo_customers_exist(session):
    for customer_id in DEMO_IDS:
        assert repositories.get_customer_by_customer_id(session, customer_id) is not None


def test_customer_ids_are_unique(session):
    total = session.scalar(select(func.count()).select_from(Customer))
    distinct = session.scalar(select(func.count(func.distinct(Customer.customer_id))))
    assert total == distinct


def test_demo001_has_at_least_one_account(session):
    accounts = repositories.get_accounts_for_customer(session, "DEMO001")
    assert len(accounts) >= 1


def test_demo001_has_transactions(session):
    account = repositories.get_account_by_type(session, "DEMO001", "Savings")
    assert account is not None
    transactions = repositories.get_recent_transactions_for_account(
        session, account.id, limit=10
    )
    assert len(transactions) >= 5


def test_demo001_has_at_least_one_loan(session):
    loans = repositories.get_loans_for_customer(session, "DEMO001")
    assert len(loans) >= 1


def test_account_balance_is_decimal(session):
    account = repositories.get_account_by_type(session, "DEMO001", "Savings")
    assert isinstance(account.available_balance, Decimal)
    assert account.available_balance > Decimal("0")


def test_loan_outstanding_balance_is_returned_correctly(session):
    loan = repositories.get_loan_by_type(session, "DEMO001", "Home Loan")
    assert loan is not None
    assert isinstance(loan.outstanding_balance, Decimal)
    assert loan.outstanding_balance == Decimal("284500.00")
    assert isinstance(loan.next_instalment_amount, Decimal)


def test_recent_transactions_respects_limit(session):
    account = repositories.get_account_by_type(session, "DEMO001", "Savings")
    limited = repositories.get_recent_transactions_for_account(
        session, account.id, limit=3
    )
    assert len(limited) == 3
    # Newest first.
    dates = [t.transaction_date for t in limited]
    assert dates == sorted(dates, reverse=True)


def test_unknown_customer_returns_empty_results(session):
    assert repositories.get_customer_by_customer_id(session, "NOPE999") is None
    assert repositories.get_accounts_for_customer(session, "NOPE999") == []
    assert repositories.get_loans_for_customer(session, "NOPE999") == []
    assert repositories.get_account_by_type(session, "NOPE999", "Savings") is None
    assert repositories.get_loan_by_type(session, "NOPE999", "Home Loan") is None


def test_seed_is_idempotent(session):
    before = repositories.count_customers(session)

    result = seed_database()

    assert result["inserted"] == 0
    assert result["skipped"] == len(DEMO_CUSTOMERS)
    session.expire_all()
    assert repositories.count_customers(session) == before


def test_dev_database_check_endpoint():
    client = TestClient(app)
    response = client.get("/dev/database-check")

    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "connected"
    assert body["demo_customers"] >= 5
    # The endpoint must not leak any customer data.
    assert set(body.keys()) == {"database", "demo_customers"}
