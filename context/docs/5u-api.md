# 5U API and environment reference

What 5U provides (mailbox, portal, gateway, board). Read only for API work; exact mail fields: `mail-format.md`; schema: `https://api.start-hack.5u.ai/openapi.json`.

- **Start kit** (`https://api.start-hack.5u.ai/kit/start-kit.zip`): 10 airline rate sheets in mixed formats, a carrier list with the email addresses of airlines and truckers, and the pickup zones. Some airlines appear only in the sheets and others only on the portal.
- **API:** the base URL is `https://api.start-hack.5u.ai`, with Swagger at `/docs` and the spec at `/openapi.json`. One team key covers the mailbox, portal and gateway. Send it as the `X-API-Key` header (or `Authorization: Bearer`). The key is kept out of the repo.
- **Mailbox** (one per team; every mail in and out is logged):
  - Signed webhooks are available via `PUT /v1/mailbox/webhook`; our receiver is `/hooks/mailbox`. Persist then acknowledge within 5 s, process in the background. `GET /v1/mailbox/messages?since_id=&limit=` remains the fallback, oldest first, at most 200 per call. Pass `next_since_id` on the next call; rate limits now apply to polling. `references` holds the thread's IDs from the first mail down to the parent.
  - `POST /v1/mailbox/messages` with `{to, subject, body, in_reply_to, attachments}`: send or reply. Limit: 60 mails/min. We may write only to customers, the carrier-list addresses, `ratedesk@hackloop.mail` and `tracking@hackloop.mail` (anything else gets 422).
  - `GET /v1/mailbox/messages/{id}` reads one mail, and `GET /v1/mailbox/messages/{id}/attachments/{attachment_id}` downloads an attachment.
- **Rate portal**, three ways in:
  - API:
    - `GET /v1/portal/ports`: the lanes and their airlines. A destination that is missing here is priced by email to the airline.
    - `GET /v1/portal/brackets?airline=`: an airline's weight breaks. An airline that is not on the portal returns 404 with its email address.
    - `GET /v1/portal/rates?origin=&destination=&airline=&weight_kg=&volume_cbm=`: one priced rate, with `rate_id`, chargeable weight, `total`, `currency`, `transit_days` and `valid_until`. A rate ID is valid for 15 minutes. Rate reads are capped per minute (10 at 15:00): over the cap you get 429 with `Retry-After`, and the `X-RateLimit-*` headers show the cap and what is left. A 503 says where to get the rate instead. The portal prices exactly the weight asked, in that weight's bracket.
    - Coverage at 15:00: 114 lanes, 15 airlines, none of them in the rate sheets (snapshot in `data/portal_lanes.json`).
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
