"""Normalise text that will later arrive from speech recognition.

These functions do no speech-to-text of their own: they take the *text* a
recogniser produced and turn it into a canonical customer id or PIN.

The rules are deliberately strict and deterministic. Anything that does not
clearly match the expected shape returns None rather than being guessed at, so
no arbitrary phrase can ever become a customer id or a PIN.
"""

import re

# Spoken forms of a single digit. "oh" and "o" are common for zero.
DIGIT_WORDS: dict[str, str] = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "nought": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}

CUSTOMER_ID_PATTERN = re.compile(r"^demo(\d{3})$")
PIN_LENGTH = 4


def _tokenise(raw_value: str) -> list[str]:
    """Lowercase and split on anything that is not a letter or digit.

    This drops commas, hyphens and stray punctuation, so "four, eight, two,
    one" and "four eight two one" tokenise identically.
    """
    return re.split(r"[^a-z0-9]+", raw_value.lower().strip())


def normalize_customer_id(raw_value: str | None) -> str | None:
    """Return a canonical customer id such as "DEMO001", or None.

    Handles "DEMO001", "demo 001", "demo zero zero one", "demo 0 0 1" and the
    letter-by-letter "D E M O zero zero one". Whether that customer actually
    exists is a database question, answered later by verify_customer().
    """
    if not raw_value or not isinstance(raw_value, str):
        return None

    pieces: list[str] = []
    for token in _tokenise(raw_value):
        if not token:
            continue
        if token.isdigit():
            pieces.append(token)
        elif len(token) > 1 and token in DIGIT_WORDS:
            # Multi-letter digit words only. A single "o" here is the letter in
            # a spelled-out "D E M O", not the digit zero.
            pieces.append(DIGIT_WORDS[token])
        else:
            pieces.append(token)

    # Letter-spelled input ("d e m o") joins back into "demo"; digits appended
    # in order rebuild the numeric part.
    candidate = "".join(pieces)
    match = CUSTOMER_ID_PATTERN.match(candidate)
    if match is None:
        return None

    return f"DEMO{match.group(1)}"


def normalize_pin(raw_value: str | None) -> str | None:
    """Return a canonical four-digit PIN such as "4821", or None.

    Handles "4821", "4 8 2 1", "four eight two one" and comma-separated
    variants. Any unrecognised word makes the whole value invalid, and the
    result must be exactly four digits.
    """
    if not raw_value or not isinstance(raw_value, str):
        return None

    digits: list[str] = []
    for token in _tokenise(raw_value):
        if not token:
            continue
        if token.isdigit():
            digits.extend(token)
        elif token in DIGIT_WORDS:
            digits.append(DIGIT_WORDS[token])
        else:
            # An unknown word means this is not a PIN. Never guess.
            return None

        if len(digits) > PIN_LENGTH:
            return None

    if len(digits) != PIN_LENGTH:
        return None

    return "".join(digits)
