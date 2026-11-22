"""Links in incoming mail: find them, check them, and only then fetch them.

5U may start sending rate sheets, bids or shipping documents as a link instead of an
attachment. Nothing here "clicks" in a browser: a link that passes `check_url` is downloaded
with `fetch` and saved next to the mail's attachments (data/inbox/<mail_id>/link<n>-<name>),
so `documents.read` handles it like any attachment (PDF, XLSX, photo, voice message ...).
`linked_files` is called by rfq_sources (RFQs) and the bid agent (carrier bids).

Which links are opened: only links to a file (the path ends in a type documents.py reads) or
to a file host from LINK_FILE_HOSTS (Google Drive, Dropbox, SharePoint ...). A company
website in a signature is never opened. At most 3 links per mail. LINK_FETCH=false = none.

Safety check (`check_url`), deterministic, no LLM, no cost per quote:
- only http/https, ports 80/443, no "user@host" tricks, URL length capped
- no IP-address hosts (also decimal/hex/octal forms), no localhost/.local/.internal
- no punycode hosts (xn--, look-alike domains) and no non-ASCII hosts
- no URL shorteners (target is hidden)
- no executable or macro file types (.exe, .js, .docm, .zip ...)
- the host must resolve, and every resolved address must be public (no 10.x, 127.x,
  169.254.x cloud metadata, ::1 ...). This stops a link from reaching our own server.
- HTML mail: link text that shows one domain but points to another is blocked (phishing)
- optional allowlist: LINK_ALLOWED_DOMAINS="a.com,b.org" lets only those domains through
- a blocked link is never opened; the session gets a warning event, the mail text still counts

Fetch (`fetch`): redirects are followed by hand (max 3) and every hop is checked again;
5 s timeout per request, 8 s for all links of a mail, 5 MB cap, only document/image/text content types, never runs scripts.

Known gap: DNS is resolved for the check and again by urllib for the download. A host that
changes its answer in between (DNS rebinding) could slip through; the content-type and size
limits still apply. Acceptable for the hackathon, noted for the codebase review.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from app.config import settings
from app.mail import documents
from app.models import Mail

log = logging.getLogger(__name__)

Resolver = Callable[[str], list[str]]
LINK_PREFIX = "link"

MAX_LINKS_PER_MAIL = 3
MAX_URL_LEN = 2048
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 5.0
TOTAL_S = 8.0  # all links of one mail together: the bid agent runs in the mail loop
MAX_REDIRECTS = 3

# Our own and 5U's domains are trusted; carrier domains can be added via the env allowlist.
TRUSTED_DOMAINS = {"hackloop.mail", "5u.ai"}

SHORTENERS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "goo.gl",
    "ow.ly",
    "is.gd",
    "buff.ly",
    "rebrand.ly",
    "cutt.ly",
    "shorturl.at",
    "tiny.cc",
    "rb.gy",
    "s.id",
    "t.ly",
    "lnkd.in",
    "bl.ink",
    "shorte.st",
    "adf.ly",
    "v.gd",
    "qr.ae",
    "trib.al",
}

BLOCKED_EXTENSIONS = {
    ".exe",
    ".scr",
    ".com",
    ".bat",
    ".cmd",
    ".msi",
    ".ps1",
    ".vbs",
    ".js",
    ".jse",
    ".jar",
    ".apk",
    ".dmg",
    ".pkg",
    ".app",
    ".iso",
    ".img",
    ".lnk",
    ".hta",
    ".reg",
    ".dll",
    ".sh",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
    ".tar",
    ".docm",
    ".xlsm",
    ".pptm",
    ".dotm",
    ".xltm",
}

ALLOWED_CONTENT_TYPES = (
    "application/pdf",
    "text/plain",
    "text/csv",
    "text/html",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/tiff",
    "image/bmp",
    "image/heic",
    "image/heif",
    "image/avif",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/msword",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.text",
    "application/rtf",
    "text/rtf",
    "application/octet-stream",  # many file hosts; extension check above still applies
)

_URL_RE = re.compile(r"""\bhttps?://[^\s<>"'`]+""", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>'\""
_BAD_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")


@dataclass
class Verdict:
    ok: bool
    url: str
    reason: str = ""


@dataclass
class Fetched:
    url: str
    content_type: str
    data: bytes
    hops: list[str] = field(default_factory=list)


class LinkBlocked(RuntimeError):
    pass


# ---------------------------------------------------------------- finding links


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []  # (href, visible text)
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href.strip(), "".join(self._text).strip()))
            self._href = None


def _strip(url: str) -> str:
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    return url


def extract_links(
    text: str = "", html: str = "", limit: int | None = MAX_LINKS_PER_MAIL
) -> list[tuple[str, str]]:
    """Links in a mail as (url, visible text). Plain-text links have empty visible text.
    Deduplicated, in order, at most `limit` (None: all)."""
    found: list[tuple[str, str]] = []
    if html:
        p = _AnchorParser()
        try:
            p.feed(html)
        except Exception:  # broken markup: the plain-text scan below still runs
            log.debug("html links: markup not parsed", exc_info=True)
        found += [(h, t) for h, t in p.anchors if h.lower().startswith(("http://", "https://"))]
        found += [(_strip(u), "") for u in _URL_RE.findall(re.sub(r"<[^>]+>", " ", html))]
    found += [(_strip(u), "") for u in _URL_RE.findall(text or "")]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for url, shown in found:
        if url in seen:
            # keep the anchor version (it carries the visible text for the mismatch check)
            continue
        seen.add(url)
        out.append((url, shown))
    return out if limit is None else out[:limit]


# ---------------------------------------------------------------- checking links


def _domain_matches(host: str, domains: set[str] | frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _looks_like_ip(host: str) -> bool:
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    # 2130706433, 0x7f000001, 0177.0.0.1, 127.1: all are 127.0.0.1 to a resolver
    return bool(re.fullmatch(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}", h, re.IGNORECASE))


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({i[4][0] for i in infos})


def _host_of_text(shown: str) -> str | None:
    m = re.search(
        r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:[/:]|\s|$)", shown.strip(), re.IGNORECASE
    )
    return m.group(1).lower() if m else None


def _registrable(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def check_url(url: str, shown_text: str = "", resolve: Resolver = _resolve) -> Verdict:
    """Is it safe to download this link? Deterministic; the only I/O is a DNS lookup."""
    if len(url) > MAX_URL_LEN:
        return Verdict(False, url, "URL zu lang")
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return Verdict(False, url, "URL nicht lesbar")
    if u.scheme.lower() not in ("http", "https"):
        return Verdict(False, url, f"Schema {u.scheme or '?'} nicht erlaubt")
    if u.username or u.password or "@" in u.netloc:
        return Verdict(False, url, "Benutzername in der URL (user@host-Trick)")
    host = (u.hostname or "").lower().rstrip(".")
    if not host:
        return Verdict(False, url, "kein Host")
    try:
        port = u.port
    except ValueError:
        return Verdict(False, url, "ungültiger Port")
    if port not in (None, 80, 443):
        return Verdict(False, url, f"Port {port} nicht erlaubt")
    if _looks_like_ip(host):
        return Verdict(False, url, "IP-Adresse statt Domain")
    if not host.isascii() or "xn--" in host:
        return Verdict(False, url, "internationalisierte Domain (Verwechslungsgefahr)")
    if host == "localhost" or host.endswith(_BAD_HOST_SUFFIXES) or "." not in host:
        return Verdict(False, url, "interner Host")
    if _domain_matches(host, SHORTENERS):
        return Verdict(False, url, "Kurz-URL, Ziel unbekannt")
    path = urllib.parse.unquote(u.path or "").lower()
    ext = os.path.splitext(path)[1]
    if ext in BLOCKED_EXTENSIONS:
        return Verdict(False, url, f"gefährlicher Dateityp {ext}")
    if shown_text:
        shown_host = _host_of_text(shown_text)
        if shown_host and _registrable(shown_host) != _registrable(host):
            return Verdict(False, url, f"Linktext zeigt {shown_host}, Ziel ist {host}")
    allow = settings.link_allowed_domains
    if allow and not _domain_matches(host, allow | TRUSTED_DOMAINS):
        return Verdict(False, url, "Domain nicht auf der Allowlist")
    try:
        addrs = resolve(host)
    except (socket.gaierror, UnicodeError, OSError):
        return Verdict(False, url, "Domain nicht auflösbar")
    if not addrs:
        return Verdict(False, url, "Domain nicht auflösbar")
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a.split("%")[0])
        except ValueError:
            return Verdict(False, url, f"unbekannte Adresse {a}")
        if not ip.is_global or ip.is_multicast:
            return Verdict(False, url, f"zeigt auf internes Netz ({a})")
    return Verdict(True, url)


# ---------------------------------------------------------------- fetching links


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # follow by hand so every hop is checked


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(
    url: str,
    shown_text: str = "",
    resolve: Resolver = _resolve,
    opener: urllib.request.OpenerDirector | None = None,
) -> Fetched:
    """Download a checked link. Raises LinkBlocked with a German reason when anything fails."""
    opener = opener or _opener
    hops: list[str] = []
    current, shown = url, shown_text
    for _ in range(MAX_REDIRECTS + 1):
        v = check_url(current, shown, resolve=resolve)
        if not v.ok:
            raise LinkBlocked(v.reason if not hops else f"Weiterleitung blockiert: {v.reason}")
        hops.append(current)
        req = urllib.request.Request(
            current, headers={"User-Agent": "AustrianPower-MailBot/1.0", "Accept": "*/*"}
        )
        try:
            resp = opener.open(req, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                current = urllib.parse.urljoin(current, e.headers["Location"])
                shown = ""
                continue
            raise LinkBlocked(f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LinkBlocked(f"nicht erreichbar ({type(e).__name__})") from None
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not any(ctype == t for t in ALLOWED_CONTENT_TYPES):
                raise LinkBlocked(f"Inhaltstyp {ctype or '?'} nicht erlaubt")
            if ctype == "text/html" and not _html_expected(current):
                raise LinkBlocked("Webseite statt Datei")
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_BYTES:
                raise LinkBlocked("Datei größer als 5 MB")
            data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise LinkBlocked("Datei größer als 5 MB")
            return Fetched(url=current, content_type=ctype, data=data, hops=hops)
    raise LinkBlocked("zu viele Weiterleitungen")


def _html_expected(url: str) -> bool:
    """A web page is only wanted when the link names an .html file or comes from 5U; a file
    host's viewer or virus-scan page is not the document."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.path.lower().endswith((".html", ".htm")) or _domain_matches(host, TRUSTED_DOMAINS)


# ---------------------------------------------------------------- which links, saved where


def wanted(url: str) -> bool:
    """Open only links to a file: a readable file type in the path, or a known file host."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    ext = os.path.splitext(urllib.parse.unquote(u.path or "").lower())[1]
    if ext in documents.SUPPORTED or ext in BLOCKED_EXTENSIONS:
        return True  # dangerous types are "wanted" so the check reports them
    return _domain_matches(host, settings.link_file_hosts | TRUSTED_DOMAINS)


_DRIVE = re.compile(r"^/file/d/([\w-]+)")
# Shared Google Sheets/Docs open an editor page; their export URL returns the file itself.
_GOOGLE_EXPORT = {"spreadsheets": "xlsx", "document": "docx"}
_GOOGLE_DOC = re.compile(r"^/(spreadsheets|document)/d/([\w-]+)")


def download_url(url: str) -> str:
    """The direct download behind a share link (Google Drive view page, Google Sheets/Docs
    editor, Dropbox preview)."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if host == "drive.google.com" and (m := _DRIVE.match(u.path)):
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if host == "docs.google.com" and (m := _GOOGLE_DOC.match(u.path)):
        kind, doc_id = m.groups()
        gid = dict(urllib.parse.parse_qsl(u.fragment)).get("gid") or dict(
            urllib.parse.parse_qsl(u.query)
        ).get("gid")
        export = f"https://docs.google.com/{kind}/d/{doc_id}/export?format={_GOOGLE_EXPORT[kind]}"
        return export + (f"&gid={gid}" if gid and gid.isdigit() and kind == "spreadsheets" else "")
    if _domain_matches(host, {"dropbox.com"}):
        q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "dl"] + [("dl", "1")]
        return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))
    return url


_SUFFIX_FOR_TYPE = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/heic": ".heic",
    "image/heif": ".heic",
    "image/avif": ".avif",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/msword": ".doc",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
}


def _file_name(n: int, fetched: Fetched) -> str:
    from app.mail.poller import _safe_filename

    base = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(fetched.url).path))
    base = _safe_filename(base) if base else "file"
    if not os.path.splitext(base)[1]:
        base += _SUFFIX_FOR_TYPE.get(fetched.content_type, "")
    return f"{LINK_PREFIX}{n}-{base}"


def _saved(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p
        for p in directory.glob(f"{LINK_PREFIX}*-*")
        if p.is_file() and not p.name.endswith(documents.TRANSCRIPT_SUFFIX)
    )


def linked_files(
    mail: Mail,
    session_id: int | None = None,
    *,
    local_only: bool = False,
    resolve: Resolver = _resolve,
    opener: urllib.request.OpenerDirector | None = None,
) -> list[Path]:
    """Files behind the links in a mail, saved next to its attachments. Already saved files
    are reused (a replay or restart never opens a link twice). Blocked or failed links are
    reported as a warning event and skipped: the mail text and attachments still count."""
    target = settings.inbox_dir / str(mail.id)
    saved = _saved(target)
    if saved or local_only or not settings.link_fetch:
        return saved
    body = mail.body or ""
    # The cap counts file links only: a website, terms and LinkedIn link before the rate
    # sheet must not use up the slots (the fourth link was never opened).
    candidates = [
        (url, shown)
        for url, shown in extract_links(
            text=body, html=body if "<a" in body.lower() else "", limit=None
        )
        if wanted(url)
    ][:MAX_LINKS_PER_MAIL]
    paths: list[Path] = []
    deadline = time.monotonic() + TOTAL_S
    for n, (url, shown) in enumerate(candidates, start=1):
        if time.monotonic() > deadline:
            _warn(session_id, url, "keine Zeit mehr für weitere Links")
            break
        try:
            fetched = fetch(download_url(url), shown, resolve=resolve, opener=opener)
        except LinkBlocked as exc:
            _warn(session_id, url, str(exc))
            continue
        target.mkdir(parents=True, exist_ok=True)
        path = target / _file_name(n, fetched)
        path.write_bytes(fetched.data)
        log.info("mail %s: link %s saved as %s", mail.id, url[:120], path.name)
        paths.append(path)
    return paths


def _warn(session_id: int | None, url: str, reason: str) -> None:
    from app import db

    shown = url if len(url) <= 80 else url[:77] + "..."
    log.warning("link not opened (%s): %s", reason, shown)
    try:
        db.emit(
            "link_blocked", f"Link nicht geöffnet: {reason} ({shown})", session_id, level="warn"
        )
    except Exception:
        log.exception("link warning not stored")
