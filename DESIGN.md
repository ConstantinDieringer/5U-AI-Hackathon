# DESIGN.md – Austrian Power Forwarder Console

Status: **v0.6** (22.11.2026, 5U palette on the v0.5 Twenty/Apple structure). Ink, white and lime
from the 5U challenge deck: flat white cards on a light canvas, black primary actions, one lime hero. Values are the source of truth for the `:root` block in
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

Structure from the Twenty CRM UI (github.com/twentyhq/twenty), colors from the 5U deck:
flat white cards without shadows on `#F6F5F8`, ink `#151515` for text and primary actions,
lime `#E2FE8C` as the single accent surface, mono uppercase labels. No Twenty code or assets.

### Canvas & surfaces
| Token | Value | Use |
|---|---|---|
| `--canvas` | `#F6F5F8` | app background, sidebar |
| `--card` | `#FFFFFF` | cards, popovers |
| `--fill-1` | `#EFEEF2` | segmented control track, empty-state circles |
| `--fill-2` | `#E4E3E8` | progress tracks |
| `--hover` / `--pressed` | `rgba(21,21,21,.04)` / `rgba(21,21,21,.08)` | hover and selected rows, nav, menu items |
| `--hairline` | `#EFEEF2` | row dividers, cell separators, chart grid |
| `--hairline-strong` | `#E6E5EA` | input, input and panel borders |
| `--border-strong` | `#CFCED5` | emphasised borders |

### Text
| Token | Value | Use |
|---|---|---|
| `--ink` | `#151515` | primary text, KPI numbers |
| `--ink-2` | `#5E5E60` | secondary text, buttons |
| `--ink-3` | `#8E8E93` | labels, table headers, captions, axis |
| `--ink-4` | `#B5B5BA` | disabled, idle dots |

### Colors + tag tints
| Token | Value | Tint (`--*-tint`) | Text on tint (`--*-ink`) | Meaning |
|---|---|---|---|---|
| `--accent` | `#151515` | `#E2FE8C` | `#151515` | primary action, chart series, in progress, active nav |
| `--lime` | `#E2FE8C` | `#F5FFD9` (`--lime-soft`) | `#0E1D00` | hero KPI, icon on ink, focus ring; `--lime-strong` `#C8EB4E` for hover |
| `--green` | `#8CC63F` | `#F1FFD0` | `#3F6A00` | won, booked |
| `--orange` | `#F08A24` | `#FDF0DF` | `#B95A00` | attention, paused |
| `--slate` | `#6E6E73` | `#EEEEF0` | `#3A3A3C` | quoted |
| `--red` | `#E5484D` | `#FCEBEC` | `#C4323A` | error, lost, stop-all |

Status mapping: `new`/`pricing` = accent · `quoted` = slate · `accepted`/`booked`/`confirmed`/
`answered` = green · `lost`/`expired`/`error` = red · `paused`/`manual` = orange.
Tags and badges always use tint background + `--*-ink` text. No gradients: the brand mark and
avatars are ink tiles with a lime glyph, the hero KPI card is solid lime. The booking chart uses
solid ink bars that turn lime on hover. CSS class names `.blue`/`.indigo` map to accent/slate.

### Typography
Font stack: `"Inter", "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`.
Mono: `"DM Mono", ui-monospace, "SF Mono", Menlo`. Weights 400 to 650.
| Role | Size / line-height / weight | Use |
|---|---|---|
| caption | 12 / 16 / 400 | metadata, axis, badges |
| body | 14 / 1.45 / 400 (tables 13) | body, buttons, nav |
| title | 15 / 20 / 600 | card titles |
| kpi | 34 / 40 / 500, -0.04em | KPI numbers |
| label | mono 11.5 / 16 / 500, uppercase, +0.08em | KPI labels, table headers |
Numbers: `font-variant-numeric: tabular-nums`.

### Spacing, radius, elevation, motion
- Spacing (4-pt): 4 · 8 · 12 · 16 · 20 · 24 · 40. Card padding 20, grid gap 16.
- Radius: `--r-xs` 4 · `--r-sm` 8 (buttons, inputs, nav) · `--r-md` 10 (popovers, drawer, map) ·
  `--r-card` 14 (cards) · `--r-xl` 18 · `--r-pill` 999 (chips); tags 6.
- Elevation: cards have a 1 px `--hairline-strong` border plus the soft `--shadow-card`.
  `--shadow-light` for buttons, inputs and the active nav item, `--shadow-float` for popovers,
  drawer, map popups and KPI hover.
- Motion: 120 ms hover, 260 ms drawer; easing `cubic-bezier(.2,.8,.2,1)`; respect
  `prefers-reduced-motion`. Charts animate only on their first render, never on refresh.

### Icons
One set: inline SVG, 24-grid drawn at 17 px, 1.85 px stroke, round caps (Tabler / Lucide feel),
`--ink-3` idle, lime on the ink-filled active nav item.

## 3. Layout

Overview cards must contain long subjects, status labels and booking references. The recent
request table uses fixed column proportions with ellipsis and full-text tooltips. Below its
readable minimum width, only the table scrolls horizontally inside its card. Grid/flex children
must shrink to the available card width; score labels and verdict badges wrap within the card.

- **App shell:** flat sidebar on the canvas with a hairline edge (228 px, 34 px nav rows, the active
  row a white pill with a light shadow); cards float on the canvas below a sticky frosted top bar
  (controls, search).
- **Bento grid:** 12 columns, 16 px gap, max width 1400 px. Cards span 3, 4, 6, 8 or 12.
- **Breakpoints:** ≥ 1200 full grid · 900–1199 sidebar collapses to a 64 px icon rail, KPIs 2×2 ·
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
| Progress rings | Apple Health style concentric rings (outer → inner: green, accent, slate), 12 px stroke, round caps, track = series tint; legend with value |
| Status badge | pill, tint background + solid text, 22 px high |
| Table / list | 52 px rows, hairline dividers, hover = fill-1, avatar/initial circle for customers |
| Segmented control | fill-1 track, ink selected thumb with white text |
| Search pill | fill-1, radius pill, 36 px, icon + placeholder |
| Buttons | `primary` ink filled · `secondary` white with hairline · `ghost` text · `danger` red text/tint |
| Banner / notice | tint background, solid text color, radius md |
| Drawer | right, 480 px, white, shadow-float, radius xl on the left corners |
| Empty state | icon in a tinted circle + one sentence |

## 5. Copy

Manual reply defaults to the latest message from the session customer. The existing mail
selector labels customer vs. other senders, and the send confirmation names the selected
recipient. An emergency quote resets the target to the customer; a carrier selection cannot
receive a customer quote. Intentional carrier replies remain available through the selector.

English UI (en-GB dates and numbers, € before the amount), short verbs ("Pause", "Take over", "Reply"). Irreversible actions confirm
and say why ("Only the first quote in a thread counts"). Mail text is untrusted: rendered as text only.
