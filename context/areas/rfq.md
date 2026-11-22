# Area: RFQ workflow (extraction, validation, clarification, rescue)

Current behavior only. Why/history: grep `context/history/`. Flag defaults: `app/config.py`.
Binding detail: TECH_SPEC §8 (LLM rules, rescue, validation) and §8a (answer contract, time budget).

## Modules

| Module | Role |
|---|---|
| `app/agents/rfq.py` | extraction, pricing (`_price`), quote send (`_price_and_quote`) |
| `app/agents/multi_shipment.py` | several shipments in one mail: sibling requests, one combined quote |
| `app/mail/rfq_validation.py`, `rfq_sources.py`, `lots.py` | evidence checks before pricing, lot sums |
| `app/agents/clarification.py` | threaded questions, reply ledger, per-workflow cap |
| `app/agents/rescue.py` | thread rescue owner (`LLM_RESCUE_MODEL`) |
| `app/agents/watchdog.py` | forced quote for unanswered RFQs (`RFQ_WATCHDOG`) |
| `app/agents/followup.py` | template nudges for unanswered offers/questions |
| `app/mail/multilingual.py` | cached English working copies for the rules; keeps source language and consent evidence |
| `app/agents/rfq_replay.py`, `app/mail/validation_replay.py` | offline replay harnesses |

## Rules

- **Extraction:** one LLM extraction (`gpt-5-mini`), one schema repair with `LLM_RETRY_MODEL` (`gpt-5-mini`). Questions and constraints survive. Lot totals are summed in code (gross, dims per piece) and override the LLM. Pickup-zone routing wins over the model; ICAO maps to IATA.
- **Validation (`RFQ_VALIDATION_MODE=enforce`, the code default):** prices only current, evidenced facts. Signatures and other shipments never replace a pickup address; forwarded subjects and history are no route facts. Material mail/attachment conflicts create one persisted clarification before pricing. State in the `rfq_validation` table (TECH_SPEC §8 still says shadow: open question).
- **History-first clarification:** search same-thread customer evidence before asking; reuse only unambiguous facts of this shipment. Accumulate partial corrections and ask only for remaining gaps, never twice without a new answer. A durable reply ledger prevents duplicates across restarts. Questions keep the original deadline and do not set `claim_quote_send` or `first_reply_at`.
- **Clarification cap** per workflow (`clarify.sent.<sid>` in kv), not per thread; at the cap `send_state`=`capped` (booking/tracking: warning, not #sos). Worker skips pause/manual.
- **Dangerous goods:** name the UN entry, ask for Shipper's Declaration + SDS. A promise to send them clears the gate, no waiting.
- **Thread rescue (`THREAD_RESCUE_ENABLED`):** blocked RFQs and workflows stalled `THREAD_RESCUE_AFTER_S` get the same-shipment conversation, validation sources and CRM state; evidence-bound plans with 90 s leases survive restarts. Limits (`THREAD_RESCUE_*`): input age 600 s, 2 parallel owners, 2 calls on `gpt-5-mini`, root + 8 latest mails × 2500 chars. Pause/manual, phishing, real-price tools, consent, deadlines and uncertain sends stay authoritative.
- **Rescue cost:** rules first: a clear yes goes to the booking tool (once per mail), a DG-only gate uses the template question, bookings awaiting the airline price are skipped, refusals and booking thanks skip the model. Context has carrier replies, customer evidence and compact failures, no bid requests or audits. Incompatible plans report `thread_rescue_blocked`.
- **LLM budget:** 8 normal calls per thread (`LLM_MAX_CALLS_PER_SESSION` may lower it) plus the 2 rescue calls; counted in the db (`db.reserve_llm_call`).
- **Watchdog (`RFQ_WATCHDOG`, off by default):** 200 s after arrival forces one quote from real prices via the one-quote claim; skips live workers, pause/manual, phishing, open DG. No price = `watchdog_alert`.
- **Follow-ups (`FOLLOWUP_ENABLED`):** template nudge 30 s after our last unanswered mail, max 3 for the offer (within 900 s) and 3 for questions (before the deadline). Never restates the price; a yes/no stops nudges; an open customer question is left to rescue; translated at send. Stops on pause/manual/booking.
- **Several shipments (`one_of_two`):** first shipment in the top-level fields, the rest in `additional_shipments` (no lot sums). Each priced in parallel with its own bid slot (`bids.slot_for`); one template mail, one EUR all-in total per shipment. Unwritten weights are dropped. Booking books the first only.
- **Restart:** `rfq.catch_up()` resumes unquoted RFQs younger than `TOO_OLD_S` from the stored extraction; asked carriers are not re-mailed.
- **Edge-case flags** (see `app/config.py` for defaults): `SELF_DELIVERY_RECOVERY`, `ASK_PICKUP_ZIP`, `CLARIFY_ONCE` (+`CLARIFY_FALLBACK_AFTER_S`), `CONFLICT_SOURCE_ANSWER`, `CUTOFF_QUESTION_NOTICE`, `TARIFF_ON_READY_DATE`, `UNIT_EVIDENCE`, `REVISION_NOT_ACCEPTANCE`, `ALL_EXCLUDED_NOTICE`, `TOTALS_SAME_LINE`, `THOUSANDS_EVIDENCE`, `CLARIFY_CAP_RESOLVE`. Tests: `tests/test_edge_cases.py` + per-flag files.
- **Replay before changing behavior:** `python -m app.agents.rfq_replay` runs stored live rounds offline; the baseline diff gates option, total, airline, carriers, questions and mail text.

## Tests (run these while iterating; the full suite only before the PR)

```
python -m pytest -q tests/test_rfq_agent.py tests/test_rfq_validation_gate.py tests/test_history_first_clarification.py tests/test_clarification_cap_per_workflow.py tests/test_clarify_cap_resolve.py tests/test_clarify_once.py tests/test_thread_rescue.py tests/test_rescue_thousands.py tests/test_rfq_watchdog.py tests/test_followup.py tests/test_edge_cases.py tests/test_extract_retry.py tests/test_multi_shipment.py tests/test_lots.py tests/test_numeric_sentence_evidence.py tests/test_origin_clarification_fallback.py tests/test_pickup_location.py tests/test_ask_pickup_zip.py tests/test_self_delivery_origin.py tests/test_dg_document_request.py tests/test_kessler_bom_thread.py tests/test_thousands_evidence.py tests/test_totals_same_line.py tests/test_rfq_history.py tests/test_recent_rounds_e2e.py tests/test_rfq_replay.py
```
