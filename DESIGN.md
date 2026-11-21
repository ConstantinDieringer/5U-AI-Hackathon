# DESIGN.md – Austrian Power Forwarder Console

Status: **v0.1, provisional** (created 21.11.2026 from `context/ui-context.md`; maturity L0 → L1).
All values below are deliberate starting points, not brand-approved. Change them here first,
then in `app/ui/static/index.html` (`:root` block). Components use tokens only, never raw values.

## 1. Product & tone

An operations console for a person who checks in at night: *is the system healthy, does any
session need me, how are we scoring?* Calm by default, loud only when action is needed.

Principles
1. **Attention first.** Anything that needs a human (error, paused, deadline < 2 min) is the
   first thing on every screen. Everything else is quiet.
2. **One question per screen.** Live = "what is happening now", CRM = "who did we quote and
   win", Performance = "how are we scoring".
3. **Show the why.** Every quote shows the options compared and why one was chosen.
4. **Numbers are tabular,** right-aligned, with unit and currency.
5. **Dark first,** high contrast text (WCAG AA ≥ 4.5:1 for body text).

## 2. Tokens

### Color – neutrals (dark theme)
| Token | Value | Use |
|---|---|---|
| `--bg` | `#0b0e14` | app background |
| `--surface-1` | `#11151d` | sidebar, cards |
| `--surface-2` | `#171c26` | inputs, hover rows, nested panels |
| `--surface-3` | `#202734` | selected row, pressed |
| `--border` | `#252d3a` | dividers, card borders |
| `--border-strong` | `#334055` | focus-adjacent, table header line |
| `--text` | `#e7ecf3` | primary text |
| `--text-2` | `#a3adbd` | secondary text, labels |
| `--text-3` | `#6b7686` | meta, timestamps, placeholders |

### Color – brand & semantic
| Token | Value | Use |
|---|---|---|
| `--accent` | `#5b8cff` | primary action, focus ring, active nav |
| `--accent-weak` | `rgba(91,140,255,.14)` | accent backgrounds |
| `--success` | `#3ccf8e` | won, booked, on target |
| `--warning` | `#f4b740` | attention, < 2 min, paused |
| `--danger` | `#f0565c` | error, lost, expired, stop-all |
| `--info` | `#a07bff` | quoted (in progress with customer) |
| `--*-weak` | same hue at 14 % alpha | chip / banner backgrounds |

Status → color mapping is fixed: `new`/`pricing` = neutral/accent, `quoted` = info,
`accepted`/`booked`/`confirmed`/`answered` = success, `lost`/`expired`/`error` = danger,
`paused` = warning, `manual` (human took over) = warning.

### Typography
Font: system UI stack (`ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto`),
mono: `ui-monospace, SFMono-Regular, Menlo`.
| Token | Size / line-height / weight | Use |
|---|---|---|
| `--fs-xs` | 11 / 16 / 500 | overline labels (uppercase, +0.06em) |
| `--fs-sm` | 12 / 16 / 400 | meta, table secondary |
| `--fs-md` | 14 / 20 / 400 | body, tables |
| `--fs-lg` | 16 / 24 / 600 | card titles |
| `--fs-xl` | 20 / 28 / 600 | page titles |
| `--fs-2xl` | 28 / 34 / 650 | KPI values |
Numbers: `font-variant-numeric: tabular-nums`.

### Spacing (4-pt scale)
`--sp-1` 4 · `--sp-2` 8 · `--sp-3` 12 · `--sp-4` 16 · `--sp-5` 20 · `--sp-6` 24 · `--sp-8` 32

### Radius (grows with depth)
`--r-sm` 6 (chips, inputs, buttons) · `--r-md` 10 (cards) · `--r-lg` 14 (drawers, dialogs)

### Elevation
`--shadow-1` `0 1px 0 rgba(255,255,255,.03) inset` (cards) ·
`--shadow-2` `0 12px 32px rgba(0,0,0,.45)` (drawer, dialog)

### Motion
`--dur-fast` 120 ms (hover) · `--dur` 200 ms (drawer, tab) · easing `cubic-bezier(.2,.8,.2,1)`.
New live events flash `--accent-weak` for 1.2 s. Respect `prefers-reduced-motion`.

### Icons
One set: inline SVG, 24-grid, 2 px stroke, round caps (Lucide style), rendered at 16 px.

## 3. Layout

- **App shell:** fixed left sidebar 220 px (logo, nav, live status, stop-all at the bottom) +
  main area. Below 960 px the sidebar collapses to a 56 px icon rail.
- **Page header:** title + one-line context + page actions, 24 px padding.
- **Content max width:** none for Live (master-detail), 1280 px for CRM and Performance.
- **Grid:** 12 columns, 16 px gutter; KPI row = 4 equal cards.

Screens
| Screen | Question | Pattern |
|---|---|---|
| Live | What needs me now? | master-detail: session list (360 px) + session detail |
| CRM | Who did we quote, win, lose? | KPI row + segmented tabs (Kunden · Quotes · Buchungen) + table + detail drawer |
| Performance | How are we scoring? | KPI row (points) + 2 charts + verdict list |

## 4. Components

| Component | Variants / states | Notes |
|---|---|---|
| Button | `primary`, `secondary`, `ghost`, `danger`; hover, focus-visible ring 2 px `--accent`, disabled 40 % | 32 px high, `--r-sm` |
| Chip (status) | one per status color, optional dot | 20 px high, `--fs-xs`, weak bg + strong text |
| Card | default, `attention` (left 3 px `--warning`) | `--surface-1`, `--border`, `--r-md`, 16 px padding |
| KPI | value, label, delta/target line, tone (ok/warn/bad/neutral) | value `--fs-2xl` |
| Table | sortable header, hover row, selected row, empty state | 40 px rows, numbers right |
| Segmented tabs | default, active | for CRM sub-views |
| List row (session) | default, hover, selected, attention | title, chips, countdown, last event |
| Countdown | normal, warning < 2:00, danger < 1:00, done | mono |
| Timeline item | info, warn, error, new (flash) | time · message · duration/cost |
| Drawer | right, 480 px | customer detail |
| Banner | danger (global stop), warning | full width under page header |
| Bar chart | single series, target lines | CSS only, no library |
| Empty state | icon + one sentence + optional action | never a blank area |

## 5. Copy

German UI, short verbs on buttons ("Pausieren", "Übernehmen", "Antworten"). Destructive or
irreversible actions confirm and say why ("Nur die erste Quote zählt").
Mail text is untrusted: always rendered as text, never HTML.
