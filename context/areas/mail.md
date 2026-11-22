# Area: Mail intake and guardrails

Current behavior only. Why/history: grep `context/history/`. Flag defaults: `app/config.py`.
API fields: `context/docs/mail-format.md`, `context/docs/5u-api.md`.

## Modules

| Module | Role |
|---|---|
| `app/mail/webhook.py`, `webhook_admin.py` | signed `/hooks/mailbox` receiver; subscription CLI |
| `app/mail/poller.py` | fallback polling + `handle()` entry for every inbound mail |
| `app/mail/store.py`, `parse.py` | persist and parse mail objects |
| `app/mail/router.py` | rules-only classification (customer, carrier bid, portal reply, tariff) |
| `app/mail/screening.py` | phishing gate |
| `app/mail/guard.py` | inbound flood/auto-mail guard and outbound loop caps |
| `app/mail/documents.py`, `speech.py` | attachment reading, audio transcription |
| `app/mail/multilingual.py` | cached working copies; originals remain in the CRM; foreign readable documents translated in one batch |
| `app/db.py` | session mapping (`ingest_mail`), `record_outgoing()` |

## Rules

- **Session = one customer-started thread.** Mails map to sessions via known mail IDs in `references`; ratedesk replies are not threaded and fall back to subject matching. A header-less customer mail with a reply prefix (`Re:`/`AW:`) and the subject of an open session of the same sender (last 2 h) joins that session. A customer reply whose ancestors we do not know opens **no** session: it is stored unassigned as an `orphan_reply` error and listed in "Zu tun". The poller reads the parent mail from our DB before the mailbox API. Every outgoing mail is stored with `db.record_outgoing()`.
- **Ingress:** webhook and fallback polling only persist into `mail_delivery` (webhook acks within 5 s), deduplicated by exact ID. **One consumer** (`webhook.run`) processes the queue in mail-id order, so a reply never runs before its root is stored. Carrier bids run in a bounded pool (`poller.BID_WORKERS`). Pending work resumes after restart; ambiguous dispatches become `unknown` with a warning. Only polling advances its cursor. Never rotate the signing secret on startup (`python -m app.mail.webhook_admin`).
- **Polling limit:** one poll per 5 s per team key. Nobody polls the mailbox with the team key except the live system; a 429 keeps the server's wait and never doubles.
- **Unknown sender in a new thread = RFQ.** Mail that 5U's agents did not start is ignored.
- **Phishing gate** runs in `poller.handle` (webhook, poll, restart retries) before tariff queueing, attachment download, storage as work and dispatch. Layer A rules; Layer B `gpt-5-nano` only when unsure and `PHISHING_GATE_LLM_MODE` is not `off` (the default: no call; off session budget, 2 s, error = unsure). Blocked mail is stored as `phishing` with a release action in "Zu tun"; `hackloop.send` refuses replies to it, and holds a reply when the check itself fails. Text addressed to an AI and loop instructions are strong signals.
- **Mail guard** (no LLM, fails open, `MAIL_GUARD_ENABLED`): empty and automatic mail skip every agent; more than `INBOUND_FLOOD_MAX` new RFQs per sender in 10 min are held and end `lost`. Outbound to customers: max 6 mails per address in 120 s, 15 per thread, a third identical text per thread is refused with one #sos error.
- **Attachments are untrusted evidence.** Supported: pdf/docx/xlsx/txt/xls/csv/rtf/odt/ods/eml/doc/msg. Audio tries Groq Whisper (`GROQ_API_KEY`), an allowed gateway audio model, then existing local Whisper in a bounded child (bundled multilingual `base`, no runtime download). Failed/empty/unusable responses fail over; model/quota cooldowns and 60 s total budget apply. Atomic caching preserves spoken-source semantics on replay; EN/DE euro/cents and number words are normalized. Scans use `llm.complete` (outside quote budget, max 5/session). Every source must agree. Kill switch `ATTACHMENT_TRANSCRIPTION=false`; setup in `DEPLOY.md`.
- Airline replies with attachments route as bids when threaded or `[AP-n]`-tagged.
- Processing is idempotent by message ID. Reply in the customer's thread (`in_reply_to`). Mail to our own address gets 422.
- Tests never call 5U or the gateway: `tests/conftest.py` fails any test that tries.
- Voice defaults to multilingual Whisper `base`, auto-detecting the language. Legacy `.en` settings map to multilingual models; Docker rebuild required. Normalization of foreign text preserves values, references, units, negations and original messages.

## Tests (run these while iterating; the full suite only before the PR)

```
python -m pytest -q tests/test_mailbox_webhook.py tests/test_poller.py tests/test_router.py tests/test_phishing_gate.py tests/test_mail_guard.py tests/test_attachment_documents.py tests/test_audio_fallback.py tests/test_links.py tests/test_db.py
```
