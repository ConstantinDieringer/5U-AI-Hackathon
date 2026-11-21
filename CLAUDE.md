# 5U AI Hackathon: Team Austrian Power

A digital air freight forwarder for the 5U AI challenge (START HACK Munich 2026).
Python 3.11 · FastAPI · SQLite (`sqlite3`, no ORM) · threads · one static HTML page.

## Context loading (only what the task needs)

- **Loaded automatically:** this file and the code standards below.
- **Loaded automatically by path:** `.claude/rules/*.md` pull in the matching `context/areas/*.md` file as soon as you read or edit a file in that area. If it does not appear, read it yourself from the table.
- **Read only when needed:** `context/progress-tracker.md` when you plan work, need open questions or pending decisions, or update it after a change.
- **Read when planning or after a pull:** the newest eight files in `context/changes/` (`ls context/changes | sort | tail -8`): recent changes, one file per PR.

| Task touches | Read |
|---|---|
| Mail intake, webhook, poller, router, phishing, mail guard, attachments | `context/areas/mail.md` |
| RFQ extraction, validation, clarification, rescue, watchdog, follow-ups | `context/areas/rfq.md` |
| Pricing, tariffs, portal, carrier/trucker bids, FX, scoring accuracy | `context/areas/pricing.md` |
| Customer email wording, composer, style rules, send path | `context/areas/customer-email.md` + skill below |
| Booking, consent, price changes, tracking answers, tracking notice | `context/areas/booking-tracking.md` |
| UI, dashboard, manual reply, Notfall-Quote, CRM screens | `context/ui-context.md` + `DESIGN.md` |
| Deploy, Railway, restarts, alerts, live-round rules | `context/areas/ops.md` + `DEPLOY.md` |
| Module boundaries, new module, cross-subsystem change | `context/architecture-context.md` |
| Goals, scoring, challenge rules, 5U API fields | `context/project-overview.md`, `context/docs/5u-api.md`, `context/docs/mail-format.md` |
| New dependency, folder, data model or DB table | the matching section of `context/docs/TECH_SPEC.md` (section index at its top) |
| Planning/scoping a larger change | `context/ai-workflow-rules.md` |

- `context/docs/TECH_SPEC.md` is binding; read **only the section** the change touches.
- `context/docs/design/`, `context/docs/research/`, the PDFs and `context/history/` are reference/archive: `grep`, never read in full by default.
- Feature-flag defaults: `app/config.py` is the source of truth, not the docs.
- Customer email wording or composer changes: `.agents/skills/customer-email-voice/SKILL.md` and `app/mail/customer_voice.md`.

## Fast loop

1. Iterate with the area's test command (bottom of each area file), e.g. `python -m pytest -q tests/test_pricing.py`.
2. Full suite once before the PR: `python -m pytest -q` (about 2–3 min). `-m "not slow"` skips the replay/full-app tests while iterating; they must still pass before the PR.
3. `ruff check` and `ruff format` on touched files only.

## After a change

- Add **one new file** to `context/changes/` for the PR (format in `context/changes/README.md`). Do not edit `context/progress-tracker.md`; only the context owner does.
- Changed subsystem behavior goes into that `context/areas/*.md` file (replace outdated lines), not the tracker.
- Budgets are enforced by `tests/test_context_budget.py`.

## Code standards

@context/code-standards.md
