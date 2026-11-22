# Code Standards

## General

- Keep modules small and single-purpose.
- Fix root causes. Do not layer workarounds.
- Do not mix unrelated concerns in one module or handler.
- Name modules after the responsibility they hold, not the technology.

## Types and Validation

- Use strict typing throughout, with strict mode wherever the language has one.
- No untyped escape hatches (any-style types, unchecked casts). Use explicit, narrowly scoped types.
- Validate external input at the boundary before trusting it: mail, attachments, rate sheets, portal and gateway responses, LLM output, UI requests.
- Return consistent, predictable response shapes.

## Money and Pricing

- Never use floats for money. Use integer minor units or a decimal type. Portal amounts arrive as strings, so parse them straight into that type.
- Currency always travels with an amount. Convert between currencies only explicitly, and record the rate used.
- Rounding happens in one place.
- Pricing is deterministic code, never the LLM. The LLM may extract fields from mail, but it never computes or chooses a price.
- A quote is one all-in total plus its currency.
- Every rate-sheet trap we find gets a unit test.

## LLM Use

- Call LLMs only through the 5U gateway, and only OpenAI models.
- User-authorized exception for speech recognition: Groq's Whisper transcription API and local Whisper may read audio attachments. No general chat/extraction/pricing calls move outside the gateway. Never log the Groq key or raw provider errors.
- Make at least one gateway call per quote, because a quote made without one earns no cost points. Keep it as cheap as possible: the smallest model that works, short prompts, and no call where rules suffice.
- Validate structured output against a schema before use.
- Log the tokens and cost of each call against its request.

## Mail Handling

- Processing is idempotent by message ID: handling the same mail twice has no extra effect.
- Reply in the customer's thread (`in_reply_to`). Never send a second quote in a thread.
- Ignore mail that 5U's agents did not start.
- Blocking gaps: history/CRM first, then a persisted threaded clarification (`context/areas/rfq.md`). Approvals must be current and explicit.
- Customer-facing text follows the hard style rules in `context/areas/customer-email.md`.
- Time budget: aim for under 2 minutes from request to quote. The hard limit is 6.

## External APIs

- Throttle and cache for the portal's rate limit, and honor `Retry-After` on a 429.
- Before booking, re-fetch a rate ID that is older than 15 minutes.
- Never confirm a booking price to the customer that the airline has not confirmed. A changed price is put to the customer first; the agent never rebooks on its own.
- Retry transient failures with backoff.
- Never bypass the portal page's bot challenge. Use the API or the email desk. Only a person may solve it, in the UI Portal-Check; no code reads or solves the sum.

## Config and Secrets

- Anything that could change hourly (addresses, zones, sheet formats, model names, limits) lives in config or data, never hardcoded.
- Secrets (the team API key, the OpenAI key) never go into the repo or the logs.
