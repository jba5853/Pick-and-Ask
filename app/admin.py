"""Staff pages: the AI pack builder, draft review/publish, and insights. Protected by ADMIN_CODE."""

import hmac
import logging
import os
import threading
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import builder, insights
from app.limits import RateLimiter

log = logging.getLogger("point_and_ask")

STATIC = Path(__file__).parent / "static"
MIN_ADMIN_CODE = 8
WRONG_ADMIN_CODES, WINDOW = 5, 10 * 60

router = APIRouter()
wrong_admin_codes = RateLimiter(WRONG_ADMIN_CODES, WINDOW)
_build_lock = threading.Lock()


def admin_code() -> str:
    return os.environ.get("ADMIN_CODE", "").strip()


def require_admin(request: Request) -> None:
    """Every admin API call needs the X-Admin-Code header. Admin is off unless ADMIN_CODE is set."""
    from app.main import client_ip, too_many  # shared helpers; imported late to avoid a cycle

    code = admin_code()
    if len(code) < MIN_ADMIN_CODE:
        raise HTTPException(403, f"Staff pages are turned off. Set ADMIN_CODE ({MIN_ADMIN_CODE}+ characters) in .env.")
    ip = client_ip(request)
    if wrong_admin_codes.blocked(ip):
        raise too_many(wrong_admin_codes, ip, "Too many wrong staff codes.")
    given = request.headers.get("x-admin-code", "").strip()
    if not hmac.compare_digest(given.encode(), code.encode()):
        wrong_admin_codes.hit(ip)
        raise HTTPException(401, "Enter the staff code.")


def user_error(e: Exception) -> HTTPException:
    if isinstance(e, FileNotFoundError):
        return HTTPException(404, str(e))
    return HTTPException(400, str(e))


# ---------- pages (the data behind them needs the code) ----------

@router.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


@router.get("/admin/insights")
def insights_page():
    return FileResponse(STATIC / "insights.html")


# ---------- API ----------

@router.post("/admin/api/check", dependencies=[Depends(require_admin)])
def check():
    return Response(status_code=204)


@router.post("/admin/api/build", dependencies=[Depends(require_admin)])
async def build(slug: str = Form(...), urls: str = Form(""), files: list[UploadFile] = File(default=[])):
    uploads = []
    for f in files:
        if not f.filename:
            continue
        data = await f.read()
        if len(data) > builder.MAX_SOURCE_BYTES:
            raise HTTPException(413, f"{f.filename} is larger than 30 MB.")
        uploads.append((Path(f.filename).name, data))
    url_list = [u.strip() for u in urls.splitlines() if u.strip()]
    if not _build_lock.acquire(blocking=False):
        raise HTTPException(409, "Another pack is being built. Try again in a minute.")
    try:
        result = await run_in_threadpool(builder.build_draft, slug.strip(), uploads, url_list)
    except (ValueError, FileNotFoundError) as e:
        raise user_error(e)
    finally:
        _build_lock.release()
    return {"slug": result.slug, "docs": result.docs, "review_count": result.review_count,
            "unverified_steps": result.unverified_steps, "staff_only_steps": result.staff_only_steps}


@router.get("/admin/api/drafts", dependencies=[Depends(require_admin)])
def drafts():
    return builder.list_drafts()


@router.get("/admin/api/drafts/{slug}", dependencies=[Depends(require_admin)])
def draft(slug: str):
    try:
        return builder.get_draft(slug)
    except (ValueError, FileNotFoundError) as e:
        raise user_error(e)


class DocText(BaseModel):
    text: str


class PackFields(BaseModel):
    name: str | None = None
    description: str | None = None
    escalation: str | None = None
    safety_message: str | None = None
    mode: str | None = None
    clear_pack_notes: bool = False


class StaffFlag(BaseModel):
    value: bool


@router.put("/admin/api/drafts/{slug}/docs/{name}", dependencies=[Depends(require_admin)])
def save_doc(slug: str, name: str, body: DocText):
    try:
        builder.save_doc(slug, name, body.text)
        return builder.get_draft(slug)
    except (ValueError, FileNotFoundError) as e:
        raise user_error(e)


@router.delete("/admin/api/drafts/{slug}/docs/{name}", dependencies=[Depends(require_admin)])
def delete_doc(slug: str, name: str):
    try:
        builder.delete_doc(slug, name)
        return builder.get_draft(slug)
    except (ValueError, FileNotFoundError) as e:
        raise user_error(e)


@router.put("/admin/api/drafts/{slug}/pack", dependencies=[Depends(require_admin)])
def save_pack(slug: str, body: PackFields):
    try:
        builder.save_pack(slug, body.model_dump(exclude_none=True))
        return builder.get_draft(slug)
    except (ValueError, FileNotFoundError) as e:
        raise user_error(e)


@router.post("/admin/api/drafts/{slug}/docs/{name}/steps/{index}/staff_only", dependencies=[Depends(require_admin)])
def staff_only(slug: str, name: str, index: int, body: StaffFlag):
    try:
        builder.set_staff_only(slug, name, index, body.value)
        return builder.get_draft(slug)
    except (ValueError, FileNotFoundError) as e:
        raise user_error(e)


@router.post("/admin/api/drafts/{slug}/publish", dependencies=[Depends(require_admin)])
def publish(slug: str):
    try:
        target = builder.publish(slug)
    except (ValueError, FileNotFoundError) as e:
        raise user_error(e)
    return {"published": target.name, "url": f"/?pack={target.name}"}


@router.delete("/admin/api/drafts/{slug}", dependencies=[Depends(require_admin)])
def delete_draft(slug: str):
    try:
        builder.delete_draft(slug)
    except (ValueError, FileNotFoundError) as e:
        raise user_error(e)
    return Response(status_code=204)


@router.get("/admin/api/insights", dependencies=[Depends(require_admin)])
def insights_data(pack: str = "", days: int = 30, include_demo: bool = True):
    log_ = insights.get_log()
    if log_ is None:
        raise HTTPException(503, "Insights logging is turned off (INSIGHTS_DISABLED=1).")
    return log_.stats(pack or None, days if days > 0 else None, include_demo)
