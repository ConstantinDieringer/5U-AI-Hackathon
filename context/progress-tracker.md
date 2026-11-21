# Progress Tracker

Keep entries to at most 3 lines; details belong in code and commits.

## Current Phase

- Setup

## Current Goal

- By 16:00: receive and store every mail, show it live in the UI, portal lanes synced.

## Completed

- Context adapted from Ghost AI.
- Stack decided (`docs/TECH_SPEC.md`), `context/architecture-context.md` added.
- Start kit parsed: 158 lanes from 10 sheets in `data/tariffs.json`, pricing module in `app/pricing/` (floats still, to fix).
- Design for task C (CRM + sessions UI) in `docs/design/aufgabe-c.md`.

## In Progress

- None.

## Next Up

1. Phase 1 tasks A/B/C (see team plan): mail poller + router, portal sync, db.py + UI.

Build order (proposed, confirm):

1. Mailbox client: receive and log mail, reply in thread.
2. Router: rules first, cheap LLM fallback.
3. Tariff ingestion of the 10 sheets plus monthly tariff mails.
4. Pricing module: chargeable weight, surcharges, minimums, validity, door-to-port trucking, cheapest/fastest, tests.
5. RFQ agent: portal API + sheets + carrier bids, 50/50 bid choice, one quote within the time budget.
6. Booking by rate ID plus confirmation.
7. CRM plus tracking agent.
8. Sessions UI with streaming plus dashboard.
9. Hardening for the hourly changes: failed-quote alerts, config reload.
10. Email tone and pitch prep.

Goal: the first accepted quote end to end before any polish.

## Open Questions

1. Architecture and stack, including where the system runs overnight.
2. Team members and who owns which part.
3. Scoring weights: the brief says 45/40/15 and the kickoff deck says 55/30/15. We use the brief's until 5U confirms.
4. Hackathon deadline, the time hourly changes start, and the pitch slot.
5. Has our OpenAI key been posted to the gateway? Which models does `/v1/gateway/models` allow, and at what cost?
6. The portal's current rate-read cap (`X-RateLimit-Limit`).
7. Formats of RFQs, carrier bids, rate desk replies, booking confirmations and tariff mails. Unknown until we see real mail.
8. Start kit contents (sheet formats, carrier list, zones), and how door-to-port trucking is priced (trucker bids by email?).
9. How to tell mail started by 5U's agents from other mail.
10. The exact "fastest" rule, and the formula for the 50/50 price/speed balance among bids.
11. How many carriers to ask and how long to wait for bids within the 2-minute target.
12. Quote currency when the rate and the request differ, and the FX source (the portal returns `fx_eur_per_usd`).
13. ~~What one UI session is~~ → one customer-started thread (see design C).

## Architecture Decisions

- See `context/architecture-context.md`.

## Session Notes

- None yet.
