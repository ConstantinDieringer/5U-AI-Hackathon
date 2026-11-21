# Progress Tracker

Keep entries to at most 3 lines; details belong in code and commits.

## Current Phase

- Phase 1: setup until the first request at 16:00

## Current Goal

- Part A: repo setup and mail intake (A1–A7).
- Team goal by 16:00: receive and store every mail, show it live in the UI, portal lanes synced.

## Completed

- Mailbox webhooks: HMAC receiver, durable shared queue, exact-ID deduplication, restart handling, safe subscription CLI and polling backoff. 283 tests pass, 1 skipped; changed files pass Ruff and Python 3.11 syntax parsing (tests ran on local Python 3.12). Provider GET returned no subscription; deployment and live push verification remain outstanding.

- RFQ validation: current-message evidence and attachment checks, additive durable clarification state, one automatic question, reply routing before booking, deadline/pause/restart protections; `RFQ_VALIDATION_MODE=shadow` remains the default pending production replay acceptance.
- RFQ validation verification: 257 tests passed, 1 existing test skipped; relevant Ruff checks pass. Read-only local replay covered 18 sessions but no stored extractions, so no live extraction accuracy claim or production activation. Integrated the latest API-cost/pickup-note changes; enforce never prices an assumed pickup.

- RFQ cost card: input/cached input/output/reasoning per model and purpose, frozen price/usage snapshots and USD totals. Legacy estimates retained; missing prices/usage visible. Accounting migration adds one nullable column; no extra API calls.
- RFQ history isolation: current cargo blocks exclude obsolete forwarded specifications and thread subjects from extraction/lot sums; pure forwarding remains supported. Round 15 carrier requests used historical AMS; actual pickup location was absent, so this fix cannot reconstruct it or recover the expired round.
- Door-to-port without ZIP (rounds 3627, 3930 lost): pickup area from the mail/signature (pickup_zones.csv) or the customer's last pickup → zone → airport → truck + air. No email-only airline bids for origins outside our 6 airports.
- Mail template guard: generated RFQ bodies reject unfilled placeholders and embedded mail headers, falling back to verified copy; placeholder customer names use a generic greeting. Observed live mail 3495 still used pre-style-fix output, so production rollout requires confirmation.
- UI v4 Übersicht (`/api/status`, `db.status_overview`): Systemcheck traffic lights (mail, quotes, speed, 5U, cost, bookings, tracking, bids, errors), step chips per RFQ, Zu tun, Punkte; charts/rings removed. Bid replies and BK confirmations now map by AP/BK reference (were 23 unassigned).
- Online: Railway runs the agents (AGENTS_ENABLED=true) since 17:17 on the imported laptop DB; nobody runs `run.py` against the live mailbox anymore.
- Mail style: quotes, booking confirmations/requests, carrier bids and tracking replies use `€`, no dash punctuation/lists, and only `Your austrian Power Team` as sign-off. Shared integer-cent formatter; API currency codes and reference IDs unchanged.
- RFQ optimisation: questions/constraints survive extraction with one repair attempt; fact-bounded
  responses enforce one EUR total and complete coverage. Carrier fan-out is capped at 5, incomplete
  door quotes are blocked, recent RFQs resume after restart; 169 tests pass.

- B merge integration: preserved main's booking/tracking, language/date replies, served-lane refusals and portal next-weight-break optimisation alongside tariff/FX/429/503 hardening; 161 tests pass on the combined code.

- B hardening: durable tariff worker and reusable monthly parsers (text/table + gateway image transcription), atomic runtime data, dataset/board alerts, bounded portal 429 waits and known-carrier 503 fallback.
- B pricing: explicit expiring EUR/USD conversion for sheets/bids, original amounts and breakpoint audit retained; `python -m app.pricing.audit` joins board evidence. Scorer breakpoint semantics remain unconfirmed; existing default preserved.
- B verification: 118 tests pass, including isolated full-app startup, 158 kit lanes, monthly reload, replay and 429/503; targeted Ruff checks/formatting pass. No live restart or live image-model extraction performed; authoritative production instance still needs identification.

- UI v3 (light bento, DESIGN.md v0.2): Übersicht with hero KPI, sparklines, trend pills, bezier area chart (volume/response/cost), Apple-Health rings, attention list (expired, error, board rejected, paused, acceptance not booked), activity table; Live and CRM restyled. `/api/overview?hours=`, `/api/attention`.
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
11. ~~How many carriers to ask and how long to wait~~ → 2–5 carriers, full-score deadline 110 s,
    final deadline 300 s; 15 s are reserved for composing and sending.
12. Quote currency when the rate and the request differ, and the FX source (the portal returns `fx_eur_per_usd`).
13. ~~What one UI session is~~ → one customer-started thread (see design C).
14. 4 airlines (Zephyr, Himalaya Bridge, Lagoon Isles, Pampero) serve only ASU/KTM/PPT/TNR/ULN, on neither sheet nor portal: email bids only.
15. Resolved: `db.ingest_mail(report_new=True)` atomically returns `(session_id, is_new)`. Mail ingress deduplicates exact IDs, including out-of-order webhook deliveries.
16. (A) Send has no `references` field; the server presumably threads by `in_reply_to`. Not yet seen on a threaded reply.
17. For C/B (A): **rate desk replies are not threaded** (`in_reply_to` null, `references` []), so they land unassigned. Map them by the quoted subject in the body (see mail-format.md)?

18. (A) Reply format of trucker and airline bids unknown: parsed by regex (`bids.parse_price`); unreadable bids are logged as `bid_unreadable`. Check the first real ones.
19. (A) Quotes are always in EUR. ~~Door-to-port without a trucker bid~~ → blocked by default;
    `ALLOW_INCOMPLETE_DOOR_QUOTE=true` is the explicit emergency override.

## Architecture Decisions

- See `context/architecture-context.md`.

## Session Notes

- 21.11: Mailbox webhook receiver, persistent shared inbox, exact-ID deduplication, restart recovery and polling backoff implemented. Subscription activation requires deployment plus the server-side admin command in `DEPLOY.md`; no live subscription has been changed during development. Signature/queue tests use isolated SQLite and mocked API calls.

- 21.11 17:12: Price check (task 5, partial): `scores_revealed` still false, so no per-request points yet. Rounds 2–8 + 12: chosen option matches the preference every time; the only too_high (round 7, MUC-LAX 998 kg) was the missed 1000 kg break, fixed in 1e53564. Rounds 5 (1228 kg) and 6 (1549 kg) also sit just under likely breaks: re-check them once points are visible. Pricing unchanged.
- 21.11 17:08: Round 11 (2530) lost: nano gave origin YYZ (= destination) for pickup 21663; `resolve_origin` now prefers the ZIP airport unless the LLM origin is a zone airport (57f660d). Round 10 (2256) lost: crash from mixed code versions in the live process (lazy import after a pull) → resumed at 190 s, but the portal deadline had passed (fixed c1916fc: ≥ 20 s). Lesson: restart right after every pull.

- 21.11 17:05: Extraction retry: an unusable first answer (bad JSON, Rfq invalid, gateway error) gets one retry with `LLM_RETRY_MODEL` (gpt-5-mini) and the error fed back, as the 2nd budget call. Live: 302 micro-USD, 1.7 s. Mini alone gets lot sums wrong (1710 vs 1952 kg); code lot sums still win.

- 21.11 16:58: Board watch: any decided verdict other than accepted, or any reason, is emitted once as an error event (Achtung list) and logged (`watchers.is_failure`). Dataset_version changes were already logged + emitted by `db.note_dataset_version`.

- 21.11 16:57: Restart-safe RFQs: `rfq.catch_up()` at startup resumes new/pricing RFQs younger than TOO_OLD_S without a quote, reusing the stored extraction; `bids.request` doesn't re-mail carriers asked before the restart and replays their stored replies. LLM budget (2/session) now counted in the db (`db.reserve_llm_call`).

- 21.11 16:54: Round 1967 lost: nano gave weight_kg 0 for lots with "each" (4 × 48 kg, 110×110×52 cm). Lot totals now summed in code (`app/mail/lots.py`, gross not net, dims per piece) and override the LLM.

- 21.11 16:50: Every email-only airline asked so far declined ("We do not serve this lane", 19 mails). `app/pricing/served_lanes.py` keeps each airline's "We fly ..." list (kv, 1 h TTL, backfilled at startup) and we skip airlines that don't fly the lane. A decline now counts as an answer, so we no longer wait the full 100 s for bids that never come.

- 21.11 16:45: Quote now answers "earliest departure and arrival" (ready date, +1 day for door pickup, + transit), de/en, no LLM; test with real mail 1628.

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
