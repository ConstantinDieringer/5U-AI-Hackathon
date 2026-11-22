# Area: RFQ workflow (extraction, validation, clarification, rescue)

Behavior and flag defaults: `app/config.py`. History: `context/history/`.
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

- **Extraction:** `LLM_MODEL=gpt-5.6-luna`; repair, composer and rescue inherit it unless explicitly overridden. Questions and constraints survive extraction. Lot totals are summed in code; pickup zones determine routing; ICAO codes map to IATA. Verify gateway availability with `python -m tools.model_check` before rollout.
- **Selection intent:** `arrival_deadline` is separate from cheapest/fastest. Current-source checks recover dates/price priority and reject unproven/conflicting limits. Feasibility precedes price ranking in every quote path; missing readiness or no feasible option asks before the send claim. Dates remain estimates, not guarantees.
- **Validation (`RFQ_VALIDATION_MODE=enforce`, the code default):** prices only current, evidenced facts. Signatures and other shipments never replace a pickup address; forwarded subjects and history are not current route facts. Material mail/attachment conflicts create one persisted clarification before pricing. State lives in the `rfq_validation` table. TECH_SPEC §8 still says shadow (open question in the tracker).
- **History-first clarification:** search same-thread customer evidence before asking; reuse only unambiguous facts of this shipment. Accumulate partial corrections and ask again for remaining gaps. Never repeat a question without a new incoming answer. A durable reply ledger prevents duplicates and resumes after restart. Questions keep the original deadline and do not set `claim_quote_send` or `first_reply_at`.
- **Clarification cap** is counted per workflow (`clarify.sent.<sid>` in kv), not per thread. Booking/tracking at the cap is a warning, not #sos. At the cap `send_state`=`capped`; worker skips paused/manual.
- **Dangerous goods:** name the UN entry and ask for the Shipper's Declaration + SDS. A promise to send them clears the gate; never wait for attachments.
- **Thread rescue:** evidenced corrections use the RFQ schema, including null removal, pickup city, insurance and further shipments; no separate field whitelist. Semantic arrival blocks get recovery before asking; physical infeasibility still asks. Rejected plans expose tool feedback for a new decision. Budget `THREAD_RESCUE_MAX_CALLS=12`; root + last 24 mails × 8000 chars, 90 s leases. Input age 600 s, 2 parallel owners. Pause/manual, phishing, real prices, consent, deadlines and uncertain sends remain authoritative.
- **Rescue cost:** rules first: a clear yes to the offer goes to the booking tool, a dangerous-goods-only gate uses the template question, a booking waiting for the airline price is skipped. Refusals and pure booking thanks skip the model (open price approvals do not). Context is compact: no bid requests or audit payloads. Incompatible plans report `thread_rescue_blocked`.
- **LLM budget:** 8 normal calls plus 12 rescue calls, persisted. Luna uses low reasoning normally, medium for rescue, with at least 2048/4096 completion tokens including reasoning. Settings override budgets.
- **Watchdog (`RFQ_WATCHDOG`, off by default):** 200 s after arrival forces one quote from real prices via the one-quote claim; skips live workers, paused/manual, phishing, open DG. No price = `watchdog_alert`. Report: `python -m app.agents.watchdog report`.
- **Versioned replies:** `customer_reply` queues post-offer work with per-thread leases and `REPLY_WORKERS=4`. Revised bids are version-correlated; original scoring deadlines stay fixed. Contracts, ownership, snapshots and rollout: `context/docs/reply-workflow.md`.
- **Follow-ups:** `FOLLOWUP_ENABLED` uses 30 s/max 3 nudges, stops on pause/manual/booking or unanswered customer questions. Versioned threads also require `awaiting_customer`, a current offer and no pending mail/open issue. Business answers do not spend the reminder cap. Details and limits: `app/config.py`, `context/docs/reply-workflow.md`.
- **Restart:** `rfq.catch_up()` resumes RFQs younger than `TOO_OLD_S` without a quote, reusing the stored extraction; carriers asked before the restart are not re-mailed.
- Edge-case flags: `app/config.py`; regressions: the RFQ and arrival workflow tests.
- **Insurance:** current-source rules require clarification for missing or conflicting terms. Partial terms accumulate; watchdog/rescue cannot omit an unpriced premium.
- **Replay:** `python -m app.agents.rfq_replay run` compares stored rounds offline.

## Area tests

```
python -m pytest -q tests/test_arrival_constraints.py tests/test_cargo_insurance.py tests/test_rfq_agent.py tests/test_history_first_clarification.py tests/test_rfq_watchdog.py tests/test_recent_rounds_e2e.py
```
