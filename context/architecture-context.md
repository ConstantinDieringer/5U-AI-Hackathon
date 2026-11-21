# Architecture Context

- **RFQ validation:** `app/mail/rfq_validation.py` checks the current request and attachment evidence before pricing. Drafts and a single clarification's durable state live in `rfq_validation`; no session-table rebuild. Shadow is the rollout default. Enforce excludes signature/history-based pickup guesses and handles clarification replies before booking.

Decided 21.11.2026. Details live in the linked docs; this file only points to them.

- **Stack and conventions (binding):** [docs/TECH_SPEC.md](docs/TECH_SPEC.md): Python 3.11, FastAPI (built-in SSE), SQLite via `sqlite3` without ORM, threads instead of asyncio, one static HTML page with vanilla JS.
- **CRM, session model and sessions UI:** [docs/design/aufgabe-c.md](docs/design/aufgabe-c.md).
- **Session = one customer-started thread.** Mails are mapped to sessions via known mail IDs in `references`; every outgoing mail is stored with `db.record_outgoing()`.
- **Boundaries:** only `app/hackloop.py` talks to the 5U API, only `app/llm.py` to the gateway, only `app/db.py` writes SQL. UI and agents are coupled only through the database (`events` table, `paused`/`mode` flags).
- **Pricing:** deterministic `app/pricing/`; Decimal amounts and integer cents. Tracked JSON files are seed data; runtime tariffs/portal snapshots live in `settings.runtime_dir` on the persistent volume. `portal_result()` adds structured 503 email fallbacks without changing `portal_options()`.
- **Tariffs:** one durable SQLite queue and worker; text/table formats parsed locally, images/scanned PDFs transcribed through `llm.complete` with no pricing decisions. Sender and values validated before atomic publication and cache reload. Pending jobs resume on restart; failed jobs remain recorded with a warning.
- **FX/audit:** EUR/USD comparison with an explicit, expiring FX source. Customer output stays EUR. `RateOption` and `pricing_audit` events retain original currency, tariff version, ordinary/breakpoint totals, and the applied FX evidence.
- **RFQ reliability:** extraction retains explicit questions and constraints, with one schema-repair attempt. A fact-bounded composer must cover every question and emit exactly one verified EUR total; deterministic copy is the fallback. Carrier fan-out is capped and informed by expiring lane observations. Door-to-port quotes require a trucking component. Open RFQs resume from SQLite after restart, while send claims keep customer replies idempotent.
- **Hosting:** one container on Railway (`Dockerfile`, `railway.json`, guide in `DEPLOY.md`), SQLite on a volume at `/data`, one replica only. UI behind HTTP Basic auth (`UI_USER`/`UI_PASSWORD`).
- **UI design:** tokens and components in `DESIGN.md` (v0.1, provisional); screens Live · CRM · Leistung.
