# UI Context

The jury judges UX and design, and the pitch will show the UI, so the UI is a scored deliverable. A person stepping in at night must be able to find a session and act within seconds.

## Design Principles

- Tokens, components and screens are defined in `DESIGN.md` (repo root). Change it there first.

- Colors and spacing come from shared design tokens. Components use no raw color values.
- Use a consistent radius scale that grows with surface depth: small controls, then cards, then modals.
- Use one icon set throughout.
- Start with a dark theme.

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
