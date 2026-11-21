# Area: Pricing, tariffs, portal and bids

Current behavior only. Why/history: grep `context/history/`. Flag defaults: `app/config.py`.
Binding detail: TECH_SPEC §6 (data models), portal API in `context/docs/5u-api.md`.

## Modules

| Module | Role |
|---|---|
| `app/pricing/pricing.py` | deterministic price calculation, `most_balanced` |
| `app/pricing/parse_tariffs.py`, `tariff_parser.py` | kit and monthly tariff parsing |
| `app/agents/tariff.py` | durable tariff queue and worker |
| `app/pricing/portal_sync.py`, `lanes.py`, `served_lanes.py` | portal lanes/brackets, lane index, airline "We fly" lists |
| `app/pricing/fx.py`, `audit.py` | explicit EUR/USD conversion, board evidence audit |
| `app/agents/bids.py` | carrier/trucker bid requests, parsing, waits |
| `app/agents/carrier_prefs.py` | per-customer exclusions/preferences |

## Rules

- **Pricing is deterministic code, never the LLM.** Decimal amounts and integer cents; currency travels with every amount. Customer output stays EUR.
- Tracked `data/tariffs.json` and `data/portal_lanes.json` are seed data; runtime tariffs and portal snapshots live in `settings.runtime_dir` on the persistent volume.
- **Tariffs:** one durable SQLite queue and worker; text/table parsed locally, images/scans transcribed through `llm.complete` with no pricing decisions. Sender and values validated before atomic publication and cache reload.
- **Weight breaks:** billing at the next break is often cheaper and the board's best price uses it. Portal next-break check is active; `BID_NEXT_BREAK` asks email airlines for their next break too.
- **Portal:** rate reads are capped per minute; honor `Retry-After` on 429. 503 gives an email fallback (`portal_result()`); eligible portal carriers get reserved bid slots before speculative email requests. Re-fetch a rate ID older than 15 minutes before booking.
- **Bids:** 2 to 5 carriers per destination; carrier fan-out is capped at 5, truckers have their own cap (`MAX_TRUCKER_BIDS`). A decline counts as an answer. Airlines that do not fly the lane are skipped (`served_lanes`).
- **50/50 balance:** `ratio` (price/cheapest + time/fastest) for trucker and airline bids; `TRUCKER_BALANCE=minmax` restores the old rule. Do not pin minmax in Railway.
- **Bid documents:** only an explicit unique total with currency counts (Tier 1 total labels, Tier 2 price labels only without a total line). Never per-unit rates, optional/waiting charges, sub-totals, reference numbers or two different totals. Conflicting sources stay `bid_unreadable`, never guessed. A bid the rules find no total in gets one checked extraction call (`app/agents/bid_reader.py`, `BID_LLM_FALLBACK`, event `bid_model_read`): the amount must appear literally in the carrier's text, EUR/USD only, otherwise `bid_unreadable`.
- **Door-to-port** quotes require a trucker price: a bid, or (`ESTIMATE_MISSING_TRUCK`, event `truck_estimated`) an estimate from the nearest-weight earlier real trucker bid for the same pickup zone, sqrt-scaled, max 4x weight ratio (`app/pricing/truck_estimate.py`). No comparable bid: error as before. `ALLOW_INCOMPLETE_DOOR_QUOTE=true` is the emergency override. Pickup area from ZIP; no guessing from other shipments.
- **Carrier preferences:** excluded carriers are never asked nor quoted; a preferred one wins when priced. Two-letter codes and short names count; a negation reaches only its own clause.
- **Board accuracy:** `45 × (1 − |ours − best| / best / 10 %)`; quoting below best costs too. Only quotes within 5 % are accepted. Every rate-sheet trap gets a unit test.
- **FX/audit:** `RateOption` and `pricing_audit` events keep original currency, tariff version, ordinary/breakpoint totals and the FX evidence. `python -m app.pricing.audit` joins board evidence.

## Tests (run these while iterating; the full suite only before the PR)

```
python -m pytest -q tests/test_pricing.py tests/test_pricing_hardening.py tests/test_parse_tariffs.py tests/test_portal_and_reference.py tests/test_portal_fallback_capacity.py tests/test_served_lanes.py tests/test_trucker_balance.py tests/test_bid_attachments.py tests/test_bid_next_break.py tests/test_document_totals.py tests/test_carrier_prefs.py tests/test_carrier_exclusion_guard.py tests/test_rounds_66_68_offers.py
```
