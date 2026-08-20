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

logger = logging.getLogger("app.main")

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


def create_app() -> FastAPI:
    """Build the application.

    A factory rather than a module-level assembly because one router is
    conditional, and the condition is read at build time: telephony must be
    enabled *and* a signing secret configured before the inbound event
    endpoint exists at all.

    Not registering the route is a stronger guarantee than registering one that
    refuses. A route that exists is a route that can be reached, mis-deployed,
    or accidentally exempted from a check by a later change; one that was never
    added answers nothing, and the default configuration is the one where it
    was never added. Turning telephony on is therefore a deliberate act that
    requires a restart, which is the correct weight for opening a public
    endpoint into a bank.
    """
    application = FastAPI(title=settings.app_name, lifespan=lifespan)

    # The customer page is served from its own port, so the browser needs this
    # to call the API at all. Named origins only, and only the methods and
    # header the page actually uses — a wildcard here would let any site on the
    # machine open banking calls. No credentials: the page carries no cookie and
    # no auth header, and its session id is the one the backend handed it.
    #
    # The telephony route below is deliberately outside this: a provider is not
    # a browser, it sends no Origin, and CORS is not what protects it.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    # The customer-facing telephone API.
    application.include_router(call.router)

    # The operator's dashboard API. Loopback-only, and deliberately a separate
    # surface from the customer routes above.
    application.include_router(admin.router)

    # Development-only diagnostics. Remove before any production deployment.
    application.include_router(dev.router)
    application.include_router(dev_sessions.router)
    application.include_router(dev_auth.router)
    application.include_router(dev_accounts.router)
    application.include_router(dev_loans.router)
    application.include_router(dev_authorization.router)
    application.include_router(dev_agents.router)
    application.include_router(dev_realtime.router)

    # The provider-facing event boundary. Absent by default.
    if settings.telephony_webhook_ready:
        from app.routers import telephony

        application.include_router(telephony.router)
        logger.info("telephony inbound event endpoint registered")

    @application.get("/")
    def read_root() -> dict:
        """Basic service identification endpoint."""
        return {"service": settings.app_name, "status": "running"}

    @application.get("/health")
    def health() -> dict:
        """Liveness check used by tooling and monitoring."""
        return {"status": "ok"}

    return application


app = create_app()
