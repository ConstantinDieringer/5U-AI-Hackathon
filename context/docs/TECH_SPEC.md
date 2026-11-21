# TECH_SPEC – Single Source of Truth (Team Austrian Power)

**Verbindlich für alle drei Teammitglieder und für jede Claude-/KI-Session, die Code für dieses Repo schreibt.**
Wer davon abweichen will, ändert zuerst diese Datei (kurz im Team ansagen), dann den Code.
Repo: `ConstantinDieringer/5U-AI-Hackathon` · Ablage: `context/docs/TECH_SPEC.md` · Stand: 21.11.2026, 15:20

---

## 1. Sprache & Laufzeit

| Thema | Festlegung |
|---|---|
| Sprache | **Python 3.11** (nichts anderes, auch nicht fürs Frontend-Backend) |
| Frontend | **Eine** statische Seite: `app/ui/static/index.html` mit Vanilla JS + CSS. **Kein** React/Node/Build-Step |
| Virtuelle Umgebung | `python -m venv .venv` im Repo-Root, `.venv/` steht in `.gitignore` |
| Start | `python run.py` startet alles (Poller + UI) |
| Code-Sprache | **Englisch** für Code, Variablen, Funktionen, Commits, Kommentare. Doku darf Deutsch sein |
| Kundenmails | in der Sprache des Kunden |

## 2. Bibliotheken (nur diese, neue nur nach Absprache)

In `requirements.txt` mit Mindestversion:

```
httpx>=0.27          # alle HTTP-Calls (kein requests, kein aiohttp)
fastapi>=0.135       # UI-/API-Server, eingebautes SSE (fastapi.sse)
uvicorn>=0.29
python-dotenv>=1.0   # .env laden
openai>=1.30         # NUR für das 5U-Gateway (base_url umgebogen)
pydantic>=2.6        # Datenmodelle / strukturierte LLM-Outputs
openpyxl>=3.1        # xlsx-Tarife
python-docx>=1.1     # docx-Tarife
pdfplumber>=0.11     # pdf-Tarife (pdftotext-Fallback erlaubt)
pytest>=8            # Tests
```

- **Datenbank:** SQLite über die Standardbibliothek `sqlite3`. **Kein ORM.**
- **Nebenläufigkeit:** synchron + Threads (`threading`). **Kein asyncio** im eigenen Code (FastAPI-Endpunkte als normale `def`).
- **Formatierung:** `ruff format` und `ruff check` vor jedem Commit (Zeilenlänge 100).

## 3. Ordnerstruktur (fix)

```
app/
  __init__.py
  config.py            # einzige Stelle, die .env liest
  hackloop.py          # einziger HTTP-Client zu 5U (Mailbox, Portal, Board)
  llm.py               # einziger Zugang zum LLM-Gateway + Kosten-Logging
  db.py                # Schema + alle SQL-Funktionen
  models.py            # gemeinsame Datenklassen (siehe Abschnitt 6)
  mail/
    poller.py          # Poll-Loop
    router.py          # Klassifizierung
  agents/              # Phase 2: rfq.py, tracking.py, tariff.py
  pricing/
    parse_tariffs.py   # Kit-Parser
    pricing.py         # Preisrechnung (deterministisch, kein LLM)
    portal_sync.py     # Portal-Lanes/Brackets
    lanes.py           # Lane-Index: airlines_for(o, d)
    reference.py       # carrier-list + pickup-zones
  ui/
    server.py
    static/index.html
data/
  start-kit/           # Originaldateien (eingecheckt)
  tariffs.json         # generiert, eingecheckt
  portal_lanes.json    # generiert, eingecheckt
  inbox/               # Anhänge, NICHT eingecheckt
  forwarder.db         # SQLite, NICHT eingecheckt
context/docs/
  TECH_SPEC.md         # diese Datei
  mail-format.md       # echtes Mail-Format (Person A, Aufgabe A3)
tests/
run.py
requirements.txt
.env.example
```

**Regel:** Kein Modul ruft die 5U-API an `hackloop.py` vorbei auf, kein Modul ruft OpenAI an `llm.py` vorbei auf, kein Modul schreibt SQL außerhalb von `db.py`.

## 4. Konfiguration & Secrets

`.env` (nie committen, das Repo ist öffentlich). **Maßgeblich ist `.env.example` im Repo-Root**: alle Variablen mit Standardwert und Kommentar. Nur `TEAM_API_KEY` ist geheim und wird von jedem selbst eingetragen. Neue Variable = Eintrag in `.env.example` + Standardwert in `app/config.py` im selben Commit.

- Zugriff nur über `from app.config import settings` (z. B. `settings.api_key`).
- Der eigene OpenAI-Key wird **einmalig per curl** im Gateway hinterlegt und steht **nirgends** im Repo oder in `.env`.
- `.gitignore` enthält mindestens: `.env`, `.venv/`, `__pycache__/`, `data/forwarder.db`, `data/inbox/`, `*.log`.

## 5. Externe Schnittstellen (Kurzfassung)

Details: Projekt-Doc `api-referenz.md` und `https://api.start-hack.5u.ai/openapi.json`.

- Auth-Header: `X-API-Key: <key>` – in `hackloop.py` zentral gesetzt.
- Timeouts: **10 s** pro Request, max. **2 Retries** mit 1 s Abstand, nur bei Netzwerkfehler/5xx/429 (bei 429 `Retry-After` beachten).
- Mailbox: Polling `GET /v1/mailbox/messages?since_id=` alle 1,5 s; Senden `POST /v1/mailbox/messages` mit `to`, `subject`, `body`, `in_reply_to`.
- Portal: `ports` und `brackets` frei; `rates` **rate-limited (10/min, Stand 15:00)**, **eine Airline pro Aufruf** (1–4 Airlines je Lane) → nur über `hackloop.get_rate(origin, destination, airline, weight_kg, volume_cbm) -> dict` mit Rate-Limit-Zähler; `bookings` idempotent über `team_reference`. `app/pricing/portal_sync.py` erwartet zusätzlich `hackloop.get_ports() -> dict` und `hackloop.get_brackets(airline) -> dict` (rohe Antwort, Exception bei Nicht-2xx).
- LLM: OpenAI-SDK mit `base_url=f"{BASE_URL}/v1/gateway"` und `api_key=TEAM_API_KEY`.

## 6. Gemeinsame Datenmodelle (Verträge zwischen den Modulen)

In `app/models.py` als `@dataclass` (oder Pydantic, wenn LLM-Output). Feldnamen sind verbindlich.

```python
@dataclass
class Mail:
    id: int
    thread_root: int          # = references[0] falls vorhanden, sonst id (Session-Zuordnung: db.ingest_mail)
    sender: str               # Feldname der API in context/docs/mail-format.md prüfen!
    to: str
    subject: str
    body: str
    received_at: str          # ISO 8601 UTC
    attachments: list[dict]   # {id, filename, content_type, size}
    references: list[int]
    raw: dict                 # komplette API-Antwort

class Category(str, Enum):
    RFQ = "rfq"               # Phase 1: alle Kundenmails landen hier oder in CUSTOMER
    CUSTOMER = "customer"     # Follow-ups / Zusage (Phase 2 genauer)
    TRACKING = "tracking"
    CARRIER_BID = "carrier_bid"
    PORTAL_REPLY = "portal_reply"
    TARIFF = "tariff"
    IGNORE = "ignore"         # eigene Mails, fremde Teams, Unbekanntes

@dataclass
class RateOption:             # Ergebnis aus Sheet, Portal oder Bid – immer gleiche Form
    airline: str
    origin: str               # IATA
    destination: str          # IATA
    transit_days: int
    total_minor: int          # all-in, in Cent (nie float, siehe context/code-standards.md)
    currency: str             # "EUR" / "USD"
    total_eur_minor: int      # zum Vergleichen umgerechnet, Kurs mitloggen
    source: str               # "sheet" | "portal" | "bid"
    rate_id: str | None       # nur Portal
    valid_until: str | None
    chargeable_kg: float
    billed_kg: float          # > chargeable_kg, wenn eine höhere Gewichtsklasse billiger ist
    fx_eur_per_unit: str | None = None  # verwendeter Kurs, wenn currency != EUR

class Rfq(BaseModel):         # Pydantic (LLM-Output); die Preisrechnung liest nur diese Felder
    origin: str | None        # IATA; None, wenn nur PLZ
    pickup_zip: str | None    # gesetzt = Door-to-Port
    destination: str          # IATA
    weight_kg: float          # Gesamtgewicht
    volume_cbm: float = 0.0   # Gesamtvolumen
    pieces: int | None
    preference: Literal["cheapest", "fastest"]
    ready_date: str | None    # ISO-Datum
    language: str = "en"
    customer_name: str | None
```

Umgesetzt in `app/models.py` (21.11., 15:30).

**Einheiten:** Gewicht in **kg** (float), Volumen in **cbm** (float), Geld als **ganzzahlige Cent** (`*_minor`) bzw. `Decimal`, nie float; gerundet an einer Stelle, Zeiten in **UTC ISO 8601**, Flughäfen als **IATA-Code in Großbuchstaben**, Währungscodes ISO 4217.

## 7. Datenbank (SQLite, `app/db.py`)

> **Maßgeblich für Schema, Session-Zuordnung und DB-Zugriffsmuster ist `context/docs/design/aufgabe-c.md` (Abschnitte 3–5).** Das Schema unten ist überholt, wo es davon abweicht.

```sql
CREATE TABLE IF NOT EXISTS mails (
  id INTEGER PRIMARY KEY,           -- ID aus der 5U-Mailbox
  thread_id INTEGER NOT NULL,
  direction TEXT NOT NULL,          -- 'in' | 'out'
  sender TEXT, recipient TEXT, subject TEXT, body TEXT,
  category TEXT,
  received_at TEXT NOT NULL,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  thread_id INTEGER PRIMARY KEY,
  customer TEXT,
  status TEXT NOT NULL DEFAULT 'open', -- open|quoted|booked|lost|paused|error
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL,
  airline TEXT, origin TEXT, destination TEXT,
  total REAL, currency TEXT, transit_days INTEGER,
  source TEXT, rate_id TEXT, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL,
  team_reference TEXT UNIQUE, booking_reference TEXT,
  rate_id TEXT, airline TEXT, price REAL, currency TEXT,
  status TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER, purpose TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  cost_usd REAL, latency_ms INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS events (       -- Stream für die Sessions-UI
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER, kind TEXT, payload_json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (            -- z. B. since_id, dataset_version
  key TEXT PRIMARY KEY, value TEXT
);
```

- Schemaänderung = diese Datei + `db.py` im selben Commit ändern.
- Eine Verbindung pro Thread (`threading.local`, `isolation_level=None`), PRAGMAs `busy_timeout`, `synchronous=NORMAL`, `foreign_keys=ON` pro Verbindung, `journal_mode=WAL` einmal in `init_db()`.
- **Jede Schreiboperation in `db.tx()` (`BEGIN IMMEDIATE`)**, keine Netzwerk-/LLM-Calls in Transaktionen.
- **Jede ausgehende Mail über `db.record_outgoing(api_mail, session_id, category)` speichern**, sonst finden Carrier-/Portal-Antworten ihre Session nicht.
- Agents prüfen `db.is_paused(session_id)` und `db.is_manual(session_id)` direkt vor jedem Senden.
- `team_reference` für Buchungen = `f"AP-{thread_id}"`.

## 8. LLM-Regeln

- Einziger Einstieg: `llm.complete(purpose, messages, thread_id, max_tokens=...)` in `app/llm.py`. Jeder Call wird in `llm_calls` geloggt.
- Standardmodell aus `LLM_MODEL`; kein anderes Modell im Code hartkodieren.
- **Hartes Limit: max. 2 LLM-Calls pro Thread** (Extraktion + Antworttext). Ein dritter Call wirft eine Exception.
- `max_tokens` immer setzen (Extraktion ≤ 200, Mailtext ≤ 250). `temperature=0` für Extraktion.
- Strukturierte Outputs über JSON-Schema/Pydantic, kein freies Parsen.
- Routing, Preisrechnung, Auswahl und Tariflookup: **kein LLM**.
- Prompts als Konstanten in der jeweiligen Datei, auf Englisch, kurz.

## 9. Logging & Fehler

- `logging` aus der Standardbibliothek, Format: `%(asctime)s %(levelname)s %(name)s thread=%(thread_id)s %(message)s` (thread_id über `extra`).
- Jede wichtige Aktion zusätzlich als Zeile in `events` (für die UI).
- **Kein Fehler darf den Poll-Loop beenden**: Verarbeitung jeder Mail in `try/except`, Fehler loggen, Session auf `error`.
- Keine `print()`-Aufrufe im App-Code.

## 10. UI / interne API

- FastAPI auf Port `UI_PORT` (8000), Bindung `0.0.0.0`.
- Endpunkte: siehe `context/docs/design/aufgabe-c.md` Abschnitt 6. SSE über `fastapi.sse.EventSourceResponse`, ohne `event:`-Feld; Mailinhalte im Frontend nur per `textContent` rendern.
- JSON-Felder in **snake_case**.

## 11. Git-Workflow

- `main` ist immer lauffähig. Arbeit auf Branches `a/...`, `b/...`, `c/...`.
- Kleine PRs, Merge nach kurzem Blick einer zweiten Person (oder selbst, wenn es eilt – dann im Chat ansagen).
- Commit-Messages englisch, Präfix: `feat:`, `fix:`, `chore:`, `docs:`, `test:`.
- Generierte Daten (`tariffs.json`, `portal_lanes.json`) werden eingecheckt, damit alle denselben Stand haben.
- **Ab 15:50 und während der Live-Runden:** nur noch Fixes auf `main`, nach jedem Merge Neustart und kurzer Check.

## 12. Tests

- `pytest` in `tests/`. Pflicht für: `pricing.py` (bekannte Beispielpreise), `router.py` (Beispielmails pro Kategorie), `parse_tariffs.py` (158 Lanes, 0 unvollständig).
- Kein Test darf echte 5U-Endpunkte aufrufen (Rate-Limit, Kosten). Echte API-Antworten als JSON-Fixtures in `tests/fixtures/`.

## 13. Für KI-Assistenten (Claude etc.)

- Diese Datei vor jeder Code-Änderung lesen und einhalten.
- Keine neuen Abhängigkeiten, Sprachen oder Frameworks einführen.
- Keine API-Felder raten: `context/docs/mail-format.md` und `openapi.json` sind die Quelle.
- Keine Secrets in Code, Logs oder Chats.

## 14. Offen / zu bestätigen

- [x] Exakte Feldnamen der Mailbox-Objekte (A3 → `context/docs/mail-format.md`): `from`, `to`, `received_at`, …
- [ ] Erlaubte Modelle im Gateway (`GET /v1/gateway/models`) → `LLM_MODEL` setzen
- [x] Rate-Limit pro Minute für `/v1/portal/rates` (B4): **10**, Header `X-RateLimit-Limit`
- [ ] Zählt der Sprung in eine höhere Gewichtsklasse als bester Preis? Schalter `use_breakpoints` in `sheet_options()` (Default an); das Portal rechnet nur das angefragte Gewicht
