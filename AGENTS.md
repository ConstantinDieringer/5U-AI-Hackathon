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

Follow the matching area in context/areas/ for its business rules. Use `context/code-standards.md` for details, the matching TECH_SPEC section for schema or dependency changes, and Git/PRs for history. Update the owner document when its contract changes.

5U calls go through app/hackloop.py, model calls through app/llm.py, SQL through app/db.py and environment settings through app/config.py. Validate external data. Keep money in minor units or Decimal with currency; use real rates. Keep consent current and explicit. Preserve send claims, pause/manual state, deadlines and retry semantics. Customer copy follows `app/mail/customer_voice.md`.

Run one test while editing, then `python -m pytest -q -m core`. Run the PR selection with `python -m tools.ship_check origin/main` (include staged and unstaged changes locally). Set TEAM_API_KEY=test-only for local tests. CI runs the full suite for shared contracts and on main.

Every team member may merge. Record behaviour, owner, counterexample and test evidence. Coordinate in #update and run `python -m tools.merge_pr NUMBER` from the trusted main checkout with REVIEW_TOKEN. It verifies the PR, deploy window and resulting revision. Announce live only after verification. See `DEPLOY.md` for setup and recovery.
