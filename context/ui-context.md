# UI Context

The jury judges UX and design, and the pitch will show the UI, so the UI is a scored deliverable. A person stepping in at night must be able to find a session and act within seconds.

## Design Principles

- Tokens, components and screens are defined in `DESIGN.md` (repo root). Change it there first.
- The earlier Twenty-inspired exploration is retained in
  [docs/design/design-guidelines.md](docs/design/design-guidelines.md) as a historical reference;
  where it differs, `DESIGN.md` wins.

- Colors and spacing come from shared design tokens. Components use no raw color values.
- Use a consistent radius scale that grows with surface depth: small controls, then cards, then modals.
- Use one icon set throughout.
- Dark theme is the default; a light theme switch exists (same tokens).

## Screens (proposed)

### Sessions List

- Find a session fast by customer, thread or request ID.
- Filter by status.
- Live updates as mail and agent steps arrive.

### Session Detail (streaming)

- The mail timeline of the thread.
- The agent steps, streamed as they happen.
- The prices considered, and the one chosen.
- The LLM cost of the session.
- A countdown to the 6-minute expiry.
- Actions: take over, approve, edit the reply.

### CRM Dashboard

- Quotes, won/lost, and bookings.
- Cost and score per quote.

### Tariff View

- The ingested tariffs per airline, held as structured prices.

## Current Behavior

- Screens: Übersicht (Systemcheck lights, step chips, "Zu tun", Punkte) · Live · CRM (customers, won/lost, quotes, bookings, "Preise") · Leistung. Server `app/ui/server.py`, page `app/ui/static/index.html`.
- Manual replies default to the latest incoming customer mail; carrier mails stay explicitly selectable. Customer quotes to other recipients are rejected before send; the confirmation names the recipient. Only customer responses set `first_reply_at`.
- Notfall-Quote (`POST /api/sessions/{id}/draft`, `app/ui/drafts.py`) includes evidenced insurance, blocks incomplete terms, and freezes insurance/air components when sent. It hands back to the agent after sending.
- Übersicht v0.3: status strip, "Fehler & Warnungen" (`GET /api/problems`, ERROR logs as `log_error` events via `app/ui/log_events.py`), "Lagebild/Kontext kopieren" (`/api/context`, `/api/sessions/{id}/context`).
- **Manual fallback export:** `app/ui/ai_context.py` builds one markdown briefing per session (thread, attachment text, state, events, diagnostics, recent log lines from the in-memory `app/ui/logbuffer.py` ring) for pasting into an outside chat model; `context.zip` adds original attachment files. Read only: no download, send or gateway call.
- **Portal-Check** (`app/ui/portal_check.py`, `PORTAL_UI_CHECK`): a person picks a lane airline, solves the portal page sum shown as an image, we submit once and show the rate as text plus a `portal_check_rate` event. Not fed into automatic pricing.
- Deploy light in the top bar (`/api/deploy-gate`); attention list `GET /api/attention`.
- Mail content is rendered only via `textContent`; JSON fields are snake_case.

## Tests (run these while iterating; the full suite only before the PR)

```
python -m pytest -q tests/test_server.py tests/test_manual_reply_targets.py tests/test_dashboard_problems.py tests/test_context_export.py tests/test_alerts.py tests/test_board_watch.py tests/test_api_costs.py
```
