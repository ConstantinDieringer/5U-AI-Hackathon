# Progress Tracker

Open decisions and current state, maintained by the context owner. Look up change history in Git and PRs.

## Current State

- Live: Railway runs all agents on `main` (one container, one replica); live rounds every ~6 min.
- Phase: hardening against the hourly setup changes, improving accuracy and email quality, pitch prep.
- Owners: A mail/router/RFQ agent Karim · B pricing/tariffs/portal Constantin · C CRM/UI/tracking Moritz.

## Pending Decisions and Rollouts (max 10)

- Flags built and tested but off by default, awaiting a team decision: `PREBOOK_ANSWER`, `RFQ_WATCHDOG`, `TOTALS_SAME_LINE`, `THOUSANDS_EVIDENCE`, `CLARIFY_CAP_RESOLVE`, `BID_NEXT_BREAK`, `CLARIFY_ONCE`, `ASK_PICKUP_ZIP`.
- Round 60 lead: "Yes", "Ja", "OK", "I confirm", "That works" are `unclear` today although our nudge asks for "a short yes". Yes rule unchanged pending a decision.
- Live health still reported the email reviewer enabled; toggling needs the authorized Railway account.
- Tracking notice v1 and strict prose v4 rollout not yet verified on production.

## Open Questions (max 10)

1. Scoring weights: brief says 45/40/15, kickoff deck 55/30/15. We use the brief's until 5U confirms.
2. TECH_SPEC §8 says `RFQ_VALIDATION_MODE=shadow` is the default; code and areas say `enforce`. Which is binding?
3. Board shows `booking: 0.0` on every booked round; the published rules list no booking points. What earns them?
4. Does the scorer want the price-change question before or after booking? (We book on yes, then ask before confirming.)
5. Preferred airline vs. the customer's cheapest/fastest, and USD requests quoted in EUR: rules not confirmed.
6. Send has no `references` field; threading presumably by `in_reply_to` only.
7. Production cause of rounds 60 and 63 not provable without their threads/logs.

## Session Notes (max 6, newest first)

- 21.11 23:15: Thread rescue woke ~40 h old threads on every restart (429 bursts, mails into stale threads). Now age- and parallel-limited.
- 21.11 23:05: "Weitere Rückfrage unterdrückt" fired too often: RFQ, booking and tracking questions shared one cap. Now per workflow.
- 21.11 21:45: Price changes at booking are live (airline confirms +10 % over the portal rate, rounds 49/50).
- 21.11 21:20: `tests/conftest.py` refuses every 5U and gateway call from tests.
- 21.11 20:55: Rounds 47/48 unanswered: extra mailbox reads with our key caused 429s for the live poller. Nobody polls with the team key except the live system.
- 21.11 19:30: Board accuracy = 45 × (1 − |ours − best| / best / 10 %); best price often bills the next weight break.
