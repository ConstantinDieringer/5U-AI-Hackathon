# DESIGN.md – Austrian Power Forwarder Console

Status: **v0.3** (21.11.2026, Übersicht: Fehler zuerst, Kontext kopieren). Replaces the dark v0.1. Direction set by Moritz: light, airy,
Apple-inspired bento grid. Values are the source of truth for the `:root` block in
`app/ui/static/index.html`; components use tokens only, never raw values.

## 1. Product & tone

An operations console someone checks at night and shows in the pitch: *is the system healthy,
does a session need me, how are we scoring?* Calm and airy by default; color only where it
carries meaning (status, data, the one hero KPI).

Principles
1. **Quiet monitoring.** Errors and warnings collect in the message buffer with a passive
   navigation badge. No automatic pop-ups or disappearing error notices interrupt the user.
2. **Generous whitespace.** Fewer, larger cards; one idea per card.
3. **Color with purpose.** Neutral base; system colors only for status, charts, active pills and
   the single featured hero card.
4. **Show the why.** Every quote shows the options compared.
5. **Numbers are tabular,** bold for KPIs, with unit and currency.

## 2. Tokens

### Canvas & surfaces
| Token | Value | Use |
|---|---|---|
| `--canvas` | `#F5F5F7` | app background |
| `--card` | `#FFFFFF` | cards |
| `--glass` | `rgba(255,255,255,.75)` + `backdrop-filter: blur(24px) saturate(1.6)` | floating sidebar, top bar |
| `--fill-1` | `#F2F2F5` | inputs, segmented control track, hover rows |
| `--fill-2` | `#E8E8ED` | pressed, progress tracks |
| `--hairline` | `rgba(0,0,0,.05)` | card borders (1 px) |
| `--hairline-strong` | `rgba(0,0,0,.08)` | dividers in tables |

### Text
| Token | Value | Use |
|---|---|---|
| `--ink` | `#1D1D1F` | primary text, KPI numbers |
| `--ink-2` | `#86868B` | secondary metadata |
| `--ink-3` | `#AEAEB2` | placeholders, axis labels |

### System colors (Apple palette) + pastel tints
| Token | Value | Tint (`--*-tint`) | Meaning |
|---|---|---|---|
| `--blue` | `#007AFF` | `rgba(0,122,255,.10)` | primary action, active nav, in progress |
| `--green` | `#34C759` | `rgba(52,199,89,.12)` | won, booked, on target |
| `--orange` | `#FF9500` | `rgba(255,149,0,.12)` | attention, < 2 min, paused |
| `--indigo` | `#5856D6` | `rgba(88,86,214,.10)` | quoted (waiting for customer) |
| `--red` | `#FF3B30` | `rgba(255,59,48,.10)` | error, lost, expired, stop-all |

Status mapping: `new`/`pricing` = blue · `quoted` = indigo · `accepted`/`booked`/`confirmed`/
`answered` = green · `lost`/`expired`/`error` = red · `paused`/`manual` = orange.

Gradients (selective): hero KPI card mesh = radial blobs of blue, indigo, green tints on white;
area-chart fill = vertical gradient from the series color at 28 % to 0 %.

### Typography
Font stack: `"SF Pro Display", "SF Pro Text", -apple-system, BlinkMacSystemFont, "Inter",
"Segoe UI", Roboto, sans-serif`. Mono: `"SF Mono", ui-monospace, Menlo`.
| Token | Size / line-height / weight / tracking | Use |
|---|---|---|
| `--t-caption` | 12 / 16 / 500 / 0 | metadata, axis, badges |
| `--t-body` | 14 / 20 / 400 / -0.01em | body, tables |
| `--t-title` | 17 / 22 / 600 / -0.02em | card titles |
| `--t-page` | 28 / 34 / 700 / -0.03em | page title |
| `--t-kpi` | 34 / 40 / 700 / -0.035em | KPI numbers |
| `--t-hero` | 48 / 52 / 700 / -0.04em | hero KPI |
Numbers: `font-variant-numeric: tabular-nums`.

### Spacing, radius, elevation, motion
- Spacing (4-pt): 4 · 8 · 12 · 16 · 20 · 24 · 32 · 40. Card padding 24, grid gap 20.
- Radius: `--r-pill` 999 · `--r-sm` 10 (inputs, buttons) · `--r-md` 14 (inner panels) ·
  `--r-card` 22 (cards) · `--r-xl` 28 (sidebar).
- Shadow: `--shadow-card` `0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.04)` ·
  `--shadow-float` `0 12px 40px rgba(0,0,0,.08)` (sidebar, drawer).
- Motion: 150 ms hover, 250 ms drawer/page, easing `cubic-bezier(.2,.8,.2,1)`; respect
  `prefers-reduced-motion`.

### Icons
One set: inline SVG, 24-grid, 1.75 px stroke, round caps (SF Symbols / Lucide feel),
monochrome `--ink-2`, active `--blue`.

## 3. Layout

Overview cards must contain long subjects, status labels and booking references. The recent
request table uses fixed column proportions with ellipsis and full-text tooltips. Below its
readable minimum width, only the table scrolls horizontally inside its card. Grid/flex children
must shrink to the available card width; score labels and verdict badges wrap within the card.

- **App shell:** floating frosted-glass sidebar (240 px, 16 px inset from the edges, radius 28)
  with minimal monochrome icons; main area with a sticky glass **top bar** (page title, search
  pill, date-range segmented control, user avatar).
- **Bento grid:** 12 columns, 20 px gap, max width 1360 px. Cards span 3, 4, 6, 8 or 12.
- **Breakpoints:** ≥ 1200 full grid · 900–1199 sidebar collapses to a 72 px icon rail, KPIs 2×2 ·
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
