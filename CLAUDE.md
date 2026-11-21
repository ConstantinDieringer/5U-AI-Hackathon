# 5U AI Hackathon: Team Austrian Power

A digital air freight forwarder for the 5U AI challenge (START HACK Munich 2026).
Python 3.11 · FastAPI · SQLite (`sqlite3`, no ORM) · threads · one static HTML page.

## Context loading (read only what the task needs)

**Always read (small, kept under budget):**

1. `context/code-standards.md`: hard implementation rules
2. `context/progress-tracker.md`: current state, pending decisions, open questions

**Then read only the rows that match the task:**

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
| New dependency, folder, data model or DB table | the matching section of `context/docs/TECH_SPEC.md` (see its section index) |
| Planning/scoping a larger change | `context/ai-workflow-rules.md` |

- `context/docs/TECH_SPEC.md` is binding, but read **only the section** the change touches; never the whole file by default.
- Long design docs (`context/docs/design/`, `context/docs/research/`) and the PDFs are reference material: grep them, do not read them in full.
- `context/history/` is an archive (old progress log, old decision log). Never read it by default; `grep` it only when you need why something was decided or a past round's details.
- Feature-flag defaults live in `app/config.py`; that file is the source of truth, not the docs.

For customer email wording or composer changes, use `.agents/skills/customer-email-voice/SKILL.md` and its shared runtime profile `app/mail/customer_voice.md`.

## After a change

- Update `context/progress-tracker.md` (one or two lines, newest first) and move entries beyond its budget to `context/history/progress-log.md`.
- If a change alters a subsystem's behavior, update that subsystem's `context/areas/*.md` file, not the tracker.
- Keep the always-read files within budget: `tests/test_context_budget.py` fails otherwise.

Challenge sources: [context/docs/CHALLENGE.md](context/docs/CHALLENGE.md) and the PDFs in `context/docs/`.
