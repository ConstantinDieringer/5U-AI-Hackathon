# Area: Customer email (composer, style, send path)

Current behavior only. Why/history: grep `context/history/`. Flag defaults: `app/config.py`.
Wording changes: use `.agents/skills/customer-email-voice/SKILL.md` and `app/mail/customer_voice.md`.

## Modules

| Module | Role |
|---|---|
| `app/hackloop.py` (`send_customer`) | the only customer send path: render, assert, transport, record |
| `app/mail/outgoing.py` | final deterministic presentation and assertion (`EMAIL_STYLE_VERSION`) |
| `app/mail/style.py`, `subjects.py`, `language.py` | dates, currency, paragraphs, subjects, language detection |
| `app/mail/voice.py` + `customer_voice.md` | shared voice profile loaded into the composer prompt |
| `app/agents/email_review.py` | optional style reviewer (`EMAIL_REVIEW_ENABLED=false`) |
| `app/agents/tracking_notice.py` | repairs a missing tracking paragraph |
| `app/mail/quote_pdf.py` | quote PDF attachment (`QUOTE_PDF`), rendered by code from the priced option, no LLM |
| `app/mail/reply_language.py` | send-time translation into any customer language but de/en (`REPLY_IN_CUSTOMER_LANGUAGE`), original plus warn event on doubt |

## Hard style rules (customer body and subject)

- No colons, semicolons, underscores, dash punctuation or parentheses, including Unicode/HTML equivalents. No lists.
- Routes use `to`/`nach`; whole euro totals as `1.475 €`, nonzero cents remain. Dates use written full month names (EN/DE).
- Sign-off is exactly `Your austrian Power Team`.
- No backend field/enum/column names (e.g. `road_transit`); timing labels in ordinary language.
- Only true reference numbers, filenames and contacts keep their original punctuation. Stored/API currency codes stay unchanged.
- Short paragraphs, friendly complete sentences, one EUR all-in total per quote, one next step.

## Send path (wire contract v4)

- Body AND subject are rendered and independently asserted immediately before sending; Unicode punctuation and HTML entities cannot bypass the rules. Re/Fw prefixes are removed without changing `in_reply_to`.
- The actually submitted body and subject are persisted. Carrier/trucker machine messages are unchanged.
- Quotes, booking confirmations and follow-ups name the verified current airport route in the subject; missing route facts keep the existing subject.
- Every RFQ customer send inserts a deterministic reply-by-email tracking paragraph before the signature (conditional before booking, direct after). `tracking_notice` checks active quotes/bookings every 30 s and repairs at most one missing notice; ambiguous transport is alerted, never resent.

## Quote PDF

- 5U requires the offer as a PDF. Every RFQ quote (and a Notfall-Quote from the UI) is sent with `Angebot_AP-n.pdf` (title/labels in de/en/fr/es/it, else en) via `attachments[]` on the mailbox API.
- Only code-priced facts: reference, route, airline, transit, pickup, cargo, billed kg, validity and the same all-in total as the body (`style.amount`). No LLM call, no extra cost; the body keeps the total too.
- A PDF fault never blocks the quote: it is sent without the attachment and an error event (#sos) is written. Off switch `QUOTE_PDF=false`.

## Composer

- Every quote uses `LLM_COMPOSE_MODEL` (`gpt-5-mini`) with bounded current-thread and earlier same-customer correspondence; older monetary amounts are removed. A failed check sends the deterministic quote copy; the second composer call is opt-in (`COMPOSE_REPAIR`, off for cost). Only verified current facts support price/service promises.
- Language: the request language (ISO code, current mail/attachment prose before signature/history) reaches composer and repair; mismatched output triggers repair. Deterministic quotes, months and subjects support EN/DE/FR/ES/IT; other languages use the English copy, translated at send by `reply_language.py` (English plus warn event if the checks fail), never no quote.
- Voice: close, cheerful best-friend tone; German defaults to du unless formal address is requested. No invented shared history.
- One validated repair attempt, then the deterministic template fallback. Fallback templates reject placeholders and embedded headers.
- Never promise unverified cargo cutoffs; a requested cutoff is stated as unconfirmed.
- Exclusions are acknowledged generically; excluded carriers are never named.
- The final LLM reviewer is disabled by default (user request); deterministic rendering and final validation remain.
