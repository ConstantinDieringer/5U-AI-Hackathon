# Deploy (Railway)

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
