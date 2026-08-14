"""The operations dashboard's API. Operator-only, and separate on purpose.

These routes describe *calls*, not customers. They exist so an instructor can
see what the voice agents are doing; they are not a second way into banking
data, and they are kept apart from `/api/call/*` so the two surfaces can never
be confused for one another.

Three rules:

* **Local only.** Every route requires the request to come from the loopback
  address. That is the right control for a classroom demo on one machine, and
  it is deliberately implemented as one dependency so real operator
  authentication can replace it without touching a handler.
* **No banking values in the list.** The table shows how a call went, not what
  the customer's balance is. Figures appear only inside a single call's
  transcript, where an operator has explicitly gone to look.
* **Nothing sensitive, ever.** No PIN, no credential, no provider error text,
  no model reasoning, no full account numbers.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.config import settings
from app.database.connection import session_scope
from app.database.models import AgentSession, AgentToolEvent, ConversationMessage
from app.observability.estimates import CARBON_NOTE, COST_NOTE
from app.realtime.browser_calls import browser_call_manager
from app.sessions import session_manager
from app.telephony.channels import normalise_channel

logger = logging.getLogger("app.admin")

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


def operator_only(request: Request) -> None:
    """Allow the dashboard from this machine only.

    The demo runs on one laptop, so loopback is the whole boundary. It is a
    dependency rather than an `if` inside each handler precisely so that
    swapping it for real authentication later is a one-line change.
    """
    client = request.client.host if request.client else None
    if client not in LOOPBACK:
        logger.warning("admin request refused from a non-local address")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Operator access only."
        )


router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(operator_only)]
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _live_duration(record: AgentSession) -> int | None:
    """Seconds so far for a live call, or the frozen total for a finished one."""
    if record.duration_seconds is not None:
        return record.duration_seconds
    started = _aware(record.started_at)
    if started is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _row(record: AgentSession) -> dict:
    """One dashboard row. Carries no balances and no banking figures."""
    started = _aware(record.started_at)
    ended = _aware(record.ended_at)
    return {
        "agent_session_id": record.agent_session_id,
        # How the audio arrived. Operational only — never an identity signal.
        "channel": normalise_channel(record.channel).value,
        "status": record.status,
        "client_id": record.customer_id,
        "auth_status": record.auth_status,
        "authenticated": bool(record.authenticated),
        "started_at": started.isoformat() if started else None,
        "ended_at": ended.isoformat() if ended else None,
        "duration_seconds": _live_duration(record),
        "active": record.ended_at is None and record.status not in {"REJECTED"},
        "current_domain": record.current_domain,
        "last_intent": record.last_intent,
        "capability": record.capability,
        "tool_calls": record.tool_call_count or 0,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "total_tokens": record.total_tokens,
        "estimated_cost_usd": (
            float(record.estimated_cost_usd)
            if record.estimated_cost_usd is not None
            else None
        ),
        "estimated_carbon_grams": (
            float(record.estimated_carbon_grams)
            if record.estimated_carbon_grams is not None
            else None
        ),
        "disconnect_reason": record.disconnect_reason,
        "error_category": record.error_category,
    }


@router.get("/agents")
def list_agents(
    search: str | None = Query(default=None),
    agent_status: str | None = Query(default=None, alias="status"),
    domain: str | None = Query(default=None),
    period: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict:
    """Agent sessions, live ones first, then most recent.

    Sorting is done here rather than in the page so that pagination means the
    same thing as the ordering: an operator on page one is looking at every
    live call, not at whichever live calls happened to sort into the first
    twenty-five rows.
    """
    with session_scope() as db:
        query = select(AgentSession)

        if search:
            needle = f"%{search.strip().upper()}%"
            query = query.where(
                func.upper(AgentSession.agent_session_id).like(needle)
                | func.upper(AgentSession.customer_id).like(needle)
            )
        if agent_status and agent_status.upper() != "ALL":
            wanted = agent_status.upper()
            if wanted == "ACTIVE":
                query = query.where(AgentSession.ended_at.is_(None))
            elif wanted == "COMPLETED":
                query = query.where(AgentSession.status == "COMPLETED")
            elif wanted == "ERROR":
                query = query.where(AgentSession.status == "ERROR")
            elif wanted == "REJECTED":
                query = query.where(AgentSession.status == "REJECTED")
        if domain and domain.upper() != "ALL":
            query = query.where(
                func.upper(AgentSession.current_domain) == domain.upper()
            )
        if period.lower() == "today":
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            query = query.where(AgentSession.started_at >= since)

        total = db.scalar(
            select(func.count()).select_from(query.subquery())
        ) or 0

        # Live calls first, then newest. `ended_at IS NULL` sorts as the
        # operator expects: what is happening now, before what already did.
        query = query.order_by(
            AgentSession.ended_at.is_(None).desc(), AgentSession.id.desc()
        )
        query = query.offset((page - 1) * page_size).limit(page_size)

        rows = [_row(record) for record in db.scalars(query)]

    return {
        "sessions": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/dashboard/summary")
def dashboard_summary() -> dict:
    """The cards above the table, plus configured capacity."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    with session_scope() as db:
        active = db.scalar(
            select(func.count())
            .select_from(AgentSession)
            .where(AgentSession.ended_at.is_(None), AgentSession.status != "REJECTED")
        ) or 0
        completed = db.scalar(
            select(func.count())
            .select_from(AgentSession)
            .where(AgentSession.status == "COMPLETED", AgentSession.started_at >= since)
        ) or 0
        rejected = db.scalar(
            select(func.count())
            .select_from(AgentSession)
            .where(AgentSession.status == "REJECTED", AgentSession.started_at >= since)
        ) or 0
        errors = db.scalar(
            select(func.count())
            .select_from(AgentSession)
            .where(AgentSession.status == "ERROR", AgentSession.started_at >= since)
        ) or 0
        average = db.scalar(
            select(func.avg(AgentSession.duration_seconds)).where(
                AgentSession.duration_seconds.is_not(None),
                AgentSession.started_at >= since,
            )
        )
        tokens = db.scalar(
            select(func.sum(AgentSession.total_tokens)).where(
                AgentSession.started_at >= since
            )
        )
        cost = db.scalar(
            select(func.sum(AgentSession.estimated_cost_usd)).where(
                AgentSession.started_at >= since
            )
        )
        carbon = db.scalar(
            select(func.sum(AgentSession.estimated_carbon_grams)).where(
                AgentSession.started_at >= since
            )
        )

    limit = browser_call_manager.max_active
    in_use = browser_call_manager.used_capacity()
    if not limit:
        capacity_state = "NORMAL"
        available = None
    elif in_use >= limit:
        capacity_state = "FULL"
        available = 0
    elif in_use >= max(1, limit - 1):
        capacity_state = "NEAR CAPACITY"
        available = limit - in_use
    else:
        capacity_state = "NORMAL"
        available = limit - in_use

    return {
        "active_agents": active,
        "completed_calls": completed,
        "rejected_calls": rejected,
        "errors": errors,
        "average_duration_seconds": int(average) if average is not None else None,
        "total_tokens": int(tokens) if tokens is not None else None,
        "estimated_cost_usd": float(cost) if cost is not None else None,
        "estimated_carbon_grams": float(carbon) if carbon is not None else None,
        "capacity": {
            "active": in_use,
            "available": available,
            "configured": limit or None,
            "state": capacity_state,
            "banking_sessions": session_manager.active_session_count(),
        },
        "estimates": {
            "cost_configured": settings.price_input_per_mtok is not None
            and settings.price_output_per_mtok is not None,
            "carbon_enabled": settings.carbon_estimation_enabled
            and settings.carbon_grams_per_ktok is not None,
            "cost_note": COST_NOTE,
            "carbon_note": CARBON_NOTE,
        },
        "timezone": settings.dashboard_timezone,
        # Which voice channels the bank is answering on. Telephony is off by
        # default; the dashboard shows it so an operator can see that at a
        # glance rather than inferring it from an absence of calls.
        "channels": {
            "webrtc_enabled": True,
            "phone_enabled": settings.telephony_configured,
            "telephony_provider": settings.telephony_provider
            if settings.telephony_enabled
            else None,
        },
        "demo_mode": settings.demo_mode,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/agents/{agent_session_id}")
def agent_detail(agent_session_id: str) -> dict:
    """Everything safe about one call, for the Details view."""
    with session_scope() as db:
        record = db.scalars(
            select(AgentSession).where(
                AgentSession.agent_session_id == agent_session_id
            )
        ).first()
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such agent session."
            )

        tools = db.scalars(
            select(AgentToolEvent)
            .where(AgentToolEvent.agent_session_id == agent_session_id)
            .order_by(AgentToolEvent.id)
        ).all()

        detail = _row(record)
        detail["tool_events"] = [
            {
                "tool_name": event.tool_name,
                "status": event.status,
                "duration_ms": event.duration_ms,
                "at": _aware(event.created_at).isoformat()
                if event.created_at
                else None,
            }
            for event in tools
        ]
        # Correlation for an operator reading logs, with the body masked: the
        # full id is not needed on screen and is not theirs to hand around.
        banking = record.banking_session_id
        detail["banking_session_masked"] = (
            f"{banking[:8]}…{banking[-4:]}" if banking and len(banking) > 12 else None
        )

    return detail


@router.get("/agents/{agent_session_id}/history")
def agent_history(agent_session_id: str) -> dict:
    """One call's transcript, and only that call's.

    The query is keyed on the agent session id, never on the customer. Two
    calls by the same synthetic customer are two separate conversations, and
    combining them would be a disclosure of one call inside another.
    """
    with session_scope() as db:
        record = db.scalars(
            select(AgentSession).where(
                AgentSession.agent_session_id == agent_session_id
            )
        ).first()
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such agent session."
            )

        messages = db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.agent_session_id == agent_session_id)
            .order_by(ConversationMessage.id)
        ).all()

        return {
            "session": _row(record),
            "messages": [
                {
                    "role": message.role,
                    "content": message.safe_content,
                    "message_type": message.message_type,
                    "at": _aware(message.created_at).isoformat()
                    if message.created_at
                    else None,
                }
                for message in messages
            ],
        }


@router.get("/events")
def recent_events(limit: int = Query(default=15, ge=1, le=50)) -> dict:
    """A short operational ticker. Counts and categories only."""
    with session_scope() as db:
        records = db.scalars(
            select(AgentSession).order_by(AgentSession.updated_at.desc()).limit(limit)
        ).all()

        events = []
        for record in records:
            when = _aware(record.updated_at) or _aware(record.started_at)
            if record.status == "REJECTED":
                text = "Call refused - voice capacity reached"
            elif record.ended_at is not None:
                text = f"{record.agent_session_id} ended ({record.disconnect_reason})"
            elif record.authenticated:
                text = f"{record.agent_session_id} verified as {record.customer_id}"
            else:
                text = f"{record.agent_session_id} started"
            events.append({"at": when.isoformat() if when else None, "text": text})

    return {"events": events}
