"""FastAPI app: serves the page and the /ask endpoint."""

import logging
import os
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

load_dotenv()  # picks up ANTHROPIC_API_KEY from .env if present; never logged

from app.assistant import ask  # noqa: E402
from app.images import prepare_image  # noqa: E402
from app.packs import list_packs, load_pack  # noqa: E402

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit("ANTHROPIC_API_KEY is not set. Export it or put it in .env (see README).")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Point and Ask")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/packs")
def packs():
    out = []
    for pack_id in list_packs():
        p = load_pack(pack_id)
        out.append({"id": p.id, "name": p.name, "description": p.description})
    return out


@app.post("/ask")
async def ask_endpoint(
    question: str = Form(...),
    pack: str = Form("ai-arcade"),
    photo: UploadFile | None = File(None),
):
    try:
        knowledge = load_pack(pack)
    except ValueError as e:
        raise HTTPException(400, str(e))

    image = None
    if photo is not None and photo.filename:
        data = await photo.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Photo is too large (25 MB max).")
        try:
            image = prepare_image(data)
        except ValueError as e:
            raise HTTPException(400, str(e))

    return asdict(ask(knowledge, question, image))
