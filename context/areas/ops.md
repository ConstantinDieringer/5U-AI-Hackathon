# Area: Operations (hosting, deploy, alerts)

Full guide: `DEPLOY.md`. Why/history: grep `context/history/`.

- **Hosting:** one container on Railway (`Dockerfile`, `railway.json`), SQLite on a volume at `/data`, one replica only. UI behind HTTP Basic auth (`UI_USER`/`UI_PASSWORD`). The live container runs the agents; nobody runs `python run.py` against the live mailbox (a second instance answers mail twice). Local work: `AGENTS_ENABLED=false` or the demo DB.
- **Deploy rule:** a push to `main` restarts the live container. One person pushes, only while the deploy light is green (`GET /api/deploy-gate`, top bar). Run `python -m pytest -q` first; never push while a session is in `pricing`.
- **After deploy:** log shows `agents loaded: ...` and `poller started at since_id=...`; `/api/health` shows `process_started_at` and `build_revision`. An `agent_import_failed` event means roll back before the next RFQ. Restart right after every pull (mixed code versions crashed round 10).
- **Alerts:** Slack #sos via `app/ui/alerts.py` (`SLACK_SOS_WEBHOOK`): no quote possible, expired, verdict not accepted, program errors, 15 min without mail, live poller got mailbox 429 three times in 10 min (`app/mail/rate_limit_watch.py`, count in `/api/health` → `mailbox_429_last_10min`). Once per session and kind.
- **Review agents:** read `/api/sessions`, `/api/events` with `Authorization: Bearer $REVIEW_TOKEN` (GET only, ≥ 24 chars). Never the team key: one mailbox poll per 5 s per key, other readers starve the live poller (rounds 47/48).
- **Board:** `GET /v1/board/me` gives verdict and reason per round; `watchers.is_failure` emits any non-accepted verdict once. Unanswered rounds keep their cause in `board_results.cause_json`.
- **When quotes start failing** (setup changes about hourly): check our logs and `/v1/board/me` first, find what changed, fix the root cause, add a Session Note in the tracker.
- Stopping `run.py` with an open SSE client needs `kill -9` or closing the UI first.
- **Live check:** after a merge `python -m app.deploy_check` waits until `build_revision` equals `origin/main`; only then "live" in #update.
- Git: branch + PR for every change, never a direct push to `main` (agents included); CI (`.github/workflows/ci.yml`: pytest + secrets scan) must be green; one merge owner per hour; commit messages in English with `feat:`/`fix:`/`chore:`/`docs:`/`test:` (TECH_SPEC §11).
