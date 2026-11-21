# Systementwurf Aufgabe C – CRM-Datenbank & Sessions-UI

Team Austrian Power · Stand 21.11.2026, 15:20 · Status: **v2** (Recherche zu SSE, SQLite und Eingreif-UI eingearbeitet, siehe Abschnitt 14)
Bindend daneben: `context/docs/TECH_SPEC.md`. Wo dieser Entwurf das Schema ändert, wird die Spec im selben Commit angepasst (Abschnitt 9).

---

## 1. Anforderungen

### Funktional
| # | Anforderung | Quelle |
|---|---|---|
| F1 | Jede ein- und ausgehende Mail wird gespeichert und genau einer **Session** zugeordnet | Brief: CRM, Sessions-UI |
| F2 | Session = ein vom Kunden gestarteter Thread (RFQ oder Tracking-Frage), mit Status und Deadline (6 Min) | Brief |
| F3 | Quotes und Buchungen mit Historie, gewonnen/verloren | Brief: "history, won and lost" |
| F4 | Dashboard mit Kennzahlen (Anzahl, akzeptiert, Antwortzeit, LLM-Kosten pro Quote, Board-Score) | Brief: "a dashboard" |
| F5 | Live-Ansicht: neue Mails und Agent-Schritte erscheinen ohne Reload (Streaming) | Brief: "Sessions UI with streaming" |
| F6 | Mensch kann eine Session **schnell finden** und **eingreifen**: pausieren, fortsetzen, manuell antworten | Brief: "Finding a session and intervening has to be easy" |
| F7 | Tracking-Agent (Phase 2) bekommt Buchungsstatus ausschließlich aus dem CRM | Brief: "Answer tracking questions from the team's own CRM" |
| F8 | Board-Ergebnisse (`/v1/board/me`) pro Session sichtbar | API |

### Nicht-funktional
- **Last:** 1 RFQ alle 6 Min ≈ 10/h, dazu 2–5 Carrier-Bids, Portal-Antworten, Tracking-Fragen, Follow-ups → grob **50–100 Mails/h**. Winzig: SQLite reicht um Größenordnungen.
- **Latenz UI:** neue Ereignisse < 2 s sichtbar.
- **Robustheit:** Die UI und die Datenbank dürfen den Poller und die Agents **nie blockieren**. Fällt die UI aus, läuft das Geschäft weiter.
- **Kosten:** kein LLM in C.
- **Zeit:** Bauzeit ~50 Min bis 15:50 für den Kern, danach iterativ.

### Rahmen
3 Personen, Python 3.11, FastAPI, SQLite ohne ORM, eine statische HTML-Seite mit Vanilla JS, Threads statt asyncio (alles aus TECH_SPEC).

---

## 2. Überblick

```
             ┌────────────── 5U ──────────────┐
             │ Mailbox API   Portal   Board   │
             └──────┬───────────┬───────┬─────┘
                    │ poll 1,5s │       │ alle 60 s
          ┌─────────▼──────┐    │  ┌────▼─────────┐
          │ mail/poller (A)│    │  │ board_sync(C)│
          │  + router (A)  │    │  └────┬─────────┘
          └───┬────────────┘    │       │
              │ db.ingest_mail()│       │ db.save_board_results()
              ▼                 │       ▼
   ┌──────────────────────────────────────────────┐
   │ app/db.py  (C)  – einzige Stelle mit SQL     │
   │ mails · sessions · quotes · bookings ·       │
   │ llm_calls · events · board_results · kv      │
   │            SQLite, WAL                       │
   └───────▲───────────────────────┬──────────────┘
           │ db.record_quote(),    │ lesen
           │ db.emit(), ...        │
   ┌───────┴────────┐       ┌──────▼───────────────────┐
   │ agents/* (A,B) │       │ ui/server.py (C) FastAPI │
   │ Phase 2        │       │ REST + SSE /api/stream   │
   └───────▲────────┘       └──────┬───────────────────┘
           │ is_paused()?          │ EventSource
           └───────────────────────┤
                            ┌──────▼───────────┐
                            │ static/index.html│  Mensch
                            └──────────────────┘
```

**Kernidee:** Alles, was passiert, wird als Zeile in `events` geschrieben (append-only). Die UI liest Zustand aus den Tabellen und Neuigkeiten aus `events`. Die Agents lesen vor jedem Senden ein einziges Flag (`paused`). Dadurch gibt es zwischen UI und Agents **keine direkte Kopplung**, nur die Datenbank.

---

## 3. Das wichtigste Detail: Mails einer Session zuordnen

Der naive Ansatz `thread_id = references[0]` reicht **nicht**:

- Eine Carrier-Bid-Anfrage starten **wir** als neue Mail an den Carrier. Die Antwort des Carriers hat in `references` **unsere** Mail, nicht die Kunden-RFQ.
- Gleiches gilt für Anfragen an `ratedesk@`.

Deshalb zwei getrennte Begriffe:

| Begriff | Bedeutung |
|---|---|
| `thread_root` | erste ID in `references` (oder eigene ID) – der technische Mail-Thread |
| `session_id` | die **Kunden-Session**, zu der die Mail fachlich gehört |

**Zuordnungsregel in `db.ingest_mail(mail, category)`:**
1. Ist irgendeine ID aus `references` (vom Parent rückwärts) bereits in `mails` bekannt → übernimm deren `session_id`.
2. Sonst, wenn Kategorie `rfq` / `customer` / `tracking` → neue Session mit `session_id = mail.id`.
3. Sonst (unbekannter Carrier, Tarif-Mail, Fremdes) → `session_id = NULL` (erscheint in der UI unter "Unzugeordnet").

**Pflicht für A:** Jede **ausgehende** Mail wird mit `db.record_outgoing(api_response, session_id)` gespeichert. Nur so finden spätere Antworten (Bids, Portal-Desk, Airline-Bestätigungen) ihre Session.

---

## 4. Datenmodell

Änderungen gegenüber TECH_SPEC fett markiert.

### Verbindung & Transaktionen (verbindlich)

```python
_local = threading.local()

def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(settings.db_path, timeout=5, isolation_level=None)  # autocommit
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")    # pro Verbindung
        c.execute("PRAGMA synchronous=NORMAL")   # pro Verbindung
        c.execute("PRAGMA foreign_keys=ON")      # pro Verbindung
        _local.c = c
    return c

@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")   # Schreibsperre sofort -> kein SQLITE_BUSY_SNAPSHOT
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
```

- `PRAGMA journal_mode=WAL` **einmal** in `init_db()` (bleibt in der Datei gespeichert).
- **Jede Schreiboperation in `with tx():`**. Ohne `BEGIN IMMEDIATE` scheitert "erst lesen, dann schreiben" sofort mit "database is locked", egal wie lang der Timeout ist.
- Das Event wird **in derselben Transaktion** wie die Zustandsänderung geschrieben.
- **Keine** Netzwerk-/LLM-/Mail-Calls innerhalb einer Transaktion.
- Ergebnisse immer komplett holen (`fetchall()`) und als `dict(row)` zurückgeben. Offene Cursor blockieren WAL-Checkpoints.
- **Geld nie als float** (`context/code-standards.md`): Beträge als ganzzahlige Cent (`*_minor`), LLM-Kosten als Mikro-USD (`*_micro_usd`), immer mit Währung. Umrechnung für die Anzeige nur im Frontend bzw. in einer Formatierungsfunktion.
- Zeitstempel in Python erzeugen: `datetime.now(timezone.utc).isoformat(timespec="seconds")`.
- Bei Fehlern `e.sqlite_errorname` loggen: `SQLITE_BUSY_SNAPSHOT` = Programmierfehler (lesen→schreiben ohne IMMEDIATE), `SQLITE_BUSY` = Transaktion zu lang.
- DB-Datei nicht in einem synchronisierten Ordner (Dropbox/OneDrive) oder auf einem Netzlaufwerk ablegen.

### Schema

```sql
CREATE TABLE IF NOT EXISTS sessions (
  id            INTEGER PRIMARY KEY,          -- = ID der ersten Kundenmail
  kind          TEXT NOT NULL DEFAULT 'rfq',  -- **rfq | tracking**
  customer      TEXT,                         -- Absenderadresse
  subject       TEXT,
  status        TEXT NOT NULL DEFAULT 'new' CHECK (status IN
                ('new','pricing','quoted','accepted','booked','confirmed','answered','lost','expired','error')),
  paused        INTEGER NOT NULL DEFAULT 0,   -- **Flag, unabhängig vom Status**
  mode          TEXT NOT NULL DEFAULT 'auto', -- **auto | manual** (Mensch hat übernommen)
  request_json  TEXT,                         -- **extrahierte RFQ (Phase 2)**
  deadline_at   TEXT,                         -- **received_at + 6 Min**
  first_reply_at TEXT,                        -- **für Antwortzeit**
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_updated ON sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS mails (
  id           INTEGER PRIMARY KEY,           -- ID aus der 5U-Mailbox
  session_id   INTEGER REFERENCES sessions(id), -- **neu, kann NULL sein**
  thread_root  INTEGER NOT NULL,              -- **umbenannt von thread_id**
  in_reply_to  INTEGER,
  direction    TEXT NOT NULL,                 -- in | out
  sender TEXT, recipient TEXT, subject TEXT, body TEXT,
  category     TEXT,
  received_at  TEXT NOT NULL,
  raw_json     TEXT
);
CREATE INDEX IF NOT EXISTS ix_mails_session ON mails(session_id, received_at);

CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  airline TEXT, origin TEXT, destination TEXT,
  total_minor INTEGER, currency TEXT, transit_days INTEGER,  -- **Geld als Cent (code-standards)**
  source TEXT,                    -- sheet | portal | bid
  rate_id TEXT, valid_until TEXT,
  alternatives_json TEXT,         -- **alle geprüften Optionen (Erklärbarkeit in der UI)**
  mail_id INTEGER,                -- die gesendete Quote-Mail
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  team_reference TEXT UNIQUE,     -- "AP-<session_id>"
  booking_reference TEXT, rate_id TEXT, airline TEXT,
  price_minor INTEGER, currency TEXT,   -- **Cent**
  status TEXT,                    -- booked | confirmed | failed
  created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER, purpose TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  cost_micro_usd INTEGER, latency_ms INTEGER, created_at TEXT  -- **1 USD = 1 000 000**
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER,             -- NULL = systemweit
  kind TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info','warn','error')),
  message TEXT,                   -- **kurzer, lesbarer Satz für die Timeline**
  duration_ms INTEGER,            -- **Dauer des Schritts (Timeline)**
  cost_micro_usd INTEGER,         -- **LLM-Kosten des Schritts in Mikro-USD**
  payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, id);

CREATE TABLE IF NOT EXISTS board_results (   -- **neu: Abgleich mit 5U-Scorer**
  round_id TEXT PRIMARY KEY,
  session_id INTEGER,             -- Zuordnung über Zeit/Betreff, best effort
  verdict TEXT, reason TEXT, points_json TEXT, raw_json TEXT, fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
```

### Session-Zustände

```
 new ──► pricing ──► quoted ──► accepted ──► booked ──► confirmed
   │        │           │                      │
   └────────┴───────────┴──► lost / expired     └──► (Tracking-Fragen hängen an)
   jederzeit ──► error
 paused = 1  hält den Agenten an, ohne den Status zu ändern
 mode = manual  Mensch hat übernommen, Agent fasst die Session nicht mehr an
```
- `expired`: Deadline vorbei ohne Quote (setzt ein Hintergrund-Check, siehe 7).
- `lost`: Kunde lehnt ab bzw. Board-Verdict `rejected`.
- Tracking-Sessions nutzen nur `new → answered | error`.

### Event-Arten (fester Katalog, als Konstanten in `db.py`)

| kind | wer schreibt | message-Beispiel |
|---|---|---|
| `mail_in` | A (via `ingest_mail`) | "RFQ von customer@… : FRA→DFW, 40 kg" |
| `routed` | A | "Als RFQ erkannt (Regel: unbekannter Absender)" |
| `extracted` | A | "Anfrage: FRA→DFW, 40 kg, günstigste Option" |
| `priced` | B | "3 Optionen, beste: Elbflug 146,80 EUR" |
| `bid_requested` / `bid_received` | A | "Bid angefragt bei 3 Airlines" |
| `llm_call` | `llm.py` | "gpt-… 312 Tokens, 0,0004 USD" |
| `mail_out` | A (via `record_outgoing`) | "Quote gesendet: 146,80 EUR" |
| `booked` | A | "Gebucht BK-…" |
| `human_action` | C | "Pausiert von Mensch" / "Manuelle Antwort gesendet" |
| `board_verdict` | C | "Board: accepted, 87 Punkte" |
| `error` | alle | "Portal 429, Retry in 5 s" |
| `phishing` | A (`poller`) | level error: "Phishing-Verdacht, keine Antwort: … Gründe: …" (Payload `mail_id`) |
| `screening` | A (`poller`) | level warn: "Absender unklar, normale Verarbeitung läuft: …" |

---

## 5. Schnittstelle `app/db.py` (Vertrag für A und B)

```python
init_db() -> None
# Mails & Sessions
ingest_mail(mail: Mail, category: Category) -> int | None      # gibt session_id zurück
record_outgoing(api_mail: dict, session_id: int | None, category: str) -> None
get_session(session_id) -> dict ; list_sessions(status=None, q=None, limit=100) -> list[dict]
set_status(session_id, status) ; set_request(session_id, request: dict)
is_paused(session_id) -> bool ; is_manual(session_id) -> bool   # Agents fragen vor jedem Senden
claim_quote_send(session_id) -> bool   # atomar: UPDATE sessions SET first_reply_at=now WHERE id=? AND mode='auto'
                                       #   AND paused=0 AND first_reply_at IS NULL (+ global_pause prüfen); nur bei True senden
# Geschäft
record_quote(session_id, option: RateOption, alternatives: list, mail_id) -> int
record_booking(session_id, resp: dict) -> int ; update_booking_status(team_reference, status)
get_booking_for_tracking(session_or_ref) -> dict | None          # für Tracking-Agent
record_llm_call(session_id, purpose, model, pt, ct, cost_micro_usd, latency_ms)
# Events
emit(kind, message, session_id=None, level='info', **payload) -> int
events_after(last_id, limit=200) -> list[dict]
# KV
kv_get(key, default=None) ; kv_set(key, value)                  # z. B. since_id
# Dashboard
stats() -> dict
```

**Sperre gegen Doppel-Quote:** Agent und manuelle Antwort senden eine Quote nur, wenn `claim_quote_send()` bzw. `takeover` die Session vorher per bedingtem `UPDATE` reserviert hat. Ein reines `is_paused()`-Prüfen reicht nicht, weil Mensch und Agent sonst im selben Moment senden können – und nur die erste Quote zählt.

**Regel:** `ingest_mail`, `record_outgoing`, `record_quote`, `record_booking`, `set_status` schreiben **selbst** das passende Event. A und B müssen dafür nichts extra tun.

---

## 6. UI-Server (`app/ui/server.py`)

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/` | liefert `static/index.html` |
| GET | `/api/stats` | Kennzahlen für die Kopfzeile |
| GET | `/api/sessions?status=&q=` | Liste (Filter, Suche über Kunde/Betreff/Lane) |
| GET | `/api/sessions/{id}` | Session + Mails + Quotes + Buchungen + Events + Board |
| GET | `/api/unassigned` | Mails ohne Session |
| POST | `/api/sessions/{id}/pause` · `/resume` | Flag setzen, Event `human_action` |
| POST | `/api/sessions/{id}/takeover` | `mode=manual`, Agent lässt die Session in Ruhe |
| POST | `/api/sessions/{id}/reply` | Body `{text}` → `hackloop.send(..., in_reply_to=letzte Kundenmail)`, `record_outgoing` |
| GET | `/api/events?since=<id>` | **zuerst bauen**: Events nach ID (Historie + Polling-Fallback) |
| POST | `/api/pause-all` · `/api/resume-all` | globaler Not-Aus für ausgehende Mails |
| POST | `/api/sessions/{id}/handback` | `mode=auto`, Agent übernimmt wieder |
| GET | `/api/stream` | Server-Sent Events aus `events`, unterstützt `Last-Event-ID` |
| GET | `/api/quarantine` | vom Phishing-Gate blockierte, noch nicht freigegebene Mails |
| POST | `/api/mails/{id}/release` | Quarantäne aufheben: normale Verarbeitung, Absender im Thread vertraut |

**Streaming (bestätigt durch Recherche und lokalen Test):**
- **FastAPI hat SSE eingebaut** (ab 0.135): `from fastapi.sse import EventSourceResponse, ServerSentEvent`. Kein `sse-starlette`, kein handgeschriebener `StreamingResponse`. Ping alle 15 s und die Header `Cache-Control: no-cache` / `X-Accel-Buffering: no` setzt FastAPI selbst. → `requirements.txt`: `fastapi>=0.135`.

```python
@app.get("/api/stream", response_class=EventSourceResponse)
def stream(request: Request, since: int = 0):
    last = int(request.headers.get("last-event-id") or since)  # Header hat Vorrang
    while True:
        rows = db.events_after(last, limit=200)    # holt alles komplett, kein offener Cursor
        for e in rows:
            last = e["id"]
            yield ServerSentEvent(data=json.dumps(e), id=str(e["id"]))  # KEIN event:-Feld
        time.sleep(0.5)
```

- **Kein `event:`-Feld** senden. Der Client wertet `data.kind` in `onmessage` aus; unbenannte Event-Typen würden sonst stillschweigend verworfen.
- Sync-Generator: jedes `next()` kann auf einem anderen Worker-Thread laufen. Deshalb **keine DB-Verbindung oder offenen Cursor über ein `yield` hinweg halten**. Mit `db.conn()` (pro Thread) und `fetchall()` ist das erfüllt.
- Die Stream-Route darf **nie 4xx/5xx** liefern: Dann gibt der Browser die Verbindung endgültig auf. Fehler im Generator abfangen und weiter pollen.
- Jede offene Verbindung belegt einen von 40 Threadpool-Plätzen. Mit wenigen Browser-Tabs egal; **pro Seite nur eine EventSource** (Browser-Limit 6 Verbindungen pro Domain).
- Client: Events nach `id` deduplizieren; Wiederaufnahme nach Abbruch macht der Browser selbst über `Last-Event-ID`.
- **Fallback**, falls SSE Probleme macht: Short Polling `GET /api/events?since=<id>` alle 1,5 s. Gleiche Daten, gleiches Rendering.
- **Sicherheit:** Mailtexte kommen von außen. Im Frontend **nur `textContent`**, nie `innerHTML` mit Mailinhalt.

---

## 7. Hintergrund-Jobs in C

Laufen als Threads aus `run.py`:
1. **board_sync** (alle 60 s): `GET /v1/board/me` → `board_results`, Event `board_verdict` bei neuem Verdict, Session ggf. `lost`.
2. **deadline_watch** (alle 10 s): Sessions in `new`/`pricing` mit `deadline_at < jetzt` → `expired` + Event `warn`. Sessions < 90 s vor Deadline → Event `warn` "läuft gleich ab" (UI hebt sie hervor).

---

## 8. UI-Layout (`static/index.html`)

```
┌───────────────────────────────────────────────────────────────────────────┐
│ Austrian Power · Forwarder    ● live   Heute: 12 RFQs · 9 quoted ·         │
│ Ø Antwort 74 s · Ø LLM 0,002 $/Quote · Board 640 P        [Pause ALLES]   │
├──────────────────────────┬────────────────────────────────────────────────┤
│ [Suche…]                 │ FRA → DFW · 40 kg · günstigste    status:quoted │
│ (Alle)(Achtung)(Offen)   │ customer@…  ⏱ 4:12 bis Deadline                 │
│ (Quoted)(Booked)(Fehler) │ [Pausieren] [Übernehmen] [Antworten…]           │
├──────────────────────────┼────────────────────────────────────────────────┤
│ ● FRA→DFW  40kg  ⏱4:12   │ Timeline (live)            │ Mails im Thread   │
│   quoted · 146,80 €      │ 16:00:03 RFQ erhalten      │ ▸ Kunde: Hi, …    │
│ ▲ MUC→LAX 300kg ⏱0:45    │ 16:00:04 extrahiert        │ ▸ Wir: Hallo …    │
│   pricing  (Achtung)     │ 16:00:05 3 Optionen        │                   │
│ ✕ HAM→MEX  error         │ 16:00:07 LLM 0,0004 $      │ Quote-Erklärung:  │
│ …                        │ 16:00:08 Quote gesendet    │ Elbflug 146,80 ✓  │
│ Unzugeordnet (3)         │ 16:01:00 Board: accepted   │ Yellowtail 174,60 │
└──────────────────────────┴────────────────────────────┴───────────────────┘
```

Vorgaben aus `context/ui-context.md`: **dunkles Theme zuerst**, Farben/Abstände nur über CSS-Variablen (Design-Tokens in `:root`), Radius-Skala klein→Karte→Modal, **ein** Icon-Set (z. B. inline-SVG aus einer Quelle).

Design-Regeln (gestützt auf OpenAI-Agents-Tracing, Temporal UI, LangChain Agent Inbox, Intercom Fin, Microsoft HAX, siehe Report):
- **Master-Detail:** Liste links, Detail mit Timeline rechts – das gängige Muster in Agent-Konsolen.
- **"Braucht Aufmerksamkeit" oben angepinnt** (error, paused, < 90 s Restzeit), danach **nach Deadline sortiert**. Countdown pro Zeile: normal, **gelb unter 2:00, rot unter 1:00**.
- Pro Zeile: Lane + Gewicht, Status-Chip, Countdown, LLM-Kosten, letztes Event.
- Filter-Chips + Suche; **Deep-Link** per Hash (`#session=123`), damit man eine Session im Team-Chat teilen kann.
- **Aktionen (fester Satz):** Pausieren / Fortsetzen · Übernehmen (manuell antworten, Agent schweigt) · Zurückgeben · globaler Not-Aus "Ausgehende Mails stoppen" in der Kopfzeile (HAX G17 "global controls"). Nur die Buttons zeigen, die im aktuellen Zustand Sinn ergeben.
- **"Mensch hat übernommen"-Banner** gut sichtbar, damit Agent und Mensch nie gleichzeitig antworten.
- **Vor dem Senden einer manuellen Quote** Hinweis: "Nur die erste Quote zählt – nicht rückgängig zu machen."
- **"Warum diese Quote"-Karte:** gewünschte Option (günstigste/schnellste), verglichene Quellen und Preise, Chargeable Weight, Annahmen, Zweitplatzierter. Kein pauschales grünes Häkchen – Studien zeigen, dass das zu blindem Vertrauen führt.
- **Timeline** mit Uhrzeit, Dauer und Kosten pro Schritt; Payload per `<details>` aufklappbar.
- **Globaler Not-Aus** setzt `kv.global_pause=1`; `is_paused()` prüft auch dieses Flag.
- Extras, wenn Zeit bleibt: Ton/Benachrichtigung bei "braucht Aufmerksamkeit", Tastenkürzel (j/k, p = pausieren).
- Status-Farben einheitlich: grau new, blau pricing, violett quoted, grün booked/confirmed, rot error/lost, gelb paused.
- Keine Bibliotheken; Uhrzeiten lokal formatiert; Countdown clientseitig aus `deadline_at`.

---

## 9. Nötige Änderungen an TECH_SPEC (mit dem ersten C-Commit)

1. Tabelle `sessions`: Spalten `id` (statt `thread_id`), `kind`, `paused`, `mode`, `request_json`, `deadline_at`, `first_reply_at`; Status-Werte wie oben.
2. Tabelle `mails`: `session_id` neu, `thread_id` → `thread_root`, `in_reply_to` neu.
3. `events`: `level`, `message` neu; `board_results` neu.
4. Datenmodell `Mail`: Feld `thread_id` → `thread_root`.
5. Abschnitt 10 (UI-API): Endpunkte aus Abschnitt 6 dieses Entwurfs.
6. Neue Regel: **jede ausgehende Mail über `db.record_outgoing()` speichern**; Agents prüfen `is_paused()`/`is_manual()` direkt vor jedem Senden.
7. `requirements.txt`: `fastapi>=0.135` (eingebautes SSE).
8. DB-Regeln: Verbindung pro Thread mit `isolation_level=None`, PRAGMAs pro Verbindung, jede Schreiboperation in `tx()` mit `BEGIN IMMEDIATE`.

---

## 10. Fehlerbehandlung

| Fall | Verhalten |
|---|---|
| DB kurz gesperrt | `busy_timeout` (z. B. 5000 ms) + WAL; Schreibzugriffe kurz halten |
| Fehler in `emit()` | wird geloggt, aber **nie** an den Aufrufer weitergeworfen (Events dürfen das Geschäft nicht stoppen) |
| Mail doppelt geliefert | `INSERT … ON CONFLICT(id) DO NOTHING` → idempotent |
| SSE-Verbindung bricht | Browser reconnect mit `Last-Event-ID`; Server ist zustandslos |
| UI-Server stürzt ab | Poller und Agents laufen in eigenen Threads weiter; `run.py` startet uvicorn mit eigenem Try/Except |
| Manuelle Antwort scheitert (4xx/5xx) | Fehlermeldung in der UI, Event `error`, kein Statuswechsel |
| Board nicht erreichbar | nächster Versuch in 60 s, kein Einfluss auf Rest |

---

## 11. Trade-offs

| Entscheidung | Gewählt | Alternative | Warum |
|---|---|---|---|
| Speicher | SQLite WAL | Postgres | Null Setup, Last winzig, ein Prozess |
| Live-Updates | SSE aus `events`-Tabelle | WebSocket / In-Memory-Queue | nur Server→Client nötig; Tabelle überlebt Neustarts und erlaubt Wiederaufnahme; kein geteilter Speicher zwischen Threads |
| Eingreifen | Flag in DB, Agent prüft vor dem Senden | Agent-Prozess anhalten/abbrechen | einfach, robust, keine Thread-Steuerung |
| Approve-before-send | **nicht** global | Freigabe jeder Mail | würde nachts, wenn niemand schaut, alle Zeitpunkte kosten. Später nur **bedingt** (wie Intercom Fin "Ask for input"): Entwurf zur Freigabe bei fehlenden RFQ-Daten, stark abweichenden Quellen oder < 90 s Restzeit – mit Countdown, danach sendet der Agent automatisch |
| SSE-Umsetzung | eingebautes `fastapi.sse` | sse-starlette, StreamingResponse | ab FastAPI 0.135 vorhanden, Ping/Header inklusive, weniger Code |
| DB-Zugriff | Verbindung pro Thread + `BEGIN IMMEDIATE` | eine geteilte Verbindung mit Lock | geteilte Verbindung vermischt Transaktionen und verliert WAL-Parallelität |
| Frontend | eine HTML-Datei | React | Spec-Vorgabe, kein Build, schnell |
| Session-Zuordnung | über bekannte Mail-IDs | über Betreff/LLM | deterministisch, kostenlos |

---

## 12. Aufgaben für Person C (Reihenfolge)

| # | Aufgabe | Zeit | Fertig, wenn |
|---|---|---|---|
| C1 | `db.py`: Schema, `init_db`, Verbindung pro Thread, PRAGMAs, `emit`, `events_after`, `kv_*` | 15:05–15:15 | `python -m app.db` legt DB an, Test schreibt/liest Event |
| C2 | `ingest_mail`, `record_outgoing` mit Zuordnungsregel (Abschnitt 3), `list_sessions`, `get_session`, `is_paused` | 15:15–15:25 | Unit-Test: RFQ → Session; Bid-Antwort auf unsere ausgehende Mail → gleiche Session |
| C3 | `server.py`: `/`, `/api/sessions`, `/api/sessions/{id}`, **`/api/events?since=` zuerst**, dann `/api/stream`, pause/resume | 15:25–15:35 | `curl -N /api/stream` zeigt Events live |
| C4 | `index.html`: Liste + Detail + Timeline + Live-Feed, Statusfarben, Countdown | 15:35–15:50 | Testmail erscheint ohne Reload in der richtigen Session |
| C5 | **Merge mit A**, gemeinsamer Test | 15:50–16:00 | Poller schreibt über `ingest_mail` |
| C6 | `board_sync`, `deadline_watch`, Kopfzeile mit `stats()` | ab 16:00 | Verdict pro Session sichtbar |
| C7 | takeover + manuelle Antwort, globaler Not-Aus | danach | Mensch kann eine Session komplett übernehmen |
| C8 | Quote-Erklärung (`alternatives_json`), Unzugeordnet-Liste, Feinschliff Design | danach | Pitch-tauglich |

Sofort mit A abstimmen: Signaturen aus Abschnitt 5 und die Pflicht `record_outgoing`.

---

## 13a. Tests für C (pytest, ohne echte 5U-API)

- 6 Schreib-Threads + 2 Lese-Threads gleichzeitig → 0 Fehler (die Recherche hat genau das lokal getestet: 1800 Events in 0,21 s, 0 Fehler, mit `BEGIN IMMEDIATE`).
- Zuordnung: RFQ → neue Session; ausgehende Bid-Anfrage → `record_outgoing`; Carrier-Antwort mit `references` auf unsere Mail → **gleiche** Session.
- Doppelte Mail → kein zweiter Datensatz, kein zweites Event.
- `/api/stream` mit Header `Last-Event-ID: 3` liefert ab Event 4.

## 13. Später überdenken

- Mehr Sessions → Paginierung in der Liste, Index auf `status`.
- Mehrere Prozesse/Server → SSE aus der DB funktioniert weiter; erst bei viel Traffic auf Pub/Sub umsteigen.
- Board-Zuordnung zu Sessions ist best effort: sobald klar ist, welches Feld `/v1/board/me` zur Mail liefert, exakt zuordnen.
- Approve-before-send als Modus für unsichere Fälle (z. B. Preis-Spread groß, unbekannte Lane).

---

## 14. Grundlage

Recherche-Report `SQLite CRM und SSE Live UI` (Projekt-Doc, Quellen: FastAPI-/Starlette-Doku und -Quellcode, sqlite.org, Python-Doku, MDN/WHATWG, OpenAI Agents Tracing, Temporal UI, LangChain Agent Inbox, Intercom Fin, Zendesk, Microsoft HAX Guidelines, arXiv 2602.16844). Die Codeskizzen zu SQLite und SSE wurden während der Recherche lokal getestet (Python 3.13, FastAPI 0.165) – auf Python 3.11 bei uns noch einmal prüfen.
