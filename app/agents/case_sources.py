"""Case-local source discovery; models never choose filesystem paths or download URLs."""

from __future__ import annotations

import dataclasses
import json

from app import db
from app.agents.case_models import Citation, Source, ToolError, Workspace
from app.mail import documents, parse
from app.mail.content import current_message
from app.mail.router import address
from app.models import Mail


def stored_mail(sid: int, mid: int) -> Mail:
    session = db.get_session(sid)
    row = next((m for m in (session or {}).get("mails", []) if m["id"] == mid), None)
    if row is None or row["category"] == "phishing":
        raise ToolError("Message is not readable in this case")
    header = db.mail_header(mid) or {}
    if header.get("raw_json"):
        raw = json.loads(header["raw_json"])
        if raw.get("id") == mid:
            try:
                return parse.mail_from_api(raw)
            except parse.MailFormatError:
                pass
    return Mail(
        mid,
        mid,
        row["sender"] or "",
        row["recipient"] or "",
        row["subject"] or "",
        row["body"] or "",
        row["received_at"],
    )


def discover(sid: int, workspace: Workspace) -> None:
    session = db.get_session(sid)
    if session is None:
        raise ToolError("Case no longer exists")
    for row in session["mails"]:
        if row["category"] == "phishing":
            continue
        mail = stored_mail(sid, row["id"])
        role = (
            "outgoing"
            if row["direction"] == "out"
            else "customer"
            if address(mail.sender) == address(session["customer"])
            else "carrier"
        )
        key = f"mail-{mail.id}"
        if key not in workspace.sources:
            current = current_message(mail.body, mail.subject)
            workspace.sources[key] = Source(
                id=key,
                mail_id=mail.id,
                role=role,
                name=mail.subject,
                text=current.subject + "\n" + current.facts,
            )
        if row["direction"] != "in":
            continue
        for attachment in mail.attachments:
            key = f"mail-{mail.id}-attachment-{attachment['id']}"
            if key not in workspace.sources:
                workspace.sources[key] = Source(
                    id=key,
                    mail_id=mail.id,
                    role=role,
                    name=str(attachment.get("filename") or attachment["id"]),
                    text="",
                    attachment_id=int(attachment["id"]),
                    read=False,
                )


def read_source(sid: int, workspace: Workspace, key: str, offset: int = 0) -> dict:
    from app.config import settings
    from app.mail.poller import _safe_filename, download_attachments

    source = workspace.sources.get(key)
    if source is None:
        raise ToolError("Unknown source; inspect_case lists available source IDs")
    if not source.read:
        mail = stored_mail(sid, source.mail_id)
        download_attachments(mail)
        path = (
            settings.inbox_dir
            / str(mail.id)
            / f"{source.attachment_id}-{_safe_filename(source.name)}"
        )
        try:
            document = documents.read(path)
            text = document.text
            if document.needs_model:
                if not settings.attachment_transcription:
                    raise ToolError("Attachment transcription is disabled")
                text = documents.transcribe(document, sid, "case_attachment", path)
            if not text.strip():
                raise ToolError("Source contains no readable evidence")
            source.text, source.read, source.problem = text, True, None
        except Exception as exc:
            source.problem = type(exc).__name__
            raise ToolError(
                "Source could not be read; ask its sender for a readable replacement"
            ) from exc
    if offset < 0 or offset > len(source.text):
        raise ToolError("Offset is outside the source")
    return {
        "source": key,
        "text": source.text[offset : offset + 16000],
        "offset": offset,
        "next_offset": offset + 16000 if offset + 16000 < len(source.text) else None,
        "length": len(source.text),
        "role": source.role,
    }


def cited(workspace: Workspace, citations: list[Citation], *, customer: bool = True) -> str:
    excerpts = []
    for citation in citations:
        source = workspace.sources.get(citation.source)
        if (
            source is None
            or not source.read
            or source.superseded_by is not None
            or (customer and source.role != "customer")
        ):
            raise ToolError("Citation requires a readable source from the appropriate party")
        if citation.text not in source.text:
            raise ToolError("Citation is not an exact passage in its named original source")
        excerpts.append(citation.text)
    if not excerpts:
        raise ToolError("At least one exact citation is required")
    return "\n\n".join(excerpts)


def shipment_mail(mail: Mail, workspace: Workspace, citations: list[Citation]) -> Mail:
    return dataclasses.replace(
        mail, subject="Current shipment", body=cited(workspace, citations), attachments=[], raw={}
    )
