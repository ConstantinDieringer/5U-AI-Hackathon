# Mail format (5U mailbox API)

Verified on real mail, 21.11 ~15:40 (ids 207/208). Fixtures: `tests/fixtures/poll_ratedesk_reply.json`, `tests/fixtures/sent_ratedesk_request.json`. Parsing: `app/mail/parse.py`.

## Poll: `GET /v1/mailbox/messages?since_id=&limit=`

```json
{"messages": [<mail>, ...], "next_since_id": 208}
```
Oldest first, only mail sent **to** us. `limit` max 200. Pass `next_since_id` as `since_id` next time.

## Mail object (poll item, `GET /v1/mailbox/messages/{id}`, and the 201 reply of a send)

| Field | Type | Example / note |
|---|---|---|
| `id` | int | `208`. Global and growing (not per team) |
| `from` | str | `"ratedesk@hackloop.mail"`. Bare address, no display name seen yet |
| `to` | str | `"austrian-power@hackloop.mail"`. One address, a string, not a list |
| `subject` | str | `"Portal rates FRA-JFK 100.0 kg"` |
| `body` | str | plain text with `\n` |
| `in_reply_to` | int \| null | id of the parent mail |
| `references` | list[int] | thread ids, first mail → parent; `[]` for a new thread |
| `received_at` | str | ISO 8601 UTC with microseconds: `"2026-11-21T14:41:37.007176+00:00"` |
| `attachments` | list | `[]` here. Docs: id, filename, content type, size; download via `GET /v1/mailbox/messages/{id}/attachments/{attachment_id}`. Exact key names not seen yet |

There are no separate message-ID headers. Threading uses only the integer ids in `in_reply_to` and `references`.

## Send: `POST /v1/mailbox/messages`

Body: `{to, subject, body, in_reply_to?, attachments?}`. There is no `references` field, so the server builds the thread from `in_reply_to`. Sending to our own address → `422 "recipient must be a customer, carrier or desk address"`.

## Findings

- **The rate desk reply is NOT threaded:** `in_reply_to: null` and `references: []`, even though it answers our mail 207. It only quotes our subject in the body (`(your mail "Rate request FRA-JFK 100 kg")`). Session mapping via `references` will not work for rate desk replies. Use the quoted subject, or the lane plus the time.
- The rate desk takes about 60 s to reply. It lists portal rates (airline, code, total, currency, transit days, `rate_id`, valid until) and names airlines that are not on the portal, with their email.
- Not verified yet: how `references` looks on a threaded reply (no customer or carrier reply seen yet), and the attachment key names.
