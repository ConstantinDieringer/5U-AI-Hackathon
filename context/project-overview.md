# Project Overview

Team **Austrian Power**, 5U AI challenge, START HACK Munich 2026. Sources: the challenge brief and [docs/CHALLENGE.md](docs/CHALLENGE.md).

## Overview

We build a digital air freight forwarder. It runs one mailbox (`austrian-power@hackloop.mail`) on its own through the night, and it quotes, books and tracks air export shipments. A new RFQ arrives every 6 minutes, and each one expires 6 minutes later.

## Goals

1. Read every mail, in any language, and route it as one of: customer request, carrier bid, portal reply, tariff sheet.
2. Price each request from the rate sheets, the rate portal, and bids from 2 to 5 carriers.
3. Quote exactly one option, the one the customer asked for: cheapest or fastest.
4. When the customer accepts, book by rate ID and keep the customer informed.
5. Answer tracking questions from our own CRM.
6. Reply in the customer's thread. Only our first quote in a thread counts.
7. Make every quote state one all-in total with its currency.

## Core Flow

RFQ in → route → price → quote → customer accepts → book by rate ID → confirm to customer → answer tracking questions

## Required Components

- A router and up to three agents: RFQ and booking, track and trace, tariff ingestion.
- A pricing module that holds tariffs as structured prices and ingests new tariffs automatically.
- A CRM of quotes and booked shipments, with history, won/lost and a dashboard.
- A sessions UI with streaming, where a person steps into the autonomous system. Finding a session and intervening must be easy.

## What 5U Provides

Start kit, mailbox, rate portal, customer/carrier agents, LLM gateway and leaderboard. Endpoints, limits and status codes: [docs/5u-api.md](docs/5u-api.md).

## Scope

- **In:** air export RFQs only, with a door-to-port option (trucking from the customer's pickup zone to the airport).
- **Out:** building a TMS, margins, ocean freight, competition between teams.

## Rules

- Every team gets the same request at the same time and is scored on its own.
- There is no second chance: a missed or rejected request is lost.
- Only mail that 5U's own agents start is scored.
- After a few hours of build time, the setup changes about once an hour, without notice. We learn of a change only from failed quotes.

## Success Criteria (Scoring)

Each accepted quote scores up to 100 points:

- **Price accuracy, 45:** full points at the best price, fewer the further above it.
- **LLM cost, 40:** gateway spend per accepted quote in the hour. Full points at $0, zero at $0.10.
- **Response time, 15:** full points within 2 minutes of the request, zero at 6 minutes.

What counts:

- A quote is **accepted** only if it is within 5% of the best price, and only accepted quotes score.
- Among carrier bids, the right one is the most balanced, with price and speed weighted 50/50.
- Read the rate sheets closely: the best price is not always the obvious one.

Bonus:

- +10 for a correct tracking answer on a booked shipment.
- +10 from the jury for the accepted email that reads most human.

Final ranking: the leaderboard alone does not pick the winner. Each team gives a 3-minute pitch plus 2 minutes of questions. The jury judges the pitch, the UX and design, the codebase, and how human the emails read.
