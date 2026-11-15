"""Anonymous conversation log for staff insights (local SQLite file).

One row per conversation: pack, the visitor's first question (scrubbed), diagnosis, confidence, outcome,
the latest question the docs didn't cover (scrubbed), how many steps were tried, and minute-level timestamps.
Never stored: photos, IP addresses, session ids, the conversation text, or questions flagged as personal data.
"""

import datetime as dt
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "insights.sqlite3"
MAX_QUESTION = 200
OUTCOMES = ("fixed", "escalated", "safety", "not_covered", "answered", "open")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,           -- random, unrelated to the session id
    started TEXT NOT NULL,         -- UTC, minute precision
    updated TEXT NOT NULL,
    pack TEXT NOT NULL,
    question TEXT,                 -- first typed question, scrubbed; NULL if it looked personal
    diagnosis TEXT,
    confidence TEXT,
    outcome TEXT NOT NULL,
    uncovered_question TEXT,       -- latest question the docs didn't cover, scrubbed
    steps_tried INTEGER NOT NULL DEFAULT 0,
    demo INTEGER NOT NULL DEFAULT 0  -- 1 = fake seed data from scripts/seed_insights.py
);
CREATE INDEX IF NOT EXISTS conversations_started ON conversations(started);
"""

_SCRUB = [
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "[email]"),
    (re.compile(r"https?://\S+"), "[link]"),
    (re.compile(r"(?<!\w)@\w{2,}"), "[handle]"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "[number]"),     # phone numbers, long IDs
    (re.compile(r"\b\d{5,}\b"), "[number]"),
]


def scrub(text: str | None) -> str | None:
    """Remove contact details and ID-like numbers; keep it short."""
    if not text:
        return None
    text = " ".join(text.split())
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text)
    return text[:MAX_QUESTION] or None


def now_minute() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0).isoformat(timespec="minutes")


def new_id() -> str:
    return uuid.uuid4().hex


class InsightsLog:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def record(self, log_id: str, pack: str, outcome: str, question: str | None = None, diagnosis: str = "",
               confidence: str = "", uncovered_question: str | None = None, steps_tried: int = 0,
               demo: bool = False, started: str | None = None, updated: str | None = None) -> None:
        """Insert or update one conversation's row."""
        if outcome not in OUTCOMES:
            raise ValueError(f"Unknown outcome {outcome!r}")
        stamp = updated or now_minute()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO conversations (id, started, updated, pack, question, diagnosis, confidence, outcome,
                                              uncovered_question, steps_tried, demo)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, question=COALESCE(question, excluded.question),
                     diagnosis=excluded.diagnosis, confidence=excluded.confidence, outcome=excluded.outcome,
                     uncovered_question=COALESCE(excluded.uncovered_question, uncovered_question),
                     steps_tried=excluded.steps_tried""",
                (log_id, started or stamp, stamp, pack, scrub(question), diagnosis or None, confidence or None,
                 outcome, scrub(uncovered_question), steps_tried, int(demo)),
            )

    def rows(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM conversations ORDER BY started")]

    def delete_demo(self) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM conversations WHERE demo = 1").rowcount

    def stats(self, pack: str | None = None, days: int | None = 30, include_demo: bool = True) -> dict:
        where, args = ["1=1"], []
        if pack:
            where.append("pack = ?")
            args.append(pack)
        if days:
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="minutes")
            where.append("started >= ?")
            args.append(since)
        if not include_demo:
            where.append("demo = 0")
        clause = " AND ".join(where)
        q = lambda sql: self._db.execute(sql.replace("{w}", clause), args).fetchall()  # noqa: E731
        with self._lock:
            outcomes = {r["outcome"]: r["n"] for r in q("SELECT outcome, COUNT(*) n FROM conversations WHERE {w} GROUP BY outcome")}
            total = sum(outcomes.values())
            problems = q("""SELECT diagnosis, COUNT(*) n, SUM(outcome='fixed') fixed, SUM(outcome='escalated') escalated
                            FROM conversations WHERE {w} AND diagnosis IS NOT NULL
                            GROUP BY diagnosis ORDER BY n DESC, diagnosis LIMIT 10""")
            uncovered = q("""SELECT uncovered_question q, COUNT(*) n, MAX(started) last_seen
                             FROM conversations WHERE {w} AND uncovered_question IS NOT NULL
                             GROUP BY LOWER(uncovered_question) ORDER BY n DESC, last_seen DESC LIMIT 15""")
            packs = {r["pack"]: r["n"] for r in q("SELECT pack, COUNT(*) n FROM conversations WHERE {w} GROUP BY pack")}
            daily = [dict(r) for r in q("""SELECT substr(started, 1, 10) day, COUNT(*) n FROM conversations
                                            WHERE {w} GROUP BY day ORDER BY day""")]
            demo_rows = q("SELECT COUNT(*) n FROM conversations WHERE {w} AND demo = 1")[0]["n"]
        fixed, escalated = outcomes.get("fixed", 0), outcomes.get("escalated", 0)
        return {
            "total": total,
            "outcomes": {o: outcomes.get(o, 0) for o in OUTCOMES},
            "fix_rate": round(fixed / (fixed + escalated), 3) if fixed + escalated else None,
            "escalations": escalated,
            "safety": outcomes.get("safety", 0),
            "not_covered": outcomes.get("not_covered", 0),
            "problems": [{"diagnosis": r["diagnosis"], "count": r["n"], "fixed": r["fixed"], "escalated": r["escalated"],
                          "fix_rate": round(r["fixed"] / (r["fixed"] + r["escalated"]), 3) if r["fixed"] + r["escalated"] else None}
                         for r in problems],
            "uncovered": [{"question": r["q"], "count": r["n"], "last_seen": r["last_seen"]} for r in uncovered],
            "packs": packs,
            "daily": daily,
            "demo_rows": demo_rows,
        }


_log: InsightsLog | None = None
_log_lock = threading.Lock()


def get_log() -> InsightsLog | None:
    """The shared log, or None when INSIGHTS_DISABLED=1 (tests, the live test runner)."""
    global _log
    if os.environ.get("INSIGHTS_DISABLED") == "1":
        return None
    with _log_lock:
        if _log is None:
            _log = InsightsLog(os.environ.get("INSIGHTS_DB") or DEFAULT_DB)
        return _log
