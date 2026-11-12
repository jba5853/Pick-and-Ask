"""FastAPI app: serves the page and the /ask endpoint."""

import hmac
import logging
import os
import re
import uuid
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

load_dotenv()  # picks up ANTHROPIC_API_KEY from .env if present; never logged

from app.conversation import BUTTON_TEXT, SessionStore, converse  # noqa: E402
from app.images import prepare_image  # noqa: E402
from app.limits import RateLimiter  # noqa: E402
from app.packs import list_packs, load_pack  # noqa: E402

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit("ANTHROPIC_API_KEY is not set. Export it or put it in .env (see README).")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
STATIC = Path(__file__).parent / "static"
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
ACTIONS = {"message", *BUTTON_TEXT}

# Limits, all in memory. The per-IP limit backs up the per-session one, since session ids come from the page.
ASKS_PER_SESSION, ASKS_PER_IP, WRONG_CODES_PER_IP, WINDOW = 20, 60, 10, 10 * 60
LOCAL_PEERS = {"127.0.0.1", "::1"}

app = FastAPI(title="Point and Ask")
sessions = SessionStore()  # in memory only; idle sessions expire after 30 minutes
session_limit = RateLimiter(ASKS_PER_SESSION, WINDOW)
ip_limit = RateLimiter(ASKS_PER_IP, WINDOW)
wrong_codes = RateLimiter(WRONG_CODES_PER_IP, WINDOW)


def access_code() -> str:
    """The shared code from ACCESS_CODE. Empty means no code is required (local use only)."""
    return os.environ.get("ACCESS_CODE", "").strip()


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    # Behind the Cloudflare tunnel every request arrives from localhost; the tunnel says who sent it.
    if peer in LOCAL_PEERS and request.headers.get("cf-connecting-ip"):
        return request.headers["cf-connecting-ip"].strip()
    return peer


def too_many(limiter: RateLimiter, key: str, message: str) -> HTTPException:
    wait = limiter.retry_after(key)
    minutes = max(1, round(wait / 60))
    return HTTPException(429, f"{message} Try again in about {minutes} minute{'s' if minutes != 1 else ''}.",
                         headers={"Retry-After": str(wait)})


def check_access(request: Request, given: str) -> None:
    """Reject requests without the shared access code. Repeated wrong codes lock that IP out for a while."""
    code = access_code()
    if not code:
        return
    ip = client_ip(request)
    if wrong_codes.blocked(ip):
        raise too_many(wrong_codes, ip, "Too many wrong access codes.")
    if not hmac.compare_digest(given.strip().encode(), code.encode()):
        wrong_codes.hit(ip)
        raise HTTPException(401, "Enter the access code to use Point and Ask.")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/access")
def check_access_code(request: Request, code: str = Form("")):
    """Lets the page check the code once before the first question."""
    check_access(request, code)
    return Response(status_code=204)


@app.get("/config")
def config():
    return {"access_code_required": bool(access_code())}


@app.get("/packs")
def packs():
    out = []
    for pack_id in list_packs():
        p = load_pack(pack_id)
        out.append({"id": p.id, "name": p.name, "description": p.description, "mode": p.mode})
    return out


@app.post("/ask")
async def ask_endpoint(
    request: Request,
    question: str = Form(""),
    pack: str = Form("ai-arcade"),
    session_id: str = Form(""),
    action: str = Form("message"),
    photo: UploadFile | None = File(None),
):
    check_access(request, request.headers.get("x-access-code", ""))
    try:
        knowledge = load_pack(pack)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if action not in ACTIONS:
        raise HTTPException(400, f"Unknown action: {action}")
    if session_id and not SESSION_ID.match(session_id):
        raise HTTPException(400, "Bad session id.")
    has_photo = photo is not None and bool(photo.filename)
    if action == "message" and not question.strip() and not has_photo:
        raise HTTPException(400, "Type a question or add a photo.")

    ip = client_ip(request)
    if session_id and not session_limit.hit(session_id):
        raise too_many(session_limit, session_id, "You've asked a lot in a short time.")
    if not ip_limit.hit(ip):
        raise too_many(ip_limit, ip, "Lots of questions from this network right now.")

    image = None
    if has_photo:
        data = await photo.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Photo is too large (25 MB max).")
        try:
            image = prepare_image(data)
        except ValueError as e:
            raise HTTPException(400, str(e))

    session = sessions.get(session_id or str(uuid.uuid4()), knowledge.id)
    # The model call blocks, so run it off the event loop.
    result = await run_in_threadpool(converse, session, knowledge, question, action, image)
    return asdict(result)
