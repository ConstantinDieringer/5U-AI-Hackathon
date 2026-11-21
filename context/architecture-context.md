# Architecture Context

Decided 21.11.2026. Details live in the linked docs; this file only points to them.

- **Stack and conventions (binding):** [docs/TECH_SPEC.md](docs/TECH_SPEC.md): Python 3.11, FastAPI (built-in SSE), SQLite via `sqlite3` without ORM, threads instead of asyncio, one static HTML page with vanilla JS.
- **CRM, session model and sessions UI:** [docs/design/aufgabe-c.md](docs/design/aufgabe-c.md).
- **Session = one customer-started thread.** Mails are mapped to sessions via known mail IDs in `references`; every outgoing mail is stored with `db.record_outgoing()`.
- **Boundaries:** only `app/hackloop.py` talks to the 5U API, only `app/llm.py` to the gateway, only `app/db.py` writes SQL. UI and agents are coupled only through the database (`events` table, `paused`/`mode` flags).
- **Pricing:** `app/pricing/` (deterministic, no LLM), tariffs in `data/tariffs.json`. Known debt: `pricing.py` still uses floats for money; switch to integer cents.
