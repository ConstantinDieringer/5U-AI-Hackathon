# TECH_SPEC – Single Source of Truth (Team Austrian Power)

**Verbindlich für alle drei Teammitglieder und für jede Claude-/KI-Session, die Code für dieses Repo schreibt.**
Wer davon abweichen will, ändert zuerst diese Datei (kurz im Team ansagen), dann den Code.
Repo: `ConstantinDieringer/5U-AI-Hackathon` · Ablage: `context/docs/TECH_SPEC.md` · Stand: 21.11.2026, 18:00

**Section index (read only the section your change touches):** §1 language/runtime · §2 libraries · §3 folder structure · §4 config/secrets · §5 external interfaces · §6 data models · §7 database · §8 LLM rules, thread rescue, RFQ validation · §8a RFQ answer contract, booking consent, tracking notice, deploy light · §9 logging · §10 UI/internal API · §11 git · §12 tests · §13 AI assistants · §14 open items.

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
- **Nebenläufigkeit:** synchron + Threads (`threading`). FastAPI-Endpunkte als normale `def`; Ausnahme: vorhandenes SSE/Auth und der Mailbox-Webhook lesen HTTP asynchron. Der Webhook begrenzt den Rohbody und übergibt nur das SQLite-Enqueue an den Threadpool; Verarbeitung bleibt synchron in Threads.
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
  portal_lanes.json    # eingecheckter Startwert; Laufzeitdaten in RUNTIME_DIR
  runtime/            # generierte Tarife und Portal-Snapshots, NICHT eingecheckt
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

`.env` (nie committen, das Repo ist öffentlich). **Maßgeblich ist `.env.example` im Repo-Root**: alle Variablen mit Standardwert und Kommentar. `TEAM_API_KEY`, UI-Passwort und Webhook-Signing-Secret sind geheim. Das Signing-Secret bleibt in einer privaten Datei auf dem persistenten Volume oder in `MAILBOX_WEBHOOK_SECRET`. Neue Variable = Eintrag in `.env.example` + Standardwert in `app/config.py` im selben Commit.

- Zugriff nur über `from app.config import settings` (z. B. `settings.api_key`).
- `UI_PASSWORD` ist vor dem Start von `run.py` verpflichtend. Browser-Schreibzugriffe senden
  `X-Requested-With: XMLHttpRequest`; die Mailbox-Webhook-Route verwendet separat HMAC-SHA256.
- Eingehende Mail-Anhänge sind nicht vertrauenswürdig: maximal 12 je Mail und 20 MB insgesamt;
  Downloads werden bei 20 MB pro Datei abgebrochen.
- Der eigene OpenAI-Key wird **einmalig per curl** im Gateway hinterlegt und steht **nirgends** im Repo oder in `.env`.
- `.gitignore` enthält mindestens: `.env`, `.venv/`, `__pycache__/`, `data/forwarder.db`, `data/inbox/`, `*.log`.

## 5. Externe Schnittstellen (Kurzfassung)

Details: Projekt-Doc `api-referenz.md` und `https://api.start-hack.5u.ai/openapi.json`.

- Auth-Header: `X-API-Key: <key>` – in `hackloop.py` zentral gesetzt.
- Timeouts: **10 s** pro Request, max. **2 Retries** mit 1 s Abstand, nur bei Netzwerkfehler/5xx/429 (bei 429 `Retry-After` beachten).
- Mailbox: signierte Pushes an `POST /hooks/mailbox`, Subscription über `PUT /v1/mailbox/webhook`. HMAC-SHA256 über Timestamp + Punkt + unveränderten Rohbody; Replay-Fenster 5 Minuten. Vor 202-Antwort dauerhaft in `mail_delivery` speichern; Agenten separat starten. Exakte Mail-ID dedupliziert Push/Poll auch bei vertauschter Reihenfolge. Nur Polling verändert `since_id`. Sicherheits-Polling alle 30 s bei konfiguriertem Signing-Secret, sonst mindestens 5 s, bei 429 `Retry-After` bzw. 30 s und Backoff beachten. Subscription niemals automatisch beim Start erneuern, weil PUT das Secret rotiert. Senden weiter über `POST /v1/mailbox/messages` mit `to`, `subject`, `body`, `in_reply_to`.
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
    PHISHING = "phishing"     # vom Phishing-Gate gesperrt: gespeichert, nie beantwortet

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
    customer_questions: list[str] = []     # jede explizite Kundenfrage, max. 5
    service_constraints: list[str] = []    # weitere Service-/Terminbedingungen, max. 10
```

Umgesetzt in `app/models.py` (21.11., 15:30).

**Einheiten:** Gewicht in **kg** (float), Volumen in **cbm** (float), Geld als **ganzzahlige Cent** (`*_minor`) bzw. `Decimal`, nie float; gerundet an einer Stelle, Zeiten in **UTC ISO 8601**, Flughäfen als **IATA-Code in Großbuchstaben**, Währungscodes ISO 4217.

## 7. Datenbank (SQLite, `app/db.py`)

`quotes.air_total_minor` und `quotes.air_currency` speichern den gewählten Luftfrachtpreis
in Originalwährung getrennt vom EUR-Kundengesamtpreis (inklusive Truck/Preisanpassung).
Additive nullable Migration: Buchungsmails an Airlines verwenden ausschließlich diese
eingefrorene Komponente. Bei Altangeboten ohne Komponente wird kein Preis behauptet;
Kundenangebote und Kundenbestätigungen behalten ihren Gesamtpreis.

`clarification_answers(session_id,mail_id)` ergänzt den bestehenden `rfq_validation`-Ablauf als Antwort-Ledger. Es verhindert Replay-Duplikate über mehrere Rückfragerunden. Fehlende Felder werden zunächst anhand eindeutiger Kundenmails desselben Threads geprüft; Konflikte bleiben rückfragepflichtig. Teilantworten werden kumuliert, bei Restlücken wird die nächste Frage gespeichert und versendet. Neustart/Worker verarbeiten bereits gespeicherte, noch nicht zugeordnete Antworten. Unklare Buchungszusagen und nicht zuordenbares Tracking nutzen denselben Ablauf mit getrenntem Workflow-Typ. Keine Fristverlängerung und kein neues Quote-Claim durch Rückfragen. `RFQ_VALIDATION_MODE` ist standardmäßig `enforce`; explizites `shadow` bleibt als Diagnose-/Rollback-Option erhalten.

`mail_screening(mail_id PK, sender, thread_root, verdict, layer, score, blocked, routed_as, reasons_json, detail_json, released_at, created_at)` ist das additive Protokoll des Phishing-Gates (`app/mail/screening.py`), einmal pro Mail vor jeder Aktion. Gesperrte Mails stehen mit `category='phishing'` in `mails`, öffnen keine Session und werden von Catch-ups, Historie, Served-Lanes- und Buchungs-Backfill ignoriert; `hackloop.send` verweigert Antworten auf sie. Freigabe stellt `routed_as` wieder her, öffnet die Session und vertraut dem Absender im Thread.

`board_results.cause_json` (additive nullable Spalte, Migration beim Start) friert bei Board-Urteil `no answer` die Ursache aus eigenen Daten ein: letzter Sessionstatus, letztes Fehlerereignis, Zeit des letzten Ereignisses, Prozessstarts innerhalb der Runde; ohne passende Session `mail_not_received` (bzw. `no_session`, wenn die Mail ohne RFQ-Session gespeichert wurde). Jeder Prozessstart ist ein `process_start`-Event; `/api/health` nennt `process_started_at`.

`llm_calls.accounting_json` speichert abwärtskompatibel Usage-Details, Modellpreise und Kostenkomponenten pro Call. Migration ergänzt die nullable Spalte beim Start. `/api/sessions/{id}` ergänzt `llm_usage` mit Calls und RFQ-Summen. Cached Input wird abgezogen und separat bepreist; Reasoning bleibt Teil des Outputs. Historische Kosten bleiben erhalten, unbekannte Preise/Usage werden als unvollständig angezeigt. Preise sind USD-Schätzungen, keine bestätigten Gateway-Rechnungsbeträge.

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
- Standard für Extraktion ist `gpt-5-mini`, für die Reparatur `LLM_RETRY_MODEL=gpt-5-mini` (vorher `gpt-5`, aus Kostengründen gesenkt). Ein zweiter Composer-Call nach gescheiterter Prüfung ist nur mit `COMPOSE_REPAIR=true` aktiv; sonst geht die geprüfte Vorlage raus. Beide wurden über die Gateway-Modellliste bestätigt. Explizite Umgebungswerte überschreiben diese Defaults weiterhin; Kostenpreise für `gpt-5` sind hinterlegt.
- **Normales Limit: max. 8 LLM-Calls pro Thread**, `LLM_MAX_CALLS_PER_SESSION` kann das Limit senken. Zusätzlich bekommt der ausdrücklich gewünschte Thread-Rettungsagent ein eigenes dauerhaftes Budget von `THREAD_RESCUE_MAX_CALLS=2` (Modell `gpt-5-mini`, Kontext: Root plus die letzten 8 Mails mit je 2500 Zeichen); ausgeschöpftes Extraktionsbudget verhindert seine Übernahme nicht. Sämtliche Calls und Kosten bleiben protokolliert. Sichere Textvorlagen und deterministische Versandprüfung bleiben Fehlerfallback.
- `max_tokens` immer setzen (Extraktion/Reparatur ≤ 350, individueller Mailtext ≤ 750).
  Der strukturierte Thread-Rettungsplan bekommt bis zu 1800 Output-Tokens.
  `temperature=0` für strukturierte Outputs.
- Strukturierte Outputs über JSON-Schema/Pydantic, kein freies Parsen.
- Routing, Preisrechnung, Auswahl und Tariflookup: **kein LLM**.
- Phishing-Gate: Regeln (Schicht A) laufen vor jeder Aktion. Nur bei „unsure" ein Call `mail_screening` mit `PHISHING_GATE_MODEL` (Standard `gpt-5-nano`), `session_id=None` (zählt nicht gegen das Session-Limit), 2 s Timeout ohne Retry, ≤ 300 Tokens, Mail als begrenzter Datenblock. Fehler oder Timeout = unsure, nie phishing. `PHISHING_GATE_MODE` off|shadow|enforce (Standard enforce, sperrt Regel-Verdikte); `PHISHING_GATE_LLM_MODE` (Standard shadow) begrenzt LLM-Verdikte.
- Prompts als Konstanten in der jeweiligen Datei, auf Englisch, kurz.

### Übernahme blockierter Threads

- `THREAD_RESCUE_ENABLED=true`: `LLM_RESCUE_MODEL` übernimmt blockierte RFQs und nach `THREAD_RESCUE_AFTER_S=45` liegengebliebene Abläufe. Es entscheidet den nächsten Workflow-Schritt aus Kunden-, Carrier- und eigener Korrespondenz, Anhangauszügen, Prüfproblemen und CRM-Daten. Weitere Kundenantworten im übernommenen Thread gehen wieder an diesen Agenten. Preisrechnung und Carrier-Auswahl bleiben deterministische Werkzeuge.
- Strukturierte Entscheidungen erlauben vollständige belegte RFQ-Korrektur, ausdrückliche Freigabe überholter semantischer Konflikte/unlesbarer Anhänge, gezielte Rückfrage, Pricing-Wiederanlauf, Angebotsfragen, Booking, Tracking, Warten oder Abschluss. Eine belegte kurze Zustimmung zur aktuellen Quote darf der Rettungsagent an den Buchungsablauf übergeben; Bedingungen, Revisionen, Fragen und ausstehende interne Freigaben bleiben gesperrt. Typen, notwendige Preisfelder, Gefahrgutprüfung, echte Preise, menschliche Pause/Übernahme, Phishing und unklarer Versand sind nicht frei überschreibbar.
- `thread_rescue_runs(session_id, input_key, state, decision_json, updated_at)` hat den zusammengesetzten Primärschlüssel `(session_id,input_key)`. Entscheidungen werden vor Ausführung gespeichert; ein 90-Sekunden-Lease verhindert doppelte Bearbeitung und erlaubt Neustartfortsetzung. Erfolgreich geplante unveränderte Eingaben bekommen keine neue Modellentscheidung; neue Carrier-Antworten oder Geschäftsfehler erlauben eine neue Bewertung. Modellfehler können nach Lease-Ablauf im Budget erneut versucht werden. Freigaben gelten nur für den geprüften Root, Kundenstand und gespeicherten RFQ und bleiben beim Pricing erhalten.
- Rettungsrückfragen verwenden das gemeinsame Schreibprofil und den bestehenden dauerhaften Versandpfad. Nach einer neuen Antwort ist eine neue gezielte Frage erlaubt, nie dieselbe Frage ohne neue Antwort. Der ausgeschaltete Stilprüfer bleibt ausgeschaltet.

### RFQ-Eingangsprüfung und Rückfragen

- Belegte numerische Totale unterstützen sowohl Labels (`Gross weight: ...`) als auch klare
  Satzformen (`gross weight is ...`, `volume of ...`); diese benötigen keine erneute Kundenbestätigung.
  Serialisierte Tabellenzeilen dürfen kombinierte Label-/Spaltentrenner (`: |`) enthalten;
  Abweichungen zwischen aktuellen Mail- und Anhangtotalen erfordern weiter eine Klärung.
- `RFQ_VALIDATION_MODE=shadow` ist der Standard: Prüfprobleme als Events erfassen, keine neuen automatischen Rückfragen. Erst nach Replay und Abnahme `enforce` aktivieren.
- `enforce` bepreist nur aktuelle, belegte Angaben. Signaturen und frühere Kundensendungen ersetzen keine Abholadresse; weitergeleitete Betreffe und Historie sind keine aktuellen Streckenangaben.
- Alle Text-/Tabellen-Anhänge vor dem Pricing abgleichen. Bildseiten werden im vorhandenen Extraktionsaufruf transkribiert (bis 2500 Output-Tokens); kein zusätzlicher Call. Preisberechnung bleibt deterministisch.
- `rfq_validation` ist eine additive Workflow-Tabelle (keine Änderung des bestehenden sessions-CHECK): Draft, Quellen, offene Punkte, Zustand und Rückfrage-Ausgang. Der API-Sessiondetail liefert `validation`; die UI zeigt den Zustand neben dem bisherigen Sessionstatus.
- Automatische Rückfragen bleiben im Kundenthread und behalten die ursprüngliche Deadline. Das Scoring-Risiko durch die erste Antwort wurde bewusst akzeptiert. Rückfragen belegen weder `claim_quote_send` noch `first_reply_at`.
- Rückfrageantworten vor Booking behandeln und nur angefragte Werte übernehmen. Bleiben RFQ-Felder nach deterministischer Auswertung offen, liest `LLM_RESCUE_MODEL` den vollständigen aktuellen Kundenthread vor der ersten Rückfrage beziehungsweise vor einem Human-Handoff. Es darf nur explizit belegte Korrekturen mit einem wörtlichen Beleg liefern; Code prüft Beleg, Feldtyp und vollständigen RFQ erneut. Erst ein weiterhin ungelöster, abgelaufener oder ausdrücklich pausierter/manueller Fall benötigt einen Menschen.
- Bei Gefahrguthinweisen ausdrücklich Gefahrgutunterlagen (DGD/Versendererklärung und SDS,
  soweit zutreffend) anfragen und Prüfung vor Angebot/Buchung erklären. Eine allgemeine
  Bestätigung der Packliste ist weder Dokumenteneingang noch Transportfreigabe. Die Zusage,
  DGD/SDS zu senden, gibt die Angebotssperre frei (Runde 41 nach bloßer Zusage akzeptiert;
  6fdc811 wartete auf Anhänge, 26b5b9b nahm das zurück): nie auf Anhänge warten.
- Schalter, alle Standard `false` (= heutiges Verhalten): `TOTALS_SAME_LINE` – ein
  Summen-Label erreicht seine Zahl über einen Zeilenumbruch nur nach of/is oder `:`/`|`
  (Tabellenkopf „Gross weight (kg)“ ist keine Summe). `THOUSANDS_EVIDENCE` – „1,674 kg“ ist
  1674 kg, wenn eine bekannte Zahl derselben Sendung das belegt, sonst bei Unplausibilität
  unter 10 kg; eine Regel (`weight_reading`) für Mail, Anhang, Antwort, Freitext und Rescue.
  `CLARIFY_CAP_RESOLVE` – nach zwei Rückfragen eindeutiger plausibler Thread-Wert, sonst
  Angebot aus der Mail (Gefahrgut erst nach gestellter DG-Frage), sonst keine Mail, Fehler-
  Event und erster Eintrag in „Zu tun“.

## 8a. RFQ-Antwortvertrag und Zeitbudget

- Angebots-Sprache bleibt als ISO-Code bis zum Composer/Reparatur erhalten, statt Nicht-Deutsch pauschal in Englisch umzuwandeln. Aktuelle Mail-/Anhangsprosa entscheidet vor Signatur/Historie; erkennbare Sprachabweichungen lösen Reparatur aus. Deterministische Angebote, Monate und Betreffe unterstützen EN/DE/FR/ES/IT. Für andere Sprachen nutzt der fehlende geprüfte Fallback die englische Vorlage, die beim Versand übersetzt wird (`app/mail/reply_language.py`); scheitert die Übersetzung, geht Englisch mit Warn-Event raus, nie gar kein Angebot. Der normale Composer bleibt verfügbar. Kein zusätzlicher Rewrite-Call.

- Explizit verneinte Buchungsabsichten (`won't go ahead`, `cannot accept`, `don't reserve`, einschließlich typografischer Apostrophe) haben Vorrang vor positiven Schlüsselwörtern. Der gemeinsame Freigabeparser darf daraus weder Buchung noch Preisänderungsfreigabe ableiten.
- Pflicht-Trackinghinweis: `send_customer` ergänzt bei jeder RFQ-Kundenmail einen eigenen Absatz vor der Signatur, vor und nach optionaler Modellprüfung. Vor Buchung bedingt, nach Kundenbestätigung direkt; keine erfundenen Trackinglinks oder Echtzeitdaten. `tracking_notice` prüft aktive Quotes/Bestätigungen alle `TRACKING_NOTICE_CHECK_EVERY_S=30` Sekunden und sendet höchstens einen fehlenden Hinweis je Durchlauf ohne Preis/Quote/LLM nach. Pause, Handbetrieb, Absage und abgelaufene ungebuchte Angebote verhindern die Nachsendung. Alte gültige Statusantwort-Hinweise und belegte referenzierte Originalmails zählen als zugestellt. Dauerhafte KV-Schlüssel `tracking.notice.recovery.<sid>` reservieren genau eine Nachsendung; `sent:<mail_id>` bleibt auch bei lokaler Aufzeichnungsstörung bestehen. Unklarer Versand/Neustart während einer Reservierung erzeugt Alarm und keinen blinden zweiten Versuch. Sicher vor Transport blockierte Versuche dürfen später erneut geprüft werden. `/api/health.tracking_notice_version` zeigt die Aktivierung.

- Carrier-Angebote mit Anhängen verwenden denselben begrenzten lokalen TXT/PDF/DOCX/XLSX-Leser wie RFQs. Nur explizite Gesamtpreise mit eindeutiger Währung werden übernommen; `Total, all-in` und unmittelbar folgende Wertzeilen werden unterstützt, aber nicht über fremde Zeilen hinweg gesucht. Komponenten, Wartezeit-Zuschläge und Referenznummern ersetzen keinen Gesamtpreis. Widersprüche zwischen Mail und Dokumenten sowie unlesbare/Audio-Anhänge bleiben als `bid_unreadable` sichtbar und werden nicht geraten. Der bisherige reine Mailtextpfad bleibt erhalten.

- Stilversion v4 prüft Kundenbetreff UND Text unmittelbar vor dem Transport mit einer unabhängigen finalen Assertion. Unicode-Satzzeichen und HTML-Entities werden normalisiert; Re/Fw-Präfixe entfallen, Threading bleibt über `in_reply_to` erhalten. Auch All-Excluded-Hinweise durchlaufen `send_customer`. Gespeichert werden tatsächlich übergebener Betreff und Text. Direkte Carrier-/Truckerprotokolle bleiben unverändert.
- Buchungsfreigabe: Vorabfragen wie „Before we book...“ und bedingte Zustimmung gelten
  ebenso wie noch laufende interne Prüfungen („check whether we can proceed“) ausdrücklich
  als ungeklärte Freigabe, auch wenn „book“ vorkommt. Eine abgeschlossene Prüfung mit
  explizitem Auftrag bleibt gültig. Direkte Aufträge dürfen zusätzliche Informationsfragen enthalten.
  Der Test auf laufende interne Prüfung verwendet dieselben Zustimmungsausdrücke wie
  die Freigabe selbst und gilt pro Satz; separate Sachfragen ersetzen keine Freigabe.
  Unklare Freigaben bleiben im vorhandenen Rückfrageablauf;
  dieselbe Prüfung gilt bei Annahme eines geänderten Buchungspreises.
- Vorabfragen beantworten (`PREBOOK_ANSWER`, Standard `false`, `app/agents/prebook.py`):
  `booking.classify_reply` (Regeln, kein LLM) unterscheidet yes/no/question/deferral/unclear;
  nur „yes“ (unverändert `is_acceptance`) bucht. Eine Antwort auf das Angebot, deren
  Fragesätze alle ein bekanntes Thema nennen (schnellste/günstigste Option, Abflug/Ankunft,
  Laufzeit, Airline, Abholung, Gültigkeit, Preisumfang, Annahmeschluss, Routing), wird aus
  dem gespeicherten ersten Angebot beantwortet: Fahrplan über `rfq.schedule` zum Angebotstag,
  Alternativen des Angebots, Trucker-Vorlauf aus `pricing_audit`. Kein zweites Angebot, kein
  anderer Preis; Gesamtpreis höchstens einmal, sonst kein Betrag; fehlende Fakten werden zur
  Buchung zugesagt, nie erfunden. Andere Flughäfen/PLZ/Abholdaten, nicht zuordenbare Fragen
  und eine bessere Option auf dem angefragten Kriterium bleiben im heutigen Rückfrageablauf.
  Versand über `send_customer` ohne LLM-Review (`review=False`), Kategorie `prebook_answer`,
  zählt mit den Follow-ups gegen `FOLLOWUP_MAX`, mindestens 10 s Abstand zur letzten eigenen
  Mail; Pause/Handbetrieb/Quarantäne senden nichts. Nach einer solchen Antwort bucht ein „Ja“
  mit Wunsch nach einer anderen Option („the faster one instead“) nicht.

- Dedizierter Stilagent `app/agents/email_review.py` auf Nutzerwunsch standardmäßig aus: `EMAIL_REVIEW_ENABLED=false`. Kein zusätzlicher Stil-Review-LLM-Aufruf; deterministische Darstellung und finale Versandprüfung bleiben aktiv. Optionaler expliziter Opt-in mit `LLM_REVIEW_MODEL=gpt-5`: genau ein Review pro Kundenmail, bis zu 1200 Output-Tokens, ohne Versand oder Preisentscheidung. Zahlen, Referenzen, volle Daten, Carrier und qualifizierte Aussagen bleiben geschützt; Fehler/Budgetende behält den geprüften Entwurf. Pause/Handbetrieb und Quote-Deadline werden unmittelbar vor dem Versand geprüft.

- Strenge Versanddarstellung: alle Kunden-Sendewege nutzen `hackloop.send_customer`, das den Mailtext unmittelbar vor dem HTTP-Aufruf über `mail/outgoing.py` rendert und prüft. Prosa enthält keine Doppelpunkte, Semikola, Unterstriche, Bindestriche oder Klammern; Referenznummern bleiben zunächst kopierbar. Daten mit ausgeschriebenen Monaten, keine Null-Nachkommastellen, kurze Absätze. Die tatsächlich versandte Fassung wird gespeichert. `/api/health` nennt Stilversion und Build-Revision zur Aktivierungsprüfung. Persönliche Wärme und leichter Humor dürfen keine erfundene Kundentreue, Sonderpreise, Rabatte oder Leistungszusagen behaupten.
- Bei Portalabfragen Airline-Anfrageplätze für bekannte Portal-Carrier derselben Strecke
  reservieren. Bei HTTP 503 zuerst diese Carrier direkt anfragen, anschließend verbleibende
  Plätze mit anderen Kandidaten füllen. Kunden-Ausschlüsse und negative Streckenbeobachtungen
  gelten weiterhin; bereits versendete Anfragen zählen auch nach Neustarts zum Airline-Limit.
  Trucker haben ein getrenntes Limit. Fehlende Luftpreise dürfen keinen Truckingpreis als
  vollständige Quote erzeugen; ein Flughafenwechsel erfordert einen passenden neuen Vorlaufpreis.

- Kundenmails: Euro als €, keine Klammerzusätze oder internen Feld-/Spaltennamen. Termine mit ausgeschriebenem Monatsnamen (`24. November 2026` / `24 November 2026`), kurze Absätze und freundliche vollständige Sätze. Composer-Ausgaben werden auf diese Darstellung geprüft und bei Verstößen repariert; Angebots-, Rückfrage-, Buchungs- und Trackingvorlagen berücksichtigen dieselben Regeln. API-/CRM-Werte und technische IDs bleiben unverändert.

- Gespeicherte kundenbezogene Carrier-Ausschlüsse gelten für Carrier-Anfragen, Auswahl,
  Notfallentwurf und Buchung. Ausgeschlossene Carrier werden vor dem Anfrage-Limit entfernt.
  Angebote nennen den ausgewählten Carrier; angewandte Ausschlüsse bei Bedarf allgemein
  bestätigen, ohne ausgeschlossene Namen im Angebot erneut zu nennen. Ein Composer-Fallback
  darf keinen ausgeschlossenen Carrier anbieten.

- Gemeinsames Schreibprofil: `app/mail/customer_voice.md` kombiniert auf Kundenmails zugeschnittene Regeln aus Humanize Writing und Humanizer. Der Composer lädt diese lokale Datei als Systemanweisung; `.agents/skills/customer-email-voice/SKILL.md` verweist für künftige Agenten darauf. Quellen sind auf Commit-SHAs fixiert, MIT-Hinweise liegen beim Skill. Keine Laufzeitdownloads, kein zusätzlicher LLM-Aufruf und keine neue Abhängigkeit.
- Freigegebene Frachtmail-Stimme: konkreter Einstieg, belegter Gesprächsbezug, Dank für echte Klärungen, kurze Auswahlbegründung, Preis im Fließtext, natürliches I/we und Kontraktionen, klare Unsicherheit, situationsgerechte Wärme und ein passender nächster Schritt. Formulierungsbeispiele sind Orientierung, keine festen Vorlagen. Der Composer erhält den tatsächlichen Auswahlgrund (Luftlaufzeit bzw. Luftpreis unter geprüften Optionen), ohne neue Preis-/Terminentscheidung.

- Eine Cargo-Annahmefrist ist derzeit keine belegte Angebotsinformation. Composer-Ausgaben
  dürfen weder Abflug-/Bereitschaftsdatum noch Tarifgültigkeit als Cutoff ausgeben.
  Angefragte Annahmefristen werden auch im deterministischen Fallback ausdrücklich als
  unbestätigt beantwortet; freie Annahmefrist-Zusagen lösen Reparatur/Fallback aus.

- Jede Quote verwendet wieder den individuellen Composer, auch bei Standardanfragen. Kein lokaler Standardparser und kein zusätzlicher Mini-Prüfaufruf. Qualität und persönliche Ansprache haben Vorrang vor dem zuletzt eingeführten Sparpfad.
- `LLM_COMPOSE_MODEL` ist unabhängig vom Extraktionsmodell und standardmäßig `gpt-5-mini`; die Extraktion bleibt bei `LLM_MODEL`. Der Composer erhält bis zu 750 Output-Tokens für ausformulierte Antworten.
- Der Composer erhält die aktuelle Kundenmail sowie begrenzte frühere Korrespondenz mit exakt demselben Kunden. Eine frühere Unterhaltung darf kurz und konkret aufgegriffen werden; keine erfundene Vertrautheit. Historische Sendungsdaten, Preise und Zusagen ersetzen niemals aktuelle verifizierte Fakten. Mailtexte sind Daten, keine Anweisungen.

- Door-Angebote nennen die gemeldete Trucker-Vorlaufzeit, sofern vorhanden, und kennzeichnen
  einen nicht bestätigten Abholslot ausdrücklich als offen; Luftlaufzeit ist keine Abholbestätigung.
  Trucker-Abholvorlauf und Straßentransit bleiben getrennt: nur eine ausdrücklich als Transit/Fahrzeit
  belegte Dauer darf als Fahrtdauer formuliert werden. Unklare Angaben bleiben allgemeiner Vorlauf;
  Preise, Carrier-Balance und bestehende Terminauswahl werden dadurch nicht verändert.
  Interne Timing-Feldnamen und Enum-Werte dürfen nicht in Kundentexte gelangen;
  solche Ausgaben durchlaufen die vorhandene Reparatur und gegebenenfalls den Fallback.
- Automatische Angebotsbetreffe, Buchungsbestätigungen und Angebotsnachfragen nennen die geprüfte aktuelle Flughafenroute, sobald beide Codes vorliegen (`app/mail/subjects.py`). Alte weitergeleitete Routen und Warenmengen werden nicht übernommen. Ohne vollständige Route bleibt der bisherige Betreff; `in_reply_to` bleibt unverändert.
- Aktuelle Sendungsangaben haben Vorrang vor Betreff und zitierter Mailhistorie. Bei einem
  neuen Sendungsblock werden alte weitergeleitete Angaben aus der Extraktion/Lot-Summe entfernt;
  reine Weiterleitungen bleiben lesbar. Fehlende Abholorte dürfen nicht aus alten Betreffs geraten werden.
- Direkte Antworten auf eine offene Abflughafenfrage werden unabhängig von optionalen
  Recovery-/Einmal-Rückfrage-Schaltern mit dem vorhandenen Freitextparser ausgewertet;
  verneinte, historische, mehrdeutige und reine Zielflughafenangaben ergeben keinen Abflugort.
- Die Extraktion bewahrt neben den Preisfeldern alle expliziten Kundenfragen und
  Servicebedingungen. Schlägt die Pydantic-Validierung fehl, ist genau ein Reparatur-Call
  mit demselben Schema erlaubt.
- Der Antwort-Composer erhält ausschließlich verifizierte Preis-, Laufzeit-, Termin- und
  Verfügbarkeitsfakten. Seine Ausgabe wird vor dem Senden geprüft: genau ein EUR-Gesamtpreis,
  jede Kundenfrage entweder beantwortet oder ausdrücklich als nicht verfügbar markiert.
  Bei jeder ungültigen Ausgabe wird ein deterministischer Antworttext verwendet.
- Carrier werden anhand aktueller Lane-Beobachtungen ausgewählt und auf
  `MIN_BID_CARRIERS..MAX_BID_CARRIERS` begrenzt. Negative Beobachtungen verfallen nach
  `CARRIER_OBSERVATION_TTL_S`.
- Bis `QUOTE_FULL_SCORE_DEADLINE_S` wird auf den besten vollständigen Preis gewartet; die
  absolute Sendeschranke ist `QUOTE_FINAL_DEADLINE_S`. Door-to-Port darf standardmäßig nie
  ohne Trucking-Anteil angeboten werden (`ALLOW_INCOMPLETE_DOOR_QUOTE=false`).
- Offene RFQ-Sessions werden beim Start aus SQLite wieder aufgenommen. Sende-Claims und bereits
  gespeicherte Carrier-Anfragen verhindern doppelte Kundenangebote bzw. unnötige Doppelanfragen.
- RFQ-Watchdog (`app/agents/watchdog.py`, `RFQ_WATCHDOG`, `RFQ_WATCHDOG_AFTER_S=200`): eine
  RFQ-Session (Root-Mail `rfq`) ohne Quote und ohne lebenden Worker dieses Prozesses erhält ab
  200 s bis `TOO_OLD_S` genau einen erzwungenen Preisdurchlauf über `rfq._price_and_quote(forced=True)`:
  Tarifblatt, Portal (Rate-Gate und Restzeit) und bereits erhaltene Gebote, keine neuen
  Gebotsanfragen, kein Warten, Versand nur über `claim_quote_send`. Nie bei Pause/Handbetrieb,
  offenem Gefahrgut, Phishing; Kundenausschlüsse bleiben aktiv, keine Ausschluss-Mitteilung.
  Offene Rückfrage: nur ausdrücklich genannte Angaben, offene Abflug-/Zielfelder werden verworfen,
  Abholung ohne Ort wird nicht angeboten. Kein Preis: nichts senden, `watchdog_alert` (error).
  Ein Quote-Claim ohne Transportmarker (`kv quote.transport.<id>`, unmittelbar vor HTTP gesetzt)
  wird freigegeben; mit Marker nie, nur Alarm. Weniger als 75 s Restzeit: geprüfte Textvorlage
  ohne Stilprüfung.
- Deploy-Ampel `/api/deploy-gate`: grün, wenn die neueste RFQ beantwortet oder ihr 360-s-Fenster
  vorbei ist und die nächste RFQ (Median-Abstand der letzten Ankünfte, Phase aus der neuesten,
  Fallback `RFQ_INTERVAL_S=364`) mehr als `DEPLOY_GATE_MARGIN_S=90` entfernt ist.

## 9. Logging & Fehler

- `logging` aus der Standardbibliothek, Format: `%(asctime)s %(levelname)s %(name)s thread=%(thread_id)s %(message)s` (thread_id über `extra`).
- Jede wichtige Aktion zusätzlich als Zeile in `events` (für die UI).
- **Kein Fehler darf den Poll-Loop beenden**: Verarbeitung jeder Mail in `try/except`, Fehler loggen, Session auf `error`.
- Keine `print()`-Aufrufe im App-Code.

## 10. UI / interne API

- Manuelle Antworten wählen standardmäßig die neueste eingegangene Mail des Session-Kunden,
  nie automatisch einen Carrier. Eine andere eingegangene Mail derselben Session bleibt
  ausdrücklich auswählbar; die Versandbestätigung nennt den tatsächlichen Empfänger.
  Notfall-Quotes dürfen nur an den Session-Kunden gehen. Carrier-Antworten markieren weder
  `first_reply_at` noch eine Kundenquote; ungültige Auswahl scheitert vor Versand/Übernahme.

- FastAPI auf Port `UI_PORT` (8000), Bindung `0.0.0.0`.
- Endpunkte: siehe `context/docs/design/aufgabe-c.md` Abschnitt 6. SSE über `fastapi.sse.EventSourceResponse`, ohne `event:`-Feld; Mailinhalte im Frontend nur per `textContent` rendern.
- JSON-Felder in **snake_case**.

## 11. Git-Workflow

- `main` ist immer lauffähig. Arbeit auf Branches `a/...`, `b/...`, `c/...`.
- Kleine PRs, Merge nach kurzem Blick einer zweiten Person (oder selbst, wenn es eilt – dann im Chat ansagen).
- Commit-Messages englisch, Präfix: `feat:`, `fix:`, `chore:`, `docs:`, `test:`.
- Die eingecheckten `data/tariffs.json` und `data/portal_lanes.json` sind Startwerte. Neue Tarife und Portal-Snapshots werden atomar in `RUNTIME_DIR` geschrieben (lokal `data/runtime`, im Container `/data/runtime`) und nicht committed.
- **Ab 15:50 und während der Live-Runden:** nur noch Fixes auf `main`, nach jedem Merge Neustart und kurzer Check.

## 12. Tests

- `pytest` in `tests/`. Pflicht für: `pricing.py` (bekannte Beispielpreise), `router.py` (Beispielmails pro Kategorie), `parse_tariffs.py` (158 Lanes, 0 unvollständig).
- Kein Test darf echte 5U-Endpunkte aufrufen (Rate-Limit, Kosten). Echte API-Antworten als JSON-Fixtures in `tests/fixtures/`.

## 13. Für KI-Assistenten (Claude etc.)

- Vor einer Code-Änderung den betroffenen Abschnitt dieser Datei lesen (Index oben) und einhalten; Einstieg und Kontext-Routing: `CLAUDE.md`.
- Keine neuen Abhängigkeiten, Sprachen oder Frameworks einführen.
- Keine API-Felder raten: `context/docs/mail-format.md` und `openapi.json` sind die Quelle.
- Keine Secrets in Code, Logs oder Chats.

## 14. Offen / zu bestätigen

- [x] Exakte Feldnamen der Mailbox-Objekte (A3 → `context/docs/mail-format.md`): `from`, `to`, `received_at`, …
- [ ] Erlaubte Modelle im Gateway (`GET /v1/gateway/models`) → `LLM_MODEL` setzen
- [x] Rate-Limit pro Minute für `/v1/portal/rates` (B4): **10**, Header `X-RateLimit-Limit`
- [ ] Zählt der Sprung in eine höhere Gewichtsklasse als bester Preis? Schalter `use_breakpoints` in `sheet_options()` (Default an); das Portal rechnet nur das angefragte Gewicht
