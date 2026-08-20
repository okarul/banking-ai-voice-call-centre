"""SQLAlchemy models for the synthetic demo bank.

Declarative models only: no banking business logic lives here.
Money is stored as NUMERIC (never floating point).
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Money: up to 13 digits before the decimal point, 2 after.
MONEY = Numeric(15, 2)
# Interest rate: e.g. 3.750 percent.
RATE = Numeric(5, 3)


class Base(DeclarativeBase):
    """Declarative base for all models."""


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    pin_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")

    accounts: Mapped[list["Account"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    loans: Mapped[list["Loan"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Customer {self.customer_id}>"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    account_type: Mapped[str] = mapped_column(String(20))
    account_number_masked: Mapped[str] = mapped_column(String(20))
    available_balance: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="SGD")
    status: Mapped[str] = mapped_column(String(20), default="active")

    customer: Mapped["Customer"] = relationship(back_populates="accounts")
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Account {self.account_number_masked} {self.account_type}>"


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    transaction_date: Mapped[datetime] = mapped_column(DateTime)
    description: Mapped[str] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    transaction_type: Mapped[str] = mapped_column(String(10))  # credit / debit

    account: Mapped["Account"] = relationship(back_populates="transactions")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Transaction {self.description} {self.amount}>"


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    loan_type: Mapped[str] = mapped_column(String(30))
    loan_reference: Mapped[str] = mapped_column(String(30), unique=True)
    outstanding_balance: Mapped[Decimal] = mapped_column(MONEY)
    interest_rate: Mapped[Decimal] = mapped_column(RATE)
    next_instalment_amount: Mapped[Decimal] = mapped_column(MONEY)
    next_instalment_date: Mapped[date] = mapped_column(Date)
    maturity_date: Mapped[date] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), default="SGD")
    status: Mapped[str] = mapped_column(String(20), default="active")

    customer: Mapped["Customer"] = relationship(back_populates="loans")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Loan {self.loan_reference} {self.loan_type}>"


# --- operational records -----------------------------------------------------
#
# Everything below is about *calls*, not about money. It exists so an operator
# can see what the voice agents are doing, and it is deliberately separate from
# the banking tables above: nothing here changes how a balance is read, and a
# failure to write any of it must never fail a customer's call.
#
# The one rule that governs every column: a customer's spoken PIN, the API key
# and the model's private reasoning are never written here, in any form.


class AgentSession(Base):
    """One voice call, as an operator sees it.

    `agent_session_id` (AGT-000001) is the operator-facing handle. The banking
    session id is kept alongside it for correlation, but the dashboard shows a
    short, stable name rather than a UUID.
    """

    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_session_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    banking_session_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )

    # Synthetic demo customer id only, and only once a PIN has actually been
    # checked. Before that it stays null: a caller who merely *claims* an
    # identity has not established one.
    customer_id: Mapped[str | None] = mapped_column(String(20), index=True, nullable=True)
    authenticated: Mapped[bool] = mapped_column(default=False)
    auth_status: Mapped[str] = mapped_column(String(20), default="PENDING")

    # How the audio reached the bank: WEBRTC today, PHONE once the telephone
    # channel exists. Operational reporting only — a channel never decides who
    # the customer is. See `app.telephony.channels`.
    channel: Mapped[str] = mapped_column(String(10), default="WEBRTC", index=True)
    # The provider's own call identifier, for correlating a telephone call with
    # a provider's records. Null for browser calls, and never a customer id.
    provider_call_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )
    # The identifier of the single provider notification that opened this call,
    # as distinct from the call itself: one `provider_call_id` may be announced
    # by several events if the provider retries. Kept so a retry can be
    # recognised as a repeat rather than answered twice — see §12 of
    # docs/TELEPHONY_SECURITY_PRIVACY_DESIGN.md. The field exists from Phase 1;
    # the dedupe check that reads it belongs to Phase 2, when there is an
    # endpoint to receive an event at all. Null for browser calls.
    provider_event_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    current_domain: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_intent: Mapped[str | None] = mapped_column(String(40), nullable=True)
    capability: Mapped[str | None] = mapped_column(String(40), nullable=True)

    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)

    # Null rather than zero when the provider gave no usage metadata: the
    # dashboard shows N/A, and an estimate is never invented.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    estimated_carbon_grams: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )

    disconnect_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(40), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["ConversationMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    tool_events: Mapped[list["AgentToolEvent"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AgentSession {self.agent_session_id} {self.status}>"


class ConversationMessage(Base):
    """One safe line of a call's transcript.

    `safe_content` is the operative word. Authentication turns are stored as
    placeholders — "[Customer ID provided]", "[PIN REDACTED]" — because the raw
    speech on those turns contains the caller's PIN. Nothing is written here
    that has not been through `app.observability.redact_transcript`.
    """

    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_pk: Mapped[int] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    agent_session_id: Mapped[str] = mapped_column(String(20), index=True)
    role: Mapped[str] = mapped_column(String(20))  # CUSTOMER / AGENT / SYSTEM_EVENT
    safe_content: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(30), default="SPEECH")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    session: Mapped["AgentSession"] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ConversationMessage {self.agent_session_id} {self.role}>"


class AgentToolEvent(Base):
    """A banking tool that ran on a call.

    Tool *arguments* are deliberately not stored. They carry account selections
    and, on the authentication tools, the spoken PIN — and an operations
    dashboard needs to know which tool ran and how long it took, not what was
    passed to it.
    """

    __tablename__ = "agent_tool_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_pk: Mapped[int] = mapped_column(
        ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True
    )
    agent_session_id: Mapped[str] = mapped_column(String(20), index=True)
    tool_name: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="OK")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    session: Mapped["AgentSession"] = relationship(back_populates="tool_events")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AgentToolEvent {self.agent_session_id} {self.tool_name}>"
