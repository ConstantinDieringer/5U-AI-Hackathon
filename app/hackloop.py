"""The only HTTP client to the 5U API (mailbox, board). Field names: context/docs/mail-format.md."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_RETRIES = 2
RETRY_PAUSE_S = 1.0
MAX_RETRY_AFTER_S = 10.0
MAX_ATTACHMENT_BYTES = 20_000_000

_client = httpx.Client(
    base_url=settings.base_url,
    headers={"X-API-Key": settings.api_key},
    timeout=TIMEOUT,
)


class HackloopError(RuntimeError):
    """A 5U API call failed after retries. The message never contains the key."""

    def __init__(self, method: str, path: str, status: int | None, detail: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {detail[:300]}")
        self.status = status
        self.detail = detail  # full body, e.g. the 404/503 hint where to ask by email


def _retry_wait(resp: httpx.Response | None) -> float:
    if resp is not None and resp.status_code == 429:
        try:
            return max(0.0, float(resp.headers.get("Retry-After", RETRY_PAUSE_S)))
        except ValueError:
            try:
                return max(
                    0.0,
                    (
                        parsedate_to_datetime(resp.headers["Retry-After"]) - datetime.now(UTC)
                    ).total_seconds(),
                )
            except (KeyError, ValueError, TypeError):
                return RETRY_PAUSE_S
    return RETRY_PAUSE_S


def _request(
    method: str,
    path: str,
    *,
    safe: bool = True,
    deadline: float | None = None,
    **kw: Any,
) -> httpx.Response:
    """Send with retries. `safe=False` (sending mail) retries only when the request surely did
    not arrive (connect error) or was rejected by the rate limit (429), never on 5xx/timeouts,
    so a mail is never sent twice."""
    last: httpx.Response | None = None
    for attempt in range(MAX_RETRIES + 1):
        if path == "/v1/portal/rates":
            _wait_rate_gate(deadline)
        try:
            request_kw = dict(kw)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HackloopError(method, path, 429, "RFQ portal budget exhausted")
                request_kw["timeout"] = min(10.0, remaining)
            resp = _client.request(method, path, **request_kw)
        except httpx.ConnectError as e:
            last, err = None, f"connect error: {e}"
        except httpx.TransportError as e:
            if not safe:
                raise HackloopError(method, path, None, f"transport error: {e}") from e
            last, err = None, f"transport error: {e}"
        else:
            if path == "/v1/portal/rates":
                _update_rate_gate(resp)
            # 503 is an answer, not an outage: it says where to get the rate/booking instead.
            retryable = resp.status_code == 429 or (
                safe and resp.status_code >= 500 and resp.status_code != 503
            )
            if not retryable:
                if resp.is_error:
                    raise HackloopError(method, path, resp.status_code, resp.text)
                return resp
            last, err = resp, f"status {resp.status_code}"
        if attempt < MAX_RETRIES:
            wait = _retry_wait(last)
            if (
                path == "/v1/mailbox/messages"
                and method == "GET"
                and last is not None
                and last.status_code == 429
                and "Retry-After" not in last.headers
            ):
                wait = 30.0
            if deadline is not None and time.monotonic() + wait >= deadline:
                raise HackloopError(
                    method,
                    path,
                    last.status_code if last is not None else None,
                    "Retry would exceed RFQ portal budget",
                )
            log.warning("%s %s failed (%s), retry in %.1fs", method, path, err, wait)
            time.sleep(wait)
    failure = HackloopError(method, path, last.status_code if last is not None else None, err)
    failure.retry_after = (
        _retry_wait(last) if last is not None and "Retry-After" in last.headers else 30.0
    )
    raise failure


def _json_obj(resp: httpx.Response) -> dict[str, Any]:
    data = resp.json()
    if not isinstance(data, dict):
        raise HackloopError(
            resp.request.method,
            resp.request.url.path,
            resp.status_code,
            "expected a JSON object",
        )
    return data


# ---------------------------------------------------------------- team


def me() -> dict[str, Any]:
    return _json_obj(_request("GET", "/v1/me"))


# ---------------------------------------------------------------- mailbox


def poll(since_id: int = 0, limit: int = 200) -> dict[str, Any]:
    """Mail sent to us with id > since_id, oldest first. Returns the raw poll object
    (`messages`, `next_since_id`, see mail-format.md)."""
    return _json_obj(
        _request("GET", "/v1/mailbox/messages", params={"since_id": since_id, "limit": limit})
    )


def get_message(message_id: int) -> dict[str, Any]:
    return _json_obj(_request("GET", f"/v1/mailbox/messages/{message_id}"))


def get_attachment(message_id: int, attachment_id: int) -> bytes:
    method = "GET"
    path = f"/v1/mailbox/messages/{message_id}/attachments/{attachment_id}"
    for attempt in range(MAX_RETRIES + 1):
        try:
            with _client.stream(method, path) as resp:
                retryable = resp.status_code == 429 or (
                    resp.status_code >= 500 and resp.status_code != 503
                )
                if retryable:
                    if attempt < MAX_RETRIES:
                        time.sleep(min(MAX_RETRY_AFTER_S, _retry_wait(resp)))
                        continue
                    raise HackloopError(method, path, resp.status_code, "retry limit reached")
                if resp.is_error:
                    detail = b"".join(resp.iter_bytes())[:300].decode("utf-8", "replace")
                    raise HackloopError(method, path, resp.status_code, detail)
                length = resp.headers.get("Content-Length")
                if length and length.isdecimal() and int(length) > MAX_ATTACHMENT_BYTES:
                    raise HackloopError(method, path, 413, "attachment exceeds 20 MB")
                content = bytearray()
                for chunk in resp.iter_bytes():
                    if len(content) + len(chunk) > MAX_ATTACHMENT_BYTES:
                        raise HackloopError(method, path, 413, "attachment exceeds 20 MB")
                    content.extend(chunk)
                return bytes(content)
        except HackloopError:
            raise
        except httpx.ConnectError as exc:
            if attempt == MAX_RETRIES:
                raise HackloopError(method, path, None, f"connect error: {exc}") from exc
            time.sleep(RETRY_PAUSE_S)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES:
                raise HackloopError(method, path, None, f"transport error: {exc}") from exc
            time.sleep(RETRY_PAUSE_S)
    raise HackloopError(method, path, None, "attachment download failed")


class QuarantinedReply(RuntimeError):
    """A reply to a mail the phishing gate quarantined; a person must release it first."""


def send(
    to: str,
    subject: str,
    body: str,
    in_reply_to: int | None = None,
    references: list[int] | None = None,
) -> dict[str, Any]:
    """Send a mail; returns the stored mail (poll-item shape).

    The API takes only `in_reply_to`; the server builds `references` (and the
    In-Reply-To/References threading) from it, verified in A3. `references` is accepted
    for callers' convenience and only checked against what the server stored.
    """
    payload: dict[str, Any] = {"to": to, "subject": subject, "body": body}
    if in_reply_to is not None:
        from app import db

        try:  # last line of the phishing gate, for every sender
            blocked = db.reply_blocked(in_reply_to)
        except Exception:
            log.exception("quarantine check failed for mail %s: sending", in_reply_to)
            blocked = False
        if blocked:
            raise QuarantinedReply(f"mail {in_reply_to} is quarantined as phishing")
        payload["in_reply_to"] = in_reply_to
    sent = _json_obj(_request("POST", "/v1/mailbox/messages", safe=False, json=payload))
    if references and sent.get("references") != [*references]:
        log.warning(
            "server references %s differ from expected %s",
            sent.get("references"),
            references,
        )
    log.info("sent mail %s to %s: %s", sent.get("id"), to, subject[:80])
    return sent


class CustomerSendBlocked(RuntimeError):
    """Review completed, but current customer controls or the deadline forbid sending."""


def send_customer(
    to: str,
    subject: str,
    body: str,
    in_reply_to: int | None = None,
    references: list[int] | None = None,
    session_id: int | None = None,
    protected_terms: tuple[str, ...] = (),
    allow_manual: bool = False,
    deadline_at: float | None = None,
    review: bool = True,
    before_transport: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """All customer replies pass the final prose renderer before transport. review=False skips
    the LLM style reviewer for fixed, pre-written templates (no gateway cost).
    `before_transport` runs after the last control check, right before the HTTP send: from
    then on the mail may have gone out, so a caller can record that durably."""
    from app import db
    from app.agents.email_review import review_customer_mail
    from app.mail.language import detect
    from app.mail.outgoing import customer_body, customer_subject, validate_customer_text

    def allowed() -> None:
        if session_id is not None and (
            db.is_paused(session_id) or (db.is_manual(session_id) and not allow_manual)
        ):
            raise CustomerSendBlocked("Session is paused or under manual control")
        if deadline_at is not None and time.time() >= deadline_at:
            raise CustomerSendBlocked("Customer reply deadline has expired")

    allowed()
    lang = detect(body)
    subject = customer_subject(subject, lang)
    rendered = customer_body(body, lang)
    if review:
        rendered = customer_body(
            review_customer_mail(rendered, lang, session_id, protected_terms), lang
        )
    validate_customer_text(rendered)
    validate_customer_text(subject)
    allowed()
    if before_transport is not None:
        before_transport()
    options: dict[str, Any] = {}
    if in_reply_to is not None:
        options["in_reply_to"] = in_reply_to
    if references is not None:
        options["references"] = references
    sent = dict(send(to=to, subject=subject, body=rendered, **options))
    sent["body"] = rendered
    sent["subject"] = subject
    return sent


# ---------------------------------------------------------------- portal

rate_limit: dict[str, str] = {}  # last X-RateLimit-* headers seen on /v1/portal/rates
_rate_lock = threading.Lock()
_rate_blocked_until = 0.0
_rate_window_start = 0.0
_rate_used = 0
_rate_cap = 10


def _wait_rate_gate(deadline: float | None) -> None:
    global _rate_window_start, _rate_used
    now = time.monotonic()
    if now - _rate_window_start >= 60:
        _rate_window_start, _rate_used = now, 0
    until = max(_rate_blocked_until, _rate_window_start + 60 if _rate_used >= _rate_cap else now)
    if deadline is not None and until >= deadline:
        raise HackloopError("GET", "/v1/portal/rates", 429, "Rate limit exceeds RFQ budget")
    if until > now:
        time.sleep(until - now)
    if time.monotonic() - _rate_window_start >= 60:
        _rate_window_start, _rate_used = time.monotonic(), 0
    _rate_used += 1


def _update_rate_gate(resp: httpx.Response) -> None:
    global _rate_blocked_until, _rate_cap
    rate_limit.update(
        {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
    )
    try:
        _rate_cap = max(1, int(resp.headers.get("X-RateLimit-Limit", _rate_cap)))
        if int(resp.headers.get("X-RateLimit-Remaining", "1")) == 0:
            _rate_blocked_until = max(_rate_blocked_until, _rate_window_start + 60)
    except ValueError:
        pass
    if resp.status_code == 429:
        _rate_blocked_until = max(_rate_blocked_until, time.monotonic() + _retry_wait(resp))


def get_ports() -> dict[str, Any]:
    """`{"lanes": [{origin, destination, airlines: [{code, name}]}]}` (not rate-limited)."""
    return _json_obj(_request("GET", "/v1/portal/ports"))


def get_brackets(airline: str) -> dict[str, Any]:
    """`{airline, airline_code, breaks_kg}`. 404 (HackloopError.detail) names the email."""
    return _json_obj(_request("GET", "/v1/portal/brackets", params={"airline": airline}))


def get_rate(
    origin: str,
    destination: str,
    airline: str,
    weight_kg: float,
    volume_cbm: float = 0.0,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """One priced rate (rate-limited per minute; 429 is retried after Retry-After)."""
    params: dict[str, Any] = {
        "origin": origin,
        "destination": destination,
        "airline": airline,
        "weight_kg": weight_kg,
        "volume_cbm": volume_cbm,
    }
    acquired = (
        _rate_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if deadline is not None
        else _rate_lock.acquire()
    )
    if not acquired:
        raise HackloopError("GET", "/v1/portal/rates", 429, "Portal queue exceeded RFQ budget")
    try:
        resp = _request("GET", "/v1/portal/rates", params=params, deadline=deadline)
    finally:
        _rate_lock.release()
    rate_limit.update(
        {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
    )
    return _json_obj(resp)


def book(rate_id: str, team_reference: str) -> dict[str, Any]:
    """Book a portal rate. 201 = booked, 200 = same booking again (idempotent).
    404/409/410/503 raise HackloopError (status + detail; 503 says how to book by email)."""
    return _json_obj(
        _request(
            "POST",
            "/v1/portal/bookings",
            json={"rate_id": rate_id, "team_reference": team_reference},
        )
    )


# ---------------------------------------------------------------- board


def get_board_me() -> dict[str, Any]:
    return _json_obj(_request("GET", "/v1/board/me"))


def webhook_status() -> dict[str, Any]:
    return _json_obj(_request("GET", "/v1/mailbox/webhook"))


def webhook_subscribe(url: str) -> dict[str, Any]:
    # Subscription creates/rotates a secret: never retry an ambiguous response automatically.
    return _json_obj(_request("PUT", "/v1/mailbox/webhook", safe=False, json={"url": url}))


def webhook_test() -> dict[str, Any]:
    return _json_obj(_request("POST", "/v1/mailbox/webhook/test", safe=False))
