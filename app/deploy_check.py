"""Wait until the live forwarder runs a given commit. Only then is a fix "live".

    python -m app.deploy_check            # expects the newest origin/main
    python -m app.deploy_check 3f9c2ab    # expects this commit (prefix is enough)

Polls the public `GET /api/health` (no credentials needed) and compares `build_revision`
(Railway's RAILWAY_GIT_COMMIT_SHA) with the expected commit. Exit 0 when it matches, 1 on timeout,
2 when the expected commit cannot be determined. Reads nothing else and changes nothing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import httpx

DEFAULT_URL = os.getenv("PUBLIC_URL", "https://5u-ai-hackathon-production.up.railway.app")
MIN_PREFIX = 7


def matches(expected: str, running: str) -> bool:
    """True if both name the same commit; a prefix of at least 7 hex chars is enough."""
    a, b = expected.strip().lower(), running.strip().lower()
    if len(a) < MIN_PREFIX or len(b) < MIN_PREFIX or b == "unknown":
        return False
    return a.startswith(b) or b.startswith(a)


def origin_main() -> str | None:
    """Newest commit of origin/main after a fetch, or None outside a git checkout."""
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], check=True, timeout=60)
        out = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def running_revision(url: str, client: httpx.Client) -> tuple[str, str]:
    """(build_revision, process_started_at) of the live instance; ('', '') if unreachable."""
    try:
        r = client.get(f"{url.rstrip('/')}/api/health", timeout=10)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return "", ""
    return str(data.get("build_revision") or ""), str(data.get("process_started_at") or "")


def wait_for(expected: str, url: str, timeout_s: float, every_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    with httpx.Client() as client:
        while True:
            running, started = running_revision(url, client)
            if matches(expected, running):
                print(f"LIVE: {running[:12]} seit {started}")
                return True
            shown = running[:12] if running else "nicht erreichbar"
            print(f"läuft noch: {shown}, erwartet {expected[:12]}")
            if time.monotonic() + every_s > deadline:
                return False
            time.sleep(every_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("commit", nargs="?", help="expected commit (default: origin/main)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds (default 600)")
    args = ap.parse_args(argv)
    expected = args.commit or origin_main()
    if not expected or len(expected) < MIN_PREFIX:
        print("Erwarteter Commit unbekannt (mind. 7 Zeichen angeben).", file=sys.stderr)
        return 2
    if wait_for(expected, args.url, args.timeout):
        return 0
    print(f"NICHT LIVE nach {int(args.timeout)} s. Railway-Deployments prüfen.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
