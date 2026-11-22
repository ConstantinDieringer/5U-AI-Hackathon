# Deploy (Railway)

## GPT-5.6 Luna rollout

The user-selected text and vision model is `gpt-5.6-luna`. Before rollout, run
`python -m tools.model_check` in a trusted environment with the existing team credential.
This reads only gateway model availability; it does not poll mail or start agents.
Do not announce model access or live use based on offline tests alone.

`LLM_TEXT_MODEL` defaults to `gpt-5.6-luna` and supersedes old Railway role-model values,
including extraction, repair, rescue, composition, image reading and optional reviewers.
Set it to another verified model to change the unified policy. Explicitly empty restores
the legacy `LLM_MODEL` and individual role settings. Luna uses low reasoning, rescue medium;
audio recognition keeps compatible transcription models. Clear old rescue
budget/context overrides or set `THREAD_RESCUE_MAX_CALLS=12`,
`THREAD_RESCUE_MAX_MAILS=24`, `THREAD_RESCUE_BODY_CHARS=8000`. Keep the optional
email reviewer disabled. Use the normal deploy gate and verify the deployed revision.

## Voice-message recognition

Set `GROQ_API_KEY` in the production service's Variables, using a key created at
https://console.groq.com/keys. `https://api.groq.com/openai/v1/audio/transcriptions` is
the endpoint, not the key. Use a Groq Free account; the application never creates an
account, upgrades a plan or submits a payment. Account billing must remain Free to
guarantee no Groq usage charges.

Recognition tries Groq `whisper-large-v3`, then the existing gateway path if its model
list permits a compatible audio model, then offline CPU Whisper `base`. A configured
`LLM_AUDIO_MODEL` must be in that allowlist; rejected models are cooled down. Missing
keys, 429s, provider outages, empty results and rejected input_audio use the next path.
`audio_fallback` and `audio_transcribed` events record the provider on the bid's session
without provider response bodies or credentials. Conflicting prices are still rejected.

The image preloads multilingual `base` into `/opt/whisper` and includes ffmpeg. Local CPU
recognition is serialized and killed at its deadline, with no runtime downloads. On a
local checkout first run `python -m app.mail.audio_local base --download`; for a custom
`LOCAL_AUDIO_MODEL` (default `SPEECH_MODEL`), preload that model at build time too. The
existing `speech.transcribe` recognizer runs in a bounded child process, without also
warming an unused model in the mail process. This has no inference API fee,
but adds image size and CPU/RAM usage. `AUDIO_TIMEOUT_S=60` bounds the provider chain;
`LOCAL_AUDIO_ENABLED=false` disables the offline safety net. `ATTACHMENT_TRANSCRIPTION=false`
disables all speech/scan recognition. Do not redeploy during an active RFQ; use the gate
below. Verify a saved MP3 before rollout, without resending mail or booking again.
Use the bundled `SPEECH_MODEL=base`; an existing service override such as `base.en`
must either be cleared/changed to `base` or explicitly baked into the new image too.

## RFQ input validation rollout

1. Use an isolated checkout for development; do not pull new modules underneath a running agent.
2. Run the full test suite with a test-only API key. With the intended CRM configured,
   `python -m app.mail.validation_replay` reads stored extractions and local attachments only:
   no mailbox polling, model calls, writes or sends. Rows without a stored extraction are
   explicitly marked and cannot establish live extraction accuracy.
3. (Historical rollout; the code default is now `enforce`.) Deploy the sole production
   instance with `RFQ_VALIDATION_MODE=shadow`. Inspect
   `validation_result` events for blocked inputs and unexpected false positives.
4. After replay acceptance, set `RFQ_VALIDATION_MODE=enforce` and restart that instance.
   Missing information triggers at most one automatic clarification. The original six-minute
   deadline and the per-session model budget (`LLM_MAX_CALLS_PER_SESSION`, thread rescue
   separately `THREAD_RESCUE_MAX_CALLS`) remain unchanged. A clarification can cost the scoring
   opportunity for the round; this is the selected product policy.
5. Check the session's validation panel and attention list. `unknown` means the send outcome
   is ambiguous; investigate manually, never retry blindly. `manual_review` means the reply
   did not resolve all required facts. Supported replies use labelled postal code, airport
   code, gross kg, cbm or complete lot lines; other wording is left to a human.
6. Roll back to `shadow` if required. Pending clarification replies are retained for manual
   review and no additional automated clarification is sent. Do not delete the workflow table.

## Team workflow: branch → PR → merge → verify

Several people and agents fix the same rounds at once. Every push to `main` restarts the live
container. So:

1. **Nobody pushes to `main` directly**, agents included (Claude, ChatGPT automation). Work on a
   branch (`fix/<round-or-topic>`), open a PR, say in #update which case you are on. If someone
   already has a PR for that case, add to it instead of opening a second one.
2. CI publishes `ship-check` for the PR. Record the owner, extension point, counterexample and measured result. For local checks use `python -m tools.ship_check`.
3. Every collaborator may merge. Announce the PR in #update and run `python -m tools.merge_pr NUMBER` from trusted main with a signed-in GitHub CLI and the read-only Railway REVIEW_TOKEN in the environment. The existing UI_USER/UI_PASSWORD login also works for the two read-only gate requests. Set PUBLIC_URL to the production HTTPS origin if needed. The command coordinates through the temporary remote branch `ops/deploy-lock`, checks the current CI and deploy gate, and waits for the merge revision to go live. After an interrupted attempt, check the PR, live revision and #update with the team before clearing a stale lock. Direct GitHub merges can bypass the command while main is unprotected. Agent sessions without REVIEW_TOKEN merge as described in `AGENTS.md`: only after a person confirms the green deploy light and no `pricing` session, then verify the live revision via `/api/health`.
4. Announce the verified commit in #update. Use `python -m app.deploy_check SHA` to recheck a specific release.

### Review agents never use the team key

The mailbox allows one poll per 5 s per team key. Every other reader takes the live poller's
slot (rounds 47 and 48 were lost that way). Review and analysis agents read our own API instead:

```
curl -H "Authorization: Bearer $REVIEW_TOKEN" $PUBLIC_URL/api/sessions
curl -H "Authorization: Bearer $REVIEW_TOKEN" "$PUBLIC_URL/api/sessions/<id>"
curl -H "Authorization: Bearer $REVIEW_TOKEN" $PUBLIC_URL/api/events
```

`REVIEW_TOKEN` (Railway variable, at least 24 characters) allows GET only; every write gets 403.
Give agents this token, never `TEAM_API_KEY` or `UI_PASSWORD`. If the live poller still gets
429 three times within 10 min, the event `mailbox_rate_limit` posts to #sos and
`/api/health` shows `mailbox_429_last_10min`: find and stop the other reader.

### Secrets

Keys and passwords never go into Slack, chats or the repo (`tests/test_ops_guardrails.py`
fails on committed OpenAI, 5U, Slack and GitHub tokens). A secret that was posted counts as
leaked: rotate it (OpenAI key in the gateway, `UI_PASSWORD` and `REVIEW_TOKEN` in Railway),
then share the new one only via Railway variables or a password manager.

## Deploy rules (during live rounds)

A push to `main` restarts the live container. While it restarts, nobody answers mail.
Rounds 4, 9, 15, 16 and 17 had one to four pushes between 2 minutes before and 4 minutes after
their RFQ and got no answer (a correlation, not a proven cause).

**The rule: every team member can merge one PR at a time while the deploy light is green.**
The customer dashboard deliberately hides deployment controls. Read the deploy light through
`GET /api/deploy-gate` (`safe`, `reason`, `seconds_to_next`) or the merge command. Green means the
newest RFQ has our answer (or its 360 s window is over) and the next RFQ is more than
`DEPLOY_GATE_MARGIN_S` (90 s) away. RFQs come about every 6 min 4 s; the spacing and phase are
read from the last arrivals, nothing is hardcoded. Railway builds before it restarts: push at
the start of a green window, not at its end. Say in the team chat who is pushing.

1. Require green ship-check for the current PR head.
2. Wait for green, then push. Never push while a session is in `pricing`.
3. After the deploy, check the log for `agents loaded: bids, booking, clarification, rfq,
   tariff, tracking, watchdog` and `poller started at since_id=...`; `/api/health` shows the new
   `process_started_at` and `build_revision`. A line `agent <name> does not
   import` (also an `agent_import_failed` event and a #sos alert) means that agent is broken
   while the others keep running: roll back to the previous deployment before the next RFQ.
4. Then open the attention list (Übersicht or `GET /api/attention`) and watch the next
   round: an `error` there means a round without a quote.
5. An RFQ that was open during the restart is resumed at startup if it is younger than
   330 s. With `RFQ_WATCHDOG=true`, any RFQ still without our quote 200 s after arrival (an
   open question, a dead end, a restart while it waited) gets the best quote real prices allow;
   no price means nothing is sent and a `watchdog_alert` appears. Each unanswered board round
   shows its cause in "Zu tun" and in `python -m app.agents.watchdog report` (one line per
   round, reads only the local DB). `mail not received` there means the mail never reached us
   (rate limit, outage); `restart at +N s` means a process start inside the round. Do not start `python run.py` on a laptop to help it along: a second instance
   answers mail twice. For local work use `AGENTS_ENABLED=false` or the demo DB.

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
   LLM_TEXT_MODEL=gpt-5.6-luna
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

## Mailbox webhooks

Deploy this code on the sole production instance before subscribing. The public receiver is
`POST /hooks/mailbox`; it uses HMAC authentication independently of the UI login. No agents,
attachment downloads or API requests run inside the HTTP handler. A 202 means the raw mail
was committed to SQLite, not that the agent already finished.

1. Keep `/data` mounted. The signing secret defaults to `/data/runtime/mailbox-webhook.secret`
   (beside `DB_PATH`); optionally set `MAILBOX_WEBHOOK_SECRET_FILE` to another private path
   on that volume. Leave `MAILBOX_WEBHOOK_SECRET` empty when using the admin command.
2. In the **running container's shell**, inspect the subscription:
   ```sh
   python -m app.mail.webhook_admin status
   ```
   A provider 404 means none exists. If one exists, inspect its URL and recent pushes first.
   Do not overwrite another receiver accidentally.
3. In that same container, subscribe once:
   ```sh
   python -m app.mail.webhook_admin subscribe https://5u-ai-hackathon-production.up.railway.app/hooks/mailbox
   python -m app.mail.webhook_admin test
   python -m app.mail.webhook_admin status
   ```
   The subscribe command writes the once-only secret atomically with private permissions
   and never prints it. It refuses an existing subscription unless `--replace` is supplied.
   Use `--replace` only for intentional rotation. A shell on a laptop, including a command
   that only imports Railway environment variables, cannot write the container's volume.
4. Confirm the test reports delivery with HTTP 204 and status shows the correct URL. Then
   inspect a real `mail.received` push (HTTP 202), the routed event and the existing CRM.
   The receiver reloads the secret file per request, so a normal subscription needs no restart.

`MAILBOX_WEBHOOK_SECRET` is an alternative for an already known secret; it overrides the
file and requires a restart when changed. Never commit either secret or paste it in logs.
Do not put subscribe in startup/deploy hooks: every PUT rotates the secret. Key rotation
removes the provider subscription, so re-subscribe intentionally afterwards.

Fallback polling always remains enabled to recover old mail and failed pushes. With a
configured secret it runs every `MAILBOX_WEBHOOK_POLL_INTERVAL_S` seconds (default 30);
without one it runs at least 5 s apart. This lowers normal polling from about 40 to 2 calls
per minute after setup. 429 honors `Retry-After` (30 s if absent), with further loop backoff.
Full result batches also wait before the next poll. Only polling advances its cursor;
webhook IDs cannot make it skip mail that has not been fetched yet.

The queue resumes pending/stored work after restart and retains failures. A crash or error
during dispatch is marked `unknown` and emits an error/warning instead of blindly repeating
an external action. Inspect the CRM and agent state manually before retrying such a mail;
the existing RFQ/booking recovery still runs. One replica remains required; this does not
guarantee exactly-once effects across provider/network failures.

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
- Foreign-currency sheets/bids (USD, JPY, GBP, CHF, ...): set `FX_EUR_RATES`
  (EUR per unit, documented source, e.g. `USD=0.92,JPY=0.0061,GBP=1.17,CHF=1.06`) and
  `FX_VALID_UNTIL` (UTC ISO timestamp with offset). A currency without a valid rate is
  recognised but its option is skipped with a warning, never guessed. The legacy
  `USD_EUR_RATE`/`USD_EUR_VALID_UNTIL` pair still covers USD if `FX_EUR_RATES` has none.
  Portal prices use the portal's own `fx_eur_per_<ccy>` when present, else the configured
  rate. Customer quotes stay in EUR.
- `USE_BREAKPOINTS=true` preserves existing pricing. `python -m app.pricing.audit` prints
  the stored pricing/board evidence without sending quotes. Accepted verdicts alone
  do not establish which breakpoint formula the scorer uses; keep the default until
  comparable best-price evidence is available.
- Before deploying, verify ship-check; then restart the sole live instance and
  check `/api/health`, poller logs and tariff events. Do not run a second production poller.
- `python -m app.agents.rfq_replay run --compare tests/fixtures/replay/baseline.json` replays
  every stored live round offline and lists what a change moves (price, airline, mail text).
  A changed price needs board evidence before the baseline is regenerated with `--out`.

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
