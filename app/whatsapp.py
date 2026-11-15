"""WhatsApp bridge: the hands-free path from the glasses.

"Hey Meta, take a photo and send it to Glance on WhatsApp."
  -> the photo arrives at this webhook
  -> we identify it and reply with a short message
  -> the glasses read the reply aloud ("Hey Meta, read my messages", or automatically)
  -> the user answers by voice ("Hey Meta, reply: set it up", "reply done") or sends another photo to check a step.
Everything also shows up live in the phone app, which polls /api/inbox.

Uses the WhatsApp Cloud API (free test number). Settings in .env:
  WA_TOKEN             access token from the Meta app dashboard (WhatsApp > API Setup)
  WA_PHONE_NUMBER_ID   the test number's "Phone number ID"
  WA_VERIFY_TOKEN      any string you choose; type the same one in the webhook settings
  WA_APP_SECRET        optional, App settings > Basic > App secret; turns on signature checks
  WA_ALLOWED_NUMBERS   optional, comma separated, e.g. 15551234567; only these senders get answers
  WA_GRAPH_VERSION     optional, default v21.0
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from collections import OrderedDict

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app import anything as A
from app.images import prepare_image
from app.router import handle_text, menu_text

log = logging.getLogger("point_and_ask")
router = APIRouter()

_seen: OrderedDict[str, None] = OrderedDict()   # WhatsApp retries deliveries; answer each message once
_by_sender: dict[str, str] = {}                  # phone number -> session id
_lock = threading.Lock()


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def enabled() -> bool:
    return bool(cfg("WA_TOKEN") and cfg("WA_PHONE_NUMBER_ID"))


def graph(path: str) -> str:
    return f"https://graph.facebook.com/{cfg('WA_GRAPH_VERSION', 'v21.0')}/{path}"


def auth() -> dict:
    return {"Authorization": f"Bearer {cfg('WA_TOKEN')}"}


def allowed(sender: str) -> bool:
    allow = {n.strip().lstrip("+") for n in cfg("WA_ALLOWED_NUMBERS").split(",") if n.strip()}
    return not allow or sender.lstrip("+") in allow


def send_text(to: str, body: str) -> None:
    if not enabled() or not body:
        return
    try:
        r = httpx.post(graph(f"{cfg('WA_PHONE_NUMBER_ID')}/messages"), headers=auth(), timeout=20, json={
            "messaging_product": "whatsapp", "to": to, "type": "text",
            "text": {"body": body[:3900], "preview_url": False}})
        if r.status_code >= 300:
            log.error("WhatsApp send failed: %s %s", r.status_code, r.text[:300])
    except httpx.HTTPError as e:
        log.error("WhatsApp send error: %s", type(e).__name__)


def download_media(media_id: str) -> bytes:
    meta = httpx.get(graph(media_id), headers=auth(), timeout=20)
    meta.raise_for_status()
    blob = httpx.get(meta.json()["url"], headers=auth(), timeout=60, follow_redirects=True)
    blob.raise_for_status()
    return blob.content


def session_for(sender: str, fresh: bool = False) -> A.Session:
    with _lock:
        sid = _by_sender.get(sender)
    s = A.store.get(sid) if sid and not fresh else None
    if s is None:
        s = A.store.new(channel="whatsapp")
        with _lock:
            _by_sender[sender] = s.id
    return s


def reply_for_identify(s: A.Session) -> str:
    if s.stage == "stopped":
        return A.STOP_MESSAGE
    if s.stage == "need_info":
        return s.messages[-1]["text"] if s.messages else "What would you like help with?"
    return f"{s.obj.get('spoken') or 'Got it.'} {menu_text(s)}"


def handle_message(msg: dict) -> None:
    sender, kind = msg.get("from", ""), msg.get("type")
    try:
        if kind == "image":
            caption = (msg.get("image", {}).get("caption") or "").strip()
            jpeg = prepare_image(download_media(msg["image"]["id"]))
            current = session_for(sender)
            if current.stage == "coaching" and current.step and "new" not in caption.lower():
                send_text(sender, "Checking your photo…")
                before = current.msg_seq
                s, out = A.verify(current, jpeg)
                after = [m["text"] for m in s.messages if m["n"] > before and m["role"] == "assistant"]
                A.store.event(s, "verify")
                send_text(sender, " ".join(after) or out.get("feedback", ""))
                return
            s = session_for(sender, fresh=True)
            send_text(sender, "Got it, taking a look…")
            A.identify(s, jpeg, caption)
            A.store.event(s, "new_photo")
            send_text(sender, reply_for_identify(s))
        elif kind in ("text", "interactive", "button"):
            body = (msg.get("text", {}).get("body")
                    or msg.get("interactive", {}).get("button_reply", {}).get("title")
                    or msg.get("button", {}).get("text") or "")
            s = session_for(sender)
            if not s.obj:
                send_text(sender, "Hi! I'm Glance. Send me a photo of anything you need help with, "
                                  "like \"Hey Meta, send a photo to Glance.\"")
                return
            replies = handle_text(s, body)
            A.store.event(s, "message")
            send_text(sender, " ".join(replies))
        elif kind == "audio":
            send_text(sender, "I can't play voice notes yet. Try \"Hey Meta, reply\" and say your answer, "
                              "and it'll come through as text.")
        else:
            send_text(sender, "Send me a photo of what you need help with, or reply with text.")
    except Exception:  # never let one bad message break the webhook
        log.exception("WhatsApp message failed")
        send_text(sender, "Sorry, something went wrong on my side. Try that again in a moment.")


@router.get("/webhooks/whatsapp")
def verify_webhook(request: Request):
    q = request.query_params
    token = cfg("WA_VERIFY_TOKEN")
    if token and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


@router.post("/webhooks/whatsapp")
async def receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    secret = cfg("WA_APP_SECRET")
    if secret:
        sent = request.headers.get("x-hub-signature-256", "")
        good = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sent, good):
            raise HTTPException(403, "Bad signature")
    try:
        payload = await request.json()
    except Exception:
        return {"ok": True}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                mid = msg.get("id", "")
                with _lock:
                    if mid in _seen:
                        continue
                    _seen[mid] = None
                    while len(_seen) > 500:
                        _seen.popitem(last=False)
                if not allowed(msg.get("from", "")):
                    log.warning("WhatsApp message from a number not in WA_ALLOWED_NUMBERS; ignored")
                    continue
                background.add_task(handle_message, msg)  # answer Meta fast; do the work after
    return {"ok": True}


def status() -> dict:
    return {"enabled": enabled(), "verify_token_set": bool(cfg("WA_VERIFY_TOKEN")),
            "signature_check": bool(cfg("WA_APP_SECRET")), "allow_list": bool(cfg("WA_ALLOWED_NUMBERS"))}
