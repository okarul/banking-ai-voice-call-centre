"""Create the tables and load synthetic demo banking data.

Run from the backend folder::

    .\\.venv\\Scripts\\python.exe -m app.database.seed

The seed is idempotent: a customer that already exists is skipped, so running
this twice never duplicates data.

ALL DATA BELOW IS SYNTHETIC AND FICTIONAL. The four-digit PINs are fake demo
credentials used only to generate hashes for local testing. Only the hashes are
stored; the PINs themselves are never written to the database.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.database.connection import get_engine, session_scope
from app.database.models import Account, Base, Customer, Loan, Transaction
from app.database.repositories import get_customer_by_customer_id
from app.security import hash_pin

# Fixed reference date keeps seeded data reproducible across runs.
_TODAY = date(2026, 8, 1)


def _dt(month: int, day: int) -> datetime:
    return datetime(2026, month, day, 10, 0)


# --- Synthetic demo dataset -------------------------------------------------
# demo_pin values are FAKE credentials for local testing only.
DEMO_CUSTOMERS: list[dict] = [
    {
        "customer_id": "DEMO001",
        "name": "Alex Tan",
        "demo_pin": "4821",
        "accounts": [
            {
                "account_type": "Savings",
                "account_number_masked": "XXXX1001",
                "available_balance": Decimal("12450.75"),
                "transactions": [
                    (_dt(7, 25), "Salary Credit", Decimal("5200.00"), "credit"),
                    (_dt(7, 27), "NTUC FairPrice", Decimal("86.40"), "debit"),
                    (_dt(7, 29), "Utility Payment", Decimal("142.30"), "debit"),
                    (_dt(7, 31), "Transport", Decimal("38.00"), "debit"),
                    (_dt(8, 1), "Restaurant", Decimal("64.90"), "debit"),
                    (_dt(8, 2), "FAST Transfer", Decimal("500.00"), "debit"),
                ],
            },
            {
                "account_type": "Current",
                "account_number_masked": "XXXX2001",
                "available_balance": Decimal("3820.10"),
                "transactions": [
                    (_dt(7, 26), "FAST Transfer", Decimal("1200.00"), "credit"),
                    (_dt(7, 28), "Insurance Premium", Decimal("310.00"), "debit"),
                    (_dt(7, 30), "ATM Withdrawal", Decimal("200.00"), "debit"),
                    (_dt(8, 1), "Utility Payment", Decimal("95.60"), "debit"),
                    (_dt(8, 3), "Restaurant", Decimal("48.20"), "debit"),
                ],
            },
        ],
        "loans": [
            {
                "loan_type": "Home Loan",
                "loan_reference": "HL-DEMO001",
                "outstanding_balance": Decimal("284500.00"),
                "interest_rate": Decimal("3.250"),
                "next_instalment_amount": Decimal("1985.40"),
                "next_instalment_date": date(2026, 9, 5),
                "maturity_date": date(2041, 8, 5),
            }
        ],
    },
    {
        "customer_id": "DEMO002",
        "name": "Priya Menon",
        "demo_pin": "7315",
        "accounts": [
            {
                "account_type": "Savings",
                "account_number_masked": "XXXX1002",
                "available_balance": Decimal("8730.20"),
                "transactions": [
                    (_dt(7, 24), "Salary Credit", Decimal("4300.00"), "credit"),
                    (_dt(7, 26), "NTUC FairPrice", Decimal("124.75"), "debit"),
                    (_dt(7, 28), "Transport", Decimal("52.00"), "debit"),
                    (_dt(7, 30), "Insurance Premium", Decimal("188.00"), "debit"),
                    (_dt(8, 2), "ATM Withdrawal", Decimal("300.00"), "debit"),
                ],
            }
        ],
        "loans": [
            {
                "loan_type": "Personal Loan",
                "loan_reference": "PL-DEMO002",
                "outstanding_balance": Decimal("18400.00"),
                "interest_rate": Decimal("6.500"),
                "next_instalment_amount": Decimal("620.00"),
                "next_instalment_date": date(2026, 9, 12),
                "maturity_date": date(2029, 8, 12),
            }
        ],
    },
    {
        "customer_id": "DEMO003",
        "name": "Wei Ming Lim",
        "demo_pin": "2648",
        "accounts": [
            {
                "account_type": "Savings",
                "account_number_masked": "XXXX1003",
                "available_balance": Decimal("25610.55"),
                "transactions": [
                    (_dt(7, 23), "Salary Credit", Decimal("6800.00"), "credit"),
                    (_dt(7, 25), "Utility Payment", Decimal("176.20"), "debit"),
                    (_dt(7, 27), "NTUC FairPrice", Decimal("98.30"), "debit"),
                    (_dt(7, 29), "Restaurant", Decimal("142.00"), "debit"),
                    (_dt(8, 1), "Transport", Decimal("44.50"), "debit"),
                    (_dt(8, 3), "FAST Transfer", Decimal("2500.00"), "debit"),
                ],
            },
            {
                "account_type": "Current",
                "account_number_masked": "XXXX2003",
                "available_balance": Decimal("15200.00"),
                "transactions": [
                    (_dt(7, 22), "FAST Transfer", Decimal("3000.00"), "credit"),
                    (_dt(7, 26), "Insurance Premium", Decimal("450.00"), "debit"),
                    (_dt(7, 31), "ATM Withdrawal", Decimal("500.00"), "debit"),
                    (_dt(8, 2), "Utility Payment", Decimal("210.40"), "debit"),
                    (_dt(8, 4), "NTUC FairPrice", Decimal("67.85"), "debit"),
                ],
            },
        ],
        "loans": [
            {
                "loan_type": "Car Loan",
                "loan_reference": "CL-DEMO003",
                "outstanding_balance": Decimal("46200.00"),
                "interest_rate": Decimal("2.780"),
                "next_instalment_amount": Decimal("890.00"),
                "next_instalment_date": date(2026, 9, 8),
                "maturity_date": date(2031, 8, 8),
            },
            {
                "loan_type": "Home Loan",
                "loan_reference": "HL-DEMO003",
                "outstanding_balance": Decimal("512000.00"),
                "interest_rate": Decimal("3.100"),
                "next_instalment_amount": Decimal("2640.75"),
                "next_instalment_date": date(2026, 9, 15),
                "maturity_date": date(2046, 8, 15),
            },
        ],
    },
    {
        "customer_id": "DEMO004",
        "name": "Sarah Johnson",
        "demo_pin": "9153",
        "accounts": [
            {
                "account_type": "Savings",
                "account_number_masked": "XXXX1004",
                "available_balance": Decimal("4310.90"),
                "transactions": [
                    (_dt(7, 24), "Salary Credit", Decimal("3900.00"), "credit"),
                    (_dt(7, 27), "Transport", Decimal("61.20"), "debit"),
                    (_dt(7, 29), "NTUC FairPrice", Decimal("143.65"), "debit"),
                    (_dt(8, 1), "Restaurant", Decimal("72.40"), "debit"),
                    (_dt(8, 3), "Utility Payment", Decimal("118.00"), "debit"),
                ],
            }
        ],
        "loans": [
            {
                "loan_type": "Personal Loan",
                "loan_reference": "PL-DEMO004",
                "outstanding_balance": Decimal("9250.00"),
                "interest_rate": Decimal("7.200"),
                "next_instalment_amount": Decimal("415.00"),
                "next_instalment_date": date(2026, 9, 20),
                "maturity_date": date(2028, 8, 20),
            }
        ],
    },
    {
        "customer_id": "DEMO005",
        "name": "Daniel Rodriguez",
        "demo_pin": "6072",
        "accounts": [
            {
                "account_type": "Current",
                "account_number_masked": "XXXX2005",
                "available_balance": Decimal("31875.40"),
                "transactions": [
                    (_dt(7, 21), "Salary Credit", Decimal("7500.00"), "credit"),
                    (_dt(7, 25), "Insurance Premium", Decimal("520.00"), "debit"),
                    (_dt(7, 28), "FAST Transfer", Decimal("1800.00"), "debit"),
                    (_dt(7, 30), "Restaurant", Decimal("96.30"), "debit"),
                    (_dt(8, 2), "ATM Withdrawal", Decimal("400.00"), "debit"),
                    (_dt(8, 4), "Transport", Decimal("57.80"), "debit"),
                ],
            }
        ],
        "loans": [
            {
                "loan_type": "Car Loan",
                "loan_reference": "CL-DEMO005",
                "outstanding_balance": Decimal("22800.00"),
                "interest_rate": Decimal("2.950"),
                "next_instalment_amount": Decimal("640.50"),
                "next_instalment_date": date(2026, 9, 3),
                "maturity_date": date(2030, 8, 3),
            }
        ],
    },
]


def create_tables() -> None:
    """Create any tables that do not exist yet."""
    Base.metadata.create_all(get_engine())


def _insert_customer(session: Session, data: dict) -> None:
    """Insert one customer with its accounts, transactions and loans."""
    customer = Customer(
        customer_id=data["customer_id"],
        name=data["name"],
        # Only the hash is persisted; the demo PIN itself is never stored.
        pin_hash=hash_pin(data["demo_pin"]),
        status="active",
    )

    for account_data in data["accounts"]:
        account = Account(
            account_type=account_data["account_type"],
            account_number_masked=account_data["account_number_masked"],
            available_balance=account_data["available_balance"],
            currency="SGD",
            status="active",
        )
        for when, description, amount, kind in account_data["transactions"]:
            account.transactions.append(
                Transaction(
                    transaction_date=when,
                    description=description,
                    amount=amount,
                    transaction_type=kind,
                )
            )
        customer.accounts.append(account)

    for loan_data in data["loans"]:
        customer.loans.append(Loan(currency="SGD", status="active", **loan_data))

    session.add(customer)


def seed_database() -> dict[str, int]:
    """Create tables and insert any missing demo customers.

    Returns a summary of how many customers were inserted and skipped.
    """
    create_tables()

    inserted = 0
    skipped = 0
    with session_scope() as session:
        for data in DEMO_CUSTOMERS:
            if get_customer_by_customer_id(session, data["customer_id"]) is not None:
                skipped += 1
                continue
            _insert_customer(session, data)
            inserted += 1

    return {"inserted": inserted, "skipped": skipped}


def main() -> None:
    result = seed_database()
    print(
        f"Seed complete. Customers inserted: {result['inserted']}, "
        f"already present (skipped): {result['skipped']}."
    )


if __name__ == "__main__":
    main()
