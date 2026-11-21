"""Start everything: `python run.py` (from the repo root).

- mail poller in a daemon thread
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

    from app import db
    from app.mail import poller

    db.init_db()
    from app.agents import booking
    from app.pricing import served_lanes

    served_lanes.backfill()  # "We fly ..." lists from refusals already in the mailbox
    from app.agents import rfq

    rfq.catch_up()  # RFQs that were mid-pricing when the app stopped
    booking.catch_up()  # acceptances that arrived while the app was down or had no booking
    stop = threading.Event()
    from app.agents import tariff

    db.queue_stored_tariffs()

    threading.Thread(target=tariff.run, args=(stop,), name="tariffs", daemon=True).start()
    threading.Thread(target=poller.run, args=(stop,), name="poller", daemon=True).start()

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
