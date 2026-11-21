# Kit card & API — team Austrian Power

Secrets live in `.env` (gitignored). Template: [`.env.example`](../.env.example).

| | |
|---|---|
| Team | `austrian-power` (Austrian Power) |
| Mailbox | `austrian-power@hackloop.mail` |
| Base URL | `https://api.start-hack.5u.ai` |
| Auth | Header `X-API-Key: <key>` (or `Authorization: Bearer <key>`) — one key for mailbox, portal, gateway |
| Swagger | https://api.start-hack.5u.ai/docs · spec: `/openapi.json` |
| Portal UI | https://start-hack.5u.ai/portal/ui (bot challenge) |
| Start kit | `/kit/start-kit.zip` |
| Leaderboard | `/v1/board` (public) · `/v1/board/me` (our verdicts per request, with reason codes) |
| First request | **21.11.2026, 16:00** |

## Endpoints

### Mailbox
- `GET /v1/mailbox/messages?since_id=N&limit=200` — poll. Oldest first, ids only grow. Keep `next_since_id`. **No push — poll every 1–2 s.** `references` = thread ids from first mail to parent. Attachments listed with id/filename/type/size.
- `GET /v1/mailbox/messages/{id}` — one mail.
- `GET /v1/mailbox/messages/{id}/attachments/{att_id}` — raw bytes.
- `POST /v1/mailbox/messages` — body `{to, subject, body, in_reply_to, attachments[]}`.
  - Quote counts **only as reply (`in_reply_to`) in a thread a 5U customer started**, only the first quote.
  - Allowed recipients: customers, airlines + truckers from carrier list, `ratedesk@hackloop.mail`, `tracking@hackloop.mail`. Others → 422.
  - Attachments: max 5, 10 MB each, pdf/xlsx/xls/csv/txt/png/jpg/docx.
  - **Limit 60 mails/min** → 429.

### Portal
- `GET /v1/portal/ports` — all lanes + airlines on them (not rate limited). Destinations missing here → price by email to the airline.
- `GET /v1/portal/brackets?airline=XX` — an airline's weight breaks (not rate limited). Airline not on portal → 404 with email address.
- `GET /v1/portal/rates?origin&destination&airline&weight_kg&volume_cbm` — one priced rate. **Rate limit: 10 reads/min per team** (fixed window, 429 + `Retry-After`; see `X-RateLimit-Remaining`). 503 → message says where to get the rate instead.
  - Returns `rate_id, total, currency, chargeable_weight_kg, transit_days, valid_until (~15 min), dataset_version, fx_eur_per_usd`.
  - Prices **in the bracket of the requested weight** — a higher bracket may be cheaper overall, check it ourselves.
- `POST /v1/portal/bookings` — `{rate_id, team_reference}` (our CRM id). 201 booked · 200 idempotent retry · 404 unknown · 409 booked by other ref · **410 expired → fetch fresh rate** · 503 → book by email with airline. Then an airline mail "Booking <ref> confirmed" arrives. **No status call — keep bookings in our CRM.**

### LLM gateway (OpenAI-compatible)
- Use `base_url = https://api.start-hack.5u.ai/v1/gateway`, `api_key = <team key>`.
- `POST /v1/gateway/openai-key` `{"api_key":"sk-..."}` — store our OpenAI key once (checked with a tiny call). `GET` shows whether one is set.
- `GET /v1/gateway/models`, `POST /chat/completions`, `/responses`, `/embeddings` (streaming supported).
- Cheap candidates: `gpt-5.4-nano`, `gpt-5-nano`, `gpt-4.1-mini`, `gpt-4o-mini`.

## Observed (21.11., ~14:45)
- `/v1/me` ok, mailbox empty, board rank 1 with 0 points (nothing scored yet).
- Portal: **114 lanes**, origins **CGN, DUS, FRA, HAM, MUC, STR**, **15 airlines** on the portal: AU BX FQ GN IB JP LM MD NV PL RV SL TM UV XC. The 10 sheet airlines (Cobalt, Danube, Elbflug, Helix, Kestrel, Orion, Quetzal, Vega, Wayzata, Yellowtail) are not on the portal.
- Sample: JP CGN→DEL, 250 kg / 1.2 cbm → 927.50 EUR, 5 days, `dataset_version v1`, breaks `[0,45,100,300,500,1000]`.
- Gateway: OpenAI key **not yet stored**.

## Start kit contents
- `carrier-list.csv` — name, kind (airline/trucker), email, serves (IATA codes for airlines, postal-zone prefixes for truckers).
- `pickup_zones.csv` — 2-digit postal zone → area → origin airport (FRA, MUC, DUS, HAM, STR, CGN).
- 10 tariff sheets, valid 01.–30.11.2026, mixed formats: pdf (Cobalt, Helix, Orion), jpg ×2 (Danube), xlsx (Elbflug, Kestrel, Vega, Wayzata), docx (Quetzal), txt (Yellowtail).
- Rules seen in Yellowtail sheet: chargeable weight = max(actual, volume × 167 kg/cbm); breaks −45/+45/+100/+250/+500/+1000/+2000; minimum charge on freight before surcharges; fuel + security per kg of billed weight.
