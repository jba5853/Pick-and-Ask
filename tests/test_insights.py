"""Offline tests for the anonymous insights log and the dashboard data. No API calls."""

import base64
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from fastapi.testclient import TestClient  # noqa: E402

from app import insights, main  # noqa: E402
from app.conversation import Session, converse  # noqa: E402
from app.insights import InsightsLog, scrub  # noqa: E402
from tests.test_conversation import ARCADE, PRINT, client, intent, m  # noqa: E402,F401  (client is a fixture)

PHOTO = b"\xff\xd8secret-photo-bytes-0123456789"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Turn logging on, into a temporary database."""
    monkeypatch.delenv("INSIGHTS_DISABLED", raising=False)
    log = InsightsLog(tmp_path / "insights.sqlite3")
    monkeypatch.setattr(insights, "_log", log)
    return log


def session(pack=PRINT, sid="visitor-session-abc123"):
    return Session(sid, pack.id, 0.0)


# ---------- what is (not) stored ----------

def test_scrub_removes_contact_details_and_ids():
    text = scrub("I'm jo.smith@psu.edu, call 814-555-0199 or @josmith, ID 912345678, see https://x.example/a?b")
    assert "@psu.edu" not in text and "814" not in text and "josmith" not in text and "912345678" not in text
    assert "https://" not in text
    assert text.count("[email]") == 1 and "[number]" in text and "[handle]" in text and "[link]" in text
    assert scrub("x" * 500) == "x" * insights.MAX_QUESTION
    assert scrub("") is None and scrub(None) is None
    assert scrub("Print at 210 °C with 1.75 mm PLA") == "Print at 210 °C with 1.75 mm PLA"  # normal numbers stay


def test_table_has_no_place_for_personal_data(db):
    cols = {r[1] for r in db._db.execute("PRAGMA table_info(conversations)")}
    assert cols == {"id", "started", "updated", "pack", "question", "diagnosis", "confidence", "outcome",
                    "uncovered_question", "steps_tried", "demo"}


def test_conversation_logs_question_diagnosis_and_outcome_only(db, client):
    client(m())
    s = session()
    converse(s, PRINT, "What's wrong with my print?", image_jpeg=PHOTO)
    converse(s, PRINT, "", action="walkthrough")
    converse(s, PRINT, "", action="done")
    rows = db.rows()
    assert len(rows) == 1                                           # one row per conversation, not per turn
    row = rows[0]
    assert (row["pack"], row["question"], row["diagnosis"], row["confidence"], row["outcome"], row["steps_tried"]) == \
        ("3d-printing", "What's wrong with my print?", "Stringing", "high", "fixed", 1)
    assert row["id"] != s.id and s.id not in str(row)               # not linkable to the session
    assert len(row["started"]) == len("2026-11-12T10:15+00:00")     # minute precision only


def test_no_photo_session_id_or_conversation_text_reaches_the_file(db, client, tmp_path):
    client(m(), intent("question", reply="Retraction pulls the filament back.", source_doc="stringing.md"))
    s = session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "What does retraction mean?")
    raw = (tmp_path / "insights.sqlite3").read_bytes()
    for secret in [PHOTO, base64.standard_b64encode(PHOTO), s.id.encode(), b"Retraction pulls", b"What does retraction"]:
        assert secret not in raw
    assert db.rows()[0]["question"] == "What's wrong?"              # only the first question is kept


def test_personal_data_questions_are_not_stored(db, client):
    import json
    client(json.dumps({"status": "escalate", "reason": "personal_data", "answer": "", "source_doc": ""}))
    converse(session(ARCADE), ARCADE, "Whose postcard is this? Find me Sam Lee's email")
    row = db.rows()[0]
    assert row["outcome"] == "open" and row["question"] is None and "Sam" not in str(row)


def test_contact_details_in_questions_are_scrubbed(db, client):
    client(m())
    converse(session(), PRINT, "My print is stringy, email me at sam@psu.edu")
    assert db.rows()[0]["question"] == "My print is stringy, email me at [email]"


@pytest.mark.parametrize("script, turns, outcome", [
    ("safety", [("It's smoking!", "message")], "safety"),
    ("not_covered", [("How do I print with TPU?", "message")], "not_covered"),
    ("escalated", [("What's wrong?", "message"), ("", "walkthrough"), ("", "failed"), ("", "failed"), ("", "failed")], "escalated"),
    ("open", [("What's wrong?", "message")], "open"),
])
def test_outcomes(db, client, script, turns, outcome):
    keep = m(intent="failed", ruled_out="that", step_ids=["stringing.md#3", "stringing.md#4", "stringing.md#5", "stringing.md#6"])
    replies = {
        "safety": [intent("other", status="escalate", reason="safety")],
        "not_covered": [m(status="escalate", reason="not_covered", step_ids=[], source_doc="", diagnosis="", confidence="")],
        "escalated": [m(step_ids=["stringing.md#2", "stringing.md#3", "stringing.md#4", "stringing.md#5"]), keep, keep, keep],
        "open": [m()],
    }[script]
    client(*replies)
    s = session()
    for text, action in turns:
        converse(s, PRINT, text, action)
    row = db.rows()[0]
    assert row["outcome"] == outcome
    if outcome == "not_covered":
        assert row["uncovered_question"] == "How do I print with TPU?"


def test_answer_packs_log_answered(db, client):
    import json
    client(json.dumps({"status": "answered", "reason": "none", "answer": "Tap Send.", "source_doc": "a006-postcard-portals.md"}))
    converse(session(ARCADE), ARCADE, "How do I share my postcard?")
    assert db.rows()[0]["outcome"] == "answered" and db.rows()[0]["diagnosis"] is None


def test_logging_failure_never_breaks_the_conversation(db, client, monkeypatch):
    client(m())

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db, "record", broken)
    assert converse(session(), PRINT, "What's wrong?").status == "answered"


def test_disabled_logging_writes_nothing(client, monkeypatch, tmp_path):
    monkeypatch.setenv("INSIGHTS_DISABLED", "1")
    monkeypatch.setattr(insights, "_log", None)
    client(m())
    converse(session(), PRINT, "What's wrong?")
    assert insights.get_log() is None


def test_summary_alone_creates_no_row(db, client):
    client()
    converse(session(), PRINT, "", action="summary")
    assert db.rows() == []


# ---------- dashboard numbers ----------

def add(db, outcome, diagnosis=None, uncovered=None, demo=False, started=None, pack="3d-printing"):
    db.record(insights.new_id(), pack, outcome, "q", diagnosis or "", "high" if diagnosis else "", uncovered,
              demo=demo, started=started, updated=started)


def test_stats(db):
    for _ in range(3):
        add(db, "fixed", "Stringing")
    add(db, "escalated", "Stringing")
    add(db, "escalated", "Clogged nozzle")
    add(db, "safety")
    add(db, "not_covered", uncovered="How do I print with TPU?")
    add(db, "not_covered", uncovered="how do i print with tpu?")
    add(db, "answered", pack="ai-arcade")
    s = db.stats(days=None)
    assert s["total"] == 9
    assert s["fix_rate"] == 0.6                                     # 3 fixed / (3 fixed + 2 escalated)
    assert (s["escalations"], s["safety"], s["not_covered"]) == (2, 1, 2)
    assert s["problems"][0] == {"diagnosis": "Stringing", "count": 4, "fixed": 3, "escalated": 1, "fix_rate": 0.75}
    assert s["uncovered"][0]["count"] == 2                          # grouped case-insensitively
    assert s["packs"] == {"3d-printing": 8, "ai-arcade": 1}
    assert db.stats(pack="ai-arcade", days=None)["total"] == 1


def test_stats_filter_demo_and_time(db):
    add(db, "fixed", "Stringing", demo=True)
    add(db, "fixed", "Stringing", started="2020-01-01T10:00+00:00")
    add(db, "escalated", "Warping")
    assert db.stats(days=None)["demo_rows"] == 1
    assert db.stats(days=None, include_demo=False)["total"] == 2
    assert db.stats(days=30)["total"] == 2                          # the 2020 row is outside the window
    assert db.stats(days=30, include_demo=False)["fix_rate"] == 0.0


def test_empty_stats(db):
    s = db.stats()
    assert s["total"] == 0 and s["fix_rate"] is None and s["problems"] == [] and s["uncovered"] == []


# ---------- seed script ----------

def test_seed_script_adds_labeled_demo_rows_and_removes_only_them(tmp_path):
    path = tmp_path / "seed.sqlite3"
    log = InsightsLog(path)
    add(log, "fixed", "Stringing")                                  # a real row
    run = lambda *a: subprocess.run([sys.executable, "scripts/seed_insights.py", "--db", str(path), *a],  # noqa: E731
                                    cwd=ROOT, capture_output=True, text=True, check=True)
    out = run("--rows", "50").stdout
    assert "Added 50 DEMO conversations" in out
    rows = InsightsLog(path).rows()
    assert len(rows) == 51 and sum(r["demo"] for r in rows) == 50
    assert {r["outcome"] for r in rows if r["demo"]} >= {"fixed", "escalated", "not_covered"}
    assert "Removed 50 demo rows" in run("--remove").stdout
    assert [r["demo"] for r in InsightsLog(path).rows()] == [0]


# ---------- API ----------

def test_insights_api_needs_the_admin_code(db, monkeypatch):
    monkeypatch.setenv("ADMIN_CODE", "staff-code-12345")
    add(db, "fixed", "Stringing")
    c = TestClient(main.app)
    assert c.get("/admin/api/insights").status_code == 401
    r = c.get("/admin/api/insights?days=0&include_demo=false", headers={"X-Admin-Code": "staff-code-12345"})
    assert r.status_code == 200 and r.json()["total"] == 1
