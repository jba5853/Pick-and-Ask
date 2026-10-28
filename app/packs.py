"""Load knowledge packs from docs/<pack-name>/."""

from dataclasses import dataclass
from pathlib import Path

import yaml

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"


@dataclass
class Pack:
    id: str               # folder name, e.g. "ai-arcade"
    name: str
    description: str
    escalation: str       # e.g. "AI Office Hours"
    safety_message: str   # fixed reply for safety escalations
    docs: dict[str, str]  # filename -> markdown text


def list_packs() -> list[str]:
    return sorted(p.parent.name for p in DOCS_DIR.glob("*/pack.yaml"))


def load_pack(pack_id: str) -> Pack:
    if pack_id not in list_packs():
        raise ValueError(f"Unknown pack: {pack_id}")
    folder = DOCS_DIR / pack_id
    meta = yaml.safe_load((folder / "pack.yaml").read_text()) or {}
    for key in ("name", "description", "escalation", "safety_message"):
        if not meta.get(key):
            raise ValueError(f"{folder}/pack.yaml is missing '{key}'")
    docs = {f.name: f.read_text() for f in sorted(folder.glob("*.md"))}
    if not docs:
        raise ValueError(f"Pack {pack_id} has no .md files")
    return Pack(pack_id, meta["name"], meta["description"], meta["escalation"], meta["safety_message"], docs)
