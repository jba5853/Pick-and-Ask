"""Offline tests: no API key or network needed. Run with `pytest`."""

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app import assistant
from app.assistant import ask, build_schema, interpret, trim_sentences
from app.images import MAX_EDGE, prepare_image
from app.packs import list_packs, load_pack
from tests.run_questions import load_cases

PACK = load_pack("ai-arcade")
DOC = "PLACEHOLDER-station-1-image-generator.md"


# --- packs ---

def test_pack_loads():
    assert "ai-arcade" in list_packs()
    assert PACK.escalation == "AI Office Hours"
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
        assert r.status == "escalate" and "AI Office Hours" in r.answer


def test_made_up_source_escalates():
    r = interpret(PACK, reply(source_doc="invented.md"))
    assert r.status == "escalate" and r.reason == "not_covered"


@pytest.mark.parametrize("reason", ["safety", "personal_data", "not_covered"])
def test_escalation_uses_pack_contact(reason):
    r = interpret(PACK, reply(status="escalate", reason=reason, answer="", source_doc=""))
    assert (r.status, r.reason) == ("escalate", reason)
    assert "AI Office Hours" in r.answer


def test_safety_reply_uses_pack_message_not_model_text():
    r = interpret(PACK, reply(status="escalate", reason="safety", answer="Just unplug it and carry on."))
    assert r.answer == f"{PACK.safety_message} Please check with AI Office Hours."
    assert "unplug" not in r.answer


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
    assert len(cases) == 8
    for c in cases:
        if c["expect"] == "answer":
            assert c["target"] in PACK.docs
        else:
            assert c["target"] in ("not_covered", "safety", "personal_data")
