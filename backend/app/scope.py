"""The banking scope gate: what this call centre is allowed to talk about.

A language model knows the capital of France. A bank's phone agent is not
permitted to tell you, and the difference between those two facts is this file.
Scope is decided here, in plain Python, before the model is allowed to answer —
because a model that *can* answer will eventually answer, however firmly the
prompt asks it not to.

Every caller turn is classified into exactly one category:

    AUTHENTICATION              identifying the caller
    OWN_ACCOUNT_ENQUIRY         their balance, their account details
    OWN_TRANSACTION_ENQUIRY     their recent transactions
    OWN_LOAN_ENQUIRY            their loan, instalment, rate, maturity
    SOCIAL                      hello, thank you, goodbye
    CROSS_CUSTOMER_REQUEST      anything about somebody else
    UNSUPPORTED_BANKING_REQUEST banking, but not offered here
    NON_BANKING_REQUEST         not banking at all
    SECURITY_OR_PROMPT_ATTACK   an attempt to change the rules

Only the first five may reach a banking tool. The rest get a fixed sentence and
never touch customer data.

Precedence is deliberate and is not "most specific wins":

    security > cross-customer > supported own banking > unsupported banking
             > social > non-banking

An utterance that asks for a balance *and* names another customer is a
cross-customer request, not a balance request. Privacy outranks helpfulness.
"""

import re
from dataclasses import dataclass
from enum import Enum

from app.agents.intents import Domain, Intent, classify, parse_type_reply


class ScopeCategory(str, Enum):
    """What kind of request this turn is."""

    AUTHENTICATION = "AUTHENTICATION"
    OWN_ACCOUNT_ENQUIRY = "OWN_ACCOUNT_ENQUIRY"
    OWN_TRANSACTION_ENQUIRY = "OWN_TRANSACTION_ENQUIRY"
    OWN_LOAN_ENQUIRY = "OWN_LOAN_ENQUIRY"
    SOCIAL = "SOCIAL"
    CROSS_CUSTOMER_REQUEST = "CROSS_CUSTOMER_REQUEST"
    UNSUPPORTED_BANKING_REQUEST = "UNSUPPORTED_BANKING_REQUEST"
    NON_BANKING_REQUEST = "NON_BANKING_REQUEST"
    SECURITY_OR_PROMPT_ATTACK = "SECURITY_OR_PROMPT_ATTACK"


# Categories that may go on to use a banking tool.
IN_SCOPE = frozenset(
    {
        ScopeCategory.AUTHENTICATION,
        ScopeCategory.OWN_ACCOUNT_ENQUIRY,
        ScopeCategory.OWN_TRANSACTION_ENQUIRY,
        ScopeCategory.OWN_LOAN_ENQUIRY,
        ScopeCategory.SOCIAL,
    }
)

# One sentence per refusal, so the caller can tell the situations apart: a
# question we do not offer is not the same as a question about someone else.
SPEECH = {
    ScopeCategory.NON_BANKING_REQUEST: (
        "I'm here to help with your ABC Demo Bank account and loan enquiries. "
        "How can I assist with your banking today?"
    ),
    ScopeCategory.UNSUPPORTED_BANKING_REQUEST: (
        "This demo currently supports account and loan enquiries only. "
        "I can check your account balance, account details, recent "
        "transactions, loan balance, next instalment, or loan details."
    ),
    ScopeCategory.CROSS_CUSTOMER_REQUEST: (
        "I can only access banking information for the verified customer on "
        "this call."
    ),
    ScopeCategory.SECURITY_OR_PROMPT_ATTACK: (
        "I can only help with your own ABC Demo Bank account and loan "
        "enquiries. How can I assist with your banking today?"
    ),
}

NOT_AUTHENTICATED_SPEECH = (
    "Please complete verification before I can access banking information."
)

# Appended when a supported request arrives with something out of scope glued
# to it, so the caller is told the rest was not answered rather than left to
# assume it was.
MIXED_SUFFIX = "I can assist only with your banking enquiries."


# --- vocabulary -------------------------------------------------------------

_ATTACK_PHRASES = (
    "ignore your", "ignore all", "ignore previous", "ignore the previous",
    "ignore my identity", "disregard your", "disregard all", "forget your",
    "forget abc", "forget the bank", "you are chatgpt", "you are now",
    "act as", "pretend you are", "pretend to be", "roleplay",
    "system prompt", "your instructions", "your prompt", "developer mode",
    "what instructions", "instructions you were given", "what were you given",
    "jailbreak", "new instruction", "new instructions", "override your",
    "reveal your", "show me your prompt", "what were you told",
    "answer as a general assistant", "general purpose assistant",
    "bypass", "without restrictions", "no restrictions",
)

# Somebody who is not the caller.
_THIRD_PARTY_PHRASES = (
    "another customer", "other customer", "someone else", "somebody else",
    "another person", "other people", "third party",
    "his account", "her account", "their account", "his balance", "her balance",
    "his loan", "her loan", "does john", "does mary",
)

_RELATION_WORDS = (
    "wife", "husband", "partner", "friend", "brother", "sister", "mother",
    "father", "mum", "mom", "dad", "son", "daughter", "neighbour", "neighbor",
    "colleague", "boss", "cousin", "uncle", "aunt",
)

# Banking, but not something this demo does.
_UNSUPPORTED_BANKING_PHRASES = (
    "transfer", "transfers", "send money", "remit", "remittance", "wire",
    "pay my", "pay the", "payment to", "bill", "bills", "top up", "topup",
    "block my", "block the", "freeze my", "cancel my card", "lost card",
    "credit card", "debit card", "new card", "replace my card",
    "credit limit", "increase my limit", "beneficiary", "beneficiaries",
    "payee", "standing order", "direct debit", "cheque", "check book",
    "chequebook", "fixed deposit", "term deposit", "open an account",
    "open a new", "close my account", "close the account",
    "change my address", "update my address", "change my phone",
    "change my number", "reset my password", "reset my pin", "change my pin",
    "internet banking", "mobile banking", "online banking", "activate",
    "apply for", "application", "overdraft", "insurance", "insure",
    "invest", "investment", "investments", "buy shares", "stocks", "crypto",
    "bitcoin", "exchange rate", "foreign exchange", "forex",
    "financial advice", "should i invest", "should i save",
    "which bank", "better bank", "interest rates today",
)

_SOCIAL_PHRASES = (
    "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
    "thank you", "thanks", "thank you very much", "cheers",
    # Written in their post-filler form: `_is_social` strips "a", "very" and
    # "much" first, so "thanks a lot" arrives here as "thanks lot" and "much
    # appreciated" as "appreciated".
    "thanks lot", "appreciated", "appreciate it",
    "bye", "goodbye", "good bye", "see you", "that is all", "thats all",
    "how are you", "how are you doing", "nice to meet you", "please",
    "no thank you", "no thanks", "nothing else", "im done", "i am done",
)

# Words that mean this turn is about banking at all. Used only to separate an
# unsupported *banking* request from a question that has nothing to do with us.
_BANKING_WORDS = (
    "account", "accounts", "balance", "bank", "banking", "money", "savings",
    "current", "transaction", "transactions", "statement", "loan", "loans",
    "mortgage", "instalment", "installment", "payment", "interest", "deposit",
    "withdraw", "withdrawal", "card", "credit", "debit", "funds", "overdraft",
    "branch", "atm", "cheque", "customer id", "pin",
)

_AUTHENTICATION_PHRASES = (
    "my customer id", "customer id is", "my id is", "my pin", "pin is",
    "my customer number", "demo zero", "verify me", "verification",
    "i am demo", "this is demo",
)

_NUMBER_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

# The trailing "s?" matters: normalising strips the apostrophe from
# "DEMO002's", leaving "demo002s", which would otherwise not match at all —
# and "DEMO002's balance" is exactly the request that must never slip through.
_CUSTOMER_ID = re.compile(r"\bdemo\s*0*(\d{1,3})s?\b")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, pad — matching the intent classifier."""
    lowered = (text or "").lower().replace("'", "").replace("’", "")
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    return f" {cleaned} "


def _digits_from_words(padded: str) -> str:
    """Turn "demo zero zero two" into "demo 002" so ids can be recognised."""
    parts = []
    for token in padded.split():
        parts.append(_NUMBER_WORDS.get(token, token))
    joined = " ".join(parts)
    # Collapse "demo 0 0 2" into "demo 002".
    return re.sub(
        r"\bdemo((?:\s+\d){2,4})\b",
        lambda m: "demo " + m.group(1).replace(" ", ""),
        joined,
    )


def _has_any(padded: str, phrases) -> bool:
    return any(f" {phrase} " in padded for phrase in phrases)


def customer_ids_mentioned(text: str) -> set[str]:
    """Every demo customer id named in the utterance, spoken or spelled."""
    padded = _digits_from_words(_normalize(text))
    return {
        f"DEMO{int(number):03d}" for number in _CUSTOMER_ID.findall(padded)
    }


def _mentions_third_party(padded: str) -> bool:
    """Whether the turn is about anyone other than the caller.

    Matches allow a trailing "s" because normalising strips apostrophes:
    "someone else's account" becomes "someone elses account", and a plain
    phrase test would miss the very phrasing people actually use.
    """
    for phrase in _THIRD_PARTY_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}s?\b", padded):
            return True
    # "my wife's balance", "my friend's account".
    for relation in _RELATION_WORDS:
        if re.search(rf"\b{relation}s?\b", padded):
            return True
    return False


@dataclass(frozen=True)
class ScopeDecision:
    """What this turn is, and what may be done about it."""

    category: ScopeCategory
    speech: str | None = None
    intent: Intent = Intent.UNKNOWN
    domain: Domain = Domain.UNKNOWN
    # True when a supported request arrived with out-of-scope content attached.
    mixed: bool = False
    # Which account or loan the caller named, if they named one. Carried so the
    # ruling describes the whole enquiry and not just its shape: a turn
    # classified before the caller was verified is the only deterministic record
    # that "savings" was said, and the lookup that finally answers it happens
    # several turns later. Drawn from the same deterministic classifier the
    # tools use, and from a fixed vocabulary — never free text.
    account_type: str | None = None
    loan_type: str | None = None

    @property
    def allowed(self) -> bool:
        """Whether this turn may reach a banking tool."""
        return self.category in IN_SCOPE

    def to_dict(self) -> dict:
        """Safe view. Never contains the caller's words."""
        return {
            "category": self.category.value,
            "allowed": self.allowed,
            "intent": self.intent.value,
            "domain": self.domain.value,
            "mixed": self.mixed,
            "account_type": self.account_type,
            "loan_type": self.loan_type,
            "speech": self.speech,
        }


SUPPORTED_CATEGORY_BY_DOMAIN = {
    Domain.ACCOUNT: ScopeCategory.OWN_ACCOUNT_ENQUIRY,
    Domain.LOAN: ScopeCategory.OWN_LOAN_ENQUIRY,
}


def classify_scope(
    text: str,
    *,
    authenticated: bool = True,
    customer_id: str | None = None,
    current_domain: str | None = None,
) -> ScopeDecision:
    """Decide what a caller turn is, before anything is allowed to answer it.

    `customer_id` is the verified caller, so that naming *themselves* is not
    treated as asking about somebody else.
    """
    padded = _normalize(text)

    # 1. Security first. An attempt to change the rules is never also a
    #    balance enquiry, however it is dressed up.
    if _has_any(padded, _ATTACK_PHRASES):
        return ScopeDecision(
            category=ScopeCategory.SECURITY_OR_PROMPT_ATTACK,
            speech=SPEECH[ScopeCategory.SECURITY_OR_PROMPT_ATTACK],
        )

    # 2. Anyone other than the caller. Checked before the request is
    #    understood, so "my balance and DEMO002's" is refused as a whole.
    named = customer_ids_mentioned(text)
    about_someone_else = _mentions_third_party(padded)

    if authenticated:
        # A verified caller naming a different id is asking about someone else,
        # whatever else the sentence contains.
        foreign = named - {customer_id} if customer_id else named
    else:
        # Before verification, saying an id is how a caller identifies
        # themselves — there is no "own" id yet to differ from.
        foreign = set()

    if foreign or about_someone_else:
        return ScopeDecision(
            category=ScopeCategory.CROSS_CUSTOMER_REQUEST,
            speech=SPEECH[ScopeCategory.CROSS_CUSTOMER_REQUEST],
        )

    # 3. Identifying themselves.
    if _has_any(padded, _AUTHENTICATION_PHRASES) or (not authenticated and named):
        return ScopeDecision(category=ScopeCategory.AUTHENTICATION)

    # 4. A supported enquiry about their own banking.
    classification = classify(text, current_domain=current_domain)
    supported = classification.intent in (
        Intent.ACCOUNT_BALANCE,
        Intent.ACCOUNT_DETAILS,
        Intent.RECENT_TRANSACTIONS,
        Intent.LOAN_BALANCE,
        Intent.LOAN_DETAILS,
        Intent.NEXT_INSTALMENT,
    )

    if supported:
        if not authenticated:
            return ScopeDecision(
                category=ScopeCategory.OWN_ACCOUNT_ENQUIRY
                if classification.domain is Domain.ACCOUNT
                else ScopeCategory.OWN_LOAN_ENQUIRY,
                speech=NOT_AUTHENTICATED_SPEECH,
                intent=classification.intent,
                domain=classification.domain,
                account_type=classification.account_type,
                loan_type=classification.loan_type,
            )

        if classification.intent is Intent.RECENT_TRANSACTIONS:
            category = ScopeCategory.OWN_TRANSACTION_ENQUIRY
        else:
            category = SUPPORTED_CATEGORY_BY_DOMAIN[classification.domain]

        # A supported request with something else attached: answer the banking
        # part, and say plainly that the rest was not answered.
        mixed = _has_any(padded, _UNSUPPORTED_BANKING_PHRASES) or _looks_general(
            padded
        )
        return ScopeDecision(
            category=category,
            intent=classification.intent,
            domain=classification.domain,
            mixed=mixed,
            account_type=classification.account_type,
            loan_type=classification.loan_type,
        )

    # 5. Banking, but not something offered here.
    if _has_any(padded, _UNSUPPORTED_BANKING_PHRASES):
        return ScopeDecision(
            category=ScopeCategory.UNSUPPORTED_BANKING_REQUEST,
            speech=SPEECH[ScopeCategory.UNSUPPORTED_BANKING_REQUEST],
        )

    # 6. A bare answer to a question the agent asked — "Savings", "Home loan",
    #    "account". It carries no verb of its own, so nothing above recognises
    #    it, but it is the second half of a banking request already in progress
    #    and must not be treated as a new, unrelated one.
    slot_reply = _slot_reply_domain(text, padded)
    if slot_reply is not None:
        return ScopeDecision(
            category=SUPPORTED_CATEGORY_BY_DOMAIN[slot_reply],
            domain=slot_reply,
            speech=None if authenticated else NOT_AUTHENTICATED_SPEECH,
        )

    # 7. Ordinary courtesy.
    if _is_social(padded):
        return ScopeDecision(category=ScopeCategory.SOCIAL)

    # 7. An ambiguous banking request — "what is my balance" with no domain —
    #    is still banking, and the agent should ask which one.
    if classification.needs_domain or _has_any(padded, _BANKING_WORDS):
        return ScopeDecision(
            category=ScopeCategory.UNSUPPORTED_BANKING_REQUEST
            if not classification.needs_domain
            else ScopeCategory.OWN_ACCOUNT_ENQUIRY,
            intent=classification.intent,
            domain=classification.domain,
            speech=None
            if classification.needs_domain
            else SPEECH[ScopeCategory.UNSUPPORTED_BANKING_REQUEST],
        )

    # 8. Everything else. The model may well know the answer. It is not the
    #    bank's to give.
    return ScopeDecision(
        category=ScopeCategory.NON_BANKING_REQUEST,
        speech=SPEECH[ScopeCategory.NON_BANKING_REQUEST],
    )


def _is_social(padded: str) -> bool:
    """A pleasantry and nothing else.

    Matching a greeting *inside* a sentence is not enough: "translate hello
    into French" contains a greeting but is a translation request. So the whole
    utterance must reduce to courtesy once filler words are removed.
    """
    tokens = [word for word in padded.split() if word not in _COURTESY_FILLER]
    if not tokens:
        return True
    remaining = " ".join(tokens)
    return any(remaining == phrase.replace("'", "") for phrase in _SOCIAL_PHRASES)


# Words that carry no request of their own, so they do not stop an utterance
# from being pure courtesy.
_COURTESY_FILLER = frozenset(
    {"a", "an", "the", "please", "very", "much", "just", "ok", "okay", "well",
     "and", "so", "then", "there", "sir", "madam", "mate"}
)


_BARE_DOMAIN_REPLIES = {
    "account": Domain.ACCOUNT,
    "accounts": Domain.ACCOUNT,
    "my account": Domain.ACCOUNT,
    "loan": Domain.LOAN,
    "loans": Domain.LOAN,
    "my loan": Domain.LOAN,
    "the loan": Domain.LOAN,
}


def _slot_reply_domain(text: str, padded: str) -> Domain | None:
    """Which domain a bare reply belongs to, or None if it is not one.

    Used only after every other reading has been ruled out, so an ordinary
    request containing the word "savings" has already been classified and only
    a genuinely bare answer reaches here.
    """
    stripped = padded.strip()
    if not stripped or len(stripped.split()) > 3:
        return None

    if stripped in _BARE_DOMAIN_REPLIES:
        return _BARE_DOMAIN_REPLIES[stripped]
    if parse_type_reply(text, Domain.ACCOUNT):
        return Domain.ACCOUNT
    if parse_type_reply(text, Domain.LOAN):
        return Domain.LOAN
    return None


def _looks_general(padded: str) -> bool:
    """Signals of a general-knowledge question riding along with a real one."""
    return _has_any(
        padded,
        (
            "capital of", "who is", "who was", "what is the weather", "weather",
            "joke", "president", "news", "translate", "recipe", "cook",
            "football", "world cup", "quantum", "python", "write me",
        ),
    )
