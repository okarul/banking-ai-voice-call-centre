"""DEVELOPMENT-ONLY endpoints.

Used to verify local database connectivity. Returns no customer data:
no PINs, no hashes, no balances, no transactions, no loan values.
This router must not be exposed in a production deployment.
"""

from fastapi import APIRouter
from sqlalchemy.exc import SQLAlchemyError

from app.database import repositories
from app.database.connection import DatabaseNotConfiguredError, get_session_factory

router = APIRouter(prefix="/dev", tags=["development"])


@router.get("/database-check")
def database_check() -> dict:
    """Report whether the database is reachable and how many demo customers exist.

    Reports a plain "unavailable" status rather than raising, so this endpoint
    stays useful while PostgreSQL is offline or unconfigured.
    """
    try:
        session = get_session_factory()()
    except DatabaseNotConfiguredError:
        return {"database": "not_configured", "demo_customers": 0}

    try:
        return {
            "database": "connected",
            "demo_customers": repositories.count_customers(session),
        }
    except SQLAlchemyError:
        return {"database": "unavailable", "demo_customers": 0}
    finally:
        session.close()
