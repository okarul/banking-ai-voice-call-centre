"""Turning structured tool results into sentences a caller can be told.

Kept apart from the agents so both domains render money, dates and refusals the
same way. Nothing here decides anything: it only phrases what the tool layer
already returned.

Money arrives as a string, having kept its Decimal type all the way from
PostgreSQL. It is parsed back into Decimal for formatting and never passes
through binary floating point.
"""

from datetime import date
import re
from decimal import Decimal, InvalidOperation

from app.authorization import Reason

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# How a call opens: a welcome, not a checkpoint. Nothing is said about
# verification until the caller asks for something that actually needs it.
WELCOME_SPEECH = (
    "Welcome to ABC Demo Bank. Thank you for calling. How may I assist you today?"
)
WELCOME_BACK_SPEECH = "Welcome back to ABC Demo Bank. How may I assist you today?"
GREETING_SPEECH = "Hello. How may I assist you with your banking today?"

# Asked only when a protected enquiry has actually been made.
AUTHENTICATION_REQUEST = (
    "Certainly. Before I access your banking information, "
    "may I have your demo customer ID, please?"
)
PIN_REQUEST = "Thank you. Please provide your four-digit demo banking PIN."
VERIFIED_SPEECH = "Thank you. Your identity has been verified."

# What the caller hears when a request cannot be answered. Wording stays
# generic for the same reason the authorization messages do: a refusal must not
# reveal anything about other customers or about why a record is missing.
SPEECH_BY_REASON = {
    Reason.SESSION_NOT_FOUND: (
        "I'm sorry, I don't have an active call for you. Please start again."
    ),
    Reason.SESSION_NOT_ACTIVE: "This call has already ended. Please start again.",
    # Phrased the way a contact-centre officer asks: the caller has just said
    # what they want, and this is the one thing needed before it can be given
    # to them. It does not announce a verification procedure.
    Reason.NOT_AUTHENTICATED: AUTHENTICATION_REQUEST,
    Reason.CUSTOMER_CONTEXT_MISSING: AUTHENTICATION_REQUEST,
    Reason.AUTHENTICATION_LOCKED: (
        "I'm sorry, verification is locked for this call. "
        "Please contact your branch for assistance."
    ),
    Reason.ACCOUNT_NOT_OWNED: "I'm sorry, I can't access that account on this call.",
    Reason.LOAN_NOT_OWNED: "I'm sorry, I can't access that loan on this call.",
    Reason.ACCOUNT_NOT_FOUND: "I couldn't find a matching account on your profile.",
    Reason.LOAN_NOT_FOUND: "I couldn't find a matching loan on your profile.",
    "INVALID_LIMIT": "I can only go back up to ten transactions. How many would you like?",
}

FALLBACK_SPEECH = (
    "I'm sorry, I didn't catch that. I can help with your account balance, "
    "account details, recent transactions, loan balance, loan details, or your "
    "next instalment."
)

DOMAIN_QUESTION = "Certainly. Is that about your account, or your loan?"

# The three lines that end or sustain a call, spoken exactly as written.
#
# Exactly matters more than it looks. The telephone channel hangs up when it
# hears the closing line, so a paraphrase is a call that never ends; and a
# customer being told the bank is going is the last thing they hear, which is
# not the moment for improvisation. The model is instructed to reproduce these
# verbatim, and a test asserts the instructions still carry them.
GOODBYE_SPEECH = "Thank you for calling ABC Demo Bank. Have a pleasant day. Goodbye."

# A caller saying thank you is being polite, not hanging up. The line stays
# open and they are asked whether they need anything else — which is what a
# contact-centre officer does, and what stops a courtesy ending the call.
YOU_ARE_WELCOME_SPEECH = (
    "You're most welcome. Is there anything else I can help you with today?"
)

# Said when a telephone caller has gone quiet, and then the call ends.
#
# Channel 1 asks "do you want to continue?" and waits a second time, because a
# browser caller may simply have looked away. Channel 2 does not: a telephone
# caller silent for ten seconds after the bank finished speaking has usually
# put the handset down, and a second wait holds a line — and a capacity slot —
# for somebody who is not there.
SILENCE_CLOSING_SPEECH = "I do not hear anything from you. Thank you."

# Channel 1 only. Kept because the browser page still asks its question.
SILENCE_CHECK_SPEECH = "I do not hear anything from you. Do you want to continue?"

# What is sent into a telephone session to make the agent speak its closing
# line to a silent caller. A cue, not the words: the wording lives in the
# agent's instructions, which is the one place that decides how this bank
# speaks. See `app.telephony.lifecycle`.
SILENCE_CLOSING_CUE = "[The caller has been silent. Close the call now.]"

# Words that mean the bank has said its closing line and the call may be taken
# down once the line has finished playing.
#
# Matched on "goodbye" as a whole word. The greeting says "Thank you for
# calling" and never says goodbye, so this cannot fire on the opening; and the
# agent is instructed to say goodbye only when it is genuinely closing, so a
# caller saying goodbye does not end the call until the bank has said its line.
_CLOSING_MARKER = re.compile(r"\bgoodbye\b", re.IGNORECASE)


def is_closing_line(text: str) -> bool:
    """Whether this assistant turn is the bank closing the call."""
    return bool(text) and bool(_CLOSING_MARKER.search(text))


# The tail of the canonical closing sentence. `is_closing_line` matches any use
# of "goodbye", which is right once the caller has asked to end the call and
# wrong before it: an assistant sentence that merely mentions the word would
# otherwise hang up on a caller who never asked to leave.
_CANONICAL_TAIL = "have a pleasant day goodbye"


def _spoken_words(text: str) -> str:
    """Punctuation and casing removed, so wording can be compared as speech."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def is_canonical_closing(text: str) -> bool:
    """Whether this is *the* controlled closing response, not a mention of it.

    Deliberately strict. This is the fallback that may end a call the caller
    never asked to end, so it recognises the bank's own closing sentence and
    nothing looser.
    """
    if not text:
        return False
    spoken = _spoken_words(text)
    return spoken == _spoken_words(GOODBYE_SPEECH) or spoken.endswith(_CANONICAL_TAIL)


def is_terminal_goodbye(text: str) -> bool:
    """Whether an assistant turn *ends* by saying goodbye.

    The safety net. If the bank has genuinely signed off, the telephone must
    not stay connected — whatever the caller's transcript did or did not do.

    Terminal, not merely present. "Goodbye" is a word this agent may
    legitimately say mid-sentence, and closing a call on any mention of it
    would hang up on a caller who never asked to leave:

        "You can say goodbye when you are finished."   -> not an ending
        "I haven't said goodbye yet."                  -> not an ending
        "Thank you. Goodbye."                          -> an ending

    Matching the last word rather than the whole sentence is what makes this
    independent of wording: the model may paraphrase everything before it.
    """
    if not text:
        return False
    spoken = _spoken_words(text)
    return spoken.endswith("goodbye") or spoken.endswith("good bye")

UNSUPPORTED_SPEECH = (
    "I'm sorry, I can't help with that one. I can give you balances, details, "
    "recent transactions, or your next instalment."
)


def money(amount: str, currency: str = "") -> str:
    """Format a Decimal-string amount for speech, e.g. '12,450.75 SGD'."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return str(amount)
    formatted = f"{value:,.2f}"
    return f"{formatted} {currency}".strip()


def spoken_date(iso_date: str) -> str:
    """Format an ISO date for speech, e.g. '5 September 2026'."""
    try:
        parsed = date.fromisoformat(iso_date)
    except (ValueError, TypeError):
        return str(iso_date)
    return f"{parsed.day} {MONTHS[parsed.month - 1]} {parsed.year}"


def last_four(masked_account: str) -> str:
    """The spoken tail of a masked account number, e.g. 'XXXX1001' -> '1001'."""
    return str(masked_account)[-4:]


def join_options(values) -> str:
    """Join choices naturally: 'Savings or Current', 'a, b or c'."""
    options = [str(value) for value in values]
    if not options:
        return ""
    if len(options) == 1:
        return options[0]
    return f"{', '.join(options[:-1])} or {options[-1]}"


def choose_account_question(available) -> str:
    return f"Which account would you like, {join_options(available)}?"


def choose_loan_question(available) -> str:
    return f"Which loan would you like, {join_options(available)}?"


def refusal(reason: str) -> str:
    """The sentence for a failed result, falling back to a safe generic line."""
    return SPEECH_BY_REASON.get(reason, "I'm sorry, I can't help with that right now.")
