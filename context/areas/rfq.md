# Area: RFQ workflow (extraction, validation, clarification, rescue)

Current behavior only. Why/history: grep `context/history/`. Flag defaults: `app/config.py`.
Binding detail: TECH_SPEC §8 (LLM rules, rescue, validation) and §8a (answer contract, time budget).

## Modules

| Module | Role |
|---|---|
| `app/agents/rfq.py` | extraction, pricing call, quote send (`_price_and_quote`) |
| `app/mail/rfq_validation.py`, `rfq_sources.py`, `lots.py`, `selection_intent.py` | evidence, lot sums and selection constraints |
| `app/agents/clarification.py` | threaded questions, reply ledger, per-workflow cap |
| `app/agents/rescue.py` | thread rescue owner (`LLM_RESCUE_MODEL`) |
| `app/agents/watchdog.py` | forced quote for unanswered RFQs (`RFQ_WATCHDOG`) |
| `app/agents/followup.py` | template nudges for unanswered offers/questions |
| `app/mail/multilingual.py` | cached English working copies for the rules; keeps source language and consent evidence |
| `app/agents/rfq_replay.py`, `app/mail/validation_replay.py` | offline replay harnesses |

## Rules

- **Extraction:** one LLM extraction (`gpt-5-mini`), one schema repair with `LLM_RETRY_MODEL` (`gpt-5-mini`; was `gpt-5`, cost). Questions and constraints survive extraction. Lot totals are summed in code (gross, dims per piece) and override the LLM. Pickup-zone routing wins over model output; ICAO codes are mapped to IATA.
- **Selection intent:** `arrival_deadline` is separate from cheapest/fastest. Current-source checks recover dates/price priority and reject unproven/conflicting limits. Feasibility precedes price ranking in every quote path; missing readiness or no feasible option asks before the send claim. Dates remain estimates, not guarantees.
- **Validation (`RFQ_VALIDATION_MODE=enforce`, the code default):** prices only current, evidenced facts. Signatures and other shipments never replace a pickup address; forwarded subjects and history are not current route facts. Material mail/attachment conflicts create one persisted clarification before pricing. State lives in the `rfq_validation` table. TECH_SPEC §8 still says shadow (open question in the tracker).
- **History-first clarification:** search same-thread customer evidence before asking; reuse only unambiguous facts of this shipment. Accumulate partial corrections and ask again for remaining gaps. Never repeat a question without a new incoming answer. A durable reply ledger prevents duplicates and resumes after restart. Questions keep the original deadline and do not set `claim_quote_send` or `first_reply_at`.
- **Clarification cap** is counted per workflow (`clarify.sent.<sid>` in kv), not per thread. Booking/tracking at the cap is a warning, not #sos. At the cap `send_state`=`capped`; worker skips paused/manual.
- **Dangerous goods:** name the UN entry and ask for the Shipper's Declaration + SDS. A promise to send them clears the gate; never wait for attachments.
- **Thread rescue (`THREAD_RESCUE_ENABLED`):** blocked RFQs and workflows stalled `THREAD_RESCUE_AFTER_S` get the full same-shipment conversation, validation sources and CRM state. Evidence-bound plans with 90 s leases survive restarts. Only input younger than `THREAD_RESCUE_MAX_AGE_S` (600 s), max `THREAD_RESCUE_MAX_PARALLEL` (2) owners. Budget `THREAD_RESCUE_MAX_CALLS=2` on `gpt-5-mini`; prompt = root + latest `THREAD_RESCUE_MAX_MAILS` (8) mails × `THREAD_RESCUE_BODY_CHARS` (2500). Pause/manual, phishing, real-price tools, booking consent, deadlines and uncertain sends stay authoritative.
- **Rescue cost:** rules first: a clear yes to the offer goes to the booking tool, a dangerous-goods-only gate uses the template question, a booking waiting for the airline price is skipped. Refusals and pure booking thanks skip the model (open price approvals do not). Context is compact: no bid requests or audit payloads. Incompatible plans report `thread_rescue_blocked`.
- **LLM budget:** 8 normal calls per thread (`LLM_MAX_CALLS_PER_SESSION` may lower it) plus the 2 rescue calls; counted in the db (`db.reserve_llm_call`).
- **Watchdog (`RFQ_WATCHDOG`, off by default):** 200 s after arrival forces one quote from real prices via the one-quote claim; skips live workers, paused/manual, phishing, open DG. No price = `watchdog_alert`. Report: `python -m app.agents.watchdog report`.
- **Follow-ups (`FOLLOWUP_ENABLED`):** template nudge 30 s after our last unanswered mail, max 3 for the offer (within 900 s) and 3 for questions (before the RFQ deadline). Never restates the price; foreign yes/no (normalized) stops nudges; an unanswered customer question is never nudged (rescue keeps it); templates are translated at send. Stops on pause/manual/booking.
- **Restart:** `rfq.catch_up()` resumes RFQs younger than `TOO_OLD_S` without a quote, reusing the stored extraction; carriers asked before the restart are not re-mailed.
- **Edge-case flags** (see `app/config.py` for defaults): `SELF_DELIVERY_RECOVERY`, `ASK_PICKUP_ZIP`, `CLARIFY_ONCE` (+`CLARIFY_FALLBACK_AFTER_S`), `CONFLICT_SOURCE_ANSWER`, `CUTOFF_QUESTION_NOTICE`, `TARIFF_ON_READY_DATE`, `UNIT_EVIDENCE`, `REVISION_NOT_ACCEPTANCE`, `ALL_EXCLUDED_NOTICE`, `TOTALS_SAME_LINE`, `THOUSANDS_EVIDENCE`, `CLARIFY_CAP_RESOLVE`. Tests: `tests/test_edge_cases.py` and the per-flag test files.
- **Replay before changing behavior:** `python -m app.agents.rfq_replay` runs stored live rounds offline; the baseline diff gates option, total, airline, carriers, clarification and mail text.

## Tests (run these while iterating; the full suite only before the PR)

```
python -m pytest -q tests/test_arrival_constraints.py tests/test_rfq_agent.py tests/test_rfq_validation_gate.py tests/test_history_first_clarification.py tests/test_thread_rescue.py tests/test_rfq_watchdog.py tests/test_extract_retry.py tests/test_recent_rounds_e2e.py tests/test_rfq_replay.py
```
