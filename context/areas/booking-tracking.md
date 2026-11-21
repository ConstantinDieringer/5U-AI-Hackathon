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
- Book fast: a re-fetched rate after 15 minutes may change the outcome. Book by rate ID; 503 means book by email with the airline. There is no status call, so our CRM is the record.
- **Price changes:** confirm the customer only once the airline price is known (portal response, airline confirmation mail, or after `AIRLINE_CONFIRM_WAIT_S`=180). A deviation beyond `PRICE_CHANGE_TOLERANCE_PCT`=0.5 asks the customer about the new all-in total and names one cheaper alternative; yes confirms, anything else is `needs_human`. Never rebook automatically (the portal has no cancel).
- Airline booking emails carry the frozen air cost only, never truck costs. Customer confirmations keep the all-in total. State in `bookings.customer_state`.
- **Tracking:** prose that says where the cargo is (at the shipper before the ready date, planned departure/arrival from `rfq.schedule`), greets the asker by first name. Matching uses reference, then customer/route/weight; on a tie ask for the BK reference instead of guessing. Tracking reports `bookings.price_minor` (air only on portal door quotes).
- Never send a second quote in a thread.

## Tests (run these while iterating; the full suite only before the PR)

```
python -m pytest -q tests/test_booking.py tests/test_prebook.py tests/test_tracking.py tests/test_tracking_notice.py tests/test_reply_replay.py
```
