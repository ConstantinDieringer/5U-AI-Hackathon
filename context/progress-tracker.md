# Progress Tracker

Read on every task, so it stays small (budget enforced by `tests/test_context_budget.py`).
Current state only: one or two lines per entry, newest first. Subsystem behavior belongs in
`context/areas/*.md`. When a section exceeds its limit, move the oldest entries verbatim to
`context/history/progress-log.md` (newest first there too).

## Current State

- Live: Railway runs all agents on `main` (one container, one replica); live rounds every ~6 min.
- Phase: hardening against the hourly setup changes, improving accuracy and email quality, pitch prep.
- Owners: A mail/router/RFQ agent Karim · B pricing/tariffs/portal Constantin · C CRM/UI/tracking Moritz.

## Recent Changes (max 8)

- Process guardrails: PR + CI (pytest, secrets scan) instead of direct pushes, read-only `REVIEW_TOKEN` for review agents, #sos on repeated mailbox 429, `python -m app.deploy_check` confirms live. CI run on GitHub pending.
- Any language, part 2: detection adds nl/pl/sv/da/nb/cs/tr/ro/hu/fi and non-Latin scripts (zh/ja/ko/ru/uk/el/ar/...; RFQ 20150 is Chinese); booking yes/no in those languages incl. CJK; translations keep € amounts verbatim and codes/references next to CJK text. 1359 passed/5 skipped.
- Round 70: "we won't go ahead" booked. Explicit negated booking intent (incl. contractions, curly apostrophes) now overrides positive verbs; no booking or price-change approval from it.
- Round 70: French RFQ got an English reply. Request language (ISO code) now reaches composer/repair; FR/ES/IT fallbacks, months, subjects and tracking notices keep the language; unsupported fallback languages fail visibly.
- Reply in the customer's language, any language: one checked gateway call in `hackloop.send_customer` names the language and translates; pt no longer read as es; no quote template means English copy translated at send, not no quote (`REPLY_IN_CUSTOMER_LANGUAGE`).
- Übersicht v0.3: errors first (`GET /api/problems`), ERROR log lines as `log_error` events, "Kontext kopieren" for manual AI sessions (`/api/context`). Checked in Chrome with the demo DB.
- Context system: lean always-read core, per-area files in `context/areas/`, history archived in `context/history/`, size budgets tested.
- Required tracking paragraph in every RFQ customer send, plus a 30 s repair worker. 1261 passed/5 skipped; rollout pending.

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
5. Preferred airline vs. the customer's cheapest/fastest, and USD requests quoted in EUR: rules not confirmed (`tests/test_edge_cases.py`).
6. Send has no `references` field; threading presumably by `in_reply_to` only.
7. Production cause of rounds 60 and 63 not provable without their threads/logs.

## Session Notes (max 6, newest first)

- 21.11 23:15: Thread rescue woke ~40 h old threads on every restart (429 bursts, mails into stale threads). Now age- and parallel-limited.
- 21.11 23:05: "Weitere Rückfrage unterdrückt" fired too often: RFQ, booking and tracking questions shared one cap. Now per workflow.
- 21.11 21:45: Price changes at booking are live (airline confirms +10 % over the portal rate, rounds 49/50).
- 21.11 21:20: `tests/conftest.py` refuses every 5U and gateway call from tests.
- 21.11 20:55: Rounds 47/48 unanswered: extra mailbox reads with our key caused 429s for the live poller. Nobody polls with the team key except the live system.
- 21.11 19:30: Board accuracy = 45 × (1 − |ours − best| / best / 10 %); best price often bills the next weight break.
