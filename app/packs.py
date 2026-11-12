"""Load knowledge packs from docs/<pack-name>/."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
MODES = ("answer", "diagnose")
NOT_DOCS = {"README.md"}  # notes for maintainers, never sent to the model

STEP_LINE = re.compile(r"^(\d+)\.\s+(.*\S)\s*$")
STAFF_MARK = re.compile(r"\s*\{\s*staff_only\s*:\s*true\s*\}\s*$", re.IGNORECASE)
HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$")


@dataclass
class Step:
    id: str            # "<doc filename>#<n>", n counts numbered lines through the whole doc
    doc: str           # doc filename
    section: str       # nearest heading above the step
    number: int        # the number as written in the doc (lists can restart per section)
    text: str          # step text without the staff_only marker
    staff_only: bool   # never walked through with a visitor

    @property
    def label(self) -> str:
        """Human reference for staff, matching what they see in the doc."""
        return f"{self.doc}, {self.section}, step {self.number}"


@dataclass
class Pack:
    id: str               # folder name, e.g. "ai-arcade"
    name: str
    description: str
    escalation: str       # e.g. "AI Office Hours"
    safety_message: str   # fixed reply for safety escalations
    docs: dict[str, str]  # filename -> markdown text (staff markers included)
    mode: str = "answer"  # "answer" (Q&A from docs) or "diagnose" (photo troubleshooting)
    steps: dict[str, Step] = field(default_factory=dict)       # step id -> Step, in doc order
    staff_only_terms: list[str] = field(default_factory=list)  # lowercase phrases that mean "needs staff"

    def mentions_staff_only(self, text: str) -> bool:
        low = text.lower()
        return any(term in low for term in self.staff_only_terms)


def parse_steps(fname: str, text: str) -> list[Step]:
    steps, section, count = [], "", 0
    for line in text.splitlines():
        if heading := HEADING.match(line):
            section = heading.group(1)
            continue
        if match := STEP_LINE.match(line):
            count += 1
            body = match.group(2)
            staff = bool(STAFF_MARK.search(body))
            steps.append(Step(f"{fname}#{count}", fname, section, int(match.group(1)),
                              STAFF_MARK.sub("", body).strip(), staff))
    return steps


def render_doc(fname: str, text: str, steps: dict[str, Step]) -> str:
    """Doc text for the model: each numbered step is tagged with its id and staff-only status."""
    by_line = iter(s for s in steps.values() if s.doc == fname)
    out = []
    for line in text.splitlines():
        if STEP_LINE.match(line):
            step = next(by_line)
            tag = " (STAFF ONLY)" if step.staff_only else ""
            line = f"{step.number}. [{step.id}]{tag} {step.text}"
        out.append(line)
    return "\n".join(out)


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
    mode = meta.get("mode", "answer")
    if mode not in MODES:
        raise ValueError(f"{folder}/pack.yaml has unknown mode '{mode}' (use one of {', '.join(MODES)})")
    docs = {f.name: f.read_text() for f in sorted(folder.glob("*.md")) if f.name not in NOT_DOCS}
    if not docs:
        raise ValueError(f"Pack {pack_id} has no .md files")
    steps = {s.id: s for fname, text in docs.items() for s in parse_steps(fname, text)}
    terms = [str(t).lower() for t in meta.get("staff_only_terms") or []]
    return Pack(pack_id, meta["name"], meta["description"], meta["escalation"], meta["safety_message"],
                docs, mode, steps, terms)
