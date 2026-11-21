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

- **Start kit** (`https://api.start-hack.5u.ai/kit/start-kit.zip`): 10 airline rate sheets in mixed formats, a carrier list with the email addresses of airlines and truckers, and the pickup zones. Some airlines appear only in the sheets and others only on the portal.
- **API:** the base URL is `https://api.start-hack.5u.ai`, with Swagger at `/docs` and the spec at `/openapi.json`. One team key covers the mailbox, portal and gateway. Send it as the `X-API-Key` header (or `Authorization: Bearer`). The key is kept out of the repo.
- **Mailbox** (one per team; every mail in and out is logged):
  - `GET /v1/mailbox/messages?since_id=&limit=`: poll, oldest first, at most 200 per call. Pass `next_since_id` on the next call. There is no push; poll every 1–2 s. `references` holds the thread's IDs from the first mail down to the parent.
  - `POST /v1/mailbox/messages` with `{to, subject, body, in_reply_to, attachments}`: send or reply. Limit: 60 mails/min. We may write only to customers, the carrier-list addresses, `ratedesk@hackloop.mail` and `tracking@hackloop.mail` (anything else gets 422).
  - `GET /v1/mailbox/messages/{id}` reads one mail, and `GET /v1/mailbox/messages/{id}/attachments/{attachment_id}` downloads an attachment.
- **Rate portal**, three ways in:
  - API:
    - `GET /v1/portal/ports`: the lanes and their airlines. A destination that is missing here is priced by email to the airline.
    - `GET /v1/portal/brackets?airline=`: an airline's weight breaks. An airline that is not on the portal returns 404 with its email address.
    - `GET /v1/portal/rates?origin=&destination=&airline=&weight_kg=&volume_cbm=`: one priced rate, with `rate_id`, chargeable weight, `total`, `currency`, `transit_days` and `valid_until`. A rate ID is valid for 15 minutes. Rate reads are capped per minute: over the cap you get 429 with `Retry-After`, and the `X-RateLimit-*` headers show the cap and what is left. A 503 says where to get the rate instead.
    - `POST /v1/portal/bookings` with `{rate_id, team_reference}`: book. 201 means booked, 200 is a repeat, 404 is an unknown rate ID, 409 means it is booked under another reference, 410 means expired, and 503 means book by email with the airline. The airline then mails "Booking <ref> confirmed". There is no status call, so our CRM is the record.
  - Browser page `https://start-hack.5u.ai/portal/ui`, behind a bot challenge. Do not try to bypass it.
  - Email desk: `ratedesk@hackloop.mail`.
- **Customer agents:** one sends RFQs and follow-ups. The other asks tracking questions from `tracking@hackloop.mail`.
- **Carrier agents:** 2–5 per destination answer bid requests by email. Airlines also send monthly tariffs; truckers do not.
- **LLM gateway:** one key per team, OpenAI models only, used with our own OpenAI key. It counts each team's cost.
  - Store our OpenAI key once with `POST /v1/gateway/openai-key`.
  - Then use `https://api.start-hack.5u.ai/v1/gateway` as the OpenAI base URL, with the team key as the API key. It serves `chat/completions`, `responses` and `embeddings` (streaming supported), and `GET /v1/gateway/models` lists the allowed models.
  - A quote made without any gateway call earns no cost points.
- **Leaderboard:** each team's score per email and in total. The page is `https://api.start-hack.5u.ai/board`. `GET /v1/board` is public. `GET /v1/board/me` gives our verdict, reason code and points for each request, plus our bookings. Cost points are added hourly, up to about an hour after a quote.

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
