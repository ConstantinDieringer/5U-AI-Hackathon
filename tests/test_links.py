import io
import urllib.error
from dataclasses import replace
from email.message import Message

import pytest

from app.mail import links
from app.mail.links import (
    LinkBlocked,
    check_url,
    download_url,
    extract_links,
    fetch,
    linked_files,
    wanted,
)
from app.models import Mail

PUBLIC = lambda host: ["93.184.216.34"]


def ok(url, shown="", resolve=PUBLIC):
    return check_url(url, shown, resolve=resolve).ok


# ------------------------------------------------------------ allowed


@pytest.mark.parametrize(
    "url",
    [
        "https://files.lufthansa-cargo.com/rates/nov.pdf",
        "https://drive.google.com/file/d/abc/view",
        "http://example.com/bid.xlsx",
        "https://example.com:443/photo.jpg",
        "https://rates.hackloop.mail/sheet.csv",
    ],
)
def test_normal_links_pass(url):
    assert ok(url)


# ------------------------------------------------------------ blocked


@pytest.mark.parametrize(
    "url,reason",
    [
        ("javascript:alert(1)", "Schema"),
        ("file:///etc/passwd", "Schema"),
        ("ftp://example.com/a.pdf", "Schema"),
        ("https://example.com@evil.com/x", "Benutzername"),
        ("http://127.0.0.1/admin", "IP-Adresse"),
        ("http://169.254.169.254/latest/meta-data", "IP-Adresse"),
        ("http://2130706433/", "IP-Adresse"),
        ("http://0x7f000001/", "IP-Adresse"),
        ("http://[::1]/", "IP-Adresse"),
        ("http://localhost:8000/api/sessions", "Port"),
        ("http://localhost/api/sessions", "interner Host"),
        ("http://printer.local/", "interner Host"),
        ("https://xn--lufthansa-cargo-xyz.com/", "internationalisierte"),
        ("https://bit.ly/3abc", "Kurz-URL"),
        ("https://example.com/invoice.exe", "Dateityp"),
        ("https://example.com/rates.xlsm", "Dateityp"),
        ("https://example.com/docs.zip", "Dateityp"),
        ("https://example.com:8080/a.pdf", "Port"),
        ("https://example.com/" + "a" * 3000, "zu lang"),
    ],
)
def test_dangerous_links_blocked(url, reason):
    v = check_url(url, resolve=PUBLIC)
    assert not v.ok and reason in v.reason, v.reason


def test_domain_resolving_to_private_ip_blocked():
    v = check_url("https://innocent.example.com/a.pdf", resolve=lambda h: ["10.0.0.5"])
    assert not v.ok and "internes Netz" in v.reason


def test_cloud_metadata_via_dns_blocked():
    v = check_url("https://meta.example.com/", resolve=lambda h: ["169.254.169.254"])
    assert not v.ok


def test_one_private_among_public_blocked():
    v = check_url("https://x.example.com/", resolve=lambda h: ["93.184.216.34", "127.0.0.1"])
    assert not v.ok


def test_unresolvable_blocked():
    def boom(h):
        raise OSError("nx")

    assert not check_url("https://nope.example.com/", resolve=boom).ok


def test_link_text_mismatch_blocked():
    v = check_url("https://evil-login.ru/x", "https://www.lufthansa-cargo.com", resolve=PUBLIC)
    assert not v.ok and "Linktext" in v.reason


def test_link_text_same_domain_ok():
    assert ok("https://files.lufthansa-cargo.com/a.pdf", "lufthansa-cargo.com/rates")


def test_link_text_without_domain_ok():
    assert ok("https://example.com/a.pdf", "hier klicken")


def test_allowlist(monkeypatch):
    monkeypatch.setattr(
        links,
        "settings",
        replace(links.settings, link_allowed_domains=frozenset({"lufthansa-cargo.com"})),
    )
    assert ok("https://files.lufthansa-cargo.com/a.pdf")
    assert ok("https://x.hackloop.mail/a.pdf")  # always trusted
    assert not ok("https://example.com/a.pdf")


# ------------------------------------------------------------ extraction


def test_extract_plain_text_strips_punctuation():
    got = extract_links("Rates here: https://example.com/a.pdf. Thanks (https://b.com/x)")
    assert [u for u, _ in got] == ["https://example.com/a.pdf", "https://b.com/x"]


def test_extract_html_keeps_visible_text_and_dedupes():
    html = '<p>See <a href="https://evil.ru/x">www.lufthansa-cargo.com</a></p>'
    got = extract_links(text="", html=html)
    assert got[0] == ("https://evil.ru/x", "www.lufthansa-cargo.com")
    assert len(got) == 1


def test_extract_caps_number_of_links():
    text = " ".join(f"https://e{i}.com/a" for i in range(10))
    assert len(extract_links(text)) == links.MAX_LINKS_PER_MAIL


# ------------------------------------------------------------ fetching


class FakeResp(io.BytesIO):
    def __init__(self, data, ctype, length=None):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Type"] = ctype
        if length is not None:
            self.headers["Content-Length"] = str(length)


class FakeOpener:
    def __init__(self, routes):
        self.routes = routes
        self.opened = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.opened.append(url)
        r = self.routes[url]
        if isinstance(r, Exception):
            raise r
        return r


def redirect(url, to):
    h = Message()
    h["Location"] = to
    return urllib.error.HTTPError(url, 302, "Found", h, None)


def test_fetch_pdf():
    op = FakeOpener({"https://example.com/a.pdf": FakeResp(b"%PDF-1.4", "application/pdf")})
    f = fetch("https://example.com/a.pdf", resolve=PUBLIC, opener=op)
    assert f.data == b"%PDF-1.4" and f.content_type == "application/pdf"


def test_fetch_follows_safe_redirect():
    op = FakeOpener(
        {
            "https://example.com/r": redirect(
                "https://example.com/r", "https://cdn.example.com/a.png"
            ),
            "https://cdn.example.com/a.png": FakeResp(b"png", "image/png"),
        }
    )
    f = fetch("https://example.com/r", resolve=PUBLIC, opener=op)
    assert f.url == "https://cdn.example.com/a.png" and len(f.hops) == 2


def test_fetch_blocks_redirect_to_internal():
    op = FakeOpener(
        {
            "https://example.com/r": redirect("https://example.com/r", "http://169.254.169.254/"),
        }
    )
    with pytest.raises(LinkBlocked, match="Weiterleitung"):
        fetch("https://example.com/r", resolve=PUBLIC, opener=op)
    assert op.opened == ["https://example.com/r"]  # the internal address was never opened


def test_fetch_redirect_loop_stops():
    op = FakeOpener(
        {
            "https://example.com/a": redirect("https://example.com/a", "https://example.com/b"),
            "https://example.com/b": redirect("https://example.com/b", "https://example.com/a"),
        }
    )
    with pytest.raises(LinkBlocked, match="zu viele"):
        fetch("https://example.com/a", resolve=PUBLIC, opener=op)


def test_fetch_rejects_wrong_content_type():
    op = FakeOpener({"https://example.com/a": FakeResp(b"x", "application/x-msdownload")})
    with pytest.raises(LinkBlocked, match="Inhaltstyp"):
        fetch("https://example.com/a", resolve=PUBLIC, opener=op)


def test_fetch_rejects_too_large():
    op = FakeOpener({"https://example.com/a.pdf": FakeResp(b"x", "application/pdf", 10**8)})
    with pytest.raises(LinkBlocked, match="5 MB"):
        fetch("https://example.com/a.pdf", resolve=PUBLIC, opener=op)


def test_fetch_rejects_too_large_without_header():
    big = b"x" * (links.MAX_BYTES + 10)
    op = FakeOpener({"https://example.com/a.pdf": FakeResp(big, "application/pdf")})
    with pytest.raises(LinkBlocked, match="5 MB"):
        fetch("https://example.com/a.pdf", resolve=PUBLIC, opener=op)


def test_fetch_never_opens_blocked_url():
    op = FakeOpener({})
    with pytest.raises(LinkBlocked):
        fetch("https://bit.ly/x", resolve=PUBLIC, opener=op)
    assert op.opened == []


# ------------------------------------------------------------ which links, saved where


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/rates.pdf", True),
        ("https://example.com/photo.JPG", True),
        ("https://example.com/voice.mp3", True),
        ("https://example.com/setup.exe", True),  # opened by nobody: the check reports it
        ("https://drive.google.com/file/d/abc/view", True),
        ("https://www.dropbox.com/s/x/bid?dl=0", True),
        ("https://www.weidner-spielwaren.de", False),  # signature website
        ("https://www.linkedin.com/in/someone", False),
    ],
)
def test_only_file_links_are_wanted(url, expected):
    assert wanted(url) is expected


def test_share_links_become_direct_downloads():
    assert download_url("https://drive.google.com/file/d/1AbC-x_9/view?usp=sharing") == (
        "https://drive.google.com/uc?export=download&id=1AbC-x_9"
    )
    assert download_url("https://www.dropbox.com/s/k/bid.pdf?dl=0") == (
        "https://www.dropbox.com/s/k/bid.pdf?dl=1"
    )
    assert download_url("https://example.com/a.pdf") == "https://example.com/a.pdf"


def _mail(body, mail_id=4711):
    return Mail(
        id=mail_id,
        thread_root=mail_id,
        sender="ops@carrier.example",
        to="austrian-power@hackloop.mail",
        subject="Offer",
        body=body,
        received_at="2026-11-21T22:00:00Z",
    )


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(
        links, "settings", replace(links.settings, inbox_dir=tmp_path, link_fetch=True)
    )
    events = []
    monkeypatch.setattr(links, "_warn", lambda sid, url, reason: events.append(reason))
    return tmp_path, events


def test_linked_file_is_saved_next_to_attachments(inbox):
    tmp, events = inbox
    op = FakeOpener({"https://example.com/bid.pdf": FakeResp(b"%PDF-1.4", "application/pdf")})
    mail = _mail("Our offer: https://example.com/bid.pdf\nhttps://www.carrier-site.com")
    paths = linked_files(mail, 1, resolve=PUBLIC, opener=op)
    assert [p.name for p in paths] == ["link1-bid.pdf"]
    assert paths[0].read_bytes() == b"%PDF-1.4"
    assert paths[0].parent == tmp / "4711"
    assert op.opened == ["https://example.com/bid.pdf"]  # the website was not opened
    assert events == []


def test_saved_links_are_never_fetched_twice(inbox):
    op = FakeOpener({"https://example.com/bid.pdf": FakeResp(b"%PDF-1.4", "application/pdf")})
    mail = _mail("https://example.com/bid.pdf")
    linked_files(mail, 1, resolve=PUBLIC, opener=op)
    again = linked_files(mail, 1, resolve=PUBLIC, opener=op)
    assert [p.name for p in again] == ["link1-bid.pdf"] and len(op.opened) == 1


def test_blocked_link_warns_and_is_skipped(inbox):
    _, events = inbox
    op = FakeOpener({})
    paths = linked_files(
        _mail("rates: http://169.254.169.254/rates.pdf"), 1, resolve=PUBLIC, opener=op
    )
    assert paths == [] and op.opened == [] and "IP-Adresse" in events[0]


def test_file_host_viewer_page_is_not_a_document(inbox):
    _, events = inbox
    url = "https://drive.google.com/uc?export=download&id=abc"
    op = FakeOpener({url: FakeResp(b"<html>virus scan</html>", "text/html")})
    paths = linked_files(
        _mail("https://drive.google.com/file/d/abc/view"), 1, resolve=PUBLIC, opener=op
    )
    assert paths == [] and "Webseite" in events[0]


def test_suffix_from_content_type(inbox):
    url = "https://drive.google.com/uc?export=download&id=abc"
    op = FakeOpener({url: FakeResp(b"\x89PNG", "image/png")})
    paths = linked_files(
        _mail("https://drive.google.com/file/d/abc/view"), 1, resolve=PUBLIC, opener=op
    )
    assert paths[0].suffix == ".png"


def test_local_only_and_switch_off_never_fetch(inbox, monkeypatch):
    op = FakeOpener({})
    assert linked_files(_mail("https://example.com/a.pdf"), local_only=True, opener=op) == []
    monkeypatch.setattr(links, "settings", replace(links.settings, link_fetch=False))
    assert linked_files(_mail("https://example.com/a.pdf"), opener=op) == []
    assert op.opened == []


def test_rfq_reads_linked_file_as_evidence(inbox):
    from app.mail import rfq_sources

    url = "https://example.com/packing-list.csv"
    op = FakeOpener({url: FakeResp(b"pieces,kg\n4,192\n", "text/csv")})
    mail = _mail(f"Please quote MUC to ORD, packing list: {url}", mail_id=9001)
    linked_files(mail, 1, resolve=PUBLIC, opener=op)  # saved once, then read from disk
    texts, media, problems = rfq_sources.read_sources(mail, local_only=True)
    assert problems == [] and media == []
    assert "192" in texts["link1-packing-list.csv"]
