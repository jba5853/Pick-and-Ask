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
from fastapi.staticfiles import StaticFiles

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

app = FastAPI(title="Glance")
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
        raise HTTPException(401, "Enter the access code to use Glance.")


from app.admin import router as admin_router  # noqa: E402

app.include_router(admin_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "app.html")


@app.get("/classic")
def classic():
    """The original knowledge-pack app (AI Arcade, 3D printing)."""
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


# ---------------------------------------------------------------- Point at anything (/api/...)

from app import anything as A  # noqa: E402
from app import whatsapp  # noqa: E402

app.include_router(whatsapp.router)
api_session_limit = RateLimiter(int(os.environ.get("ASKS_PER_SESSION", "80")), WINDOW)
api_ip_limit = RateLimiter(int(os.environ.get("ASKS_PER_IP", "150")), WINDOW)


def gate(request: Request, session_id: str = "") -> None:
    """Access code + rate limits for every model-backed /api call."""
    check_access(request, request.headers.get("x-access-code", ""))
    ip = client_ip(request)
    if session_id and not api_session_limit.hit(session_id):
        raise too_many(api_session_limit, session_id, "You've asked a lot in a short time.")
    if not api_ip_limit.hit(ip):
        raise too_many(api_ip_limit, ip, "Lots of requests from this network right now.")


async def read_photo(photo: UploadFile | None) -> bytes | None:
    if photo is None or not photo.filename:
        return None
    data = await photo.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Photo is too large (25 MB max).")
    try:
        return prepare_image(data)
    except ValueError as e:
        raise HTTPException(400, str(e))


def find(session_id: str) -> A.Session:
    s = A.store.get(session_id)
    if s is None:
        raise HTTPException(404, "That conversation has expired. Take a new photo to start again.")
    return s


@app.post("/api/identify")
async def api_identify(request: Request, note: str = Form(""), photo: UploadFile | None = File(None)):
    gate(request)
    jpeg = await read_photo(photo)
    if jpeg is None:
        raise HTTPException(400, "Add a photo.")
    s = A.store.new("web")
    await run_in_threadpool(A.identify, s, jpeg, note.strip()[:500])
    A.store.event(s, "new_photo")
    return A.view(s)


@app.post("/api/task")
async def api_task(request: Request, session_id: str = Form(...), action: str = Form(...), detail: str = Form("")):
    gate(request, session_id)
    s = find(session_id)
    await run_in_threadpool(A.start_task, s, action, detail.strip()[:500])
    A.store.event(s, "task")
    return A.view(s)


@app.post("/api/step")
async def api_step(request: Request, session_id: str = Form(...), op: str = Form(...)):
    check_access(request, request.headers.get("x-access-code", ""))
    if op not in {"next", "back", "repeat", "skip", "restart"}:
        raise HTTPException(400, "Unknown step op.")
    s = find(session_id)
    A.advance(s, op)
    A.store.event(s, "step")
    return A.view(s)


@app.post("/api/verify")
async def api_verify(request: Request, session_id: str = Form(...), photo: UploadFile | None = File(None)):
    gate(request, session_id)
    s = find(session_id)
    jpeg = await read_photo(photo)
    if jpeg is None:
        raise HTTPException(400, "Add a photo.")
    _, verdict = await run_in_threadpool(A.verify, s, jpeg)
    A.store.event(s, "verify")
    return {**A.view(s), "verdict": verdict}


@app.post("/api/chat")
async def api_chat(request: Request, session_id: str = Form(...), message: str = Form(""),
                   photo: UploadFile | None = File(None)):
    gate(request, session_id)
    s = find(session_id)
    jpeg = await read_photo(photo)
    if not message.strip() and jpeg is None:
        raise HTTPException(400, "Type a question.")
    await run_in_threadpool(A.chat, s, message.strip()[:1000], jpeg)
    A.store.event(s, "message")
    return A.view(s)


@app.get("/api/session/{session_id}")
def api_session(request: Request, session_id: str):
    check_access(request, request.headers.get("x-access-code", ""))
    return A.view(find(session_id))


@app.get("/api/inbox")
def api_inbox(request: Request, after: int = 0):
    """Events since `after`, so the phone page can follow what's happening on the glasses."""
    check_access(request, request.headers.get("x-access-code", ""))
    events, latest = A.store.events_after(after)
    return {"events": events, "latest": latest}


@app.get("/api/recent")
def api_recent(request: Request):
    check_access(request, request.headers.get("x-access-code", ""))
    return [A.summary(s) for s in A.store.recent()]


@app.get("/api/status")
def api_status():
    return {"model": A.MODEL, "web_search": A.WEB_SEARCH, "whatsapp": whatsapp.status(),
            "access_code_required": bool(access_code())}
