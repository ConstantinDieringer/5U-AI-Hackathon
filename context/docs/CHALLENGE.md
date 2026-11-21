# 5U AI Challenge — START HACK Munich 2026 (21.11.2026)

Source documents (originals in this folder):
- [5U-AI-challenge-brief.pdf](5U-AI-challenge-brief.pdf) — official challenge brief (4 pages)
- [5U-AI-challenge-deep-dive.pdf](5U-AI-challenge-deep-dive.pdf) — kickoff deck (13 slides)

## Goal

Build a **digital air forwarder** that quotes, books and tracks air export shipments through **one mailbox**, autonomously, all night.
A new air export RFQ reaches every team **every 6 minutes**; each one **expires 6 minutes later**.

Domain: a forwarder buys transport on the customer's behalf. Customer sends an RFQ by email → forwarder prices it (rate sheets, portal, carrier bids) → quotes → books → tracks. Carriers (airlines, truckers) bid by email.

## The task

1. Read every mail and **route** it: customer request, carrier bid, portal reply or tariff sheet.
2. **Price** the request from the rate sheets, the rate portal and bids from 2–5 carriers.
3. **Quote one option** — the one the customer asked for: *cheapest* or *fastest*.
4. **Book by rate ID** when the customer accepts, and keep the customer informed.
5. Answer **tracking** questions from the team's own CRM.
6. Reply **in the customer's thread**. Only the **first quote in a thread** counts.
7. Every quote states **one all-in total with its currency**.

Flow per request: **QUOTE** (RFQ arrives → check rates → ask 2–5 carriers → quote within 6 min) → **BOOK** (customer accepts → book with rate ID → confirm) → **TRACK** (customer asks status → answer from CRM).

## What we build

| Part | Description |
|---|---|
| Router + up to 3 agents | RFQ & booking agent, track & trace agent, tariff ingestion agent |
| Pricing module | Holds tariffs as structured prices; ingestion of new tariffs is automatic |
| CRM | Quotes and booked shipments: history, won/lost, a dashboard |
| Sessions UI with streaming | Where a human steps into the autonomous system — finding a session and intervening must be easy |

## What 5U provides

| Part | What it does |
|---|---|
| Start kit | 10 airline rate sheets in mixed formats, carrier list (airline + trucker email addresses), pickup zones. Some airlines only in the sheets, others only on the portal |
| Mailbox | One per team. Every mail in and out is logged |
| Rate portal | API with rate IDs and booking (**rate limited**; a **rate ID is valid for 15 min**), a browser page behind a bot challenge, and an email desk at `ratedesk@hackloop.mail` |
| Customer agents | One sends RFQs and follow-ups; one asks tracking questions from `tracking@hackloop.mail` |
| Carrier agents | 2–5 per destination answer bid requests by email. Airlines also send **monthly tariffs**, truckers do not |
| LLM gateway | One key per team, **OpenAI models only** (own OpenAI key). Counts each team's cost. **A quote made without a gateway call earns no cost points** |
| Leaderboard | Each team's score per email and in total. Who booked what stays visible |
| API docs | Swagger for mailbox, portal and gateway at the Base URL on the kit card, under `/docs` |

5U touchpoints: **Mailbox, Rate portal, LLM gateway.** 5U runs: request engine, customer agents, carrier agents, scorer & leaderboard.

## Scope

- **In:** air export RFQs only, with a door-to-port option (→ trucker leg for pickup).
- **Out:** building a TMS, margins, ocean freight, competition between teams.

## Rules

- Every team gets the same request at the same time and is scored on its own.
- No second chance: a missed or rejected request is lost.
- Only mail started by 5U's own agents is scored.
- After a few hours of build time, **the setup changes about once an hour, unannounced**. Teams learn of a change from failed quotes → system must be robust/adaptive.

## Scoring (per accepted quote, max 100)

| Criterion | Brief (PDF) | Deck (kickoff) | Detail |
|---|---|---|---|
| Price accuracy | **45** | 55 | Full at best price, fewer points the further above |
| LLM cost | **40** | 30 | Gateway spend per accepted quote in the hour. Full at 0 spend (but ≥1 gateway call needed), zero at **0.10 USD** |
| Response time | **15** | 15 | Full within **2 min** of the request, zero at **6 min** |

> ⚠️ Brief and deck disagree on the weights (45/40/15 vs. 55/30/15) — clarify with 5U.

**Accepted** = within **5 % of the best price**, first reply in thread only, within 6 minutes. Only accepted quotes score.

- Among carrier bids, the right one is the **most balanced: price and speed weighted 50/50**.
- "Read the rate sheets closely. The best price is not always the obvious one." (surcharges, weight breaks, chargeable weight, validity, etc.)

**Bonus:** +10 for a correct tracking answer on a booked shipment · +10 (jury) for the accepted email that reads most human.

## Winner selection

Leaderboard alone does not decide. Final pitch: **3 min + 2 min Q&A**.
Jury criteria (brief): 1. pitch, 2. UX & design, 3. codebase, 4. how human the emails read.
Deck lists: pitch, UX, leaderboard, tech depth.

Prizes: 2nd — Hans Zimmer concert tickets in Munich; 3rd — exclusive founders dinner.

## Design implications (our notes)

- **Minimise LLM spend**: deterministic parsing/pricing wherever possible; use the cheapest OpenAI model only for routing/extraction/email writing, but always make ≥1 gateway call per quote.
- **Speed**: aim to quote < 2 min — carrier bids take time, so decide on a cutoff and quote from sheets/portal if bids are late.
- **Rate IDs expire after 15 min** → re-fetch before booking if needed; portal is rate limited → cache.
- **Hourly unannounced changes** → log failures, monitor quote acceptance, make parsers tolerant.
- Human-sounding, multilingual replies in the original thread.
