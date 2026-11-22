# 5U-AI-Hackathon · Team Austrian Power

Digital air forwarder for the **5U AI challenge** at START HACK Munich 2026. One freight mailbox, answered around the clock without a person: every mail is routed, air export requests (RFQs) are priced from rate sheets, the rate portal and carrier bids, quoted in the customer's thread and language, booked on acceptance and tracked from our own CRM. A person can watch every session live and take over at any moment.

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
| RFQ agent | `app/agents/rfq.py`, `app/mail/rfq_validation.py`, `clarification.py` | Extracts the request, checks every figure against the mail and attachments, asks once when something is missing or implausible, prices and quotes exactly one option |
| Pricing | `app/pricing/` | Chargeable weight, weight breaks, surcharges, FX, door-to-port trucking, cheapest / fastest / balanced selection. No LLM |
| Carrier bids | `app/agents/bids.py` | Asks airlines and truckers in parallel, reads mail, PDF, Word, Excel and voice replies |
| Booking | `app/agents/customer_reply.py`, `booking.py`, `consent_check.py` | Books only on current, explicit consent; refreshes an expired rate ID |
| Tracking | `app/agents/tracking.py` | Answers status questions from the CRM, never guesses |
| Dashboard | `app/ui/` | Live sessions via server-sent events, pause / take over / hand back, emergency quote drafts, board verdicts, cost per RFQ, deploy light |

## Design choices

- **Code decides, the model reads.** Routing, pricing, option selection and every safety check are deterministic and tested. The model is used where language is unstructured: understanding the request and writing the reply.
- **Every model call is counted.** All calls go through `app/llm.py`, are logged with tokens and cost per session (`llm_calls`, dashboard) and are capped per session (`LLM_MAX_CALLS_PER_SESSION`). The model is configured in `app/config.py` (`LLM_TEXT_MODEL`); check its price in `app/llm_prices.json` against the 0.10 USD cost limit per quote.
- **Nothing is sent twice, nothing is lost on a restart.** Atomic send claims, message deduplication, resume of RFQs that were mid-pricing, ambiguous sends marked `unknown` instead of retried.
- **A person can always step in.** Pause, manual mode and hand back per session or globally; the system never overrides a person's decision.
- **Compliance before price.** Missing dangerous-goods documents are requested before quoting; damaged lithium batteries (IATA DGR SP A154) and sanctioned destinations get a reasoned refusal.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in TEAM_API_KEY and UI_PASSWORD
python run.py                 # poller, agents and dashboard on UI_PORT
```

Only **one** instance may process the live mailbox. For a demo without live mail:

```bash
DB_PATH=data/demo.db python -m app.ui.demo --live &   # realistic sessions, streaming events
DB_PATH=data/demo.db AGENTS_ENABLED=false python run.py
```

## Test it

For the optional local portal-browser experiment, install `requirements-browser.txt`
and Google Chrome, then run:

```bash
python -m tools.portal_browser_check --pure-wu --interactive --output-dir /path/to/screenshots
```

This opens visible Chrome using the Pure-WU browser settings. Add `--headless`
for comparison. It records screenshots and whether the Cloudflare page remains;
it does not verify rates, send credentials, run mail agents or change live pricing.

The unattended experiment starts and controls its own Chrome process and temporary
application profile, without ChatGPT browser control or your personal Chrome profile:

```bash
python -m tools.portal_pipeline_check --mode chrome-cdp --chrome-path /path/to/chrome --output-dir /path/to/results
python -m tools.portal_pipeline_check --mode reader --output-dir /path/to/results
```

Chrome runs headlessly by default; `--headed` compares a visible application-owned
browser. Verification attempts are bounded with `--attempts` (0 to 2). Reader mode
tests Pure-WU's Jina fallback using only the public URL, without credentials.

```bash
python -m tools.ship_check     # the essential test suite + ruff, same command as CI
```

Tests never call 5U or a model: the mailbox, portal and gateway are blocked in `tests/conftest.py`.

## Exploratory case worker

Opt in with `CASE_WORKER_ENABLED=true`. The model discovers sourced tasks, investigates
documents and speech, prepares independent shipments, uses existing real-price tools,
evaluates unit-aware selection expressions and verifies coverage before delivery. PDFs
are generated and checked as actual files. The dashboard shows tasks and tool results.

`CASE_WORKER_WORKERS`, `CASE_WORKER_MAX_STEPS` and `CASE_WORKER_MAX_CALLS` bound concurrency
and work per input. State and receipts survive restarts. Unknown services require evidence
or review; the model cannot invent prices. Booking retains existing consent checks;
combined-offer booking requires review. Keep the default disabled until gateway access,
real-model quality, cost and timing have been evaluated for rollout.

## More

- Project context and essential contracts: [CONTEXT.md](CONTEXT.md)
- How to ship a change: [AGENTS.md](AGENTS.md) · Deploy rules and the deploy light: [DEPLOY.md](DEPLOY.md)
- Significant changes: [CHANGELOG.md](CHANGELOG.md)
- Challenge summary: [context/docs/CHALLENGE.md](context/docs/CHALLENGE.md) · Original documents: [brief](context/docs/5U-AI-challenge-brief.pdf) · [deep dive deck](context/docs/5U-AI-challenge-deep-dive.pdf)
