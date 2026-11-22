<p align="center">
  <img src="app/ui/static/logo.png" alt="Austrian Power logo" width="96">
</p>

<h1 align="center">Austrian Power</h1>

<p align="center"><b>A digital air forwarder that never sleeps and always lets a human step in.</b><br>
Built by Constantin, Karim and Moritz for the 5U AI challenge at START HACK Munich 2026.</p>

<p align="center"><a href="https://5u-ai-hackathon-production.up.railway.app/#overview"><b>Live dashboard ↗</b></a></p>

---

## Who answers at 2 a.m.?

A new air export request arrives every six minutes and expires six minutes later. Austrian Power runs the freight mailbox around the clock without a person. It routes every mail, prices each request from rate sheets, the rate portal and carrier bids, and quotes exactly one option in the customer's thread and language. It books when the customer accepts and answers tracking questions from its own CRM. A person can watch every session live and take over at any moment.

## How a request flows

```
 5U mailbox ──webhook / poll──▶ Router (rules, no LLM) ──┬─▶ RFQ agent ─▶ Quote ─▶ Booking ─▶ Tracking
                                                         │     │  ├─ rate sheets (parsed once, deterministic)
                                                         │     │  ├─ rate portal (rate IDs, rate-limited)
                                                         │     │  └─ carrier bids (airlines + truckers, in parallel)
                                                         ├─▶ Carrier bid / portal reply ─▶ waiting RFQ
                                                         ├─▶ Tariff mail ─▶ tariff ingestion
                                                         ├─▶ Tracking question ─▶ tracking agent (CRM only)
                                                         └─▶ Ignore (own mail, auto-replies, phishing, other teams)

 SQLite CRM (sessions, mails, quotes, bookings, events, LLM costs) ──SSE──▶ Dashboard (watch, pause, take over)
```

| Part | Where | What it does |
|---|---|---|
| Router | `app/mail/router.py`, `guard.py`, `screening.py` | Classifies by sender and thread, holds auto-replies and loops, quarantines phishing |
| RFQ agent | `app/agents/rfq.py`, `clarification.py`, `app/mail/rfq_validation.py` | Extracts the request, checks every figure against the mail and attachments, asks once when something is missing, then quotes exactly one option |
| Pricing | `app/pricing/` | Chargeable weight, weight breaks, surcharges, FX, door-to-port trucking, cheapest / fastest / balanced selection. No LLM |
| Carrier bids | `app/agents/bids.py` | Asks airlines and truckers in parallel and reads replies as mail, PDF, Word, Excel or voice message |
| Booking | `app/agents/customer_reply.py`, `booking.py`, `consent_check.py` | Books only on current, explicit consent and refreshes an expired rate ID |
| Tracking | `app/agents/tracking.py` | Answers status questions from the CRM and never guesses |
| Dashboard | `app/ui/` | Live sessions over server-sent events, pause / take over / hand back, emergency quote drafts, board verdicts, cost per request, airport map with weather and event alerts, rates tab, deploy light |

## Design principles

- **Code decides, the model reads.** Routing, pricing, option selection and every safety check are deterministic and tested. The model only handles unstructured language: understanding the request and writing the reply.
- **Every model call is counted.** All calls go through `app/llm.py`, are logged with tokens and cost per session and are capped per session (`LLM_MAX_CALLS_PER_SESSION`, default 8). The model is set in `app/config.py` (`LLM_TEXT_MODEL`), prices live in `app/llm_prices.json`.
- **Nothing is sent twice, nothing is lost on a restart.** Atomic send claims, message deduplication, resume of requests that were mid-pricing, and ambiguous sends marked `unknown` instead of retried.
- **A person can always step in.** Pause, manual mode and hand back, per session or globally. The system never overrides a person's decision.
- **Compliance before price.** Missing dangerous-goods documents are requested before quoting. Damaged lithium batteries (IATA DGR SP A154) and sanctioned destinations get a reasoned refusal. Mailbox-move and payment-redirect claims are quarantined, even from known contacts.
- **Customer mails sound human.** Short paragraphs in the language of the customer's first mail, no bot punctuation, no invented history. Rules: [`app/mail/customer_voice.md`](app/mail/customer_voice.md).

## Run it

Requires Python 3.11.

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in TEAM_API_KEY and UI_PASSWORD
python run.py                 # poller, agents and dashboard on UI_PORT
```

Production runs at <https://5u-ai-hackathon-production.up.railway.app/#overview>. A local run serves the same dashboard at <http://localhost:8000>. Both log in with user `team` and the `UI_PASSWORD` of that environment. Every other setting has a default in `app/config.py` and is documented in `.env.example`.

> [!WARNING]
> Only **one** instance may process the live mailbox. Production already does, so never start a second live instance locally.

For a demo without live mail:

```bash
DB_PATH=data/demo.db python -m app.ui.demo --live &   # realistic sessions with streaming events
DB_PATH=data/demo.db AGENTS_ENABLED=false python run.py
```

## Test it

```bash
python -m tools.ship_check     # essential test suite + ruff, the same command CI runs on every PR
```

While editing, run just the file you are working on:

```bash
TEAM_API_KEY=test-only python -m pytest tests/test_booking.py -q
```

The suite covers pricing, routing, clarification, booking consent, tracking and multilingual quotes. Tests never call 5U or a model: mailbox, portal and gateway are blocked in `tests/conftest.py`.

## Repository map

```
app/
  agents/      RFQ, bids, booking, tracking, follow-ups, case worker
  mail/        intake, router, validation, phishing screening, customer voice
  pricing/     tariff parser, rates, selection (deterministic)
  ui/          FastAPI server, dashboard, demo seeder
  hackloop.py  the only client for 5U (mailbox, portal, board)
  llm.py       the only path to the model, with cost logging
  db.py        all SQL (SQLite)
  config.py    all settings
data/          parsed tariffs and the start kit
tests/         pytest suite
tools/         ship check, safe merge, map data
```

## Deploy

One Railway container from `Dockerfile`, live at <https://5u-ai-hackathon-production.up.railway.app>. A PR merges only with green CI, and only while the deploy light shows that no request is mid-pricing and the next one is more than 90 seconds away. A change is live once `/api/health` reports the merge commit as `build_revision`. Details and recovery: [DEPLOY.md](DEPLOY.md).

## Optional: exploratory case worker

`CASE_WORKER_ENABLED=true` lets the model plan sourced tasks, read documents and speech, compare real offers and produce checked multi-page PDF quotes. It uses the existing pricing and consent checks and cannot invent prices. It is off by default and bounded by `CASE_WORKER_WORKERS`, `CASE_WORKER_MAX_STEPS` and `CASE_WORKER_MAX_CALLS`.

## More

- Project context and essential contracts: [CONTEXT.md](CONTEXT.md)
- How to ship a change: [AGENTS.md](AGENTS.md)
- Dashboard design tokens: [DESIGN.md](DESIGN.md)
- Significant changes: [CHANGELOG.md](CHANGELOG.md)
- Portal browser experiments (standalone, not used in production): [context/docs/portal-experiments.md](context/docs/portal-experiments.md)
- Challenge: [summary](context/docs/CHALLENGE.md) · [brief](context/docs/5U-AI-challenge-brief.pdf) · [deep dive deck](context/docs/5U-AI-challenge-deep-dive.pdf)
