"""Start everything: `python run.py` (from the repo root).

- mail poller in a daemon thread
- RFQ watchdog in a daemon thread (RFQ_WATCHDOG=true, agents only)
- C's background watchers (deadline, board sync) in daemon threads
- C's UI server (FastAPI) via uvicorn in the main thread
"""

from __future__ import annotations

import logging
import threading

from app.config import settings  # loads .env first (db.py reads DB_PATH on import)


def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll is noise
    log = logging.getLogger("run")

    if not settings.ui_password:
        log.error("UI_PASSWORD missing: dashboard locked; mail agents and signed webhook continue")

    from app import db
    from app.mail import poller, webhook

    db.init_db()
    from app import process

    try:
        process.record_start(settings.agents_enabled)
    except Exception:
        log.exception("could not record the process start: starting anyway")
    _check_agents(log)
    stop = threading.Event()
    if not settings.agents_enabled:
        log.warning("AGENTS_ENABLED=false: UI only, no polling or sending")
        _serve_ui(log, stop)
        return
    db.delivery_recover()
    from app.agents import booking
    from app.pricing import served_lanes

    served_lanes.backfill()  # "We fly ..." lists from refusals already in the mailbox
    from app.agents import clarification, rfq

    if settings.rfq_validation_mode == "enforce":
        clarification.catch_up()
        threading.Thread(
            target=clarification.run, args=(stop,), name="clarifications", daemon=True
        ).start()

    rfq.catch_up()  # RFQs that were mid-pricing when the app stopped
    if settings.rfq_watchdog:
        # after catch_up: resumed workers count as live, the rest is due at once
        from app.agents import watchdog

        threading.Thread(
            target=watchdog.run, args=(stop,), name="rfq-watchdog", daemon=True
        ).start()
    booking.catch_up()  # acceptances that arrived while the app was down or had no booking
    from app.agents import followup

    threading.Thread(target=followup.run, args=(stop,), name="followups", daemon=True).start()
    from app.agents import tariff

    db.queue_stored_tariffs()

    threading.Thread(target=tariff.run, args=(stop,), name="tariffs", daemon=True).start()
    threading.Thread(target=webhook.run, args=(stop,), name="mail-delivery", daemon=True).start()
    threading.Thread(target=poller.run, args=(stop,), name="poller", daemon=True).start()

    _serve_ui(log, stop)


def _check_agents(log: logging.Logger) -> None:
    """A module that does not import shows in the deploy log (and #sos) right after the start,
    not at the first mail that needs it. The start goes on: the other agents keep working."""
    from app import db
    from app.agents import check_imports

    loaded, failed = check_imports()
    log.info("agents loaded: %s", ", ".join(loaded) or "none")
    for name, error in failed.items():
        log.error("agent %s does not import: %s", name, error)
        db.emit("agent_import_failed", f"Agent {name} lädt nicht: {error}", level="error")


def _serve_ui(log: logging.Logger, stop: threading.Event) -> None:
    try:
        from app.ui import server, watchers
    except ImportError as e:
        log.warning("UI not available (%s): running the poller only", e)
        try:
            stop.wait()
        except KeyboardInterrupt:
            stop.set()
        return

    import uvicorn

    watchers.start()
    log.info("UI on http://localhost:%s", settings.ui_port)
    try:
        uvicorn.run(server.app, host="0.0.0.0", port=settings.ui_port, log_level="warning")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
