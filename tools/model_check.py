"""Check configured text models against the gateway, without completions or mailbox access."""

from app import llm
from app.config import settings


def main() -> None:
    required = {
        llm.model(),
        settings.llm_retry_model,
        settings.llm_rescue_model,
        settings.llm_compose_model,
    }
    if settings.email_review_enabled:
        required.add(settings.llm_review_model)
    try:
        available = llm.available_models(timeout_s=10)
    except Exception as exc:  # noqa: BLE001 - sanitize provider errors before terminal output
        # Never echo provider response bodies, which may contain sensitive diagnostics.
        raise SystemExit(f"Gateway model discovery failed ({type(exc).__name__})") from None
    for name in sorted(required):
        print(f"{name}: {'available' if name in available else 'NOT AVAILABLE'}")
    if missing := required - available:
        raise SystemExit(
            "Do not roll out: configured models unavailable: " + ", ".join(sorted(missing))
        )


if __name__ == "__main__":
    main()
