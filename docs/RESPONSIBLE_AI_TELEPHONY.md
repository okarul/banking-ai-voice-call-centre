# Responsible AI — telephone channel

**Status: Phase 1 (foundation).** The telephone channel is not implemented.
These are the commitments the implementation must meet.

A telephone changes the ethics of this demonstration in one specific way: a
caller on a phone has fewer cues that they are speaking to software. There is
no page, no "AI voice banking" heading, no visible disclaimer — just a voice
that answers. Everything below follows from that.

---

## 1. AI disclosure

A caller must know they are speaking to an AI, and must not have to work it out.

`AI_DISCLOSURE_ENABLED` defaults to `true`, with configurable text:

> "This is an AI-powered demonstration using synthetic banking data."

Phase 1 creates the configuration and **does not change the current greeting**.
When the telephone channel is built, disclosure belongs in the opening moments
of the call — before any banking question is answered, not buried at the end.

## 2. Synthetic data disclosure

Every customer, account, balance, loan and PIN is invented. The disclosure says
so, because "this is a demo bank" and "this is your real money" are very
different things for someone to be confused about. `DEMO_MODE` defaults to
`true` and there is no production banking connector.

## 3. Non-deception

The agent presents as what it is. It must not:

- claim to be a human, or a named employee;
- imply a human is listening, reviewing, or available;
- claim to be transferring the caller to a person (see §5);
- imply the synthetic figures are the caller's real finances.

Brief courtesy is fine. Pretending to be a person is not.

## 4. No biometric or inferential profiling

Voice is **not** an identity mechanism here, and it is not a source of
inferences about the speaker. The system must not add:

- speaker recognition or voiceprints;
- emotion, sentiment or stress detection;
- age, gender, ethnicity, nationality or accent inference;
- any "risk score" derived from how somebody sounds.

Authentication is demo customer ID + demo PIN. Nothing else.

This is not only a privacy position. Voice-derived inference is unreliable and
unevenly unreliable — it fails differently across accents, ages and speech
differences — so a banking decision resting on it would be both wrong and
unfairly wrong.

## 5. Human escalation — an honest limitation

**This demonstration contains no human-agent handoff. There is nobody to
transfer a caller to.**

The agent must never say it is transferring, escalating, or connecting anyone
to a person. If asked, the honest answer is:

> "This demonstration does not currently support transfer to a human agent."

Saying "let me put you through to a colleague" and then not doing so would be a
straightforward lie to a customer, and in a real bank it is the kind that leaves
a vulnerable caller stranded.

## 6. Fairness

No banking or authorization decision may depend on accent, voice
characteristics, inferred gender, age, nationality, ethnicity, or emotional
state. Architecturally this holds because those signals are never derived at
all — there is nothing for a decision to depend on.

**Recognition difficulty is not suspicion.** If the system mishears somebody
repeatedly, that is a failure of the system, not evidence about the caller. It
must not shorten their patience allowance, escalate scrutiny, or lock them out
faster. Authentication attempt counting is driven by *wrong credentials*, never
by *unclear audio*.

## 7. Accessibility

- Ask people to repeat themselves rather than guessing at a customer ID, a PIN
  or an amount.
- Do not rush a caller: the silence windows exist to check somebody is there,
  not to hurry them.
- Accept natural corrections — if a caller corrects themselves, use what they
  said last.
- Keep sentences short and plain; the browser channel's wording carries over.
- A caller who cannot complete voice authentication is not thereby suspicious.

## 8. Customer autonomy

- A caller may end the call at any time, and asking to do so is honoured
  immediately.
- The agent does not pressure, upsell, or push a caller toward any product.
- The demo is read-only, so there is nothing a caller can be manoeuvred into
  agreeing to.

## 9. Privacy

Covered in full in `TELEPHONY_SECURITY_PRIVACY_DESIGN.md`. In short: the caller
number is discarded by default, PINs are never stored, transcripts are redacted
before they are written, and the channel keeps the least it can rather than the
most the provider offers.

## 10. Explainability

Proportionate to the use case. This system answers factual questions about
synthetic balances and loans; it makes no credit, pricing or eligibility
decisions, so there is no adverse decision requiring explanation.

What *is* explainable, through the operations dashboard: which tool ran, at what
time, for which session, with what outcome. An operator can account for every
figure the agent spoke, because every figure came from a recorded tool call
against the authenticated customer's own data.

## 11. No financial advice

The agent provides factual synthetic account and loan information only. It must
not offer:

- investment advice or product recommendations;
- credit or borrowing advice;
- financial planning or budgeting guidance;
- opinions on whether a caller can afford something;
- comparisons between banks or products.

The existing banking-scope gate already refuses these, and the telephone channel
reuses it rather than introducing its own rules.

## 12. Auditability

Every call produces an operational record: channel, timestamps, duration,
authentication status, domain and intent, tools called, disconnect reason. The
audit vocabulary is fixed (`app.observability.events`) and its fields are an
allow-list, so an event can say what happened without ever carrying what was
said.

## 13. Responsible failure behaviour

When something breaks, the caller gets a short, honest, generic sentence — never
a stack trace, a provider name, or a database error. The system fails **closed**
on anything touching money: a tool that cannot read data says so rather than
guessing, and the agent never invents a figure.

If the bank cannot help, it says so plainly instead of improvising.

---

## Summary of commitments

| Commitment | Phase 1 status |
|---|---|
| AI disclosure configurable, on by default | Configuration added; greeting unchanged |
| Synthetic data only | `DEMO_MODE=true` default |
| No biometric or inferential profiling | None exists; asserted by test |
| No false human handoff | Documented; honest fallback specified |
| Fairness — no voice-derived decisions | Structural: signals never derived |
| Read-only banking scope | Unchanged from baseline |
| No financial advice | Existing scope gate reused |
| Auditability without sensitive values | Event taxonomy added, allow-listed |
