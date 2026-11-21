# Progress Tracker

Keep entries to at most 3 lines; details belong in code and commits.

## Current Phase

- Phase 1: setup until the first request at 16:00

## Current Goal

- Part A: repo setup and mail intake (A1–A7).
- Team goal by 16:00: receive and store every mail, show it live in the UI, portal lanes synced.

## Completed

- Thread rescue owner: evidence-bound full-thread plans, obsolete source-hold releases, targeted questions, pricing/offer/booking/tracking ownership, separate recovery budget and restart leases. Case 17998 reaches pricing from the later written replacement for MP3; short clear consent can book, conditions/revisions cannot. Integrated Python 3.11: 1135 passed/5 skipped, stored RFQ replay unchanged; production activation not yet verified.

- Writer tone: the composer's shared profile now asks for a close, cheerful best-friend voice throughout each mail, including first contact without invented history. German defaults to du unless formal address is explicitly requested. Latest main's subject rules, separate reviewer and factual/send controls preserved. Integrated full suite passes on Python 3.11 and 3.12: 1104 passed, 5 skipped; no manual live sends.
- Round 64 carrier documents additionally use `Total, all-in` and adjacent label/value rows. Explicit totals now support those ordinary table layouts without scanning past an unrelated row; all four observed document totals replay correctly. Production no-answer cause remains unverified beyond the already reproduced body-only bid gap.
- Pre-booking answers (`PREBOOK_ANSWER`, default off, PR open): a reply to our quote that only asks about the quoted option (round 54 style) is answered from the stored quote with a go-ahead question and never booked; rules only, no LLM review, shares `FOLLOWUP_MAX` with the nudges; "book the faster one instead" after it never books. Reply replay: stored replies identical off/on, 19/19 injected round-54 questions answered and booked on the next yes, 0 worse. Not verified live.
- Round 60 cause not provable without its thread. Strongest offline lead: "Yes", "Ja", "OK", "I confirm", "That works" are `unclear` today (same clarification twice, then silence, never booked) although our nudge asks for "a short yes"; a question containing "not"/"nicht" counts as a decline. Yes rule unchanged pending a decision.
- Kessler BOM thread (no mail after 2 questions): root cause = XLSX header "Gross weight (kg)" read the next row as a total, a false conflict; "1,674 kg" then stayed ambiguous and the cap ended silently. Fixes `TOTALS_SAME_LINE`, `THOUSANDS_EVIDENCE`, `CLARIFY_CAP_RESOLVE` (all default false, recommend true); DG promise still clears the gate (round 41 pinned).
- Each flag alone and all together quote this thread on 1674 kg; replay rounds 1–31 unchanged off and on (no attachments in the corpus); merged with main 978 passed/4 skipped. Not verified: the real attachment and production events, any live round.
- Carrier offers in any common attachment (PR #48): `app/mail/documents.py` reads xls/csv/rtf/odt/ods/eml/doc/msg besides pdf/docx/xlsx/txt; mp3/wav and scans are transcribed via the gateway (own thread, cached, outside the session LLM budget, max 5/session) and must agree with every other source. Airline replies with attachments route as bids when threaded or `[AP-n]`-tagged. Kill switch `ATTACHMENT_TRANSCRIPTION=false`.

- Round 63 expired without a quote after carrier prices moved into attachments. Carrier bids now reuse bounded document reading and require an explicit unique total/currency, keeping timing from the same evidence. Unsupported audio, unreadable sources and conflicting totals remain visible and unpriced; actual production failure attribution is not verified without logs.

- Round 62 customer explicitly questioned an obsolete forwarded subject despite correct shipment facts in the offer. Quotes, booking confirmations and offer follow-ups now name the verified current airport route; missing route facts preserve the existing subject, and reply identifiers stay unchanged. No price, booking-consent or carrier-selection changes.
- Extra customer-email LLM reviewer disabled by default at the user's request (`EMAIL_REVIEW_ENABLED=false`); individual composition and deterministic body/subject checks remain active. Python 3.11: 810 passed/4 skipped. Live health still reports review enabled; the signed-in Railway account gets project 404, so live toggle requires the authorized account.

- Mail guard (`app/mail/guard.py`): empty/automatic customer mail and RFQ floods are stored but never processed (no LLM, no reply); outgoing loops to one customer are cut by burst/thread/repeat caps in `hackloop.send` with one #sos error. Screening blocks loop instructions (0 hits on the 175 genuine mails). Seven edge-case flags now default on (SELF_DELIVERY_RECOVERY, CONFLICT_SOURCE_ANSWER, CUTOFF_QUESTION_NOTICE, TARIFF_ON_READY_DATE, UNIT_EVIDENCE, REVISION_NOT_ACCEPTANCE, ALL_EXCLUDED_NOTICE); replay changes only round 28 (quote without question). 826 passed/4 skipped; not verified live.
- Round 61 pending "check whether we can go ahead" exposed a duplicated approval vocabulary. Pending internal checks now use the existing consent-word matcher within the same sentence; all approval markers are covered together, while a separate unrelated check does not block an explicit order. DG documents remain promised and customer hold explicit; no live booking triggered.
- RFQ watchdog (`RFQ_WATCHDOG`, default off, PR open): unanswered RFQ at 200 s gets one quote from real prices via the one-quote claim; skips paused/manual/phishing/open DG, no price = `watchdog_alert`. Replay: off unchanged, on changes no stored round; killed after bid requests, 25/25 rounds recover the identical quote. Not verified on a live round.
- Unanswered board rounds keep their cause (`board_results.cause_json`, "Zu tun", `python -m app.agents.watchdog report`); `process_start` events + `/api/health.process_started_at`; deploy light `/api/deploy-gate` and DEPLOY.md rule: one person pushes, only on green.

- Round 56 customer will check whether booking can proceed: current keyword rule still returned acceptance. The shared consent guard now also recognizes pending EN/DE internal approval checks; positive past checks followed by an explicit booking order remain valid. Existing booking/price-change flows reuse it; no live booking triggered.

- Strict customer prose v4 closes the unchecked all-excluded notice and subject paths, including followups without an LLM review. Independent final assertions reject punctuation, including Unicode/HTML variants, and unresolved EUR/numeric dates/zero fractions; transmitted subject/body are stored, reply IDs retain threading. Reviewer acceptance instructions tightened. Integrated Python 3.11 suite: 742 passed/4 skipped; wire tests include a misbehaving reviewer and failed renderer. Existing v3 deployment verified active; v4 rollout pending.
- Round 54 followup reproduced a consent defect: "Before we book, could you confirm...?" matched the bare `book` rule. Booking and changed-price acceptance now leave pre-booking questions/conditional approval unresolved; direct booking requests with later informational questions still work. Existing clarification flow handles the unresolved reply; actual production actor remains unverified.

- Rounds 49/50 (-22.5 each): portal-booked XC, customer confirmed the quoted total, airline then confirmed +10 % air. Booking now waits for the airline price (`AIRLINE_CONFIRM_WAIT_S`=180), asks the customer on a change beyond `PRICE_CHANGE_TOLERANCE_PCT`=0.5 with the new all-in and one cheaper alternative, yes confirms, other answers `needs_human` + #sos. Final style reviewer keeps amounts/airlines as protected terms. 689 passed/4 skipped; airline mail price format and scorer rule unverified live.
- Phishing gate (`app/mail/screening.py`) in `poller.handle` before tariff queueing, downloads and dispatch; send backstop, catch-up/history filters, UI release in "Zu tun". Real set (175 genuine, 2 live phishing): 0 false blocks, 2/2 caught by rules alone, 0 sent to the LLM; replay unchanged.
- Defaults `PHISHING_GATE_MODE=enforce`, `PHISHING_GATE_LLM_MODE=shadow` (gpt-5-nano, off session budget, 2 s). Not verified: live gateway call, live round, browser render. Rollback: mode `off` in `app/config.py`.

- Strict customer send path and dedicated gpt-5 style reviewer added. Every customer path validates prose before HTTP, records the transmitted version, and rechecks controls after one review. Real reference punctuation remains; whole numbers lose zero fractions. 639 tests pass/4 skip; 31 replay cases retain all facts with 19 text-only updates. Synthetic real-gateway review accepted, 396 input/94 output tokens, estimated $0.001435; no customer mail sent. Public health exposes style/build/model activation.

- Dashboard overflow: fixed recent-request column proportions, bounded chip/subject ellipsis with full tooltips and a card-local scroll region on narrow screens; score labels and verdicts wrap. Browser checks at 1440/1024/390/320 show no page/card overflow. Integrated current main: 621 tests passed, 4 skipped; JS and Python 3.11 syntax checks pass. Production rollout requested.

- Session 12867 hardening: airport names plus `(IATA)` and natural single-code answers resolve deterministically. Remaining RFQ gaps receive an evidence-bound full-thread `LLM_RESCUE_MODEL` pass before the first question or human review.
- Task 4 LLM cost (no model change): measured 14:52 to 16:14 (nano) about 100 µ$ per accepted quote; estimated at today's defaults about 2,011 µ$ per accepted quote = 39.2 of 40 cost points, composer 66 %. Cheaper setups gain at most +0.2 to +0.5 points; none is proven to give identical extractions (needs live gateway calls), and production `accounting_json` is the missing evidence.
- Task 5 bonuses: the 3 stored tracking questions (1677, 1850, 2252) replayed offline through today's `tracking.handle` all name the BK reference, where the cargo is and planned departure/arrival (the live replies then failed 2 of 3 checks); a name-only question tying two bookings asks for the reference. Five fallback quote texts collected for review; the live composer text cannot be produced offline.
- Task 3 edge cases (`tests/test_edge_cases.py`, today pinned first): preferred airline beats the customer's cheapest/fastest and sticks for later RFQs (rule open); USD requests get EUR (rule open); a December ready date gets today's November sheet; unit/separator/dimension-only mails ask; 3 of 4 revision replies book the old offer; all carriers excluded ends silent. New settings, all default false = today: `CUTOFF_QUESTION_NOTICE`, `TARIFF_ON_READY_DATE`, `UNIT_EVIDENCE`, `REVISION_NOT_ACCEPTANCE`, `ALL_EXCLUDED_NOTICE`. Replay unchanged; not verified live.
- Case 12460 portal outage: reserve slots for eligible portal airlines before speculative bids; 503 fallbacks take priority, healthy results release slots, persisted sends enforce the cap on restart. Full legacy budgets warn instead of silently losing fallbacks; trucking alone remains unquotable. Targeted regression checks cover the outage and complete quote.
- Case 12460 manual replies: default to the latest customer mail even after carrier replies, show recipient in confirmation, reject customer quotes to carriers, and keep explicit carrier replies out of customer response metrics. Selected-mail support is preserved; a literal JS newline that prevented script parsing is repaired. Local tests only; deployment unverified.
- Case 12460 verification on integrated main: Python 3.11, 615 passed/4 skipped; changed Python files pass Ruff, full console script passes Node syntax check, customer-first mail selection verified in the local browser. Added 14 regressions for outage capacity, complete quotes, restart caps, pause, exclusions and reply targets; no production rollout or live quote verification.
- Round 48 expired after two origin questions despite explicit DUS answers. Origin clarification now reuses the existing negation/ambiguity-aware free-text parser independently of optional recovery/one-question flags. No question-cap, pricing or deployment setting changes; initial extraction/runtime state remain unverified.
- Customer presentation: €, full EN/DE months, sentence-based copy without parentheses/field names, short paragraphs across quotes/clarifications/bookings/tracking. Composer repairs leaks and safely formats dates/currency/paragraphs. 575 tests pass/4 skipped; 31 replay records unchanged except 18 quote bodies. No live mail sent; deployment unverified.

- End-to-end tests rounds 35/39/42 (`tests/test_recent_rounds_e2e.py`, real flow, stubbed mail/portal/gateway, both clarify modes): 42 (Lagoon 3,210.00 + trucker, Zephyr never asked, holds for the next RFQ) and 39 (DGD + SDS asked, quote after the answer) pass on main. 35 failed: "the attached packing specification is correct" was no answer; fixed behind `CONFLICT_SOURCE_ANSWER` (default false = today: second question, or no quote under CLARIFY_ONCE). Bodies rebuilt from verified values; real mails 32 to 46 are not stored here.
- Round 45 accepted/booked but customer quote leaked `road_transit`. Shared voice profile now translates timing labels into ordinary language; composer validation rejects internal timing enums/field names and uses its existing repair/fallback. No timing, selection or price changes; production SHA unverified.

- 50/50 balance now `ratio` by default for trucker AND airline bids (`pricing.most_balanced`, `TRUCKER_BALANCE=minmax` restores the old rule). Min-max tied every 2-bid case 0.5/0.5 and the cheaper bid won the tie. Replay moves rounds 21/22/25/26/31 (trucker only, no air choice changes); 21–26 scored 28–38 accuracy and would hit the board's best price (+56.8). Not verified live; Railway variable must not pin minmax.
- Round 42 `excluded_airline`: actual offer selected Lagoon but echoed excluded Zephyr in acknowledgement; exact board trigger unverified. Reuse parallel carrier preferences; filter before fan-out cap, pass exclusions to composer/fallback, keep emergency draft and booking consistent, and acknowledge exclusions generically. Combined suite: 544 passed, 4 skipped; production SHA unverified.
- Approved freight voice implemented: specific openings, grounded thanks/history, I/we, contractions, situational warmth, plain uncertainty and one next step; composer gets a short fastest/cheapest rationale. Full suite 540 passed/4 skipped; 35 relevant tests passed after wording refinements. Synthetic EN/DE probes sent no mail; production activation unverified.

- Customer carrier preferences (round 10820, excluded_airline: "do not offer Zephyr Steppe Cargo ... future inquiries"): `app/agents/carrier_prefs.py` parses refusals/preferences in code, stores them per customer (table customer_carrier_prefs). Excluded carriers are never asked for bids nor quoted; a preferred one wins when priced. Also applied in the emergency draft.
- Setup change ~18:50: RFQs with SLI/packing-spec attachments declaring dangerous goods (UN3481 class 9). 5U rejects quotes without asking for DG documents (reason dg_docs_missing). Clarification now names the UN entry and asks for the Shipper's Declaration + SDS; documents or "attached" in the reply clear it. A ZIP in the mail beats the shipper city in the document.
- Round 39 rejected `dg_docs_missing`: gate identified DG, but its questions omitted an explicit document request. EN/DE DG clarification now asks for applicable DG documents and review before quoting/booking; generic packing-list confirmation does not release the existing gate. 481 tests passed, 4 skipped; later non-template quote source and production deployment unverified.

- Combined customer-email voice skill from Humanize Writing/Humanizer, pinned sources/MIT notices and shared runtime profile. Composer and repairs load it directly, retaining factual/style guards. Skill validation, 477 integrated tests and synthetic EN/DE gateway probes pass; no customer mail sent or production rollout verified.
- Trucker balance review (task 5, default unchanged): replay records the trucker. On the 10 replayable door rounds ratio changes only 21, 22, 25, 26, 31; on 21/22/25/26 the board accuracy of the minmax quote equals 45 − 4.5 per % off the ratio total (±0.05), so ratio is recommended. Fastest should not add the truck lead time: it cannot reorder air options, and round 27 fits the ratio/minmax trucker, not the fastest (36.7 vs 39.32 points). Round 31 (ratio would pick Alpenblick) awaits its accuracy.
- Email bids at the next weight break (task 6a, `BID_NEXT_BREAK`, default false): an airline with known portal brackets gets a second request billed at its next break; the cheaper offer counts. Round 27: Meridian 859 kg × 3.35 € = 2,877.65 € vs board best implied 1000 kg × 2.84 € (+ Hansekogge 140.72 € = 2,980.75 €, matches 39.32 points). Replay cannot show it (no stored reply to the second request); would fire on rounds 27 and 29 only. Carrier reaction to the wording not verified live.
- One question per RFQ (task 3, `CLARIFY_ONCE`, default false = today's cap of two): the question lists every gap; free-text answers count (bare code in capitals, airport city, ZIP, "gross weight is N kg", "no ZIP, we drop it at CGN ourselves"); an unusable answer, or none by `CLARIFY_FALLBACK_AFTER_S`=45 s, leads to the best quote from real prices (stated airport only, no pickup guess) plus a `needs_human` attention item; no real price, no quote. Replay: only round 28 changes (one question, quote UV 390.45 €); rounds 15 to 20 stay unquoted and flagged. Not verified live.
- Pickup ZIP instead of a guess (task 2, `ASK_PICKUP_ZIP`, default false = today): a pickup request without ZIP or area asks once for the ZIP. Only this shipment counts: the current text without signature, or earlier mail of the same thread (also unlabelled ZIPs in enforce's history check); the customer's other shipments never (Weidner used 61391/41007/72379). Enforce, the live default, already asked for round 20; the guess survived in shadow mode and the UI Notfall-Quote, both now covered. Replay: no enforce round changes; in shadow rounds 15 to 17, 19, 20 ask for the ZIP. Not verified live.
- Replay corpus to round 31 (task 4): rounds 28 to 31 added (round 28 extraction reconstructed from its validation event, plus the later live portal read UV 390.45 €/LM 457.08 €, labelled manual). Harness fix: replayed customer answers now get an id after our question, so the reply ledger no longer drops them; round 28 now replays today's two-question loop. Rounds after 31 need a fresh /api/sessions export.
- Self-delivery follow-ups (task 1, same `SELF_DELIVERY_RECOVERY`): with it on, negated sentences ("We do not deliver to FRA ourselves", "liefern nicht selbst") give no airport and count as a pickup request, German order ("liefern selbst nach Frankfurt") is read, codes count in capitals only. Off keeps today's rule, which still reads the negated sentence as FRA. Round 28 test: portal UV 390.45 € beats sheet 416.16 €. Not verified live.
- Ruff config (task 6c): `pyproject.toml` sets line length 100 and py311 as TECH_SPEC says; no file reformatted. At 100, 57 files would rewrap (24 at the old default 88), so format touched files only; 7 existing ruff check findings unchanged.
- Round 38: preserve collection lead versus explicit road transit in carrier timing evidence; composer rejects road-duration claims for collection/unknown lead, and fallback uses neutral carrier lead wording. 473 tests passed, 4 skipped; replay changes only 8 quote texts, with identical prices/options/schedules. Deployment unverified.

- Round 35 rejected: XLSX labels serialized as `Total gross weight (kg): | 1751` were ignored by the numeric gate despite conflicting mail weight 2644. Accept combined table separators for weight/volume/pieces; preserve explicit conflict clarification and customer corrections. Exact production pricing cause remains unverified.
- Replay cleanup closes the final thread-local SQLite connection before removing temporary databases on Windows, including error paths; production database handles in other threads are untouched.
- Round 35 verification: full offline suite 463 passed, 4 skipped, plus the new replay-failure cleanup test passed; targeted Ruff and diff checks clean. Production audit/SHA and causal price reconciliation remain unavailable.

- Self-delivery departure (`SELF_DELIVERY_RECOVERY`, default false = today): reads the airport from "deliver to Cologne airport (CGN) ourselves", "drop it off at CGN ourselves" or "from CGN to DFW" in the current message and in the answer to our origin question. Round 28 replays as a quote without a question; no stored round changes. Live portal price for round 28 not verified (the manual sheet price 416.16 € was too_high).
- Trucker balance (`TRUCKER_BALANCE`, superseded: default is now `ratio`): `ratio` picks price/cheapest + time/fastest, which reproduces the board's best price on all 11 scored door rounds (minmax: 7). Replay with `ratio` moves only rounds 21/22/25/26, each to the implied best (+56.8 accuracy points there). Not verified live.
- Stronger RFQ models: extraction defaults to gpt-5-mini (was nano); failed extraction repair defaults to gpt-5 (was mini), with price accounting. Both passed a synthetic gateway extraction/repair probe; 40 relevant tests pass. Composer stays mini; deployment overrides still take precedence and live rollout is unverified.
- Startup agent check: `run.py` imports every agent from `app.agents.AGENTS` and logs `agents loaded: ...`; a broken one logs an error, emits `agent_import_failed` (#sos) and does NOT stop the other agents. CI imports every app module. DEPLOY.md has live-round deploy rules. Not verified on Railway; local start with AGENTS_ENABLED=false logged the line.
- RFQ replay harness (`python -m app.agents.rfq_replay`, `tests/test_rfq_replay.py`): all 27 live rounds through the real flow offline (stored extraction/portal/bids, no network); baseline diff gates option, total, airline, carriers, clarification and mail text. 22/27 match production exactly; rounds 2/12 lack stored data, 18 to 20 now ask for the pickup. Composer output and new portal reads are not replayed.
- Round 30 review: block unverified cargo cutoff promises (including close of business), retain personal quote repair and state requested cutoff as unconfirmed in composed and fallback text; no current carrier cutoff field exists. Offline suite: 353 passed, 4 skipped; EN/DE promises and rate-validity counterexample covered; deployment not verified.

- Notfall-Quote (Live → "Notfall-Quote", `POST /api/sessions/{id}/draft`, `app/ui/drafts.py`): computes the quote from stored request, sheets, portal (8 s) and the session's bids, fills a house-style mail; sending it records the quote (rate_id) and hands back to the agent so booking works.
- Reverted PR12 shortcuts (local parsing, composer bypass, cache keys). Every quote uses the individual `LLM_COMPOSE_MODEL` (default gpt-5-mini), with bounded same-customer history and audited source IDs; default budget four allows repairs. Synthetic gateway dry-run produced a personal grounded reply; no live customer mail sent or production rollout verified.
- Round 27 booking review: freeze selected air cost/currency separately from the customer total; airline booking emails exclude truck costs and commercial adjustments. Legacy quotes omit an unverified air amount; customer confirmations keep their all-in total. Offline suite: 350 passed, 4 skipped; migration/reopen and EUR/USD coverage; production deployment not verified.

- Numeric evidence accepts labelled sentence totals with `is` as well as `of`/colon forms. Round 24 was accepted but redundantly asked for 1754 kg already stated in the current mail; regression covers gate evidence, alternate labels and negative/non-gross statements.
- RFQ 5844 regression: explicit self-delivery to FRA is recovered deterministically from the current body even if LLM origin is null. The final missing-origin guard now asks a durable clarification instead of setting error, including in shadow mode; its answers can continue without enabling the full strict gate.
- Quote pickup timing: deterministic door quotes show reported truck lead time and explicitly unconfirmed collection slot; composer receives separate truck-lead/slot facts. Round 22 accepted but its fallback omitted the requested earliest pickup; no schedule or price policy changed.
- Mailbox webhooks: HMAC receiver, durable shared queue, exact-ID deduplication, restart handling, safe subscription CLI and polling backoff. 283 tests pass, 1 skipped; changed files pass Ruff and Python 3.11 syntax parsing (tests ran on local Python 3.12). Provider GET returned no subscription; deployment and live push verification remain outstanding.

- History-first follow-up policy implemented on the existing validation lifecycle: unique same-thread facts can resolve gaps without another mail; incomplete replies trigger targeted follow-ups with cumulative corrections. Durable reply ledger/restart replay, sender checks, booking/tracking questions and default enforce mode added. Original deadlines and human controls retained.
- Slack #sos alerts (`app/ui/alerts.py`, SLACK_SOS_WEBHOOK): no quote possible, expired, 5U verdict not accepted, program errors, 15 min without mail. Once per session and kind, with console link. Needs the webhook set in Railway.
- Pricing review (round 20 too_high): 5 trucker requests had used every bid slot, so no airline was asked. Truckers now have their own cap (MAX_TRUCKER_BIDS). CRM tab "Preise" lists all options, truck, bids and verdict per RFQ. PRICE_ADJUST_PCT (default 0) is an untested lever.
- RFQ validation: current-message evidence and attachment checks, additive durable clarification state, one outstanding question at a time, reply routing before booking, deadline/pause/restart protections; `RFQ_VALIDATION_MODE=enforce` is the default, with explicit `shadow` available for diagnostics/rollback.
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
- UI design exploration (branch `ui/design-guidelines`): Twenty-derived guidelines retained as a historical reference; the merge resolution preserves the current `DESIGN.md` v0.2 UI and all overview functionality from `main`.
- Moved `docs/` (TECH_SPEC, CHALLENGE, PDFs, design) into `context/docs/`; all references updated, `docs/` removed.

- RFQ agent (`app/agents/rfq.py`, `bids.py`): 1 LLM extraction, sheet + portal, trucker bids (door-to-port) and email-only airline bids by mail, balanced bid choice, one EUR all-in quote per thread. Router: unknown sender in a new thread = RFQ. Extraction + portal live-checked on mail 265; full send not yet verified live.

## In Progress

- Task C, Moritz (CRM `app/db.py` + sessions UI `app/ui/`): design done (`context/docs/design/aufgabe-c.md`), C3+C4 done (server, SSE, sessions UI, dashboard); next: wire `watchers.start()` and the UI into `run.py` with Karim, then C7/C8.
- Task A, Karim: A1–A8 done. 16:21 PR #5 (Constantin's RFQ agent + bids) merged into main, local `a/rfq-agent` retired; live app now runs main. 16:30 booking agent live (`app/agents/booking.py`): 3 bookings confirmed by the airlines. Next: restart resume for RFQs, tracking agent (C).
- PR #4 conflict resolved on its branch: current `main` UI v3 wins; the earlier Twenty-derived guidelines remain documented as a historical reference.
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
    → Truckers: price/cheapest + time/fastest (board fit, rounds 13 to 29). Airline bids: same rule since 21.11 20:30 (no board evidence yet).
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
20. Board points carry `booking: 0.0` on every booked round, also on time; the published rules list no booking points. What earns them?
21. TECH_SPEC §8 still says `RFQ_VALIDATION_MODE=shadow` is the default; code, tracker and architecture say `enforce`. Which is binding?

## Architecture Decisions

- See `context/architecture-context.md`.

## Session Notes

- 21.11 21:40: Rounds 54/55 accepted but not booked (customer stalled, no follow-up). New `app/agents/followup.py`: template nudge in the thread 30 s after our last unanswered mail (`FOLLOWUP_AFTER_S`), for the offer (max 3, within 900 s) and for clarification questions before the quote (max 3, before the RFQ deadline). No LLM, price never restated; stops on yes/explicit no/booking/pause/manual. Offline tests only.

- 21.11 21:45: Price changes at booking are now live in the setup (rounds 49/50, airline confirms +10 % over the portal rate). Whether the scorer wants the question before or after booking is not published; we book on yes, then ask before confirming. Tracking still reports `bookings.price_minor` (air only on portal door quotes), not `customer_total_minor`.

- 21.11 21:20: `tests/conftest.py` refuses every 5U and gateway call from tests and fails the test that tries. `test_reply_without_mail_client_is_503` posted to the real mailbox on every pytest run (a real mail with the key from `.env`); it now stubs the send.

- 21.11 20:55: Rounds 47 and 48 unanswered: the mailbox allows one poll per 5 s per team key, extra reads with our key (an analysis session bulk-read the mailbox) gave the live poller 429s, and its backoff doubled to minutes. Fix: a 429 keeps the server's wait and never doubles. Rule: nobody polls the mailbox with the team key except the live system. No webhook subscription exists (GET /v1/mailbox/webhook -> 404), so polling is the only intake.

- 21.11 20:35: Carrier exclusions hardened (`carrier_prefs.parse`): two-letter codes ("ZS") and short names ("Zephyr Steppe") count, a negation only reaches its own clause ("we do not mind ..., but we prefer X" no longer bans X), "no objection to X" refuses nothing, country codes in brackets are ignored. Offline tests only; no live round with an exclusion since.

- 21.11 19:30: Board fit: accuracy = 45 × (1 − |ours − best| / best / 10 %), quoting below best costs too. Rounds 2 and 27 lost on the air leg: the best price billed the next weight break (Solace 2000 kg, Meridian 1000 kg). Round 2 is fixed by the portal next-break check; the 503 → email-bid path (27) has no break check yet.

- 21.11 19:00: Board accuracy (now visible) is the largest leak on answered rounds: rounds 2, 19, 21, 22, 25, 26 scored 28 to 38 of 45 while accepted. Rounds 15 to 17 (no answer) were forwarded-subject origin AMS and two RFQs without pickup; current code asks instead. LLM cost was about 0.2 points per quote, not a leak.

- 21.11 18:30: `bids.wait(until_kinds=...)` returns once nobody of the awaited kind is left to answer (all airlines declined no longer waits out the timeout). Tested offline only.

- 21.11 18:15: Tracking answers are now prose that says where the cargo is (still at the shipper before the ready date, planned departure/arrival from `rfq.schedule`), greet the asker by first name (`app/mail/signature.py`), 4 variants per language, team style kept; tracking sessions end as `answered`. Replayed on real mails 1677/1850/2252; bonus not yet verified on the board. Karim's rules cross-check and last-resort commits are NOT merged: they overlap `rfq_validation`.

- 21.11: Webhook merge integrated main's history-first clarification, tracking reply routing, enforce default and trucker pricing changes; conflicts resolved without reverting them. Combined verification: 292 tests passed, 1 skipped; targeted Ruff checks pass.

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
