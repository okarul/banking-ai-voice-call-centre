"""Deterministic intent classification.

No language model is involved and none ever should be at this layer. Every
routing decision is made by explicit Python rules against the caller's words,
so the same utterance always produces the same route and a test can pin it.

One utterance is reduced to three things:

    what the caller wants to do   -> an action (balance, details, ...)
    which part of the bank        -> a domain (ACCOUNT or LOAN)
    which account or loan         -> a slot   (Savings, Home Loan, ...)

A missing domain is never invented. It falls back to the domain the session was
already discussing, and when there is none the caller is asked which they mean.
The same rule the tool layer already follows applies here: never silently
substitute something the caller did not ask for.
"""

import re
from dataclasses import dataclass
from enum import Enum


class Domain(str, Enum):
    """The part of the bank a turn belongs to.

    Values match the strings the tool layer already writes to
    `Session.current_domain`, so a domain read back from a session compares
    directly against these members.
    """

    ACCOUNT = "ACCOUNT"
    LOAN = "LOAN"
    UNKNOWN = "UNKNOWN"


class Intent(str, Enum):
    """What the caller asked for on this turn."""

    ACCOUNT_BALANCE = "ACCOUNT_BALANCE"
    ACCOUNT_DETAILS = "ACCOUNT_DETAILS"
    RECENT_TRANSACTIONS = "RECENT_TRANSACTIONS"
    LOAN_BALANCE = "LOAN_BALANCE"
    LOAN_DETAILS = "LOAN_DETAILS"
    NEXT_INSTALMENT = "NEXT_INSTALMENT"
    END_CALL = "END_CALL"
    UNKNOWN = "UNKNOWN"


class Action(str, Enum):
    """The verb of the request, independent of which domain it applies to."""

    BALANCE = "BALANCE"
    DETAILS = "DETAILS"
    TRANSACTIONS = "TRANSACTIONS"
    INSTALMENT = "INSTALMENT"


DOMAIN_BY_INTENT = {
    Intent.ACCOUNT_BALANCE: Domain.ACCOUNT,
    Intent.ACCOUNT_DETAILS: Domain.ACCOUNT,
    Intent.RECENT_TRANSACTIONS: Domain.ACCOUNT,
    Intent.LOAN_BALANCE: Domain.LOAN,
    Intent.LOAN_DETAILS: Domain.LOAN,
    Intent.NEXT_INSTALMENT: Domain.LOAN,
    Intent.END_CALL: Domain.UNKNOWN,
    Intent.UNKNOWN: Domain.UNKNOWN,
}

# Which tool answers which enquiry. One map, read by every path that has to name
# an enquiry: the Phase 8 supervisor, and the Phase 11 scope gate when it holds
# an enquiry made before the caller was verified. Two copies of this map is how
# the two channels came to resume a held enquiry differently.
TOOL_BY_INTENT = {
    Intent.ACCOUNT_BALANCE: "get_account_balance",
    Intent.ACCOUNT_DETAILS: "get_account_details",
    Intent.RECENT_TRANSACTIONS: "get_recent_transactions",
    Intent.LOAN_BALANCE: "get_loan_balance",
    Intent.LOAN_DETAILS: "get_loan_details",
    Intent.NEXT_INSTALMENT: "get_next_instalment",
}

# Only these combinations are answerable. A pair that is absent — asking for
# transactions on a loan, or an instalment on a savings account — resolves to
# UNKNOWN rather than being bent into the nearest available answer.
INTENT_BY_DOMAIN_ACTION = {
    (Domain.ACCOUNT, Action.BALANCE): Intent.ACCOUNT_BALANCE,
    (Domain.ACCOUNT, Action.DETAILS): Intent.ACCOUNT_DETAILS,
    (Domain.ACCOUNT, Action.TRANSACTIONS): Intent.RECENT_TRANSACTIONS,
    (Domain.LOAN, Action.BALANCE): Intent.LOAN_BALANCE,
    (Domain.LOAN, Action.DETAILS): Intent.LOAN_DETAILS,
    (Domain.LOAN, Action.INSTALMENT): Intent.NEXT_INSTALMENT,
}

# Actions that only exist in one domain. Used when the caller named an action
# but no domain: "what have I spent" can only be an account question.
DOMAIN_BY_UNAMBIGUOUS_ACTION = {
    Action.TRANSACTIONS: Domain.ACCOUNT,
    Action.INSTALMENT: Domain.LOAN,
}

# --- vocabulary -------------------------------------------------------------

END_CALL_PHRASES = (
    "goodbye",
    "bye",
    "thats all",
    "that is all",
    "nothing else",
    "no more questions",
    "end the call",
    "hang up",
    "im done",
    "i am done",
)

# Courtesy, not instruction. A caller saying thank you is not asking to hang up
# and must not be treated as if they were: the line stays open and they are
# asked whether they need anything else. Note what is deliberately absent —
# "thanks, bye" contains "bye", so it ends the call as it should.
THANKS_PHRASES = (
    "thank you",
    "thanks",
    "thank you very much",
    "thanks a lot",
    "much appreciated",
    "cheers",
    "appreciate it",
)

GREETING_PHRASES = (
    "hello",
    "hi",
    "hey",
    "good morning",
    "good afternoon",
    "good evening",
    "how are you",
)

# Checked before the account words, because "loan" is the more specific noun:
# "my loan account" is a loan question, not an account question.
LOAN_WORDS = (
    "loan",
    "loans",
    "mortgage",
    "instalment",
    "installment",
    "instalments",
    "installments",
    "emi",
    "repayment",
    "repayments",
    "interest rate",
    "borrowed",
)

ACCOUNT_WORDS = (
    "account",
    "accounts",
    "savings",
    "saving",
    "chequing",
    "checking",
    "transaction",
    "transactions",
    "spending",
    "spent",
    "statement",
    "deposit",
)

# Ordered most specific first: "next payment" must win over bare "payment",
# and an instalment question must not be read as a plain balance question.
ACTION_PHRASES = (
    (
        Action.INSTALMENT,
        (
            "instalment",
            "installment",
            "instalments",
            "installments",
            "emi",
            "next payment",
            "next repayment",
            "payment due",
            "due date",
            "when is my next",
            "how much do i pay",
            # Elliptical follow-ups after discussing a loan. Nothing else in
            # Version 1 has a "next one" — an account has no next anything —
            # so these are unambiguous within this scope.
            "when is the next",
            "next one",
            "when is it due",
            # "next loan payment" is not a contiguous "next payment", so the
            # payment wording is matched on its own. Listed under INSTALMENT,
            # which is checked first, so "how much is my next loan payment"
            # asks about the instalment rather than the outstanding balance.
            "loan payment",
            "monthly payment",
            "monthly instalment",
            "monthly installment",
        ),
    ),
    (
        Action.TRANSACTIONS,
        (
            "transaction",
            "transactions",
            "activity",
            "last few",
            "statement",
            "spending",
            "spend",
            "spent",
            "history",
        ),
    ),
    (
        Action.DETAILS,
        (
            "details",
            "detail",
            "information",
            "info",
            "tell me about",
            "interest rate",
            "maturity",
            "mature",
            "matures",
            "status",
        ),
    ),
    (
        Action.BALANCE,
        (
            "balance",
            "how much",
            "how much do i have",
            "how much do i owe",
            "outstanding",
            "owe",
            "left in",
            # Everyday ways of asking the same thing.
            "sitting in",
            "got in",
            "have in",
        ),
    ),
)

# Strong slot phrases: unambiguous in any sentence. Bare words such as "current"
# or "home" are deliberately excluded here — see `parse_type_reply`.
ACCOUNT_TYPE_PHRASES = (
    ("Savings", ("savings", "saving", "savings account")),
    ("Current", ("current account", "chequing", "chequing account", "checking",
                 "checking account")),
)

LOAN_TYPE_PHRASES = (
    ("Home Loan", ("home loan", "housing loan", "mortgage")),
    ("Personal Loan", ("personal loan",)),
    ("Car Loan", ("car loan", "auto loan", "vehicle loan")),
)

# Bare replies accepted only when answering "which one?" — never mid-sentence,
# because "current balance" means the balance now, not the Current account.
ACCOUNT_TYPE_REPLIES = (
    ("Savings", ("savings", "saving")),
    ("Current", ("current", "chequing", "checking")),
)

LOAN_TYPE_REPLIES = (
    ("Home Loan", ("home", "housing", "mortgage")),
    ("Personal Loan", ("personal",)),
    ("Car Loan", ("car", "auto", "vehicle")),
)

_APOSTROPHES = str.maketrans("", "", "'’")


@dataclass(frozen=True)
class Classification:
    """The routing decision for one utterance.

    `needs_domain` is set when the caller named a recognisable action but no
    domain and the session has no domain to fall back on. The supervisor turns
    that into a clarifying question rather than picking a side.
    """

    intent: Intent
    domain: Domain
    account_type: str | None = None
    loan_type: str | None = None
    needs_domain: bool = False
    # The verb, kept even when the intent could not be resolved, so a turn that
    # only lacked a domain can be completed once the caller supplies one.
    action: Action | None = None

    def to_dict(self) -> dict:
        """Safe view for development endpoints. Carries no banking values."""
        return {
            "intent": self.intent.value,
            "domain": self.domain.value,
            "account_type": self.account_type,
            "loan_type": self.loan_type,
            "needs_domain": self.needs_domain,
            "action": self.action.value if self.action else None,
        }


def _normalize(text: str) -> str:
    """Lowercase, drop apostrophes and punctuation, and pad with spaces.

    Padding lets a phrase be matched with a plain `in` test while still
    respecting word boundaries, so "bye" never matches inside "byte".
    """
    lowered = (text or "").lower().translate(_APOSTROPHES)
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    return f" {cleaned} "


def _has(padded: str, phrase: str) -> bool:
    return f" {phrase} " in padded


def _has_any(padded: str, phrases) -> bool:
    return any(_has(padded, phrase) for phrase in phrases)


def _match_slot(padded: str, table) -> str | None:
    """First canonical value whose phrases appear in the utterance."""
    for value, phrases in table:
        if _has_any(padded, phrases):
            return value
    return None


def _detect_action(padded: str) -> Action | None:
    for action, phrases in ACTION_PHRASES:
        if _has_any(padded, phrases):
            return action
    return None


def _detect_domain(padded: str) -> Domain:
    if _has_any(padded, LOAN_WORDS):
        return Domain.LOAN
    if _has_any(padded, ACCOUNT_WORDS):
        return Domain.ACCOUNT
    return Domain.UNKNOWN


def _as_domain(value: str | None) -> Domain:
    """Convert a stored `current_domain` string into a Domain, safely."""
    try:
        return Domain(value)
    except ValueError:
        return Domain.UNKNOWN


THANKS = "THANKS"
GREETING = "GREETING"


def social_turn(text: str) -> str | None:
    """THANKS, GREETING, or None if the turn is asking for something.

    Only a turn that is *entirely* courtesy counts. "Thanks, what is my
    balance?" is a balance question with a polite opening, and answering it with
    "you're welcome" would be absurd — so anything carrying a banking word is
    left alone for the classifier to route.
    """
    padded = _normalize(text)
    if not padded.strip():
        return None

    if _has_any(padded, END_CALL_PHRASES):
        return None
    if _has_any(padded, LOAN_WORDS) or _has_any(padded, ACCOUNT_WORDS):
        return None
    if _detect_action(padded) is not None:
        return None

    if _has_any(padded, THANKS_PHRASES):
        return THANKS
    if _has_any(padded, GREETING_PHRASES):
        return GREETING
    return None


def classify(text: str, *, current_domain: str | None = None) -> Classification:
    """Classify one caller utterance into an intent, domain and slots.

    `current_domain` is the session's existing domain and is used only to
    resolve an otherwise ambiguous request such as "what's my balance". It
    never overrides a domain the caller stated explicitly.
    """
    padded = _normalize(text)

    if _has_any(padded, END_CALL_PHRASES):
        return Classification(intent=Intent.END_CALL, domain=Domain.UNKNOWN)

    account_type = _match_slot(padded, ACCOUNT_TYPE_PHRASES)
    loan_type = _match_slot(padded, LOAN_TYPE_PHRASES)

    spoken_domain = _detect_domain(padded)
    # A named loan or account type is itself a domain signal.
    if spoken_domain is Domain.UNKNOWN:
        if loan_type is not None:
            spoken_domain = Domain.LOAN
        elif account_type is not None:
            spoken_domain = Domain.ACCOUNT

    action = _detect_action(padded)
    if action is None:
        # No recognisable request. The domain is still reported, so a bare
        # "savings" can be picked up as an answer to an earlier question.
        return Classification(
            intent=Intent.UNKNOWN,
            domain=spoken_domain,
            account_type=account_type,
            loan_type=loan_type,
        )

    domain = spoken_domain
    if domain is Domain.UNKNOWN:
        domain = DOMAIN_BY_UNAMBIGUOUS_ACTION.get(action, Domain.UNKNOWN)
    if domain is Domain.UNKNOWN:
        domain = _as_domain(current_domain)
    if domain is Domain.UNKNOWN:
        # Understood the request but not what it is about: ask, do not guess.
        # The action is carried so the answer can complete this same request.
        return Classification(
            intent=Intent.UNKNOWN,
            domain=Domain.UNKNOWN,
            account_type=account_type,
            loan_type=loan_type,
            needs_domain=True,
            action=action,
        )

    intent = INTENT_BY_DOMAIN_ACTION.get((domain, action), Intent.UNKNOWN)
    return Classification(
        intent=intent,
        domain=DOMAIN_BY_INTENT[intent] if intent is not Intent.UNKNOWN else domain,
        account_type=account_type if domain is Domain.ACCOUNT else None,
        loan_type=loan_type if domain is Domain.LOAN else None,
        action=action,
    )


def parse_type_reply(text: str, domain: Domain) -> str | None:
    """Read a bare answer to "which account?" / "which loan?".

    Only used when a clarifying question is outstanding, which is why bare
    words like "current" and "home" are accepted here and nowhere else. An
    utterance that also contains an action word is not a bare reply and is
    rejected, so "my current balance" never resolves to the Current account.
    """
    padded = _normalize(text)
    if _detect_action(padded) is not None:
        return None

    if domain is Domain.ACCOUNT:
        return _match_slot(padded, ACCOUNT_TYPE_REPLIES)
    if domain is Domain.LOAN:
        return _match_slot(padded, LOAN_TYPE_REPLIES)
    return None
