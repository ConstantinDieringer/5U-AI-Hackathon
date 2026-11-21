"""The only HTTP client to the 5U API (mailbox, board). Field names: context/docs/mail-format.md."""

from __future__ import annotations

import logging
import threading
import time
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
    method: str, path: str, *, safe: bool = True, deadline: float | None = None, **kw: Any
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
            if deadline is not None and time.monotonic() + wait >= deadline:
                raise HackloopError(
                    method,
                    path,
                    last.status_code if last is not None else None,
                    "Retry would exceed RFQ portal budget",
                )
            log.warning("%s %s failed (%s), retry in %.1fs", method, path, err, wait)
            time.sleep(wait)
    raise HackloopError(method, path, last.status_code if last else None, err)


def _json_obj(resp: httpx.Response) -> dict[str, Any]:
    data = resp.json()
    if not isinstance(data, dict):
        raise HackloopError(
            resp.request.method, resp.request.url.path, resp.status_code, "expected a JSON object"
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
    return _request("GET", f"/v1/mailbox/messages/{message_id}/attachments/{attachment_id}").content


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
        payload["in_reply_to"] = in_reply_to
    sent = _json_obj(_request("POST", "/v1/mailbox/messages", safe=False, json=payload))
    if references and sent.get("references") != [*references]:
        log.warning(
            "server references %s differ from expected %s", sent.get("references"), references
        )
    log.info("sent mail %s to %s: %s", sent.get("id"), to, subject[:80])
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


# ---------------------------------------------------------------- board


def get_board_me() -> dict[str, Any]:
    return _json_obj(_request("GET", "/v1/board/me"))
