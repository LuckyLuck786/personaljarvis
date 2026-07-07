"""Email tool: IMAP read + draft. Configure IMAP_HOST/USER/PASSWORD in .env
(for Gmail: an app password). Reading is `read` permission; drafting only
produces text (stored as a note) — actually SENDING email is deliberately
not implemented yet and will be a `destructive` tool when it is.

Email bodies are untrusted input; the agent loop fences tool output as data.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import imaplib

from jarvis.tools.registry import Tool, ToolContext, ToolError

MAX_MESSAGES = 10
MAX_BODY_CHARS = 1500


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for chunk, enc in email.header.decode_header(value):
        parts.append(chunk.decode(enc or "utf-8", errors="replace")
                     if isinstance(chunk, bytes) else chunk)
    return "".join(parts)


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          errors="replace")
        return "(no text/plain part)"
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8",
                          errors="replace") if payload else ""


def _fetch_unread_sync(host: str, user: str, password: str, folder: str) -> list[dict]:
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select(folder, readonly=True)  # readonly: reading never mutates flags
        _, data = imap.search(None, "UNSEEN")
        ids = data[0].split()[-MAX_MESSAGES:]
        out = []
        for msg_id in reversed(ids):
            _, fetched = imap.fetch(msg_id, "(RFC822)")
            msg = email.message_from_bytes(fetched[0][1])
            out.append({
                "from": _decode(msg.get("From")),
                "subject": _decode(msg.get("Subject")),
                "date": msg.get("Date", ""),
                "body": _body_text(msg)[:MAX_BODY_CHARS],
            })
        return out


async def email_unread(args: dict, ctx: ToolContext) -> str:
    s = ctx.cfg.secrets
    if not (s.imap_host and s.imap_user and s.imap_password):
        return ("Email is not configured. Set IMAP_HOST, IMAP_USER, "
                "IMAP_PASSWORD in .env to enable it.")
    try:
        messages = await asyncio.to_thread(
            _fetch_unread_sync, s.imap_host, s.imap_user, s.imap_password, s.imap_folder
        )
    except imaplib.IMAP4.error as exc:
        raise ToolError(f"IMAP error: {exc}")
    if not messages:
        return "No unread email."
    blocks = [
        f"From: {m['from']}\nSubject: {m['subject']}\nDate: {m['date']}\n{m['body']}"
        for m in messages
    ]
    return f"{len(messages)} unread message(s):\n\n" + "\n---\n".join(blocks)


async def email_draft(args: dict, ctx: ToolContext) -> str:
    to = (args.get("to") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = (args.get("body") or "").strip()
    if not (to and body):
        raise ToolError("to and body are required")
    draft = f"DRAFT EMAIL\nTo: {to}\nSubject: {subject}\n\n{body}"
    doc_id = await ctx.store.ingest(draft, kind="note", source="email_draft",
                                    tags=("personal",))
    return (f"Draft saved to memory (doc {doc_id}). Sending is not implemented "
            "yet — copy it into your mail client.")


TOOLS = [
    Tool(
        name="email_unread",
        description="Read unread email headers + short bodies over IMAP (read-only).",
        params={},
        handler=email_unread,
        permission="read",
    ),
    Tool(
        name="email_draft",
        description=("Compose an email draft and store it in memory. Does NOT "
                     "send — say so to the operator."),
        params={"to": {"type": "string", "required": True},
                "subject": {"type": "string"},
                "body": {"type": "string", "required": True}},
        handler=email_draft,
        permission="act",
    ),
]
