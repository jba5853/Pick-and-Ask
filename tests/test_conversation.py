"""Offline tests for conversations: sessions, history, guided fixes, staff-only steps, summaries."""

import json
from types import SimpleNamespace

import pytest

from app import assistant, conversation
from app.conversation import (
    MAX_FAILURES, MAX_HISTORY, MAX_PHOTOS, PHOTO_REMOVED, Session, SessionStore, build_messages, converse, record,
)
from app.packs import load_pack, parse_steps

PRINT = load_pack("3d-printing")
ARCADE = load_pack("ai-arcade")
PHOTO = b"\xff\xd8first-photo"


# ---------- fakes ----------

class ScriptedClient:
    """Returns the scripted model replies in order and records every request."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.replies.pop(0)
        usage = SimpleNamespace(input_tokens=10, cache_read_input_tokens=5000, cache_creation_input_tokens=0)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)], usage=usage)


def m(**kw):
    """A diagnose-mode model reply. Defaults: a high-confidence stringing diagnosis."""
    base = {
        "status": "answered", "reason": "none", "intent": "new_problem",
        "observation": "Thin strands between two towers.", "diagnosis": "Stringing", "confidence": "high",
        "plan_type": "fix",
        "step_ids": ["stringing.md#1", "stringing.md#2", "stringing.md#3", "stringing.md#4", "stringing.md#7"],
        "source_doc": "stringing.md", "reply": "", "ruled_out": "", "next_check": "",
        "ai_suggestion": "", "follow_up_question": "",
    }
    return json.dumps(base | kw)


def intent(name, **kw):
    """A reply that only classifies the visitor's message."""
    fields = {"observation": "", "diagnosis": "", "confidence": "", "plan_type": "", "step_ids": [], "source_doc": ""}
    return m(**(fields | {"intent": name} | kw))


@pytest.fixture
def client(monkeypatch):
    def install(*replies):
        fake = ScriptedClient(*replies)
        monkeypatch.setattr(assistant, "_client", fake)
        return fake
    return install


def new_session(pack=PRINT):
    return Session("test-session-1", pack.id, 0.0)


# ---------- session store ----------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_session_expires_after_30_minutes_idle():
    clock = Clock()
    store = SessionStore(clock=clock)
    s = store.get("abc12345", "3d-printing")
    s.diagnosis = "Stringing"
    clock.now += 29 * 60
    assert store.get("abc12345", "3d-printing") is s          # still alive, and touched
    clock.now += 29 * 60
    assert store.get("abc12345", "3d-printing") is s          # idle time counts from the last use
    clock.now += 30 * 60 + 1
    assert store.peek("abc12345") is None                      # expired and forgotten
    assert store.get("abc12345", "3d-printing").diagnosis == ""  # a fresh session


def test_expired_sessions_are_purged_from_memory():
    clock = Clock()
    store = SessionStore(clock=clock)
    for i in range(5):
        store.get(f"session-{i}", "3d-printing")
    clock.now += 31 * 60
    store.get("another-one", "3d-printing")
    assert len(store) == 1


def test_switching_pack_starts_a_new_session():
    store = SessionStore(clock=Clock())
    s = store.get("abc12345", "3d-printing")
    assert store.get("abc12345", "ai-arcade") is not s


def test_session_count_is_capped():
    clock = Clock()
    store = SessionStore(max_sessions=3, clock=clock)
    for i in range(5):
        clock.now += 1
        store.get(f"session-{i}", "3d-printing")
    assert len(store) == 3
    assert store.peek("session-0") is None and store.peek("session-4") is not None


# ---------- history ----------

def test_history_alternates_in_order_and_photo_stays_attached(client):
    fake = client(m(), intent("question", reply="Retraction pulls filament back during travel.",
                              source_doc="stringing.md"))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "What does retraction do?")
    roles = [msg["role"] for msg in s.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    sent = fake.calls[1]["messages"]
    assert [msg["role"] for msg in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"][0]["type"] == "image"            # first photo still in the conversation
    assert sent[0]["content"][1]["text"] == "What's wrong?"
    assert sent[2]["content"][0]["text"] == "What does retraction do?"


def test_session_state_goes_only_on_the_newest_message(client):
    fake = client(m(), intent("question", reply="It pulls filament back.", source_doc="stringing.md"))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "What does retraction do?")
    sent = fake.calls[1]["messages"]
    assert "<session_state>" in sent[-1]["content"][-1]["text"]
    assert "Diagnosis: Stringing" in sent[-1]["content"][-1]["text"]
    assert all("<session_state>" not in json.dumps(msg) for msg in sent[:-1])
    assert all("<session_state>" not in json.dumps(msg) for msg in s.messages)  # never stored


def test_prompt_cache_prefix_is_system_then_history(client):
    fake = client(m(), intent("question", reply="It pulls filament back.", source_doc="stringing.md"))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "What does retraction do?")
    first, second = fake.calls
    # Same cached system prompt (instructions + docs) on every turn.
    assert first["system"] == second["system"]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    # History comes after it, with a breakpoint at its end; the new message has none.
    assert second["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(second["messages"][-1])
    assert "cache_control" not in json.dumps(s.messages)        # stored history is untouched
    # Earlier turns are sent byte-identical, so the cached prefix keeps matching.
    assert second["messages"][0] == first["messages"][0]
    assert s.usage[-1]["cache_read"] == 5000


def test_new_photo_mid_conversation_is_added(client):
    fake = client(m(), m(diagnosis="Stringing", observation="Fewer strands now."))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "Here's what it looks like now", image_jpeg=b"\xff\xd8second-photo")
    sent = fake.calls[1]["messages"]
    assert sent[0]["content"][0]["type"] == "image" and sent[-1]["content"][0]["type"] == "image"


def test_photos_are_capped_but_the_first_is_kept():
    s = new_session()
    for i in range(MAX_PHOTOS + 3):
        record(s, assistant.user_content(f"photo {i}", f"img{i}".encode()), "ok")
    photos = [msg for msg in s.messages if conversation.has_image(msg)]
    assert len(photos) == MAX_PHOTOS
    assert photos[0]["content"][1]["text"] == "photo 0"         # the first photo stays
    assert photos[-1]["content"][1]["text"] == f"photo {MAX_PHOTOS + 2}"
    assert PHOTO_REMOVED in json.dumps(s.messages)


def test_long_history_keeps_first_exchange_and_latest_turns():
    s = new_session()
    record(s, assistant.user_content("first", PHOTO), "first reply")
    for i in range(30):
        record(s, assistant.user_content(f"turn {i}"), f"reply {i}")
    assert len(s.messages) == MAX_HISTORY
    assert s.messages[0]["content"][1]["text"] == "first"
    assert s.messages[-2]["content"][0]["text"] == "turn 29"
    assert [msg["role"] for msg in s.messages] == ["user", "assistant"] * (MAX_HISTORY // 2)


def test_build_messages_does_not_mutate_history():
    s = new_session()
    record(s, assistant.user_content("hi"), "hello")
    before = json.dumps(s.messages)
    build_messages(s, assistant.user_content("next"), "<session_state>x</session_state>")
    assert json.dumps(s.messages) == before


def test_answer_mode_packs_also_keep_history(client):
    a = json.dumps({"status": "answered", "reason": "none", "answer": "Use Midjourney.",
                    "source_doc": "a006-postcard-portals.md"})
    fake = client(a, a)
    s = new_session(ARCADE)
    converse(s, ARCADE, "How do I make a postcard?")
    r = converse(s, ARCADE, "Which tool was that again?")
    assert r.status == "answered" and len(fake.calls[1]["messages"]) == 3
    assert fake.calls[1]["output_config"]["format"]["schema"] == assistant.build_schema(ARCADE)


# ---------- staff-only steps ----------

def test_staff_only_markers_are_parsed():
    steps = parse_steps("x.md", "# X\n## Fix\n1. Wipe it.\n2. Do a cold pull. {staff_only: true}\n- not a step\n"
                                "## More\n1. Restarted list. {Staff_Only: TRUE}\n")
    assert [(s.id, s.number, s.staff_only, s.text) for s in steps] == [
        ("x.md#1", 1, False, "Wipe it."), ("x.md#2", 2, True, "Do a cold pull."), ("x.md#3", 1, True, "Restarted list.")]
    assert steps[2].label == "x.md, More, step 1"


def test_pack_marks_the_risky_steps_staff_only():
    staff = {sid for sid, s in PRINT.steps.items() if s.staff_only}
    for sid in ["clogged-nozzle.md#3", "clogged-nozzle.md#4", "clogged-nozzle.md#5", "stringing.md#7",
                "under-extrusion.md#8", "layer-shifting.md#4"]:
        assert sid in staff
    for s in PRINT.steps.values():
        low = s.text.lower()
        if any(w in low for w in ["cold pull", "heat gun", "pliers", "260 °c", "280 °c", "set (grub) screws"]):
            assert s.staff_only, s.id
    assert not any(s.staff_only for s in ARCADE.steps.values())


def test_diagnosis_hides_staff_only_instructions(client):
    client(m())
    r = converse(new_session(), PRINT, "What's wrong?", image_jpeg=PHOTO)
    texts = [s["text"] for s in r.steps]
    assert r.steps[0] == {"text": "This step needs makerspace staff.", "staff_only": True}
    assert "heat gun" not in json.dumps(r.steps).lower()
    assert PRINT.steps["stringing.md#2"].text in texts
    assert "Clean old residue" not in r.answer                  # spoken version starts at a visitor step


def test_guided_fix_never_presents_a_staff_only_step(client):
    client(m())  # plan starts with a staff-only step (stringing.md#1)
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    r = converse(s, PRINT, "", action="walkthrough")
    assert r.status == "guided"
    assert r.step["text"] == PRINT.steps["stringing.md#2"].text
    assert "needs makerspace staff, so I've noted it for them" in r.answer
    assert s.staff_needed == ["stringing.md#1"]
    assert (r.step["number"], r.step["total"]) == (1, 3)        # visitor steps only: #2, #3, #4


def test_procedure_stops_at_a_staff_only_step(client):
    load = m(diagnosis="Unloading filament", plan_type="procedure", source_doc="loading-unloading-filament.md",
             step_ids=["loading-unloading-filament.md#1", "loading-unloading-filament.md#2",
                       "loading-unloading-filament.md#3"])
    client(load)
    s = new_session()
    converse(s, PRINT, "How do I unload filament?")
    converse(s, PRINT, "", action="walkthrough")
    converse(s, PRINT, "", action="done")
    r = converse(s, PRINT, "", action="done")                   # next is #3, staff-only: can't skip in a procedure
    assert (r.status, r.reason) == ("escalate", "staff_only")
    assert "makerspace staff" in r.answer
    assert "loading-unloading-filament.md, Unloading filament, step 3" in r.handoff
    assert PRINT.steps["loading-unloading-filament.md#3"].text not in r.answer + r.handoff


def test_question_citing_a_staff_only_step_is_handed_to_staff(client):
    client(m(), intent("question", reply="Heat the nozzle and push PLA through.", source_doc="clogged-nozzle.md",
                       step_ids=["clogged-nozzle.md#3"]))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "", action="walkthrough")
    r = converse(s, PRINT, "How would I clear a clog myself?")
    assert (r.status, r.reason) == ("escalate", "staff_only")
    assert "push PLA" not in r.answer
    assert r.step is not None and r.actions == ["done", "failed", "repeat"]  # the guided fix carries on
    assert s.phase == "guided"


def test_model_text_with_staff_only_terms_is_blocked_even_without_a_citation(client):
    client(m(), intent("question", reply="Just do a cold pull with some PLA.", source_doc="stringing.md"))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    r = converse(s, PRINT, "Anything else I can try?")
    assert r.reason == "staff_only" and "cold pull" not in r.answer.lower()


def test_staff_only_cause_names_are_not_spoken(client):
    client(m(), m(intent="failed", ruled_out="the extruder gears", next_check="the retraction settings",
                  step_ids=["stringing.md#4"]))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "", action="walkthrough")
    r = converse(s, PRINT, "", action="failed")
    assert "extruder gears" not in r.answer
    assert r.answer.startswith("Okay, that didn't fix it.") or "rules out" not in r.answer


# ---------- guided fix flow ----------

def test_failed_step_rediagnoses_and_says_so(client):
    client(m(), m(intent="failed", ruled_out="The factory presets", next_check="the nozzle temperature",
                  step_ids=["stringing.md#3", "stringing.md#4"]))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "", action="walkthrough")
    r = converse(s, PRINT, "", action="failed")
    assert r.status == "guided"
    assert r.answer.startswith("Okay, that rules out the factory presets. Let's check the nozzle temperature.")
    assert r.step["text"] == PRINT.steps["stringing.md#3"].text
    assert (r.step["number"], r.step["total"]) == (2, 3)
    assert s.tried == [("stringing.md#2", "failed")] and s.failures == 1


def test_rediagnosis_can_switch_documents(client):
    client(m(), m(intent="failed", diagnosis="Clogged nozzle", source_doc="clogged-nozzle.md",
                  ruled_out="the presets", next_check="a blocked nozzle",
                  step_ids=["clogged-nozzle.md#1", "clogged-nozzle.md#2"]))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "", action="walkthrough")
    r = converse(s, PRINT, "That didn't work and now nothing comes out")
    assert r.step["doc"] == "clogged-nozzle.md"
    assert s.diagnosis == "Clogged nozzle" and s.docs_used == ["stringing.md", "clogged-nozzle.md"]


def test_escalates_after_three_failures_with_a_handoff_line(client):
    keep_going = m(intent="failed", ruled_out="that", next_check="the next thing",
                   step_ids=["stringing.md#3", "stringing.md#4", "stringing.md#5", "stringing.md#6"])
    fake = client(m(diagnosis="Stringing", step_ids=["stringing.md#2", "stringing.md#3", "stringing.md#4",
                                                      "stringing.md#5", "stringing.md#6"]),
                  keep_going, keep_going, keep_going)
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "", action="walkthrough")
    r1 = converse(s, PRINT, "", action="failed")
    r2 = converse(s, PRINT, "", action="failed")
    assert r1.status == r2.status == "guided"
    r3 = converse(s, PRINT, "", action="failed")
    assert s.failures == MAX_FAILURES == 3
    assert (r3.status, r3.reason) == ("escalate", "not_fixed")
    assert "makerspace staff" in r3.answer and "tried 3 steps" in r3.answer
    assert r3.handoff.startswith("Problem: Stringing (stringing.md). Tried: ")
    assert r3.handoff.count("(didn't work)") == 3
    assert s.phase == "escalated" and len(fake.calls) == 4     # the third failure needed no extra call


def test_runs_out_of_visitor_steps_with_staff_steps_left(client):
    client(m(diagnosis="Clogged nozzle", source_doc="clogged-nozzle.md",
             step_ids=[f"clogged-nozzle.md#{i}" for i in range(1, 6)]),
           m(intent="failed", ruled_out="a stuck filament", diagnosis="Clogged nozzle", source_doc="clogged-nozzle.md",
             step_ids=[f"clogged-nozzle.md#{i}" for i in range(2, 6)]))
    s = new_session()
    converse(s, PRINT, "Nothing comes out")
    r = converse(s, PRINT, "", action="walkthrough")
    assert r.step["text"] == PRINT.steps["clogged-nozzle.md#1"].text
    r = converse(s, PRINT, "", action="failed")
    assert (r.status, r.reason) == ("escalate", "staff_only")
    assert "Needs staff: clogged-nozzle.md, How to fix it, step 2" in r.handoff
    assert "260" not in r.answer + r.handoff


def test_all_steps_failing_without_staff_steps_is_not_fixed(client):
    client(m(step_ids=["stringing.md#2"]), m(intent="failed", ruled_out="the presets", step_ids=[]))
    s = new_session()
    converse(s, PRINT, "What's wrong?")
    converse(s, PRINT, "", action="walkthrough")
    r = converse(s, PRINT, "", action="failed")
    assert (r.status, r.reason) == ("escalate", "not_fixed")


def test_done_resolves_a_fix_without_calling_the_model(client):
    fake = client(m())
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "", action="walkthrough")
    again = converse(s, PRINT, "", action="repeat")
    assert again.step["text"] == PRINT.steps["stringing.md#2"].text and again.answer.startswith("Here it is again.")
    r = converse(s, PRINT, "", action="done")
    assert r.status == "resolved" and s.phase == "resolved"
    assert len(fake.calls) == 1                                 # walkthrough, repeat and done are handled in code
    assert [msg["role"] for msg in s.messages] == ["user", "assistant"] * 4


def test_done_moves_through_a_procedure(client):
    client(m(diagnosis="Loading filament", plan_type="procedure", source_doc="loading-unloading-filament.md",
             step_ids=["loading-unloading-filament.md#4", "loading-unloading-filament.md#5"]))
    s = new_session()
    converse(s, PRINT, "How do I load filament?")
    r = converse(s, PRINT, "", action="walkthrough")
    assert (r.step["number"], r.step["total"]) == (1, 2)
    r = converse(s, PRINT, "", action="done")
    assert r.status == "guided" and (r.step["number"], r.step["total"]) == (2, 2)
    assert converse(s, PRINT, "", action="done").status == "resolved"


def test_typed_done_is_understood(client):
    client(m(), intent("done"))
    s = new_session()
    converse(s, PRINT, "What's wrong?")
    converse(s, PRINT, "", action="walkthrough")
    assert converse(s, PRINT, "yep that sorted it, thanks").status == "resolved"


def test_safety_mid_fix_stops_the_guided_fix(client):
    client(m(), intent("other", status="escalate", reason="safety", reply="Unplug it."))
    s = new_session()
    converse(s, PRINT, "What's wrong?")
    converse(s, PRINT, "", action="walkthrough")
    r = converse(s, PRINT, "Wait, now it smells like burning")
    assert (r.status, r.answer, r.step, r.actions) == ("escalate", PRINT.safety_message, None, [])
    assert s.phase == "escalated" and s.current is None


# ---------- summary ----------

def test_summary_recaps_problem_tried_outcome_and_sources(client):
    client(m(), m(intent="failed", ruled_out="the presets", next_check="the temperature",
                  step_ids=["stringing.md#3", "stringing.md#4"]))
    s = new_session()
    converse(s, PRINT, "What's wrong?", image_jpeg=PHOTO)
    converse(s, PRINT, "", action="walkthrough")
    converse(s, PRINT, "", action="failed")
    converse(s, PRINT, "", action="done")
    r = converse(s, PRINT, "", action="summary")
    assert r.status == "summary"
    assert r.summary["problem"] == "Stringing"
    assert [t["outcome"] for t in r.summary["tried"]] == ["didn't work", "fixed it"]
    assert r.summary["outcome"].startswith("Fixed: Lower the nozzle temperature")
    assert r.summary["sources"] == ["stringing.md"]
    assert r.summary["needs_staff"] == ["stringing.md, How to fix it, step 1"]
    assert r.answer == "Summary. Problem: Stringing. Tried 2 steps. Outcome: " + r.summary["outcome"] + "."


def test_summary_before_anything_happens(client):
    fake = client()
    r = converse(new_session(), PRINT, "", action="summary")
    assert r.summary["outcome"] == "Not diagnosed yet" and r.summary["tried"] == []
    assert fake.calls == []


def test_summary_mid_fix_keeps_the_step_on_screen(client):
    client(m())
    s = new_session()
    converse(s, PRINT, "What's wrong?")
    converse(s, PRINT, "", action="walkthrough")
    r = converse(s, PRINT, "", action="summary")
    assert r.summary["outcome"] == "In progress (step 1 of 3)"
    assert r.step is not None and r.actions == ["done", "failed", "repeat"]
    assert len(s.messages) == 4                                 # summaries aren't added to the history


def test_conversation_table_is_valid():
    from tests.run_questions import load_conversations, turn_action
    convs = load_conversations()
    assert [c["num"] for c in convs] == ["C1", "C2", "C3"]
    for c in convs:
        assert c["pack"] == "3d-printing" and c["expect"] in ("resolved", "escalate")
        assert turn_action(c["turns"][0])[0] == "message"
        assert "[walkthrough]" in c["turns"] and c["turns"][-1] == "[summary]"
