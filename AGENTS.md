# Ship a change

Implement the requested customer behaviour. Start at its owner, exercise the normal entry point with a new formulation and a counterexample, then report the decision and evidence in the PR.

| Change | Start here | First test |
|---|---|---|
| Request meaning, correction, deadline evidence | app/mail/selection_intent.py and rfq_validation.py | tests/test_arrival_constraints.py |
| Eligible flight, cheapest/fastest, favourite | app/pricing/selection.py | tests/test_arrival_constraints.py |
| Mail intake, retries | app/mail/webhook.py or poller.py | tests/test_mailbox_webhook.py |
| RFQ, missing information, resume | app/agents/rfq.py or clarification.py | tests/test_history_first_clarification.py |
| Accept, decline or defer a booking | app/agents/customer_reply.py, booking.py and consent_check.py | tests/test_booking.py |
| Status of a shipment | app/agents/tracking.py | tests/test_tracking.py |
| Customer wording, language | app/mail/ and `.agents/skills/customer-email-voice/SKILL.md` | tests/test_quote_languages.py |
| Dashboard or manual quote | app/ui/drafts.py and the shared business function | tests/test_arrival_constraints.py |
| Deploy or alerts | `DEPLOY.md` | tests/test_ops_guardrails.py |

Read `CONTEXT.md` as the single project context. Consult technical references only when needed for the task. Update context only for major capabilities, architecture decisions or essential contracts, under 500 words. `CHANGELOG.md` is the only changelog, with at most eight short entries for truly significant changes. Combine related changes and replace older entries when full. Do not create area summaries, progress trackers, history archives or per PR change fragments. Small fixes and test evidence belong in Git and PRs.

5U calls go through app/hackloop.py, model calls through app/llm.py, SQL through app/db.py and environment settings through app/config.py. Validate external data. Keep money in minor units or Decimal with currency; use real rates. Keep consent current and explicit. Preserve send claims, pause/manual state, deadlines and retry semantics. Customer copy follows `app/mail/customer_voice.md`.

Run the relevant test file while editing. Set TEAM_API_KEY=test-only for local pytest. Run the entire essential suite and lint with `python -m tools.ship_check`. CI uses this same command on PRs and main. Add a regression only for a distinct customer-visible failure or operational hazard.

Every team member may merge. Record behaviour, owner, counterexample and test evidence. Coordinate in #update and run `python -m tools.merge_pr NUMBER` from the trusted main checkout with REVIEW_TOKEN. It verifies the PR, deploy window and resulting revision. Announce live only after verification. See `DEPLOY.md` for setup and recovery.
