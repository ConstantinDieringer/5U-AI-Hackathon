# Progress Tracker

Keep entries to at most 3 lines; details belong in code and commits.

## Current Phase

- Phase 1: setup until the first request at 16:00

## Current Goal

- Part A: repo setup and mail intake (A1–A7).
- Team goal by 16:00: receive and store every mail, show it live in the UI, portal lanes synced.

## Completed

- Context adapted from Ghost AI.
- Stack decided (`context/docs/TECH_SPEC.md`), `context/architecture-context.md` added.
- Start kit parsed: 158 lanes from 10 sheets in `data/tariffs.json`.
- Task B, Constantin, pricing: parser fix (first break 0 kg, PDFs via pdfplumber), `RateOption` in cents, `sheet_options`/`pick`, portal rates + lane snapshot (114 lanes, 15 airlines), lane index, reference data; `app/models.py` (Rfq validated); portal airlines fetched in parallel, snapshot auto-refresh; 35 pricing tests green.
- Design for task C (CRM + sessions UI) in `context/docs/design/aufgabe-c.md`.
- UI v2 + online prep: `DESIGN.md` (tokens/components), UI redesigned into Live · CRM (customers, won/lost, quotes, bookings, drawer) · Leistung; `/api/crm`; Basic auth; `Dockerfile` + `railway.json` + `DEPLOY.md`. Docker build not tested locally (no daemon).
- C3+C4 done: `app/ui/server.py` (REST, SSE via `fastapi.sse`, actions), `static/index.html` (sessions + dashboard, dark), `watchers.py` (deadline, board sync), `demo.py` (seed `data/demo.db`); 20 tests green, checked in a real browser.
- C1+C2 done: `app/db.py` (schema, `tx()` with BEGIN IMMEDIATE, session mapping, events, quotes, bookings, `claim_quote_send`, stats), 16 tests green on 3.11. 
- A1 repo setup; fresh clone installs `requirements.txt` (Python 3.11).
- A2 `config.py` + `hackloop.py` (mailbox, board, portal get_ports/get_brackets/get_rate for B): `/v1/me` OK live; portal calls not tried live.
- A3 `context/docs/mail-format.md` from a real mail: self-mail is rejected (422), so one rate request went to ratedesk.
- A4 `mail/poller.py` + `mail/store.py` + `mail/parse.py`: live run stores mail 208 once, a restart resumes at since_id 208; 6 tests (broken mail, restart, attachments).
- A7 `app/llm.py`: live call OK (gpt-5-nano, 10+10 tokens, logged 5 micro-USD). The gateway returns no cost, so we use list prices (`app/llm_prices.json`). max_tokens=16 fails: use ≥ 100.
- A5 `mail/router.py`: rules only, 10 tests green. A6 `python run.py` = poller + watchers + UI on :8000, checked live.
- Moved `docs/` (TECH_SPEC, CHALLENGE, PDFs, design) into `context/docs/`; all references updated, `docs/` removed.

- RFQ agent (`app/agents/rfq.py`, `bids.py`): 1 LLM extraction, sheet + portal, trucker bids (door-to-port) and email-only airline bids by mail, balanced bid choice, one EUR all-in quote per thread. Router: unknown sender in a new thread = RFQ. Extraction + portal live-checked on mail 265; full send not yet verified live.

## In Progress

- Task C, Moritz (CRM `app/db.py` + sessions UI `app/ui/`): design done (`context/docs/design/aufgabe-c.md`), C3+C4 done (server, SSE, sessions UI, dashboard); next: wire `watchers.start()` and the UI into `run.py` with Karim, then C7/C8.
- Task A, Karim: A1–A8 done. 16:21 PR #5 (Constantin's RFQ agent + bids) merged into main, local `a/rfq-agent` retired; live app now runs main. 16:30 booking agent live (`app/agents/booking.py`): 3 bookings confirmed by the airlines. Next: restart resume for RFQs, tracking agent (C).
- PR #4 (UI restyle) NOT merged: it conflicts with Moritz's console redesign 299faf2 on main. Moritz + Constantin decide.
- Task B, Constantin: B1–B7 in PR #2 (pricing, portal, lanes, reference, dataset_version); next door-to-port trucking, bid selection, tariff mails.

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
5. ~~OpenAI key~~ → posted 15:52 (last4 5NoA). 32 models allowed, no prices; default gpt-5-nano.
6. ~~Portal rate-read cap~~ → 10/min (15:00), one read per airline, 1–4 airlines per lane.
7. Formats of RFQs, carrier bids, rate desk replies, booking confirmations and tariff mails. Unknown until we see real mail.
8. ~~Start kit contents~~ → 10 sheets (xlsx/pdf/docx/txt/jpg), 29 airlines + 23 truckers in `carrier-list.csv`, zones = 2-digit postcode → FRA/MUC/DUS/HAM/STR/CGN. Still open: trucking price (assumed: trucker bids by email).
9. How to tell mail started by 5U's agents from other mail.
10. The exact "fastest" rule, and the formula for the 50/50 price/speed balance among bids.
11. How many carriers to ask and how long to wait for bids within the 2-minute target.
12. Quote currency when the rate and the request differ, and the FX source (the portal returns `fx_eur_per_usd`).
13. ~~What one UI session is~~ → one customer-started thread (see design C).
14. 4 airlines (Zephyr, Himalaya Bridge, Lagoon Isles, Pampero) serve only ASU/KTM/PPT/TNR/ULN, on neither sheet nor portal: email bids only.
15. For C (A): `db.ingest_mail` doesn't say if the mail was new. Please return `(session_id, is_new)` or add `save_mail(...) -> bool`; until then `mail/store.py` uses a kv high-water mark.
16. (A) Send has no `references` field; the server presumably threads by `in_reply_to`. Not yet seen on a threaded reply.
17. For C/B (A): **rate desk replies are not threaded** (`in_reply_to` null, `references` []), so they land unassigned. Map them by the quoted subject in the body (see mail-format.md)?

18. (A) Reply format of trucker and airline bids unknown: parsed by regex (`bids.parse_price`); unreadable bids are logged as `bid_unreadable`. Check the first real ones.
19. (A) Quotes are always in EUR (portal USD converted with `fx_eur_per_usd`, USD bids ignored). Door-to-port without a trucker bid by `BID_MAX_WAIT_S` is sent without trucking.

## Architecture Decisions

- See `context/architecture-context.md`.

## Session Notes

- 21.11 16:45: Tracking questions name no reference ("Weidner Spielwaren GmbH, STR to EZE, 1228 kg, ready on 2026-11-26"). Tracking match now also uses the weight and asks for the BK reference on a tie instead of guessing.

- 21.11 16:35: Tracking agent live (2c5e9dd, not yet seen a real tracking mail). Quote language now from function words (83b6884): the LLM had tagged 3/6 English RFQs as German. Round 6 booked live 26 s after the quote.

- 21.11 16:30: Booking agent live. Board counts round 5 as booked; rounds 2+3 (booked 20 min late, rate re-fetched) show booked=false. Book fast. `kill` alone does not stop run.py while the UI has an SSE client open: use `kill -9` or close the UI first.

- 21.11 16:20: Round 4 (mail 937) lost: LLM gave ICAO origin `EDDL`, Rfq rejected it. Fixed (9b068ab: ICAO→IATA, bad origin dropped when a ZIP is given). Round 2 acceptance (mail 618) not booked: no booking agent yet.

- 21.11 16:01: First real RFQ (mail 265, lindqvist-medtech, fastest to ORD, door pickup 51447, 676 kg / 5.79 cbm) received, routed as customer, session + UI live. `/v1/board/me` rows: `round_no, sent_at, answered_at, verdict, reason, booked`; matched to sessions via `sent_at` = session created_at.

- 21.11 15:45: Real mail fields: `id, from, to, subject, body, in_reply_to, references, received_at, attachments`. Ratedesk replies carry NO in_reply_to/references (only `your mail "<subject>"` in the body) → `db.ingest_mail` falls back to subject matching.
- 21.11 15:45: Mail to our own address → 422. Gateway OpenAI key not set yet (`set: false`).

- 21.11: Kit card received (mailbox `austrian-power@hackloop.mail`, first request 16:00); API key kept out of the repo.
- 21.11: Sheets show a break-point effect (billing at the next weight break is often cheaper); unconfirmed which price the scorer uses, check on the board.
- 21.11 15:30: Portal does not round chargeable weight (0.1234 cbm -> 20.6078 kg), 404 for an airline off the lane; `dataset_version` (now v1) is logged on change as an early sign of the hourly setup changes.
