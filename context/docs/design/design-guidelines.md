# Design Guidelines (historical Sessions UI exploration)

> Historical reference only. `DESIGN.md` v0.2 is the current source of truth. Where this document
> differs from `DESIGN.md`, follow `DESIGN.md`.

Derived on 21.11.2026 from the open-source CRM
[Twenty](https://github.com/twentyhq/twenty) (`packages/twenty-ui/src/theme`, `Button.module.scss`,
`Tag.module.scss`, `twenty-front/src/index.css`). We take its design language (calm, dense, flat,
neutral greys, one blue accent), not its code, logo or name.

## 1. Principles

1. **Content first, chrome quiet.** Neutral grey surfaces, hairline borders, no gradients, no glow. Colour
   only carries meaning (status, accent, danger).
2. **Dense but readable.** 13 px base text, 32 px rows and controls, 4 px spacing grid. A night operator
   scans many sessions at once.
3. **Flat hierarchy through surface steps.** Depth comes from four background steps and 1 px borders,
   not from shadows. Shadows only on floating elements (menus, toasts).
4. **One accent.** Blue marks the primary action, the selected item and focus. Never use it for status.
5. **Status as soft tags.** Tinted background plus tinted text, 20 px high, 4 px radius, no border.
6. **Tokens only.** Components never use raw colours, sizes or radii. Everything comes from `:root`.

## 2. Tokens

All tokens live in `:root` (dark, default) and `[data-theme="light"]`. Names follow Twenty's
`--t-*` scheme, shortened.

### Colour: surfaces and text

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `--bg-primary` | `#171717` | `#ffffff` | page panel, cards, table body |
| `--bg-secondary` | `#1b1b1b` | `#fcfcfc` | sidebar, page header |
| `--bg-tertiary` | `#1d1d1d` | `#f1f1f1` | app background, hover |
| `--bg-quaternary` | `#222222` | `#ebebeb` | pressed, selected row |
| `--bg-trans-light` | `rgba(255,255,255,.06)` | `rgba(0,0,0,.04)` | ghost hover |
| `--bg-trans-medium` | `rgba(255,255,255,.10)` | `rgba(0,0,0,.08)` | ghost active |
| `--border-strong` | `#484848` | `#d6d6d6` | inputs, outline buttons |
| `--border-medium` | `#222222` | `#ebebeb` | cards, panels |
| `--border-light` | `#1d1d1d` | `#f1f1f1` | table rows, dividers |
| `--text-primary` | `#ebebeb` | `#333333` | body, titles |
| `--text-secondary` | `#b3b3b3` | `#666666` | buttons, secondary info |
| `--text-tertiary` | `#818181` | `#999999` | table headers, labels |
| `--text-light` | `#666666` | `#b3b3b3` | timestamps, hints |
| `--text-inverted` | `#171717` | `#ffffff` | on solid buttons |

### Colour: accent, danger, tags

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `--accent` | `#4864d6` | `#4864d6` | primary button, selection bar, focus ring |
| `--accent-text` | `#a1b0ff` | `#415ac1` | links, accent text |
| `--accent-bg` | `#1b2446` | `#eef2fd` | selected item, accent tag |
| `--danger` | `#d4544f` | `#d4544f` | destructive action, error text |
| `--danger-bg` | `#361519` | `#fbecec` | danger surfaces |

Tag pairs (`--tag-<color>-text` / `--tag-<color>-bg`): `gray`, `blue`, `purple`, `green`, `orange`,
`amber`, `red`. Status mapping:

| Status | Tag | Status | Tag |
| --- | --- | --- | --- |
| new | gray | booked, confirmed, answered, accepted | green |
| pricing | blue | paused | amber |
| quoted | purple | manual (human) | orange |
| error, lost, expired | red | | |

### Typography

- Font: `Inter`, fallback `ui-sans-serif, system-ui`. Numbers: `font-variant-numeric: tabular-nums`.
  Monospace only for ids, timestamps and countdowns.
- Base `html { font-size: 13px }`. Scale (rem): `xs .85` (11px), `sm .92` (12px), `md 1` (13px),
  `lg 1.23` (16px), `xl 1.54` (20px), `xxl 1.85` (24px).
- Weights: 400 regular, 500 medium (buttons, table headers, labels), 600 semibold (titles, KPI values).
  Never 700+.
- Line height 1.5 for text, 1.1 for single-line UI (buttons, tags).

### Spacing, size, radius, motion

- Spacing: multiples of 4 px: `--sp-1` 4 · `--sp-2` 8 · `--sp-3` 12 · `--sp-4` 16 · `--sp-6` 24 ·
  `--sp-8` 32. Half steps 2 / 6 px only inside tags and buttons.
- Control heights: 32 px (md), 24 px (sm). Table rows 32 px, list rows 2 lines.
- Radius grows with depth: `--r-xs` 2 (bars), `--r-sm` 4 (tags, nav items, inputs),
  `--r-md` 8 (buttons, cards, panels), `--r-lg` 16 (modals). Pills (`999px`) only for the live dot.
- Icons: one line-icon set (Tabler/Lucide style, 24 viewBox), stroke 2 at 16 px, 1.6 at 14 px.
- Motion: background transitions `0.1s ease`; instant 75 ms, fast 150 ms, normal 300 ms. Respect
  `prefers-reduced-motion`.

## 3. Layout

```
┌──────────┬─────────────────────────────────────────────┐
│ sidebar  │ page header: title · KPIs · actions  (48px) │
│ 220px    ├─────────────────────────────────────────────┤
│ bg-2     │ page panel (bg-1, border-medium, r-md)      │
│          │                                             │
│ nav      │                                             │
│ ...      │                                             │
│ stop btn │                                             │
└──────────┴─────────────────────────────────────────────┘
          app background bg-3, 8px gap around the panel
```

- **Sidebar** 220 px: workspace row (square logo mark + name), live status, nav items (28 px high,
  icon + label, `r-sm`, hover `bg-trans-light`, active `bg-trans-medium` + `text-primary`), footer with
  global stop and theme switch.
- **Page panel**: one rounded panel (`r-md`, 1 px `border-medium`) on the `bg-tertiary` app background.
- **Sessions view**: list column 360 px with a search field and filter tags, detail on the right.
- Below 900 px: sidebar collapses to a top bar, columns stack.

## 4. Components

| Component | Rule |
| --- | --- |
| Button (secondary, default) | 32 px, padding 0 8 px, `r-md`, bg `bg-trans-lighter`, 1 px `bg-trans-light` border, `text-secondary`, 500; hover `bg-tertiary`; active `bg-quaternary` |
| Button primary | bg `--accent`, white text, same size; one per view at most |
| Button danger | text `--danger`, border `--danger` at 40 %; "on" state solid `--danger` |
| Button ghost | transparent, hover `bg-trans-light` |
| Tag / status | 20 px, padding 0 8 px, `r-sm`, 400, `--tag-*` pair, no border |
| Filter tag | like a tag; active = `accent-bg` + `accent-text` |
| Input / search | 32 px, `r-sm`, `bg-transparent`, 1 px `border-medium`, focus ring 1 px `--accent` |
| Card | `bg-primary`, 1 px `border-medium`, `r-md`, padding 16 px; title 12 px 500 `text-tertiary` with 14 px icon |
| Table | header 12 px 500 `text-tertiary`; rows 32 px, 1 px `border-light`, cell padding 8 px; numbers right, tabular |
| List row | 2 lines, padding 8 × 12 px, hover `bg-tertiary`, selected `bg-quaternary` + 2 px `--accent` bar left |
| Notice | `r-md`, tag background of its colour, text in the tag colour |
| Countdown | mono, `text-tertiary`; < 120 s `--tag-orange-text`; < 60 s `--danger`, 600 |
| Focus | `outline: 1px solid var(--accent); outline-offset: 1px` on every interactive element |

## 5. Writing

- German UI copy, sentence case, no exclamation marks, verbs on buttons ("Pausieren", "Übernehmen").
- Empty states say what will appear and when ("Noch keine Quotes.").
- Mail bodies are untrusted: render only via `textContent`.

## 6. Checklist for UI changes

- [ ] No raw colour, size or radius outside `:root` / `[data-theme="light"]`.
- [ ] Works in dark and light, and at 900 px width.
- [ ] Status shown as a tag from the mapping above; accent never used for status.
- [ ] Every button and row reachable by keyboard with a visible focus ring.
