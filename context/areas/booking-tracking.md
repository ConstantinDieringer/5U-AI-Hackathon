# Area: Booking and tracking

Current behavior only. Why/history: grep `context/history/`. Flag defaults: `app/config.py`.
Binding detail: TECH_SPEC §8a (booking consent, pre-booking answers). Booking API: `context/docs/5u-api.md`.

## Modules

| Module | Role |
|---|---|
| `app/agents/booking.py` | consent classification, portal/email booking, customer confirmation |
| `app/agents/prebook.py` | answers questions about the quoted option (`PREBOOK_ANSWER`, off) |
| `app/agents/tracking.py` | tracking answers from our CRM |
| `app/agents/tracking_notice.py` | tracking paragraph repair worker |
| `app/agents/reply_replay.py` | offline replay of stored replies |

## Rules

- **Consent:** `booking.classify_reply` is rules only (yes/no/question/deferral/unclear); only "yes" books. Pre-booking questions ("Before we book, could you confirm...?"), conditional approval and pending internal checks ("check whether we can go ahead") are unresolved even if "book" appears. The pending-check test uses the same consent words, per sentence. Explicit negated intent ("won't go ahead", "cannot accept", "don't reserve", curly apostrophes too) overrides positive verbs. Approvals must be current and explicit; never infer consent from an old approval. `CONSENT_LLM_CHECK` (`booking._consent`, `app/agents/consent_check.py`): a reply the rules leave undecided, or a yes next to a doubt word ("not", "hold off", "leider", "pas"), gets one schema-checked gateway call (event `consent_check`). Only label `book` books; the rules' explicit decline always stands; questions, conditions, pending checks and change requests never reach the model. Confirmation language: a bare "OK" keeps the request's de/en.
- **Changed cargo after the quote** (`booking.cargo_revision`, rules only): weight/pieces/volume in the customer's own words that differ from the quoted request mean the offer no longer fits. No booking (not on a later "yes" either), no generic question: the session goes to `manual` with a `needs_human` error; a person answers the customer.
- Book fast: a re-fetched rate after 15 minutes may change the outcome. Book by rate ID; 503 means book by email with the airline. There is no status call, so our CRM is the record.
- **Price changes:** confirm the customer only once the airline price is known (portal response, airline confirmation mail, or after `AIRLINE_CONFIRM_WAIT_S`=180). A deviation beyond `PRICE_CHANGE_TOLERANCE_PCT`=0.5 asks the customer about the new all-in total and names one cheaper alternative; yes confirms, anything else is `needs_human`. Never rebook automatically (the portal has no cancel).
- Airline booking emails carry the frozen air cost only. Customer totals and price-change alternatives retain truck and insurance costs (`quotes.insurance_minor`). State in `bookings.customer_state`.
- **Tracking:** prose that says where the cargo is (at the shipper before the ready date, planned departure/arrival from `rfq.schedule`), greets the asker by first name. Matching uses reference, then customer/route/weight; on a tie ask for the BK reference instead of guessing. Tracking reports `bookings.price_minor` (air only on portal door quotes).
- **Versioned customer replies:** `REPLY_WORKFLOW_MODE=shadow` (default) evaluates without execution; `enforce` gives `customer_reply` sole ownership after the first offer. `reply_intent` extracts changes, questions and current consent together; `reply_conditions` proves supported booking conditions from the frozen offer. A change blocks the previous offer, recalculates, sends one replacement per revision, then requires approval of that replacement. The original first quote remains the scoring record. Explicit go-ahead wording is sufficient; no literal yes is required. Current evidence, version and pending input are checked again before transport. Details: `context/docs/reply-workflow.md`.
- Quote selection is explicit: first for scoring, active for pre-booking, booking-bound for confirmations and tracking. Conditional consent never overrides an unresolved change, stop or unknown condition. Older bookings without a quote link retain their legacy lookup.
- Foreign replies use a cached English working copy before these rules. Booking also requires an explicit unconditional consent decision with an exact current-message excerpt; translated conditions and option changes cannot book. Confirmations, tracking answers and required tracking invitations follow the customer's language. See `context/docs/multilingual-mail.md`.

## Area tests

```
python -m pytest -q tests/test_booking.py tests/test_prebook.py tests/test_tracking.py tests/test_tracking_notice.py tests/test_reply_replay.py
```
