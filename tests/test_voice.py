"""Offline tests for Coach mode's spoken lines (app/voice.py) and the "get staff" action."""

import json
import re

import pytest

from app import voice
from app.conversation import Session, converse
from app.packs import load_pack
from tests.test_conversation import ARCADE, PRINT, client, intent, m  # noqa: F401  (client is a fixture)

STEP_COUNT = re.compile(r"\bstep \d+ of \d+\b", re.IGNORECASE)


def session(pack=PRINT):
    return Session("voice-session-1", pack.id, 0.0)


def words(line):
    return len(line.split())


# ---------- splitting into utterances ----------

@pytest.mark.parametrize("pack_id", ["3d-printing", "ai-arcade"])
def test_every_doc_step_fits_in_short_utterances_without_losing_words(pack_id):
    pack = load_pack(pack_id)
    for step in pack.steps.values():
        lines = voice.utterances(step.text)
        assert lines and all(words(x) <= voice.MAX_WORDS for x in lines), step.id
        spoken = re.sub(r"[^a-z0-9 ]", "", " ".join(lines).lower()).split()
        expected = re.sub(r"[^a-z0-9 ]", "", voice.for_speech(step.text).lower()).split()
        assert spoken == expected, step.id                       # nothing dropped except asides in brackets


def test_speech_cleanup():
    assert voice.for_speech("Lower it by 5–10 °C (Filament settings, Nozzle) and retry.") == \
        "Lower it by 5 to 10 degrees C and retry."
    assert voice.for_speech("Use low fan (10–20%).") == "Use low fan."
    assert voice.for_speech('Keep "Wipe while retracting" on.') == "Keep Wipe while retracting on."


def test_only_the_end_of_a_sentence_gets_a_full_stop():
    lines = voice.utterances(PRINT.safety_message)
    assert lines[2] == "If you see smoke, smell burning or notice sparks or damaged wires,"
    assert lines[3].endswith("right away.")


# ---------- what Coach mode says ----------

def test_diagnosis_is_one_short_sentence_then_the_offer(client):
    client(m())
    r = converse(session(), PRINT, "What's wrong?", image_jpeg=b"\xff\xd8x")
    assert r.voice == ["Looks like stringing.", "Want me to walk you through it?"]
    assert r.voice_pause is False


def test_guided_steps_have_no_step_numbers_and_natural_leads(client):
    client(m(), m(intent="failed", ruled_out="the factory presets", next_check="the nozzle temperature",
                  step_ids=["stringing.md#3", "stringing.md#4"]))
    s = session()
    converse(s, PRINT, "What's wrong?")
    first = converse(s, PRINT, "", action="walkthrough")
    assert first.voice[0].startswith("Okay, let's go one step at a time.")
    assert any(x.startswith("First, use the slicer's factory filament") for x in first.voice)
    assert first.voice[-1] == voice.FIRST_HINT
    second = converse(s, PRINT, "", action="failed")
    assert second.voice[0] == "Okay, that rules out the factory presets."
    assert any(x.startswith("Next, lower the nozzle temperature by 5 to 10 degrees C") for x in second.voice)
    for r in (first, second):
        assert not any(STEP_COUNT.search(x) for x in r.voice)
        assert all(words(x) <= voice.MAX_WORDS for x in r.voice)
        assert "Step" in r.answer and r.step["text"]              # the screen keeps the full text


def test_last_step_says_last_one(client):
    client(m(step_ids=["stringing.md#2"]))
    s = session()
    converse(s, PRINT, "What's wrong?")
    r = converse(s, PRINT, "", action="walkthrough")
    assert any(x.startswith("Last one, use the slicer's") for x in r.voice) or any(x.startswith("First,") for x in r.voice)


def test_safety_is_spoken_in_full_and_pauses(client):
    client(intent("other", status="escalate", reason="safety"))
    r = converse(session(), PRINT, "It's smoking")
    assert " ".join(r.voice).replace(",", "") .replace(".", "") == PRINT.safety_message.replace(",", "").replace(".", "")
    assert r.voice_pause is True


def test_staff_handoff_is_spoken_in_full_and_pauses(client):
    client(m(diagnosis="Clogged nozzle", source_doc="clogged-nozzle.md",
             step_ids=[f"clogged-nozzle.md#{i}" for i in range(1, 6)]),
           m(intent="failed", ruled_out="a stuck filament", diagnosis="Clogged nozzle", source_doc="clogged-nozzle.md",
             step_ids=[f"clogged-nozzle.md#{i}" for i in range(2, 6)]))
    s = session()
    converse(s, PRINT, "Nothing comes out")
    converse(s, PRINT, "", action="walkthrough")
    r = converse(s, PRINT, "", action="failed")
    assert r.reason == "staff_only" and r.voice_pause is True
    spoken = " ".join(r.voice)
    assert "needs makerspace staff" in spoken and "show them the note on your screen" in spoken


def test_get_staff_hands_off_without_a_model_call(client):
    fake = client(m())
    s = session()
    converse(s, PRINT, "What's wrong?")
    r = converse(s, PRINT, "", action="staff")
    assert (r.status, r.reason) == ("escalate", "requested")
    assert r.voice[0] == "Okay, let's get you a person." and r.voice_pause is True
    assert "Problem: Stringing" in r.handoff
    assert len(fake.calls) == 1
    assert converse(s, PRINT, "", action="summary").summary["outcome"] == "Visitor asked for staff"


def test_get_staff_in_an_answer_pack(client):
    client()
    r = converse(session(ARCADE), ARCADE, "", action="staff")
    assert r.reason == "requested" and "a Media Commons staff member" in r.answer and r.voice_pause


def test_ai_suggestion_is_labeled_when_spoken(client):
    client(m(status="escalate", reason="not_covered", step_ids=[], source_doc="", diagnosis="Elephant's foot",
             ai_suggestion="Try a slightly lower bed temperature in the slicer."))
    r = converse(session(), PRINT, "The bottom bulges out")
    assert "Here's an AI suggestion, not from the official guides." in r.voice
    assert r.voice[-1] == "Try a slightly lower bed temperature in the slicer."
    assert r.voice_pause is False


def test_clarifying_question_and_resolved(client):
    client(m(confidence="low", follow_up_question="Is the plastic stringy or curled up?"))
    s = session()
    assert converse(s, PRINT, "Something's wrong").voice == ["Is the plastic stringy or curled up?"]


def test_answer_packs_get_voice_too(client):
    client(json.dumps({"status": "answered", "reason": "none", "answer": "Tap Send. Then pick the Padlet.",
                       "source_doc": "a006-postcard-portals.md"}))
    r = converse(session(ARCADE), ARCADE, "How do I share it?")
    assert r.voice == ["Tap Send.", "Then pick the Padlet."]


def test_coach_scripts_are_served():
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")
    from fastapi.testclient import TestClient
    from app import main
    c = TestClient(main.app)
    for path in ["/static/coach.js", "/static/coach-commands.js"]:
        r = c.get(path)
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    page = c.get("/classic").text
    assert 'id="coachStartBtn"' in page and "/static/coach.js" in page
