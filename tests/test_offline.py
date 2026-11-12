"""Offline tests: no API key or network needed. Run with `pytest`."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app import assistant
from app.assistant import ask, build_diagnose_schema, build_schema, interpret, interpret_diagnose, trim_sentences
from app.images import MAX_EDGE, prepare_image
from app.packs import list_packs, load_pack
from tests.run_questions import load_cases

PACK = load_pack("ai-arcade")
DOC = "a006-postcard-portals.md"


# --- packs ---

def test_pack_loads():
    assert "ai-arcade" in list_packs()
    assert PACK.escalation == "a Media Commons staff member or aiarcade@psu.edu"
    assert DOC in PACK.docs


def test_unknown_pack_rejected():
    with pytest.raises(ValueError):
        load_pack("../etc")


def test_schema_only_allows_real_docs():
    assert build_schema(PACK)["properties"]["source_doc"]["enum"] == sorted(PACK.docs) + [""]


# --- answer handling ---

def reply(**kw):
    base = {"status": "answered", "reason": "none", "answer": "Tap Send to email.", "source_doc": DOC}
    return json.dumps(base | kw)


def test_good_answer():
    r = interpret(PACK, reply())
    assert (r.status, r.source_doc) == ("answered", DOC)


def test_unparseable_reply_escalates():
    for bad in [None, "", "not json", "{}", '{"status": "answered"}']:
        r = interpret(PACK, bad)
        assert r.status == "escalate" and "a Media Commons staff member or aiarcade@psu.edu" in r.answer


def test_made_up_source_escalates():
    r = interpret(PACK, reply(source_doc="invented.md"))
    assert r.status == "escalate" and r.reason == "not_covered"


@pytest.mark.parametrize("reason", ["personal_data", "not_covered"])
def test_escalation_uses_pack_contact(reason):
    r = interpret(PACK, reply(status="escalate", reason=reason, answer="", source_doc=""))
    assert (r.status, r.reason) == ("escalate", reason)
    assert "a Media Commons staff member or aiarcade@psu.edu" in r.answer


def test_not_covered_reply_says_what_pack_covers():
    r = interpret(PACK, reply(status="escalate", reason="not_covered", answer="", source_doc=""))
    assert r.answer == (
        "I don't have that in the AI Arcade guides, so I won't guess. Please check with a Media Commons staff member or aiarcade@psu.edu. "
        "I can help with the AI Arcade tools, activities and how to get help."
    )


@pytest.mark.parametrize("reason", ["personal_data", "error"])
def test_other_escalations_have_no_help_line(reason):
    r = assistant.escalation(PACK, reason)
    assert "I can help with" not in r.answer
    assert r.answer.endswith("Please check with a Media Commons staff member or aiarcade@psu.edu.")


def test_safety_reply_uses_pack_message_not_model_text():
    r = interpret(PACK, reply(status="escalate", reason="safety", answer="Just unplug it and carry on."))
    assert (r.status, r.reason) == ("escalate", "safety")
    assert r.answer == PACK.safety_message


def test_trim_keeps_whole_sentences():
    text = "One. Two is here! Three? Four should go. Five too."
    assert trim_sentences(text) == "One. Two is here! Three?"
    assert trim_sentences("Short answer without a period") == "Short answer without a period"
    assert trim_sentences("Use version 2.5 here. Then tap Done.") == "Use version 2.5 here. Then tap Done."


# --- API call wiring (fake client) ---

class FakeClient:
    def __init__(self, response):
        self.response, self.kwargs = response, None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


def test_ask_sends_schema_and_image(monkeypatch):
    fake = FakeClient(fake_response(reply()))
    monkeypatch.setattr(assistant, "_client", fake)
    r = ask(PACK, "How do I save it?", b"\xff\xd8fakejpeg")
    assert r.status == "answered"
    assert fake.kwargs["model"] == "claude-sonnet-5-5"
    assert fake.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert fake.kwargs["messages"][0]["content"][0]["type"] == "image"


def test_refusal_escalates(monkeypatch):
    monkeypatch.setattr(assistant, "_client", FakeClient(fake_response("", stop_reason="refusal")))
    assert ask(PACK, "anything").status == "escalate"


# --- images ---

def jpeg_with_exif(size):
    img = Image.new("RGB", size, "red")
    exif = Image.Exif()
    exif[0x0112] = 6       # orientation: rotate 90
    exif[0x010F] = "Meta"  # camera make
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def test_image_resized_rotated_and_stripped():
    out = Image.open(io.BytesIO(prepare_image(jpeg_with_exif((4000, 3000)))))
    assert out.format == "JPEG"
    assert max(out.size) <= MAX_EDGE
    assert out.size[1] > out.size[0]   # orientation applied: now portrait
    assert len(out.getexif()) == 0     # metadata gone


def test_heic_supported():
    buf = io.BytesIO()
    try:
        Image.new("RGB", (2000, 1500), "blue").save(buf, format="HEIF")
    except (KeyError, OSError):
        pytest.skip("HEIF encoder not available")
    out = Image.open(io.BytesIO(prepare_image(buf.getvalue())))
    assert out.format == "JPEG" and max(out.size) <= MAX_EDGE


def test_bad_image_rejected():
    with pytest.raises(ValueError):
        prepare_image(b"not an image")


# --- test table ---

def test_questions_table_is_valid():
    cases = load_cases()
    assert len(cases) == 14
    assert {c["pack"] for c in cases} == {"ai-arcade", "3d-printing"}
    assert sum(c["pack"] == "3d-printing" for c in cases) == 6
    for c in cases:
        pack = load_pack(c["pack"])
        if c["photo"]:
            assert (Path(__file__).parent / "images" / c["photo"]).is_file(), c["photo"]
        if c["expect"] == "answer":
            assert all(doc in pack.docs for doc in c["target"].split(" or "))
        elif c["expect"] == "escalate":
            assert c["target"] in ("not_covered", "safety", "personal_data")
        else:
            assert c["expect"] in ("needs_more_info", "ai_suggestion") and pack.mode == "diagnose"


# --- 3d-printing pack + diagnose mode ---

PRINT = load_pack("3d-printing")
PRINT_DOC = "stringing.md"


def test_print_pack_loads_in_diagnose_mode():
    assert PRINT.mode == "diagnose"
    assert PACK.mode == "answer"  # packs without a mode keep the old behavior
    assert PRINT.escalation == "makerspace staff"
    for topic in ["first-layer-not-sticking", "stringing", "warping", "spaghetti-failed-print", "under-extrusion",
                  "clogged-nozzle", "filament-jam-and-runout", "layer-shifting", "loading-unloading-filament"]:
        assert f"{topic}.md" in PRINT.docs
    assert "README.md" not in PRINT.docs  # maintainer notes are never sent to the model


def test_every_print_doc_names_source_and_date():
    for name, text in PRINT.docs.items():
        header = text.splitlines()[2]
        assert header.startswith("> Source") and "help.prusa3d.com" in header and "Retrieved 2026-11-11" in header, name


def test_print_safety_message_covers_hazards():
    msg = PRINT.safety_message.lower()
    for word in ["nozzle", "bed", "smoke", "burning", "wires"]:
        assert word in msg


def test_unknown_mode_rejected(tmp_path, monkeypatch):
    from app import packs
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "pack.yaml").write_text("name: X\ndescription: X.\nescalation: X\nsafety_message: X\nmode: wizard\n")
    (folder / "a.md").write_text("# A")
    monkeypatch.setattr(packs, "DOCS_DIR", tmp_path)
    with pytest.raises(ValueError, match="unknown mode"):
        load_pack("bad")


def test_diagnose_schema_is_strict():
    schema = build_diagnose_schema(PRINT)
    props = schema["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(props) == {
        "status", "reason", "intent", "observation", "diagnosis", "confidence", "plan_type", "step_ids",
        "source_doc", "reply", "ruled_out", "next_check", "ai_suggestion", "follow_up_question"}
    assert props["status"]["enum"] == ["answered", "needs_more_info", "escalate"]
    assert props["confidence"]["enum"] == ["high", "medium", "low", ""]
    # The model can only point at real steps; it never writes step text.
    assert props["step_ids"] == {"type": "array", "items": {"type": "string", "enum": list(PRINT.steps)}}
    assert props["source_doc"]["enum"] == sorted(PRINT.docs) + [""]


def dreply(**kw):
    base = {
        "status": "answered", "reason": "none", "intent": "new_problem",
        "observation": "Two orange towers with thin strands of plastic between them.",
        "diagnosis": "Stringing", "confidence": "high", "plan_type": "fix",
        "step_ids": ["stringing.md#3", "stringing.md#4", "stringing.md#6"],
        "source_doc": PRINT_DOC, "reply": "", "ruled_out": "", "next_check": "",
        "ai_suggestion": "", "follow_up_question": "",
    }
    return json.dumps(base | kw)


def test_good_diagnosis_uses_doc_text_not_model_text():
    r = interpret_diagnose(PRINT, dreply())
    assert (r.status, r.source_doc, r.reason, r.confidence) == ("answered", PRINT_DOC, "none", "high")
    assert r.observation.startswith("Two orange towers")
    assert [s["text"] for s in r.steps] == [PRINT.steps[i].text for i in ["stringing.md#3", "stringing.md#4", "stringing.md#6"]]
    assert r.answer == "Stringing. First: Lower the nozzle temperature by 5–10 °C (Filament settings, Nozzle) and see if stringing drops. 2 more steps on screen."
    assert r.actions == ["walkthrough"]


def test_medium_confidence_still_answers():
    assert interpret_diagnose(PRINT, dreply(confidence="medium")).status == "answered"


def test_steps_from_other_docs_are_dropped_from_a_diagnosis():
    r = interpret_diagnose(PRINT, dreply(step_ids=["warping.md#2", "stringing.md#3"]))
    assert [s["text"] for s in r.steps] == [PRINT.steps["stringing.md#3"].text]


def test_low_confidence_asks_instead_of_fixing():
    r = interpret_diagnose(PRINT, dreply(confidence="low", follow_up_question="Is the plastic stringy or curled up?"))
    assert (r.status, r.confidence) == ("needs_more_info", "low")
    assert r.follow_up_question == r.answer == "Is the plastic stringy or curled up?"
    assert r.steps == [] and r.source_doc is None


def test_needs_more_info_keeps_only_one_question():
    r = interpret_diagnose(PRINT, dreply(status="needs_more_info", confidence="low", step_ids=[], source_doc="",
                                         follow_up_question="Is it stringy? Or is it warped? Also, what filament?"))
    assert r.follow_up_question == "Is it stringy?"


def test_needs_more_info_without_question_escalates():
    r = interpret_diagnose(PRINT, dreply(status="needs_more_info", confidence="low", follow_up_question="  "))
    assert (r.status, r.reason) == ("escalate", "not_covered")
    assert "makerspace staff" in r.answer


@pytest.mark.parametrize("bad", [{"source_doc": "invented.md"}, {"source_doc": ""}, {"step_ids": []},
                                 {"step_ids": ["invented.md#1"]}, {"diagnosis": ""}, {"confidence": ""}])
def test_incomplete_diagnosis_escalates_without_ai_text(bad):
    r = interpret_diagnose(PRINT, dreply(ai_suggestion="Just crank the temperature.", **bad))
    assert (r.status, r.reason) == ("escalate", "not_covered")
    assert r.ai_suggestion == "" and r.steps == []


def test_ai_suggestion_kept_separately_when_not_covered():
    r = interpret_diagnose(PRINT, dreply(status="escalate", reason="not_covered", step_ids=[], source_doc="",
                                         diagnosis="Elephant's foot", confidence="medium",
                                         ai_suggestion="Lower the bed temperature a little. Check the first layer isn't squished."))
    assert (r.status, r.reason, r.source_doc) == ("escalate", "not_covered", None)
    assert r.ai_suggestion == "Lower the bed temperature a little. Check the first layer isn't squished."
    assert r.answer.startswith("I don't have that in the 3D Printing guides")  # official text stays separate
    assert r.answer.endswith(assistant.help_line(PRINT))
    assert assistant.AI_LABEL == "AI suggestion (not from official guides)"


def test_ai_suggestion_is_trimmed():
    long = "One. Two. Three. Four. Five."
    r = interpret_diagnose(PRINT, dreply(status="escalate", reason="not_covered", step_ids=[], source_doc="", ai_suggestion=long))
    assert r.ai_suggestion == "One. Two. Three."


def test_ai_suggestion_with_staff_only_content_is_dropped():
    r = interpret_diagnose(PRINT, dreply(status="escalate", reason="not_covered", step_ids=[], source_doc="",
                                         ai_suggestion="Do a cold pull with some PLA."))
    assert r.ai_suggestion == ""


@pytest.mark.parametrize("status", ["answered", "escalate", "needs_more_info"])
def test_safety_drops_everything_the_model_wrote(status):
    r = interpret_diagnose(PRINT, dreply(status=status, reason="safety", ai_suggestion="Unplug it and blow on it.",
                                         follow_up_question="Is it smoking a lot?"))
    assert (r.status, r.reason, r.answer) == ("escalate", "safety", PRINT.safety_message)
    assert (r.observation, r.diagnosis, r.confidence, r.steps, r.ai_suggestion, r.follow_up_question, r.source_doc) == \
        ("", "", "", [], "", "", None)


def test_personal_data_drops_everything():
    r = interpret_diagnose(PRINT, dreply(status="escalate", reason="personal_data", ai_suggestion="It's probably Sam's."))
    assert (r.status, r.reason, r.ai_suggestion, r.steps) == ("escalate", "personal_data", "", [])


@pytest.mark.parametrize("bad", [None, "", "not json", "{}", '{"status": "answered"}',
                                 dreply(step_ids="stringing.md#3"), dreply(observation=None)])
def test_unparseable_diagnosis_escalates(bad):
    r = interpret_diagnose(PRINT, bad)
    assert (r.status, r.reason) == ("escalate", "error")
    assert "makerspace staff" in r.answer


def test_ask_uses_diagnose_prompt_and_schema(monkeypatch):
    fake = FakeClient(fake_response(dreply()))
    monkeypatch.setattr(assistant, "_client", fake)
    r = ask(PRINT, "What's wrong?", b"\xff\xd8fakejpeg")
    assert r.status == "answered" and r.steps
    assert fake.kwargs["output_config"]["format"]["schema"] == build_diagnose_schema(PRINT)
    assert fake.kwargs["output_config"]["effort"] == "medium"
    system = fake.kwargs["system"][0]["text"]
    assert "troubleshooting assistant" in system
    assert "1. [stringing.md#1] (STAFF ONLY) Clean old residue off the nozzle before printing." in system
    assert "{staff_only" not in system  # the raw marker is replaced by the tag


def test_answer_mode_unchanged_by_diagnose(monkeypatch):
    fake = FakeClient(fake_response(reply()))
    monkeypatch.setattr(assistant, "_client", fake)
    r = ask(PACK, "How do I share it?")
    assert fake.kwargs["output_config"]["format"]["schema"] == build_schema(PACK)
    assert fake.kwargs["output_config"]["effort"] == "low"
    assert (r.observation, r.steps, r.ai_suggestion) == ("", [], "")
