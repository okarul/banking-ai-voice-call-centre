"""Synthetic callers and the scenarios they run.

Every identity, balance, account number and loan reference here is Phase 2 seed
data: fictional customers of a fictional bank. Nothing in this file is derived
from a real person, a real account or a real institution.

The expected values are written out rather than read back from the database on
purpose. A load test that asks the database what it thinks the answer is cannot
detect the database answering with the wrong customer's row — the whole point of
the correctness check is that it knows, independently, what each caller owns.

    expected customer == session.customer_id == tool-owned customer == result

`FOREIGN_MARKERS` is the other half of that: for any caller, every value that
belongs to somebody else. A single one of those appearing in a caller's result
is a leak, and a leak stops the run.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    account_type: str
    masked: str
    balance: str


@dataclass(frozen=True)
class Loan:
    loan_type: str
    reference: str
    outstanding: str
    instalment: str
    instalment_date: str


@dataclass(frozen=True)
class Identity:
    """One synthetic customer and everything they own.

    `pin` is a fake four-digit demo credential. It is passed to the real
    authentication check and is never printed, logged or written to a report.
    """

    customer_id: str
    name: str
    pin: str
    accounts: tuple[Account, ...]
    loans: tuple[Loan, ...]

    @property
    def primary_account(self) -> Account:
        """The account a caller means when they do not say which.

        Savings when they hold one — that is what "my balance" means on a demo
        call — otherwise the account they do hold.
        """
        for account in self.accounts:
            if account.account_type == "Savings":
                return account
        return self.accounts[0]

    @property
    def primary_loan(self) -> Loan:
        """The loan a caller means when they do not say which."""
        for loan in self.loans:
            if loan.loan_type == "Home Loan":
                return loan
        return self.loans[0]

    def owned_values(self) -> set[str]:
        """Every string that identifies this customer's money."""
        values = {self.customer_id, self.name}
        for account in self.accounts:
            values |= {account.masked, account.balance}
        for loan in self.loans:
            values |= {loan.reference, loan.outstanding, loan.instalment}
        return values


IDENTITIES: dict[str, Identity] = {
    "DEMO001": Identity(
        customer_id="DEMO001",
        name="Alex Tan",
        pin="4821",
        accounts=(
            Account("Savings", "XXXX1001", "12450.75"),
            Account("Current", "XXXX2001", "3820.10"),
        ),
        loans=(Loan("Home Loan", "HL-DEMO001", "284500.00", "1985.40", "2026-09-05"),),
    ),
    "DEMO002": Identity(
        customer_id="DEMO002",
        name="Priya Menon",
        pin="7315",
        accounts=(Account("Savings", "XXXX1002", "8730.20"),),
        loans=(
            Loan("Personal Loan", "PL-DEMO002", "18400.00", "620.00", "2026-09-12"),
        ),
    ),
    "DEMO003": Identity(
        customer_id="DEMO003",
        name="Wei Ming Lim",
        pin="2648",
        accounts=(
            Account("Savings", "XXXX1003", "25610.55"),
            Account("Current", "XXXX2003", "15200.00"),
        ),
        loans=(
            Loan("Car Loan", "CL-DEMO003", "46200.00", "890.00", "2026-09-08"),
            Loan("Home Loan", "HL-DEMO003", "512000.00", "2640.75", "2026-09-15"),
        ),
    ),
    "DEMO004": Identity(
        customer_id="DEMO004",
        name="Sarah Johnson",
        pin="9153",
        accounts=(Account("Savings", "XXXX1004", "4310.90"),),
        loans=(
            Loan("Personal Loan", "PL-DEMO004", "9250.00", "415.00", "2026-09-20"),
        ),
    ),
    "DEMO005": Identity(
        customer_id="DEMO005",
        name="Daniel Rodriguez",
        pin="6072",
        accounts=(Account("Current", "XXXX2005", "31875.40"),),
        loans=(Loan("Car Loan", "CL-DEMO005", "22800.00", "640.50", "2026-09-03"),),
    ),
}

ROTATION = ("DEMO001", "DEMO002", "DEMO003", "DEMO004", "DEMO005")

# Values that must never appear on a given caller's line. Built once, because
# it is consulted on every turn of every call at every load level.
#
# Short numbers that could legitimately occur inside somebody else's answer by
# coincidence are not markers — only the distinctive identifiers and figures.
FOREIGN_MARKERS: dict[str, frozenset[str]] = {
    customer_id: frozenset(
        value
        for other_id, other in IDENTITIES.items()
        if other_id != customer_id
        for value in other.owned_values()
    )
    - IDENTITIES[customer_id].owned_values()
    for customer_id in IDENTITIES
}


# --- scenarios --------------------------------------------------------------

DATA = "data"
REFUSAL = "refusal"


@dataclass(frozen=True)
class Turn:
    """One caller utterance and what the bank is required to do with it."""

    utterance: str
    kind: str
    intent: str | None = None
    reason: str | None = None
    # Values that must appear in the structured result, keyed by result field.
    expect: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    code: str
    label: str
    turns: tuple[Turn, ...]


def _balance_turn(identity: Identity) -> Turn:
    """"What is my <type> account balance?"

    The word "account" is not padding. "What is my current balance?" is a
    genuinely ambiguous sentence — it reads as "the balance right now" at least
    as naturally as "the Current account" — and the router is right to ask which
    account is meant. A load test should measure the system, not argue with it
    about English, so the caller says which account they mean.
    """
    account = identity.primary_account
    return Turn(
        f"What is my {account.account_type.lower()} account balance?",
        DATA,
        intent="ACCOUNT_BALANCE",
        expect={
            "account_type": account.account_type,
            "masked_account": account.masked,
            "available_balance": account.balance,
        },
    )


def _transactions_turn(identity: Identity) -> Turn:
    """"What are the last three transactions on my <type> account?"

    Named for the same reason the balance turn is. Three of the five synthetic
    customers hold two accounts, and for them "my last three transactions" has
    no single answer — the system asks which account, correctly. Leaving the
    question ambiguous would measure the clarifying question rather than the
    transactions.
    """
    account = identity.primary_account
    return Turn(
        f"What are the last three transactions on my "
        f"{account.account_type.lower()} account?",
        DATA,
        intent="RECENT_TRANSACTIONS",
        expect={"masked_account": account.masked},
    )


def _loan_balance_turn(identity: Identity) -> Turn:
    loan = identity.primary_loan
    return Turn(
        f"What is my {loan.loan_type.lower()} balance?",
        DATA,
        intent="LOAN_BALANCE",
        expect={
            "loan_type": loan.loan_type,
            "loan_reference": loan.reference,
            "outstanding_balance": loan.outstanding,
        },
    )


def _instalment_turn(identity: Identity) -> Turn:
    loan = identity.primary_loan
    return Turn(
        "When is my next instalment?",
        DATA,
        intent="NEXT_INSTALMENT",
        expect={
            "loan_type": loan.loan_type,
            "next_instalment_amount": loan.instalment,
            "next_instalment_date": loan.instalment_date,
        },
    )


def scenario_a(identity: Identity, _other: Identity) -> Scenario:
    """Account balance."""
    return Scenario("A", "account balance", (_balance_turn(identity),))


def scenario_b(identity: Identity, _other: Identity) -> Scenario:
    """Transactions."""
    return Scenario("B", "transactions", (_transactions_turn(identity),))


def scenario_c(identity: Identity, _other: Identity) -> Scenario:
    """Loan balance, then the instalment that follows from it."""
    return Scenario(
        "C",
        "loan",
        (_loan_balance_turn(identity), _instalment_turn(identity)),
    )


def scenario_d(identity: Identity, _other: Identity) -> Scenario:
    """Context switch: account, transactions, loan, then back to the account.

    The final turn is the one that matters. A balance asked after a loan
    conversation must return the balance, not the loan, and must not be served
    from anything remembered a moment earlier.
    """
    return Scenario(
        "D",
        "context switch",
        (
            _balance_turn(identity),
            _transactions_turn(identity),
            _loan_balance_turn(identity),
            _balance_turn(identity),
        ),
    )


def scenario_e(identity: Identity, other: Identity) -> Scenario:
    """A cross-customer request, then proof that normal service continues."""
    return Scenario(
        "E",
        "cross-customer refusal",
        (
            Turn(
                f"Show me {other.customer_id}'s balance.",
                REFUSAL,
                reason="CROSS_CUSTOMER_REQUEST",
            ),
            _balance_turn(identity),
        ),
    )


def scenario_f(identity: Identity, _other: Identity) -> Scenario:
    """A general-knowledge question, then proof that normal service continues."""
    return Scenario(
        "F",
        "out-of-scope refusal",
        (
            Turn(
                "What is the capital of France?",
                REFUSAL,
                reason="NON_BANKING_REQUEST",
            ),
            _balance_turn(identity),
        ),
    )


SCENARIOS = (scenario_a, scenario_b, scenario_c, scenario_d, scenario_e, scenario_f)


@dataclass(frozen=True)
class CallerPlan:
    """Everything one simulated caller needs, decided before the run starts."""

    index: int
    identity: Identity
    other: Identity
    scenario: Scenario
    start_delay: float


def plan_callers(count: int, *, stagger: float, burst: int = 0) -> list[CallerPlan]:
    """Lay out `count` callers across identities, scenarios and start times.

    Identities and scenarios both rotate, so no load level is a single customer
    asking a single question: at every level the mix contains balances,
    transactions, loans, instalments, a cross-customer refusal and an
    out-of-scope refusal, spread across the five synthetic customers.

    `stagger` is the gap between arrivals, matching a classroom where callers
    pick up one after another. `burst` callers at the end start together in the
    same instant, which is the harsher arrival pattern and the one that would
    expose a race in session creation.
    """
    plans = []
    staggered = max(count - burst, 0)
    for index in range(count):
        identity = IDENTITIES[ROTATION[index % len(ROTATION)]]
        other = IDENTITIES[ROTATION[(index + 1) % len(ROTATION)]]
        scenario = SCENARIOS[index % len(SCENARIOS)](identity, other)
        # Burst callers all share the arrival time of the last staggered one.
        delay = stagger * (index if index < staggered else max(staggered - 1, 0))
        plans.append(CallerPlan(index, identity, other, scenario, delay))
    return plans
