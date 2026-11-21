"""Run on the deployed host: python -m app.mail.webhook_admin status|test|subscribe URL.

Subscription secret is written privately on the server and never printed.
"""

import argparse
import json
import os
import tempfile
from urllib.parse import urlparse

from app import hackloop
from app.config import settings


def subscribe(url: str, replace: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
        raise ValueError("Use a public HTTPS webhook URL without user info or fragment")
    if settings.webhook_secret:
        raise ValueError(
            "Use secret-file mode before subscribing: an env secret overrides the new file"
        )
    try:
        hackloop.webhook_status()
    except hackloop.HackloopError as exc:
        if exc.status != 404:
            raise
    else:
        if not replace:
            raise ValueError(
                "Subscription already exists; inspect status and use --replace only for intentional rotation"
            )
    path = settings.webhook_secret_file
    path.parent.mkdir(parents=True, exist_ok=True)
    # Verify writable storage before creating the subscription.
    fd, name = tempfile.mkstemp(prefix="webhook-", dir=path.parent)
    persisted = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(name, 0o600)
            response = hackloop.webhook_subscribe(url)
            secret = response.get("secret")
            if not isinstance(secret, str) or not secret:
                raise ValueError(
                    "Subscribe response lacks secret; inspect provider docs before retrying"
                )
            stream.write(secret)
            stream.flush()
            os.fsync(stream.fileno())
        persisted = True
        try:
            os.replace(name, path)
        except OSError as exc:
            raise RuntimeError(
                f"Subscription created; recover its private secret file at {name} into {path}. "
                "Do not subscribe again."
            ) from exc
    finally:
        if not persisted and os.path.exists(name):
            os.unlink(name)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["status", "test", "subscribe"])
    p.add_argument("url", nargs="?")
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()
    if args.action == "subscribe":
        if not args.url:
            p.error("subscribe requires the deployed public HTTPS endpoint")
        subscribe(args.url, args.replace)
        print("Subscription saved. Signing secret stored privately; run the test command next.")
    elif args.action == "test":
        print(json.dumps(hackloop.webhook_test(), indent=2))
    else:
        print(json.dumps(hackloop.webhook_status(), indent=2))


if __name__ == "__main__":
    main()
