# DESIGN.md – Austrian Power Forwarder Console

Status: **v0.4** (22.11.2026, Twenty-style restyle). Light, flat, hairline-bordered UI modelled on
the Twenty CRM. Values are the source of truth for the `:root` block in
`app/ui/static/index.html`; components use tokens only, never raw values.

## 1. Product & tone

An operations console someone checks at night and shows in the pitch: *is the system healthy,
does a session need me, how are we scoring?* Calm and airy by default; color only where it
carries meaning (status, data, the one hero KPI).

Principles
1. **Quiet monitoring.** Errors and warnings collect in the message buffer with a passive
   navigation badge. No automatic pop-ups or disappearing error notices interrupt the user.
2. **Dense but calm.** Compact 13 px type, 36 px table rows, one idea per card.
3. **Color with purpose.** Neutral grays; color only for status tags, charts, the primary action
   and the hero KPI value.
4. **Show the why.** Every quote shows the options compared.
5. **Numbers are tabular,** bold for KPIs, with unit and currency.

## 2. Tokens

Visual language follows the Twenty CRM UI (github.com/twentyhq/twenty, `twenty-ui` light theme):
flat white surfaces, hairline borders instead of shadows, small radii, 13 px Inter, quiet grays
and pastel tags. Only visual tokens are borrowed; no Twenty code or assets.

### Canvas & surfaces
| Token | Value | Use |
|---|---|---|
| `--canvas` | `#FCFCFC` | app background behind the sidebar and content panel |
| `--card` | `#FFFFFF` | content panel, cards, popovers |
| `--fill-1` | `#F5F5F5` | tracks, empty-state circles |
| `--fill-2` | `#EBEBEB` | progress tracks |
| `--hover` / `--pressed` | `rgba(0,0,0,.04)` / `rgba(0,0,0,.08)` | hover and selected rows, nav, menu items |
| `--hairline` | `#F1F1F1` | row dividers, cell separators, chart grid |
| `--hairline-strong` | `#EBEBEB` | card, input and panel borders |
| `--border-strong` | `#D6D6D6` | emphasised borders |

### Text
| Token | Value | Use |
|---|---|---|
| `--ink` | `#333333` | primary text, KPI numbers |
| `--ink-2` | `#666666` | secondary text, buttons |
| `--ink-3` | `#999999` | labels, table headers, captions, axis |
| `--ink-4` | `#B3B3B3` | disabled, idle dots |

### Colors + tag tints
| Token | Value | Tint (`--*-tint`) | Text on tint (`--*-ink`) | Meaning |
|---|---|---|---|---|
| `--blue` | `#3E63DD` | `#EEF2FD` | `#3A5AC1` | primary action, chart series, in progress |
| `--green` | `#55A271` | `#E9F6EC` | `#308052` | won, booked |
| `--orange` | `#E67333` | `#FCEFD9` | `#C25700` | attention, paused |
| `--indigo` | `#8551C0` | `#F6EEFC` | `#7848AF` | quoted (Twenty purple) |
| `--red` | `#D45453` | `#FBECEC` | `#BE3B38` | error, lost, stop-all |

Status mapping: `new`/`pricing` = blue · `quoted` = indigo · `accepted`/`booked`/`confirmed`/
`answered` = green · `lost`/`expired`/`error` = red · `paused`/`manual` = orange.
Tags and badges always use tint background + `--*-ink` text. No gradients except the
area-chart fill (series color 16 % to 0 %).

### Typography
Font stack: `"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`.
Mono: `"DM Mono", ui-monospace, "SF Mono", Menlo`. Weights 400 / 500 / 600 only.
| Role | Size / line-height / weight | Use |
|---|---|---|
| caption | 12 / 16 / 400 | metadata, axis, badges |
| body | 13 / 1.5 / 400 | body, tables, buttons, nav |
| title | 14 / 20 / 600 | card titles |
| kpi | 24 / 32 / 600, -0.02em | KPI numbers |
Numbers: `font-variant-numeric: tabular-nums`.

### Spacing, radius, elevation, motion
- Spacing (4-pt): 4 · 8 · 12 · 16 · 24 · 32. Card padding 16, grid gap 12.
- Radius: `--r-xs` 2 · `--r-sm` 4 (buttons, inputs, tags, nav) · `--r-md` 8 (cards, panel,
  popovers, drawer) · `--r-xl` 12 · `--r-pill` 999 (chips).
- Elevation: cards have no shadow, only a 1 px `--hairline-strong` border. `--shadow-light`
  for small floating controls, `--shadow-float` for popovers, drawer and map popups.
- Motion: 100 ms hover, 300 ms drawer; easing `cubic-bezier(.2,.8,.2,1)`; respect
  `prefers-reduced-motion`. Charts animate only on their first render, never on refresh.

### Icons
One set: inline SVG, 24-grid drawn at 16 px, 2 px stroke, round caps (Tabler / Lucide feel),
`--ink-3` idle, `--ink` on hover and active.

## 3. Layout

Overview cards must contain long subjects, status labels and booking references. The recent
request table uses fixed column proportions with ellipsis and full-text tooltips. Below its
readable minimum width, only the table scrolls horizontally inside its card. Grid/flex children
must shrink to the available card width; score labels and verdict badges wrap within the card.

- **App shell:** flat sidebar on the `--canvas` background (220 px, 28 px nav rows, monochrome
  16 px icons); the main area is one white panel with a hairline border and 8 px radius, inset
  8 px, with a sticky top bar (controls, search) separated by a hairline.
- **Bento grid:** 12 columns, 12 px gap, max width 1440 px. Cards span 3, 4, 6, 8 or 12.
- **Breakpoints:** ≥ 1200 full grid · 900–1199 sidebar collapses to a 56 px icon rail, KPIs 2×2 ·
  < 900 sidebar hidden behind a menu button, cards full width.

Screens
| Screen | Question | Grid |
|---|---|---|
| Übersicht (default) | Welche Anfragen sind zuletzt eingegangen? | Kompakte Status-Leiste, direkt darunter letzte Anfragen mit Step-Chips. Keine Zu-tun-Section. Suche findet auch ältere Anfragen. |
| Meldungspuffer | Welche Fehler und Warnungen sind angefallen? | Bestehende gruppierte Servermeldungen mit 3/12/24-h-Filter, standardmäßig beide Stufen. Browserfehler bleiben lokal bis zur Bestätigung erhalten. Nur Badge, keine Pop-ups. Quarantäne und Freigabe bleiben hier erreichbar. |
| Anfrage (nur über Klick oder Direktlink) | Wie ist der Stand und was steht im Mailverlauf? | Keine eigene Live-Navigation und keine Sessionliste oder Sessionfilter links. Volle Detailbreite mit Zurück-Link, oben allgemeine Infos, Flow und API-Kosten. Darunter große, offen lesbare E-Mails. Agent-Protokoll und einzelne Logdetails sind standardmäßig eingeklappt. Explizit geöffnete Details und Antwortentwürfe bleiben bei Live-Updates erhalten. |
| CRM | Who did we quote, win, lose? | 4 KPIs · table card with segmented tabs · customer drawer |

## 4. Components

RFQ detail places general information, flow and API costs above the conversation. API-cost
totals are visible; call and token details expand on demand. Mail bodies use the body font
at 15 px with generous line height, preserve whitespace and render only as text. Native
details/summary controls support keyboard use. Background refreshes never open logs or
replace an unsent reply. Request links retain direct URLs and browser back navigation.

| Component | Spec |
|---|---|
| KPI card | label (caption, ink-2) · value (t-kpi) · trend pill · sparkline (40 px high, series color, gradient fill) |
| Hero KPI card | as KPI, value t-hero, mesh-gradient background, one per screen |
| Trend pill | pill radius, caption; up/good = green tint, down/bad = red tint, neutral = fill-1; arrow glyph + % vs previous period |
| Area chart card | smooth bezier (Catmull-Rom → cubic) lines, 1–2 series, vertical gradient fill, light horizontal gridlines, legend chips |
| Progress rings | Apple Health style concentric rings (outer → inner: green, blue, indigo), 12 px stroke, round caps, track = series tint; legend with value |
| Status badge | pill, tint background + solid text, 22 px high |
| Table / list | 52 px rows, hairline dividers, hover = fill-1, avatar/initial circle for customers |
| Segmented control | fill-1 track, white selected thumb with shadow-card |
| Search pill | fill-1, radius pill, 36 px, icon + placeholder |
| Buttons | `primary` blue filled · `secondary` white with hairline · `ghost` text · `danger` red text/tint |
| Banner / notice | tint background, solid text color, radius md |
| Drawer | right, 480 px, white, shadow-float, radius xl on the left corners |
| Empty state | icon in a tinted circle + one sentence |

## 5. Copy

Manual reply defaults to the latest message from the session customer. The existing mail
selector labels customer vs. other senders, and the send confirmation names the selected
recipient. An emergency quote resets the target to the customer; a carrier selection cannot
receive a customer quote. Intentional carrier replies remain available through the selector.

German UI, short verbs ("Pausieren", "Übernehmen", "Antworten"). Irreversible actions confirm
and say why ("Nur die erste Quote zählt"). Mail text is untrusted: rendered as text only.
