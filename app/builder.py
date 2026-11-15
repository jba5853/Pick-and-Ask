"""AI pack builder: turn official manuals (PDFs or web pages) into a DRAFT knowledge pack for staff review.

Hard rules, enforced in code where possible:
- Steps come from the source. Each step carries a short verbatim quote; if the quote can't be found in the
  source text, the step gets a "REVIEW:" note.
- Every doc keeps its source reference (document, page/section, retrieval date).
- Risky steps are suggested as staff-only, by the model and by a keyword backstop.
- Drafts live in docs/_drafts/<slug>/ and never go live until a person publishes them, which is blocked while
  any "REVIEW:" note is left.
"""

import datetime as dt
import io
import ipaddress
import json
import logging
import re
import shutil
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from pypdf import PdfReader

from app import assistant
from app import packs
from app.packs import STAFF_MARK, STEP_LINE, load_pack_dir

log = logging.getLogger("point_and_ask")

BUILD_MODEL = "claude-opus-5-5"   # one-off, quality-sensitive extraction from long documents
MAX_SOURCES = 5
MAX_SOURCE_BYTES = 30 * 1024 * 1024
MAX_SOURCE_CHARS = 600_000        # ~150k tokens of manual text; larger sets must be split, never truncated
MIN_QUOTE_WORDS = 4
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
DOC_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.md$")
REVIEW = re.compile(r"\bREVIEW:", re.IGNORECASE)
META_FILE = "_draft.json"
PACK_FIELDS = ("name", "description", "escalation", "safety_message", "mode")

# Keyword backstop for staff-only suggestions. Errs towards staff-only: a reviewer can always un-tick it.
STAFF_RULES = [
    (r"\b(laser tube|beam path|beam|alignment|align the|mirrors?|lens(es)?|optics?)\b", "laser optics or alignment"),
    (r"\b(electric(al)?|wiring|wires?|power supply|fuses?|voltage|circuit|mains)\b", "electrical"),
    (r"\b(open|remove|unscrew|take off)\b[^.]{0,40}\b(panel|housing|casing|case|enclosure|back cover|side cover)\b",
     "opening the machine"),
    (r"\b(disassembl\w*|take apart|replace the|replacement|repair)\b", "repair or part replacement"),
    (r"\b(screwdriver|wrench|spanner|allen key|hex key|pliers|torx|tool kit)\b", "needs tools"),
    (r"\b(heat(ed|ing)?|hot|flames?|fire|burn(s|ing)?|°c|°f)\b", "heat, fire or burns"),
    (r"\b(blades?|knife|razor|scalpel)\b", "sharp blade"),
    (r"\b(acetone|solvents?|chemicals?)\b", "chemicals"),
    (r"\b(lubricat\w*|grease|oil(ing)?)\b", "machine maintenance"),
    (r"\b(firmware|calibrat\w*|factory reset)\b", "calibration or firmware"),
]


def suggest_staff_only(text: str) -> str:
    """The reason a step should be staff-only, or "" if no rule matches."""
    low = text.lower()
    for pattern, reason in STAFF_RULES:
        if re.search(pattern, low):
            return reason
    return ""


# ---------- reading sources ----------

@dataclass
class Source:
    id: str          # "S1", "S2", ...
    label: str       # file name or URL
    kind: str        # "pdf" or "web"
    pages: list[str]
    retrieved: str = field(default_factory=lambda: dt.date.today().isoformat())

    def text(self) -> str:
        return "\n".join(self.pages)


def pdf_source(sid: str, label: str, data: bytes) -> Source:
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"{label} is larger than 30 MB.")
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as e:
        raise ValueError(f"Couldn't read {label} as a PDF.") from e
    if sum(len(p) for p in pages) < 200:
        raise ValueError(f"{label} has almost no text to read. Is it a scanned PDF? Use a text PDF or a web page.")
    return Source(sid, label, "pdf", pages)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "header", "noscript", "svg", "form"}
    BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if tag.startswith("h"):
                self.parts.append("## ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line and line != "##")


def check_public_url(url: str) -> None:
    """Only fetch public http(s) addresses: never localhost or the local network."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Not a web address: {url}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ValueError(f"Couldn't find {parts.hostname}.") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"{parts.hostname} is not a public address.")


def fetch(url: str, client: httpx.Client | None = None) -> tuple[bytes, str, str]:
    """Download a public URL, re-checking every redirect. Returns (body, content type, final URL)."""
    own = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False,
                                    headers={"User-Agent": "PointAndAsk-PackBuilder/1.0"})
    try:
        for _ in range(4):
            check_public_url(url)
            with client.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                r.raise_for_status()
                body = bytearray()
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_SOURCE_BYTES:
                        raise ValueError(f"{url} is larger than 30 MB.")
                return bytes(body), r.headers.get("content-type", ""), url
        raise ValueError(f"Too many redirects for {url}.")
    except httpx.HTTPError as e:
        raise ValueError(f"Couldn't download {url}: {type(e).__name__}.") from e
    finally:
        if own:
            client.close()


def url_source(sid: str, url: str, client: httpx.Client | None = None) -> Source:
    body, ctype, final = fetch(url, client)
    if "pdf" in ctype or final.lower().split("?")[0].endswith(".pdf"):
        return pdf_source(sid, url, body)
    parser = _TextExtractor()
    parser.feed(body.decode("utf-8", errors="replace"))
    text = parser.text()
    if len(text) < 200:
        raise ValueError(f"{url} has almost no readable text.")
    return Source(sid, url, "web", [text])


def sources_prompt(sources: list[Source]) -> str:
    parts = []
    for s in sources:
        if s.kind == "pdf":
            body = "\n".join(f"[Page {i}]\n{page}" for i, page in enumerate(s.pages, 1))
        else:
            body = s.pages[0]
        parts.append(f'<source id="{s.id}" label="{s.label}" kind="{s.kind}">\n{body}\n</source>')
    text = "\n\n".join(parts)
    if len(text) > MAX_SOURCE_CHARS:
        raise ValueError(f"These sources are too long to read in one go ({len(text):,} characters; the limit is "
                         f"{MAX_SOURCE_CHARS:,}). Build from fewer or shorter documents, for example just the "
                         "troubleshooting and maintenance chapters.")
    return text


# ---------- the model call ----------

BUILD_SYSTEM = """You turn official manuals and guides into a draft knowledge pack for "Glance", a help assistant that visitors in a university makerspace or learning space use with a photo and a question. Staff will review your draft before anything goes live.

Hard rules:
- Use ONLY the sources. Never add steps, facts, numbers or advice that are not in them, even if you know them to be true.
- Every step must come from the source: keep its wording close (you may shorten it), and put a short verbatim excerpt of 5-25 words copied exactly from the source text in quote. If you cannot quote the source for a step, leave that step out.
- Every doc gets source_id and source_ref: the page number(s) (for PDFs, from the [Page N] markers) and the section heading, for example "p. 30, Maintenance".
- Anything uncertain (unclear wording, model-specific details, conflicting instructions, a step you had to shorten a lot) gets a short note in review on that step, or in the doc's or pack's review_notes. Notes are plain sentences; the app adds the "REVIEW:" prefix.

What to make:
- Short topic docs, ONE problem or task per doc (for example "Material not cutting through", "Cleaning the lens", "Starting a job"). Prefer problems and tasks a visitor would ask about. Skip warranty, legal and sales text.
- signs: how a visitor would recognise the problem, from the source. Empty for plain tasks.
- steps: numbered steps a person would follow, in order, from the source.
- staff_only: true for any step that involves lasers or beam alignment, optics, mirrors or lenses, opening covers or panels, electrical parts, heat or fire, blades, chemicals, tools, repairs, calibration or firmware, or anything risky. When unsure, mark it staff-only. Say why in staff_reason.
- mode: "diagnose" when the pack is mostly about a machine that visitors troubleshoot, otherwise "answer".
- description: one short sentence on what the pack covers, starting with what it helps with (for example "Using and troubleshooting the laser cutter.").
- safety_message: a fixed 1-3 sentence message shown whenever a visitor raises a safety issue (fire, smoke, burns, fumes, electrical problems, injury), built from the source's safety guidance: tell them to stop, step away, and get staff. Never advice to fix it themselves.
- slug: a short lowercase-hyphenated file name for each doc, without .md.
"""


def build_schema() -> dict:
    step = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "quote": {"type": "string"},
            "staff_only": {"type": "boolean"},
            "staff_reason": {"type": "string"},
            "review": {"type": "string"},
        },
        "required": ["text", "quote", "staff_only", "staff_reason", "review"],
        "additionalProperties": False,
    }
    doc = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "title": {"type": "string"},
            "source_id": {"type": "string"},
            "source_ref": {"type": "string"},
            "summary": {"type": "string"},
            "signs": {"type": "array", "items": {"type": "string"}},
            "steps": {"type": "array", "items": step},
            "review_notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["slug", "title", "source_id", "source_ref", "summary", "signs", "steps", "review_notes"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "mode": {"type": "string", "enum": ["diagnose", "answer"]},
            "safety_message": {"type": "string"},
            "review_notes": {"type": "array", "items": {"type": "string"}},
            "docs": {"type": "array", "items": doc},
        },
        "required": ["name", "description", "mode", "safety_message", "review_notes", "docs"],
        "additionalProperties": False,
    }


def call_builder(sources: list[Source], slug: str, client=None) -> dict:
    client = client or assistant.get_client()
    content = [{"type": "text", "text": sources_prompt(sources)},
               {"type": "text", "text": f"Build the draft pack. Its id will be {slug!r}."}]
    # Long input and long structured output: stream so the request can't time out.
    with client.beta.messages.stream(
        model=BUILD_MODEL,
        max_tokens=64000,
        system=BUILD_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": build_schema()}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise ValueError("The model declined to process these documents.")
    if message.stop_reason == "max_tokens":
        raise ValueError("The draft was too long to finish. Build from fewer or shorter documents.")
    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError as e:
        raise ValueError("The model's draft couldn't be read. Try again.") from e


# ---------- checking and writing the draft ----------

def normalize(text: str) -> str:
    text = text.replace("­", "")
    text = re.sub(r"-\s*\n\s*", "", text)                     # words hyphenated across lines
    text = text.translate(str.maketrans("‘’“”–—", "''\"\"--"))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())


def quote_found(quote: str, source_norm: str) -> bool:
    q = normalize(quote)
    return len(q.split()) >= MIN_QUOTE_WORDS and q in source_norm


def doc_filename(raw: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:50] or "doc"
    name, n = f"{base}.md", 2
    while name in taken:
        name, n = f"{base}-{n}.md", n + 1
    taken.add(name)
    return name


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def render_doc(doc: dict, source: Source | None, quote_ok: list[bool]) -> str:
    label = source.label if source else "UNKNOWN SOURCE"
    retrieved = source.retrieved if source else dt.date.today().isoformat()
    lines = [f"# {one_line(doc['title'])}", "",
             f"> Source: {label}, {one_line(doc['source_ref']) or 'page/section not given'} · Retrieved {retrieved}"]
    if source is None:
        lines.append("> REVIEW: The source for this doc couldn't be matched. Check where it came from.")
    lines += [f"> REVIEW: {one_line(n)}" for n in doc["review_notes"] if one_line(n)]
    if one_line(doc["summary"]):
        lines += ["", one_line(doc["summary"])]
    signs = [one_line(s) for s in doc["signs"] if one_line(s)]
    if signs:
        lines += ["", "## Signs", ""] + [f"- {s}" for s in signs]
    if doc["steps"]:
        lines += ["", "## Steps", ""]
        for i, (step, ok) in enumerate(zip(doc["steps"], quote_ok), 1):
            marker = " {staff_only: true}" if step["staff_only"] else ""
            lines.append(f"{i}. {one_line(step['text'])}{marker}")
            if not ok:
                lines.append("   REVIEW: Couldn't find this step's quoted text in the source. Check it against the manual.")
            if one_line(step["review"]):
                lines.append(f"   REVIEW: {one_line(step['review'])}")
    return "\n".join(lines) + "\n"


@dataclass
class DraftResult:
    slug: str
    folder: Path
    docs: list[str]
    review_count: int
    unverified_steps: int
    staff_only_steps: int


def drafts_dir() -> Path:
    return packs.DOCS_DIR / "_drafts"


def check_slug(slug: str) -> str:
    if not SLUG.match(slug or ""):
        raise ValueError("Pack id must be 2-40 characters: lowercase letters, numbers and hyphens.")
    return slug


def write_draft(slug: str, data: dict, sources: list[Source], overwrite: bool = False) -> DraftResult:
    """Check the model's draft against the sources and write it to docs/_drafts/<slug>/."""
    check_slug(slug)
    folder = drafts_dir() / slug
    if folder.exists():
        if not overwrite:
            raise ValueError(f"A draft called {slug!r} already exists. Delete it or pick another id.")
        shutil.rmtree(folder)
    by_id = {s.id: s for s in sources}
    norms = {s.id: normalize(s.text()) for s in sources}
    all_norm = " ".join(norms.values())

    taken, docs, steps_meta = set(), {}, {}
    unverified = staff = 0
    for doc in data["docs"]:
        if not doc["steps"] and not one_line(doc["summary"]) and not doc["signs"]:
            continue
        source = by_id.get(doc["source_id"])
        quote_ok = []
        for step in doc["steps"]:
            # Prefer the doc's own source; accept a quote from any provided source.
            ok = quote_found(step["quote"], norms.get(doc["source_id"], "")) or quote_found(step["quote"], all_norm)
            quote_ok.append(ok)
            unverified += not ok
            rule = suggest_staff_only(step["text"])
            if rule and not step["staff_only"]:
                step["staff_only"], step["staff_reason"] = True, f"Keyword rule: {rule}"
            staff += step["staff_only"]
        name = doc_filename(doc["slug"] or doc["title"], taken)
        docs[name] = render_doc(doc, source, quote_ok)
        steps_meta[name] = [{"text": one_line(s["text"]), "staff_reason": one_line(s["staff_reason"]),
                             "quote": one_line(s["quote"]), "quote_found": ok}
                            for s, ok in zip(doc["steps"], quote_ok)]
    if not docs:
        raise ValueError("The model didn't find anything usable in these documents.")

    folder.mkdir(parents=True)
    pack = {
        "name": one_line(data["name"]) or slug,
        "description": one_line(data["description"]) or "REVIEW: describe what this pack covers.",
        "escalation": "REVIEW: who should visitors ask for help? (for example: makerspace staff)",
        "safety_message": one_line(data["safety_message"]) or "REVIEW: write the safety message.",
        "mode": data["mode"] if data["mode"] in ("diagnose", "answer") else "answer",
    }
    write_pack_yaml(folder, pack, data.get("review_notes", []))
    for name, text in docs.items():
        (folder / name).write_text(text)
    meta = {
        "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "model": BUILD_MODEL,
        "sources": [{"id": s.id, "label": s.label, "kind": s.kind, "pages": len(s.pages), "retrieved": s.retrieved}
                    for s in sources],
        "steps": steps_meta,
    }
    (folder / META_FILE).write_text(json.dumps(meta, indent=2))
    return DraftResult(slug, folder, sorted(docs), count_reviews(folder), unverified, staff)


def write_pack_yaml(folder: Path, pack: dict, review_notes: list[str] = ()) -> None:
    header = "# Draft pack generated by the AI pack builder. Review everything before publishing.\n"
    header += "".join(f"# REVIEW: {one_line(n)}\n" for n in review_notes if one_line(n))
    (folder / "pack.yaml").write_text(header + yaml.safe_dump(pack, sort_keys=False, allow_unicode=True, width=1000))


def build_draft(slug: str, files: list[tuple[str, bytes]] = (), urls: list[str] = (), client=None,
                http_client: httpx.Client | None = None, overwrite: bool = False) -> DraftResult:
    check_slug(slug)
    if (drafts_dir() / slug).exists() and not overwrite:
        raise ValueError(f"A draft called {slug!r} already exists. Delete it or pick another id.")
    if (packs.DOCS_DIR / slug).exists():
        raise ValueError(f"A live pack called {slug!r} already exists. Pick another id.")
    items = [("file", name, data) for name, data in files] + [("url", u.strip(), None) for u in urls if u.strip()]
    if not items:
        raise ValueError("Add at least one PDF or URL.")
    if len(items) > MAX_SOURCES:
        raise ValueError(f"Use at most {MAX_SOURCES} sources at a time.")
    sources = []
    for i, (kind, label, data) in enumerate(items, 1):
        sid = f"S{i}"
        sources.append(pdf_source(sid, label, data) if kind == "file" else url_source(sid, label, http_client))
    data = call_builder(sources, slug, client)
    result = write_draft(slug, data, sources, overwrite=overwrite)
    log.info("built draft %s: %d docs, %d review notes, %d unverified steps", slug, len(result.docs),
             result.review_count, result.unverified_steps)
    return result


# ---------- reviewing and publishing drafts ----------

def draft_folder(slug: str) -> Path:
    folder = drafts_dir() / check_slug(slug)
    if not (folder / "pack.yaml").is_file():
        raise FileNotFoundError(f"No draft called {slug!r}.")
    return folder


def check_doc_name(name: str) -> str:
    if not DOC_NAME.match(name or "") or name == "README.md":
        raise ValueError("Doc names are lowercase letters, numbers and hyphens, ending in .md.")
    return name


def review_lines(folder: Path) -> list[str]:
    issues = []
    for path in [folder / "pack.yaml", *sorted(folder.glob("*.md"))]:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if REVIEW.search(line):
                issues.append(f"{path.name}:{n}: {line.strip().lstrip('#> ').strip()}")
    return issues


def count_reviews(folder: Path) -> int:
    return len(review_lines(folder))


def doc_steps(text: str) -> list[dict]:
    steps = []
    for line in text.splitlines():
        if m := STEP_LINE.match(line):
            body = m.group(2)
            steps.append({"index": len(steps), "number": int(m.group(1)),
                          "text": STAFF_MARK.sub("", body).strip(), "staff_only": bool(STAFF_MARK.search(body))})
    return steps


def list_drafts() -> list[dict]:
    out = []
    if not drafts_dir().is_dir():
        return out
    for folder in sorted(p for p in drafts_dir().iterdir() if (p / "pack.yaml").is_file() and SLUG.match(p.name)):
        pack = yaml.safe_load((folder / "pack.yaml").read_text()) or {}
        meta = read_meta(folder)
        out.append({"slug": folder.name, "name": pack.get("name", folder.name), "docs": len(list(folder.glob("*.md"))),
                    "review_count": count_reviews(folder), "created": meta.get("created", ""),
                    "sources": [s["label"] for s in meta.get("sources", [])]})
    return out


def read_meta(folder: Path) -> dict:
    try:
        return json.loads((folder / META_FILE).read_text())
    except (OSError, ValueError):
        return {}


def get_draft(slug: str) -> dict:
    folder = draft_folder(slug)
    pack = yaml.safe_load((folder / "pack.yaml").read_text()) or {}
    meta = read_meta(folder)
    docs = []
    for path in sorted(folder.glob("*.md")):
        text = path.read_text()
        reasons = meta.get("steps", {}).get(path.name, [])
        steps = doc_steps(text)
        for s in steps:
            s["staff_reason"] = reasons[s["index"]]["staff_reason"] if s["index"] < len(reasons) else ""
        docs.append({"filename": path.name, "text": text, "steps": steps,
                     "review_count": sum(1 for line in text.splitlines() if REVIEW.search(line))})
    pack_notes = [line[len("# REVIEW:"):].strip() for line in (folder / "pack.yaml").read_text().splitlines()
                  if line.startswith("# REVIEW:")]
    return {"slug": slug, "pack": {k: pack.get(k, "") for k in PACK_FIELDS}, "pack_notes": pack_notes, "docs": docs,
            "issues": review_lines(folder), "sources": meta.get("sources", []), "created": meta.get("created", "")}


def save_doc(slug: str, name: str, text: str) -> None:
    folder = draft_folder(slug)
    check_doc_name(name)
    if len(text) > 100_000:
        raise ValueError("That doc is too long.")
    (folder / name).write_text(text.replace("\r\n", "\n").rstrip("\n") + "\n")


def delete_doc(slug: str, name: str) -> None:
    path = draft_folder(slug) / check_doc_name(name)
    path.unlink(missing_ok=True)


def save_pack(slug: str, fields: dict) -> None:
    folder = draft_folder(slug)
    current = yaml.safe_load((folder / "pack.yaml").read_text()) or {}
    for key in PACK_FIELDS:
        if key in fields:
            current[key] = one_line(fields[key])
    if current.get("mode") not in ("diagnose", "answer"):
        raise ValueError("Mode must be diagnose or answer.")
    # Keep any pack-level REVIEW comments only if the reviewer hasn't cleared them.
    notes = [line[len("# REVIEW:"):].strip() for line in (folder / "pack.yaml").read_text().splitlines()
             if line.startswith("# REVIEW:")] if not fields.get("clear_pack_notes") else []
    write_pack_yaml(folder, current, notes)


def set_staff_only(slug: str, name: str, index: int, value: bool) -> None:
    """Add or remove the {staff_only: true} marker on the index-th numbered step of a draft doc."""
    path = draft_folder(slug) / check_doc_name(name)
    lines, seen = path.read_text().splitlines(), -1
    for i, line in enumerate(lines):
        if STEP_LINE.match(line):
            seen += 1
            if seen == index:
                body = STAFF_MARK.sub("", line).rstrip()
                lines[i] = body + (" {staff_only: true}" if value else "")
                path.write_text("\n".join(lines) + "\n")
                return
    raise ValueError(f"{name} has no step {index + 1}.")


def publish(slug: str) -> Path:
    """Make a reviewed draft live. Refuses while any REVIEW note is left or the pack doesn't load."""
    folder = draft_folder(slug)
    issues = review_lines(folder)
    if issues:
        raise ValueError(f"{len(issues)} REVIEW note(s) left. Resolve them before publishing.")
    target = packs.DOCS_DIR / slug
    if target.exists():
        raise ValueError(f"A live pack called {slug!r} already exists.")
    pack = load_pack_dir(folder, slug)  # raises if pack.yaml or the docs are incomplete
    today = dt.date.today().isoformat()
    (folder / "pack.yaml").write_text(
        f"# Generated by the AI pack builder and published after staff review on {today}.\n"
        + yaml.safe_dump({"name": pack.name, "description": pack.description, "escalation": pack.escalation,
                          "safety_message": pack.safety_message, "mode": pack.mode},
                         sort_keys=False, allow_unicode=True, width=1000))
    shutil.move(str(folder), str(target))
    meta_path = target / META_FILE
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        meta["published"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        (target / "_build.json").write_text(json.dumps(meta, indent=2))
        meta_path.unlink()
    log.info("published pack %s", slug)
    return target


def delete_draft(slug: str) -> None:
    shutil.rmtree(draft_folder(slug))
