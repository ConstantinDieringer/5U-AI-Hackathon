# Progress Tracker

Keep entries to at most 3 lines; details belong in code and commits.

## Current Phase

- Phase 1: setup until the first request at 16:00

## Current Goal

- By 16:00: receive and store every mail, show it live in the UI, portal lanes synced.

## Completed

- Context adapted from Ghost AI.
- Stack decided (`context/docs/TECH_SPEC.md`), `context/architecture-context.md` added.
- Start kit parsed: 158 lanes from 10 sheets in `data/tariffs.json`.
- Task B, Constantin, pricing: parser fix (first break 0 kg, PDFs via pdfplumber), `RateOption` in cents, `sheet_options`/`pick`, portal rates + lane snapshot (114 lanes, 15 airlines), lane index, reference data; `app/models.py` (Rfq validated); portal airlines fetched in parallel, snapshot auto-refresh; 34 tests green.
- Design for task C (CRM + sessions UI) in `context/docs/design/aufgabe-c.md`.
- Moved `docs/` (TECH_SPEC, CHALLENGE, PDFs, design) into `context/docs/`; all references updated, `docs/` removed.

## In Progress

- Task C, Moritz (CRM `app/db.py` + sessions UI `app/ui/`): design done (`context/docs/design/aufgabe-c.md`), implementation starting with C1 `db.py`.
- Task A, Karim (mail poller, router, `hackloop.py`): not in repo yet. `portal_options()` (B) is untested live until `hackloop.get_rate/get_ports/get_brackets` exist.

## Next Up

1. Phase 1 tasks A/B/C (see team plan): mail poller + router, portal sync, db.py + UI.
2. Agree with A: every outgoing mail via `db.record_outgoing()`, quote sends only after `db.claim_quote_send()`.
3. B next: door-to-port trucking, carrier-bid selection, tariff mails (`agents/tariff.py`).

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

1. ~~Architecture and stack~~ → decided (`architecture-context.md`). Still open: where the system runs overnight.
2. ~~Team members and owners~~ → A (mail, router, RFQ agent): Karim · B (pricing, tariffs, portal): Constantin · C (CRM, sessions UI, tracking): Moritz.
3. Scoring weights: the brief says 45/40/15 and the kickoff deck says 55/30/15. We use the brief's until 5U confirms.
4. First request arrives 16:00 (kit card). Still open: deadline, start of hourly changes, pitch slot.
5. Has our OpenAI key been posted to the gateway? Which models does `/v1/gateway/models` allow, and at what cost?
6. ~~Portal rate-read cap~~ → 10/min (15:00), one read per airline, 1–4 airlines per lane.
7. Formats of RFQs, carrier bids, rate desk replies, booking confirmations and tariff mails. Unknown until we see real mail.
8. ~~Start kit contents~~ → 10 sheets (xlsx/pdf/docx/txt/jpg), 29 airlines + 23 truckers in `carrier-list.csv`, zones = 2-digit postcode → FRA/MUC/DUS/HAM/STR/CGN. Still open: trucking price (assumed: trucker bids by email).
9. How to tell mail started by 5U's agents from other mail.
10. The exact "fastest" rule, and the formula for the 50/50 price/speed balance among bids.
11. How many carriers to ask and how long to wait for bids within the 2-minute target.
12. Quote currency when the rate and the request differ, and the FX source (the portal returns `fx_eur_per_usd`).
13. ~~What one UI session is~~ → one customer-started thread (see design C).
14. 4 airlines (Zephyr, Himalaya Bridge, Lagoon Isles, Pampero) serve only ASU/KTM/PPT/TNR/ULN, on neither sheet nor portal: email bids only.

## Architecture Decisions

- See `context/architecture-context.md`.

## Session Notes

- 21.11: Kit card received (mailbox `austrian-power@hackloop.mail`, first request 16:00); API key kept out of the repo.
- 21.11: Sheets show a break-point effect (billing at the next weight break is often cheaper); unconfirmed which price the scorer uses, check on the board.
- 21.11 15:30: Portal does not round chargeable weight (0.1234 cbm -> 20.6078 kg), 404 for an airline off the lane; `dataset_version` (now v1) is logged on change as an early sign of the hourly setup changes.
