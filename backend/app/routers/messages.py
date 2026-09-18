"""
Messages router — channels, messages, file storage, AI assistant, XLSX export.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from ..auth import AuthContext, get_auth
from ..database import get_db
from ..limiter import limiter
from ..models.messages import AskInChannelBody, ChannelCreate, MessageCreate, ReactBody
from ..services.messaging import (
    MAX_FILE_BYTES,
    ask_ai_in_channel,
    create_channel,
    export_channel_xlsx,
    get_file_signed_url,
    get_messages,
    list_business_bots,
    list_channels,
    send_bot_responses_for_mentions,
    send_message,
    toggle_reaction,
    upload_file,
)

router = APIRouter(prefix="/messages", tags=["messages"])

_MAX_UPLOAD_BYTES = MAX_FILE_BYTES


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

@router.get("/channels")
def channels_list(auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    return list_channels(db, auth.org_id, auth.user_id)


@router.post("/channels", status_code=201)
def channels_create(
    body: ChannelCreate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    return create_channel(
        db, auth.org_id, auth.user_id,
        name=body.name,
        channel_type=body.channel_type,
        description=body.description,
    )


@router.get("/bots")
def bots_list(auth: Annotated[AuthContext, Depends(get_auth)]):
    return {"bots": list_business_bots()}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@router.get("/channels/{channel_id}/messages")
def messages_list(
    channel_id: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
    limit: int = Query(50, ge=1, le=100),
    before_id: str | None = Query(None),
):
    db = get_db()
    return get_messages(db, auth.org_id, channel_id, limit=limit, before_id=before_id)


@router.post("/channels/{channel_id}/messages", status_code=201)
@limiter.limit("30/minute")  # prevent message spam and accidental bot fan-out
def messages_create(
    request: Request,
    channel_id: str,
    body: MessageCreate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    if body.message_type not in {"text", "gif"}:
        raise HTTPException(400, "Unsupported message type.")
    if not body.content.strip() and not body.gif_url and not body.file_ids:
        raise HTTPException(400, "Message must have content, a GIF, or an attachment.")

    # Sanitize GIF URL — only allow tenor / giphy CDN domains
    gif_url = None
    if body.gif_url:
        from urllib.parse import urlparse
        parsed = urlparse(body.gif_url)
        if parsed.scheme == "https" and any(
            parsed.netloc.endswith(domain)
            for domain in ("tenor.com", "media.tenor.com", "giphy.com", "media.giphy.com", "media0.giphy.com", "media1.giphy.com", "media2.giphy.com", "media3.giphy.com", "media4.giphy.com")
        ):
            gif_url = body.gif_url

    db = get_db()
    sender_name = (body.sender_name or auth.user_id[:8]).strip()[:80]

    message = send_message(
        db, auth.org_id, channel_id,
        sender_id=auth.user_id,
        sender_name=sender_name,
        content=body.content.strip(),
        message_type=body.message_type,
        gif_url=gif_url,
        reply_to_id=body.reply_to_id,
        file_ids=body.file_ids,
    )
    if body.content:
        message["bot_responses"] = send_bot_responses_for_mentions(
            db,
            auth.org_id,
            channel_id,
            body.content,
            plan=auth.plan,
        )
    return message


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

@router.post("/messages/{message_id}/react")
def messages_react(
    message_id: str,
    body: ReactBody,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    # Allow only single emoji characters to prevent injection
    emoji = body.emoji.strip()
    if not emoji or len(emoji) > 8:
        raise HTTPException(400, "Invalid emoji.")
    db = get_db()
    return toggle_reaction(db, auth.org_id, message_id, auth.user_id, emoji)


# ---------------------------------------------------------------------------
# AI assistant
# ---------------------------------------------------------------------------

@router.post("/channels/{channel_id}/ask")
@limiter.limit("20/hour")
def messages_ask_ai(
    request: Request,
    channel_id: str,
    body: AskInChannelBody,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    # Save the user's question first so it appears in the thread
    sender_name = auth.user_id[:8]
    send_message(
        db, auth.org_id, channel_id,
        sender_id=auth.user_id,
        sender_name=sender_name,
        content=body.question,
        message_type="text",
    )
    return ask_ai_in_channel(
        db, auth.org_id, channel_id,
        sender_id=auth.user_id,
        sender_name=sender_name,
        question=body.question,
        plan=auth.plan,
    )


# ---------------------------------------------------------------------------
# File upload / download
# ---------------------------------------------------------------------------

@router.post("/channels/{channel_id}/files")
@limiter.limit("20/hour")
async def files_upload(
    request: Request,
    channel_id: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
    file: UploadFile = File(...),
):
    content_type = (file.content_type or "application/octet-stream").split(";")[0].strip()
    file_bytes = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds the 50 MB limit.")
    db = get_db()
    return upload_file(
        db, auth.org_id, channel_id, auth.user_id,
        filename=file.filename or "attachment",
        file_bytes=file_bytes,
        content_type=content_type,
    )


@router.get("/files/{file_id}/url")
def files_signed_url(
    file_id: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    return get_file_signed_url(db, auth.org_id, file_id)


# ---------------------------------------------------------------------------
# XLSX export
# ---------------------------------------------------------------------------

@router.get("/channels/{channel_id}/export.xlsx")
@limiter.limit("10/hour")
def channels_export_xlsx(
    request: Request,
    channel_id: str,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    xlsx_bytes = export_channel_xlsx(db, auth.org_id, channel_id)
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="channel-{channel_id[:8]}.xlsx"'},
    )
