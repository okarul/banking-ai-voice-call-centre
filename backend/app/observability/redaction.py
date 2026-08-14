"""What may be written into a stored transcript, and what may not.

The authentication turns are the dangerous ones. A caller saying their PIN out
loud produces a transcript containing their PIN, and a dashboard that stores
raw speech would put four digits of credential into an operational table that
an instructor can read on a projector.

So the transcript is not stored as spoken. Authentication turns become
placeholders:

    "four eight two one"     -> "[PIN REDACTED]"
    "my ID is DEMO001"       -> "[Customer ID provided]"

and every stored line additionally passes through `app.redaction`, which strips
API keys, bearer tokens and database passwords wherever they appear.

The test for whether something belongs in a transcript is not "is it useful to
the operator" but "would it matter if this were read aloud in a classroom".
"""

import re

from app.redaction import redact

PIN_PLACEHOLDER = "[PIN REDACTED]"
CUSTOMER_ID_PLACEHOLDER = "[Customer ID provided]"
UNAVAILABLE = "[not recorded]"

# Anything that could be a four-digit demo PIN, spoken or typed. Deliberately
# broad: a false positive costs one redacted line, a false negative stores a
# credential.
_DIGIT_PIN = re.compile(r"\b\d[\d\s\-]{2,}\d\b")

_SPOKEN_DIGITS = (
    "zero", "oh", "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine",
)

# A customer id in any of the shapes a caller might say it.
_CUSTOMER_ID = re.compile(r"\bdemo[\s\-]?\d{3}\b", re.IGNORECASE)

_PIN_WORDS = ("pin", "passcode", "pass code", "password")
_ID_WORDS = ("customer id", "customer number", "identification", "my id", "demo")


def _spoken_digit_run(text: str) -> bool:
    """Three or more number words in a row, as a spoken PIN sounds."""
    words = re.findall(r"[a-z]+", text.lower())
    run = 0
    for word in words:
        if word in _SPOKEN_DIGITS:
            run += 1
            if run >= 3:
                return True
        else:
            run = 0
    return False


def looks_like_pin(text: str) -> bool:
    """Whether this line could carry a PIN, spoken or in figures."""
    if not text:
        return False
    lowered = text.lower()
    if _DIGIT_PIN.search(text):
        return True
    if _spoken_digit_run(lowered):
        return True
    # "my pin is ..." with anything after it.
    return any(word in lowered for word in _PIN_WORDS) and any(
        character.isdigit() for character in text
    )


def redact_transcript(text: str | None, *, role: str = "CUSTOMER") -> str:
    """The version of this line that is safe to store and to display.

    Customer speech is treated as guilty until proven innocent: a turn that
    could be a PIN is replaced wholesale rather than edited, because a partial
    redaction of "four eight two one" still leaves the shape of the credential.
    """
    if not text or not text.strip():
        return UNAVAILABLE

    cleaned = redact(text.strip())

    if role == "CUSTOMER":
        if looks_like_pin(cleaned):
            return PIN_PLACEHOLDER
        if _CUSTOMER_ID.search(cleaned):
            return CUSTOMER_ID_PLACEHOLDER

    # The assistant is instructed never to repeat a PIN, but the transcript is
    # not the place to find out that it did.
    if looks_like_pin(cleaned) and role != "AGENT":
        return PIN_PLACEHOLDER
    if role == "AGENT":
        cleaned = _DIGIT_PIN.sub(
            lambda match: match.group(0) if _is_money(match.group(0)) else "[redacted]",
            cleaned,
        )

    return cleaned


def _is_money(fragment: str) -> bool:
    """Whether a run of digits is a banking figure rather than a credential.

    Money has a decimal point or a thousands separator; a PIN does not. The
    assistant's answers are full of the former and must stay readable, or the
    transcript is useless to the operator who has to check what was said.
    """
    return "." in fragment or "," in fragment
