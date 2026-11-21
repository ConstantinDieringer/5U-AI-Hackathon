# Deploy (Railway)

## RFQ input validation rollout

1. Use an isolated checkout for development; do not pull new modules underneath a running agent.
2. Run the full test suite with a test-only API key. With the intended CRM configured,
   `python -m app.mail.validation_replay` reads stored extractions and local attachments only:
   no mailbox polling, model calls, writes or sends. Rows without a stored extraction are
   explicitly marked and cannot establish live extraction accuracy.
3. Deploy the sole production instance with `RFQ_VALIDATION_MODE=shadow`. Inspect
   `validation_result` events for blocked inputs and unexpected false positives.
4. After replay acceptance, set `RFQ_VALIDATION_MODE=enforce` and restart that instance.
   Missing information triggers at most one automatic clarification. The original six-minute
   deadline and two-call model budget remain unchanged. A clarification can cost the scoring
   opportunity for the round; this is the selected product policy.
5. Check the session's validation panel and attention list. `unknown` means the send outcome
   is ambiguous; investigate manually, never retry blindly. `manual_review` means the reply
   did not resolve all required facts. Supported replies use labelled postal code, airport
   code, gross kg, cbm or complete lot lines; other wording is left to a human.
6. Roll back to `shadow` if required. Pending clarification replies are retained for manual
   review and no additional automated clarification is sent. Do not delete the workflow table.


The whole forwarder runs as **one** container: poller, agents and UI. Only one instance may
poll the mailbox, otherwise mails are processed twice. Once it runs online, nobody runs
`python run.py` against the real mailbox on a laptop any more.

## One-time setup (≈10 min)

1. https://railway.com → sign in with GitHub → **New Project → Deploy from GitHub repo** →
   `ConstantinDieringer/5U-AI-Hackathon`. Railway finds `Dockerfile` and `railway.json`.
2. Service → **Variables** (Raw editor), paste and fill:
   ```
   TEAM_API_KEY=hk_...            # from the kit-card mail
   UI_PASSWORD=<strong password>  # required: the UI can stop all mail and send replies
   UI_USER=team
   LLM_MODEL=gpt-5-nano
   LOG_LEVEL=INFO
   ```
   `DB_PATH=/data/forwarder.db` and `INBOX_DIR=/data/inbox` are set in the Dockerfile.
3. Service → **Settings → Volumes → Add volume**, mount path **`/data`** (keeps the SQLite DB
   across redeploys).
4. Service → **Settings → Networking → Generate domain**. That URL is the online UI
   (login: `UI_USER` / `UI_PASSWORD`).
5. Every push to `main` redeploys automatically. Check **Deployments → Logs** for
   `poller started` and `UI on http://localhost:…`.

## Notes

- Health check: `GET /api/health` (no login).
- First start polls from `since_id=0` and stores old mail once; that is expected.
- Keep `numReplicas` at 1.

## Tariffs and portal updates

- `RUNTIME_DIR` defaults to a `runtime` directory beside `DB_PATH`: `/data/runtime` in
  the container. Keep this on the same persistent volume as SQLite. Do not set it to
  the tracked `data` directory. Tracked JSON snapshots bootstrap a fresh installation.
- `python run.py` starts the tariff worker as well as the poller. Old stored tariff mails
  are backfilled once; pending jobs resume after restart. `tariff_updated` and
  `tariff_failed` events show the outcome. A malformed file leaves the last valid data.
- Unknown layouts fail visibly; they are not guessed. JPG/PNG and scanned PDF extraction
  uses the existing gateway and configured model, with separately logged tariff costs.
- Portal 429 waits are shared across rate calls; requests stop waiting when their RFQ
  budget runs out. Portal 503 asks the known airline for an email bid. The booking
  agent remains responsible for booking-specific errors.
- Board transitions other than `accepted` generate warning events (including `open`
  and `pending`); unchanged polls do not duplicate alerts. Dataset changes invalidate
  rates and trigger background snapshot refresh.
- For USD sheets/bids set `USD_EUR_RATE` (EUR per USD, from a documented source) and
  `USD_EUR_VALID_UNTIL` (UTC ISO timestamp with offset). Both are required; expired or
  missing rates exclude the USD option with a warning. Portal USD prices use only
  their own supplied `fx_eur_per_usd`. Other currencies are unsupported.
- `USE_BREAKPOINTS=true` preserves existing pricing. `python -m app.pricing.audit` prints
  the stored pricing/board evidence without sending quotes. Accepted verdicts alone
  do not establish which breakpoint formula the scorer uses; keep the default until
  comparable best-price evidence is available.
- Before deploying, run `python -m pytest -q`; then restart the sole live instance and
  check `/api/health`, poller logs and tariff events. Do not run a second production poller.

## Moving a running local instance online (keeps all mails in and out)

The local `data/forwarder.db` holds every mail we received and sent, quotes and bookings.
Move it instead of starting empty (an empty DB would re-read old mail and could re-book).

1. Railway variables: `TEAM_API_KEY=...` and `AGENTS_ENABLED=false` → deploy. The UI runs,
   nothing polls or sends.
2. On the laptop: stop `python run.py` (Ctrl+C). From now on nothing answers mail.
   Then fold the write-ahead log into the file, otherwise the newest minutes (they live in
   `forwarder.db-wal`) are missing online and the server answers those RFQs a second time:
   `python -c "import sqlite3; sqlite3.connect('data/forwarder.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"`
   Check that no other machine still runs `run.py` against the live mailbox.
3. Upload the DB (Linux/macOS):
   ```
   curl -u team:<UI_PASSWORD> --data-binary @data/forwarder.db \
     https://5u-ai-hackathon-production.up.railway.app/api/admin/import-db
   ```
   Windows PowerShell:
   ```
   curl.exe -u team:<UI_PASSWORD> --data-binary "@data/forwarder.db" https://5u-ai-hackathon-production.up.railway.app/api/admin/import-db
   ```
   The answer shows the counts and `since_id`.
4. Railway: set `AGENTS_ENABLED=true` → deploy. Polling resumes at `since_id`, so no mail is
   processed twice. Do steps 2–4 quickly: every minute here is a minute nobody answers.
