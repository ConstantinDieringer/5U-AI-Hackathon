# Architecture Context

Stack and conventions (binding): [docs/TECH_SPEC.md](docs/TECH_SPEC.md): Python 3.11, FastAPI (built-in SSE), SQLite via `sqlite3` without ORM, threads instead of asyncio, one static HTML page with vanilla JS. CRM, session model and sessions UI: [docs/design/aufgabe-c.md](docs/design/aufgabe-c.md).

Subsystem behavior lives in `context/areas/*.md`; the old full decision log is archived in `context/history/architecture-decisions.md`.

## Boundaries (never cross)

- Only `app/hackloop.py` talks to the 5U API, only `app/llm.py` to the gateway, only `app/db.py` writes SQL. Only `app/config.py` reads `.env`.
- UI and agents are coupled only through the database (`events` table, `paused`/`mode` flags).
- Pricing, routing, selection and tariff lookup are deterministic code; the LLM extracts and writes, never prices.
- Session = one customer-started thread; every outgoing mail goes through `db.record_outgoing()`, quote sends only after `db.claim_quote_send()`.

## Flow

mail in (`/hooks/mailbox` or poller) → phishing gate + mail guard → router → agent (rfq, bids, booking, tracking, tariff, clarification, rescue) → deterministic pricing → composer → `hackloop.send_customer` → CRM/events → UI via SSE.

## Module map

| Path | Area file |
|---|---|
| `app/mail/` (webhook, poller, router, screening, guard, documents, speech) | `areas/mail.md` |
| `app/agents/rfq.py`, `clarification.py`, `rescue.py`, `watchdog.py`, `followup.py`, `app/mail/rfq_validation.py` | `areas/rfq.md` |
| `app/pricing/`, `app/agents/bids.py`, `tariff.py`, `carrier_prefs.py` | `areas/pricing.md` |
| `app/mail/outgoing.py`, `style.py`, `multilingual.py`, `voice.py`, `customer_voice.md`, `app/agents/email_review.py` | `areas/customer-email.md` |
| `app/agents/booking.py`, `prebook.py`, `tracking.py`, `tracking_notice.py` | `areas/booking-tracking.md` |
| `app/ui/` (server, static, drafts, alerts, watchers, deploy_gate) | `ui-context.md` |
| `Dockerfile`, `railway.json`, `run.py`, `DEPLOY.md` | `areas/ops.md` |
| `app/db.py`, `app/models.py` | TECH_SPEC §6–7, `docs/design/aufgabe-c.md` §4–5 |
| `app/llm.py`, `app/llm_prices.json` | TECH_SPEC §8; `llm_calls.accounting_json` keeps usage subtypes and frozen rates |

## Adding things

- New module: put it under the matching folder, name it after its responsibility, add a row to the area file.
- New dependency, folder or table: change TECH_SPEC first (§2, §3, §7), then the code.
