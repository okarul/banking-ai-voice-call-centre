"""FastAPI application entry point.

Service endpoints only. Database access lives in the repository layer and is
reached through routers, never directly from this module.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.redaction import install_redaction
from app.routers import (
    admin,
    call,
    dev,
    dev_accounts,
    dev_agents,
    dev_auth,
    dev_authorization,
    dev_loans,
    dev_realtime,
    dev_sessions,
)

# Before anything can log. The API key travels inside ordinary structures — an
# Authorization header, a model config dict — so any log line, exception message
# or traceback that renders one would print it. Scrubbing at the handler is the
# only place that covers paths nobody wrote deliberately, including third-party
# loggers that share this output.
install_redaction(logging.getLogger())
install_redaction(logging.getLogger("uvicorn.error"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup housekeeping for the operations dashboard.

    Banking sessions live in memory, so a restart ended every call the previous
    process was carrying — but their operational rows survive in PostgreSQL and
    would otherwise sit at ACTIVE for ever, showing an operator agents that
    nobody is on. Safe if it fails: the recorder swallows its own errors, and a
    dashboard that is briefly wrong must never stop the API from starting.
    """
    from app.observability import recorder

    recorder.reconcile_active_sessions()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# The customer page is served from its own port, so the browser needs this to
# call the API at all. Named origins only, and only the methods and header the
# page actually uses — a wildcard here would let any site on the machine open
# banking calls. No credentials: the page carries no cookie and no auth header,
# and its session id is the one the backend handed it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# The customer-facing telephone API.
app.include_router(call.router)

# The operator's dashboard API. Loopback-only, and deliberately a separate
# surface from the customer routes above.
app.include_router(admin.router)

# Development-only diagnostics. Remove before any production deployment.
app.include_router(dev.router)
app.include_router(dev_sessions.router)
app.include_router(dev_auth.router)
app.include_router(dev_accounts.router)
app.include_router(dev_loans.router)
app.include_router(dev_authorization.router)
app.include_router(dev_agents.router)
app.include_router(dev_realtime.router)


@app.get("/")
def read_root() -> dict:
    """Basic service identification endpoint."""
    return {"service": settings.app_name, "status": "running"}


@app.get("/health")
def health() -> dict:
    """Liveness check used by tooling and monitoring."""
    return {"status": "ok"}
