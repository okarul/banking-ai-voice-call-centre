"""FastAPI application entry point.

Service endpoints only. Database access lives in the repository layer and is
reached through routers, never directly from this module.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.observability.readiness import readiness_report
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

async def _release_live_calls() -> None:
    """End the calls this process is carrying, rather than abandoning them.

    Every registry involved has had a `close_all()` documented as "used on
    shutdown" since it was written, and until now nothing but the test suite
    ever called one. What that meant in practice is that a deployment did not
    end its live calls — it stopped answering them:

    * the caller's line stayed up, silent, because the media socket was never
      closed and so the gateway never sent the SIP BYE;
    * the model session stayed open at the provider, holding its slot and
      billing, until it timed out by itself;
    * and the operations row stayed ACTIVE with no `ended_at`, waiting for a
      later process to reconcile it — which never happens if the release is
      rolled back.

    The telephone calls go first and through `tear_down`, because they own the
    most: a bridge, a media socket, a model session and a capacity slot, in
    that order of dependency. Whatever model sessions remain after that are
    browser calls, whose audio belongs to a page this process cannot reach;
    their record is closed and their slot returned.

    Reservations last. A reservation is a slot claimed by a caller still
    connecting, so it is invisible to `close_all()` by design — and shutdown is
    the one moment that makes it nobody's to finish.
    """
    import asyncio

    from app.observability import recorder
    from app.realtime.browser_calls import voice_call_manager
    from app.telephony import reasons, service

    await service.release_live_calls()

    for banking_session_id in voice_call_manager.active_session_ids():
        # Off the loop: this is synchronous SQLAlchemy, and the drain it sits
        # in is already running against a deadline.
        await asyncio.to_thread(
            recorder.end_session,
            banking_session_id,
            reason=reasons.SERVICE_SHUTDOWN,
        )

    closed = await voice_call_manager.close_all()
    released = await voice_call_manager.release_all()
    if closed or released:
        logger.info(
            "shutdown: closed %d model session(s), released %d reservation(s)",
            closed,
            released,
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Bring this process up: schema first, then housekeeping.

    **Schema before anything can ask for it.** The declared tables are created
    or completed here, through the one canonical initialiser, because nothing
    else in production ever did. `create_tables` was reachable only from
    `seed_database`, which a deployment runs at most once — so a release that
    declared a new table shipped code that queried it against a database that
    had never been told about it. That is not hypothetical: Phase 6.12 added
    `call_trace_events`, the deployment did not run the seed, and the first
    request for a call trace returned 500 with `relation ... does not exist`.
    Hanging it here fixes every future additive table by the same act, with no
    table named anywhere in this file.

    Awaited, not scheduled: startup does not complete until the schema is
    usable, so no request can be served against a half-built database. Run on a
    worker thread because the initialiser is synchronous SQLAlchemy DDL and the
    event loop is not the place for it.

    **A schema failure does not stop the process.** This application already
    starts with PostgreSQL offline on purpose — importing it opens no
    connection, and `/health` answers so a restart loop cannot be triggered by a
    database blip. Killing startup here would trade that for a crash loop, and
    restarting fixes neither an unreachable database nor a broken migration. So
    the failure is recorded instead, and `/ready` reports it: liveness stays up,
    readiness goes red, and traffic stops arriving at a process that cannot
    serve it.

    **Then the dashboard housekeeping.** Banking sessions live in memory, so a
    restart ended every call the previous process was carrying — but their
    operational rows survive in PostgreSQL and would otherwise sit at ACTIVE
    for ever, showing an operator agents that nobody is on. Safe if it fails:
    the recorder swallows its own errors, and a dashboard that is briefly wrong
    must never stop the API from starting.

    **And on the way out, the calls are ended rather than abandoned.** The
    reconciliation above is a repair, and it only ever ran because there was
    nothing to repair *from*: this process carried live calls and then simply
    stopped answering them. See `_release_live_calls` for what that left
    behind. Bounded, and never fatal — a process that cannot tidy up still has
    to stop.
    """
    import asyncio

    from app.database.seed import ensure_schema
    from app.observability import readiness, recorder

    try:
        await asyncio.to_thread(ensure_schema)
        readiness.record_schema_initialisation(ready=True)
        logger.info("startup: database schema verified")
    except Exception as error:
        # Type only, never the exception text: a connection failure renders the
        # connection string, password included.
        logger.error(
            "startup: database schema initialisation failed (%s)",
            type(error).__name__,
        )
        readiness.record_schema_initialisation(
            ready=False, reason="initialisation_failed"
        )

    # Scoped to what this process does not hold. See `app.process_ownership`
    # for why ownership is answered from memory, and for the deployment
    # constraint that makes this correct.
    from app import process_ownership

    process_ownership.reconcile_on_startup()

    yield

    # --- stopping ---------------------------------------------------------
    #
    # Bounded, and the bound is the point. A supervisor gives a process a grace
    # period and then kills it, so a drain that could run indefinitely is a
    # drain that gets cut off mid-release — the ungraceful stop it was added to
    # avoid, now with a half-released call as well. Better to release what can
    # be released quickly and leave the rest to the reconciliation above, which
    # is exactly the case it was written for.
    #
    # Never fatal. A process that cannot tidy up still has to stop.
    try:
        await asyncio.wait_for(
            _release_live_calls(), timeout=settings.shutdown_drain_timeout
        )
    except asyncio.TimeoutError:
        logger.error(
            "shutdown: draining live calls exceeded %ss; the next startup will "
            "reconcile whatever was left",
            settings.shutdown_drain_timeout,
        )
    except Exception as error:
        # Type only, for the same reason as every other log in this module.
        logger.error("shutdown: draining live calls failed (%s)", type(error).__name__)


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

    # Development-only diagnostics, and now actually only in development.
    #
    # "Remove before any production deployment" was a comment, which is a
    # request rather than a guarantee: every one of these was mounted wherever
    # the application ran. `/dev/realtime/session/{id}/start` opens a provider
    # session, so on a production deployment it was a route that could spend
    # money and hold capacity outside the customer paths entirely.
    #
    # Not registering the route is a stronger guarantee than registering one
    # that refuses - the same reasoning the telephony endpoint above already
    # follows - and `APP_ENV` is the switch this application already has.
    if not settings.is_production:
        application.include_router(dev.router)
        application.include_router(dev_sessions.router)
        application.include_router(dev_auth.router)
        application.include_router(dev_accounts.router)
        application.include_router(dev_loans.router)
        application.include_router(dev_authorization.router)
        application.include_router(dev_agents.router)
        application.include_router(dev_realtime.router)
        logger.info("development diagnostics registered (APP_ENV=%s)", settings.app_env)

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
        """Liveness only: is this process answering?

        Deliberately cheap and dependency-free. A liveness check that touches
        the database restarts a healthy process during a database blip, which
        turns one outage into two.
        """
        return {"status": "ok"}

    @application.get("/readiness")
    def readiness(response: Response) -> dict:
        """Whether this process can actually serve a call right now.

        Separate from liveness because the answers lead to different actions:
        liveness failing means restart me, readiness failing means do not send
        me traffic yet. Reported per dependency so an operator can see which.

        **No paid usage.** The model provider is reported from configuration
        only — whether a key and a model are present — and never by opening a
        realtime session. A readiness endpoint that cost money per call would
        be one a monitoring system could bankrupt.
        """
        checks = readiness_report()
        if not checks["ready"]:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return checks

    return application


app = create_app()
