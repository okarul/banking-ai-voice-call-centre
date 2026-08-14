"""The account or loan this call is already discussing.

The tool layer records the chosen account or loan on the session each time one
is answered. Both front ends read it back from here, so a caller who has just
picked "Home Loan" is not asked which loan again on the next question — whether
they are typing to the developer text mode or speaking to the realtime agent.

Reading it is deliberately narrow: it only ever fills a slot the caller left
unsaid, and only within the domain the session is already in. A caller who
names a different account or loan always moves the conversation to that one.
"""

from app.agents.intents import Domain

# The conversation_context keys the tool layer writes when it answers.
CONTEXT_KEY_BY_DOMAIN = {
    Domain.ACCOUNT: "account_type",
    Domain.LOAN: "loan_type",
}


def remembered_type(session, domain: Domain) -> str | None:
    """The account or loan already selected on this session, if any.

    Returns None when the session is discussing a different domain, so an
    account chosen earlier can never silently answer a loan question.
    """
    if session is None or domain not in CONTEXT_KEY_BY_DOMAIN:
        return None
    if session.current_domain != domain.value:
        return None

    return session.conversation_context.get(CONTEXT_KEY_BY_DOMAIN[domain]) or None


def carry_type(session, domain: Domain, stated: str | None) -> str | None:
    """Prefer what the caller said; otherwise reuse what they chose earlier."""
    if stated is not None:
        return stated
    return remembered_type(session, domain)
