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
- Notfall-Quote (`POST /api/sessions/{id}/draft`, `app/ui/drafts.py`) computes a quote from stored data and hands back to the agent after sending.
- Deploy light in the top bar (`/api/deploy-gate`); attention list `GET /api/attention`.
- Mail content is rendered only via `textContent`; JSON fields are snake_case.
