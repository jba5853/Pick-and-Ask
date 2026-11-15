"""Offline tests for the AI pack builder: drafts, review, publish, staff-only suggestions. No API calls."""

import io
import json
import os
from types import SimpleNamespace

import pytest
import yaml

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import admin, builder, main, packs  # noqa: E402
from app.builder import Source  # noqa: E402
from app.limits import RateLimiter  # noqa: E402

ADMIN = "staff-code-12345"
MANUAL = [
    "Section I. Safety\nNever leave the laser cutter unattended while it is running. Keep a fire extinguisher nearby.",
    "Section IV. Maintenance\nBefore starting a job, make sure the lens and optics are clean and cleared of debris "
    "using a lens tissue. Empty the crumb tray at the bottom of the machine after every few jobs.\n"
    "If the material does not cut all the way through, lower the speed or increase the power and try again. "
    "Check that the honey-\ncomb bed is level.",
]


@pytest.fixture
def docs(tmp_path, monkeypatch):
    folder = tmp_path / "docs"
    folder.mkdir()
    monkeypatch.setattr(packs, "DOCS_DIR", folder)
    return folder


def source():
    return Source("S1", "mc-series-manual.pdf", "pdf", list(MANUAL), retrieved="2026-11-12")


def step(text, quote, staff=False, reason="", review=""):
    return {"text": text, "quote": quote, "staff_only": staff, "staff_reason": reason, "review": review}


def model_output(**kw):
    base = {
        "name": "Laser Cutter", "description": "Using and troubleshooting the laser cutter.", "mode": "diagnose",
        "safety_message": "Stop the job, step away and tell a staff member right away.",
        "review_notes": [],
        "docs": [
            {"slug": "material-not-cutting-through", "title": "Material not cutting through", "source_id": "S1",
             "source_ref": "p. 2, Maintenance", "summary": "The laser marks the material but doesn't cut it.",
             "signs": ["The cut doesn't go all the way through"],
             "steps": [
                 step("Lower the speed or increase the power and try again.",
                      "lower the speed or increase the power and try again"),
                 step("Check that the honeycomb bed is level.", "Check that the honeycomb bed is level"),
                 step("Clean the lens with a lens tissue.", "the lens and optics are clean and cleared of debris"),
             ], "review_notes": []},
            {"slug": "emptying-the-crumb-tray", "title": "Emptying the crumb tray", "source_id": "S1",
             "source_ref": "p. 2, Maintenance", "summary": "",
             "signs": [],
             "steps": [
                 step("Empty the crumb tray after every few jobs.", "Empty the crumb tray at the bottom of the machine"),
                 step("Vacuum the inside of the machine weekly.", "vacuum the inside of the machine every week",
                      review="Not sure how often."),
             ], "review_notes": ["Check this applies to our model."]},
        ],
    }
    return base | kw


def build(docs, data=None, slug="laser-cutter"):
    return builder.write_draft(slug, data or model_output(), [source()])


# ---------- staff-only suggestions ----------

@pytest.mark.parametrize("text, reason", [
    ("Clean the lens with a lens tissue.", "laser optics or alignment"),
    ("Check the mirror alignment.", "laser optics or alignment"),
    ("Remove the back cover to reach the fuse.", "electrical"),
    ("Open the side panel and check the belt.", "opening the machine"),
    ("Use a hex key to tighten the bolt.", "needs tools"),
    ("Let the bed cool; it gets hot.", "heat, fire or burns"),
    ("Replace the blade in the housing.", "repair or part replacement"),
    ("Wipe it with acetone.", "chemicals"),
    ("Run the calibration from the menu.", "calibration or firmware"),
])
def test_risky_steps_are_suggested_staff_only(text, reason):
    assert builder.suggest_staff_only(text) == reason


@pytest.mark.parametrize("text", ["Press the Start button.", "Open the lid and place your material on the bed.",
                                  "Lower the speed and try again.", "Empty the crumb tray."])
def test_everyday_steps_are_not_flagged(text):
    assert builder.suggest_staff_only(text) == ""


# ---------- writing a draft ----------

def test_draft_is_written_with_sources_markers_and_review_notes(docs):
    r = build(docs)
    folder = docs / "_drafts" / "laser-cutter"
    assert r.folder == folder and r.docs == ["emptying-the-crumb-tray.md", "material-not-cutting-through.md"]
    doc = (folder / "material-not-cutting-through.md").read_text()
    lines = doc.splitlines()
    assert lines[0] == "# Material not cutting through"
    assert lines[2] == "> Source: mc-series-manual.pdf, p. 2, Maintenance · Retrieved 2026-11-12"
    assert "1. Lower the speed or increase the power and try again." in doc
    assert "3. Clean the lens with a lens tissue. {staff_only: true}" in doc       # keyword backstop
    assert "REVIEW" not in doc                                                     # every quote was found
    other = (folder / "emptying-the-crumb-tray.md").read_text()
    assert "> REVIEW: Check this applies to our model." in other                   # doc-level note
    assert "   REVIEW: Couldn't find this step's quoted text in the source." in other  # invented step caught
    assert "   REVIEW: Not sure how often." in other                                # model's own note
    assert r.unverified_steps == 1 and r.staff_only_steps == 1


def test_quote_check_handles_case_hyphenation_and_punctuation():
    norm = builder.normalize("\n".join(MANUAL))
    assert builder.quote_found("CHECK THAT THE HONEYCOMB BED IS LEVEL.", norm)   # "honey-\ncomb" in the PDF text
    assert builder.quote_found("the lens and optics are clean, and cleared of debris", norm)
    assert not builder.quote_found("vacuum the inside of the machine every week", norm)
    assert not builder.quote_found("lens", norm)                                 # too short to prove anything


def test_pack_yaml_needs_review_before_publishing(docs):
    build(docs)
    meta = yaml.safe_load((docs / "_drafts" / "laser-cutter" / "pack.yaml").read_text())
    assert meta["mode"] == "diagnose" and meta["safety_message"].startswith("Stop the job")
    assert meta["escalation"].startswith("REVIEW:")                              # a person must set the contact


def test_staff_reasons_are_kept_for_the_review_screen(docs):
    build(docs)
    d = builder.get_draft("laser-cutter")
    doc = next(x for x in d["docs"] if x["filename"] == "material-not-cutting-through.md")
    assert [s["staff_only"] for s in doc["steps"]] == [False, False, True]
    assert doc["steps"][2]["staff_reason"] == "Keyword rule: laser optics or alignment"


def test_drafts_are_never_live(docs):
    build(docs)
    assert packs.list_packs() == []
    with pytest.raises(ValueError):
        packs.load_pack("laser-cutter")


def test_unknown_source_is_flagged(docs):
    data = model_output()
    data["docs"][0]["source_id"] = "S9"
    build(docs, data)
    text = (docs / "_drafts" / "laser-cutter" / "material-not-cutting-through.md").read_text()
    assert "UNKNOWN SOURCE" in text and "REVIEW: The source for this doc couldn't be matched" in text


def test_existing_draft_or_live_pack_is_not_overwritten(docs):
    build(docs)
    with pytest.raises(ValueError, match="already exists"):
        build(docs)
    (docs / "laser-cutter").mkdir()
    with pytest.raises(ValueError, match="live pack"):
        builder.build_draft("laser-cutter", urls=["https://example.com"], overwrite=True)


# ---------- review and publish ----------

def resolve_reviews(slug="laser-cutter"):
    d = builder.get_draft(slug)
    for doc in d["docs"]:
        text = "\n".join(line for line in doc["text"].splitlines() if "REVIEW:" not in line)
        builder.save_doc(slug, doc["filename"], text)
    builder.save_pack(slug, {"escalation": "makerspace staff", "clear_pack_notes": True})


def test_publish_is_blocked_while_review_notes_remain(docs):
    build(docs)
    with pytest.raises(ValueError, match="REVIEW note"):
        builder.publish("laser-cutter")
    assert not (docs / "laser-cutter").exists()


def test_publish_after_review_makes_the_pack_live(docs):
    build(docs)
    resolve_reviews()
    assert builder.get_draft("laser-cutter")["issues"] == []
    target = builder.publish("laser-cutter")
    assert target == docs / "laser-cutter"
    assert packs.list_packs() == ["laser-cutter"]
    pack = packs.load_pack("laser-cutter")
    assert pack.mode == "diagnose" and pack.escalation == "makerspace staff"
    assert pack.steps["material-not-cutting-through.md#3"].staff_only
    assert not (docs / "_drafts" / "laser-cutter").exists()
    assert json.loads((target / "_build.json").read_text())["published"]
    assert "Draft pack" not in (target / "pack.yaml").read_text()


def test_pack_level_review_notes_block_until_cleared(docs):
    build(docs, model_output(review_notes=["Two models are described; check which one we have."]))
    d = builder.get_draft("laser-cutter")
    assert d["pack_notes"] == ["Two models are described; check which one we have."]
    builder.save_pack("laser-cutter", {"escalation": "makerspace staff"})
    assert any("Two models" in i for i in builder.get_draft("laser-cutter")["issues"])
    builder.save_pack("laser-cutter", {"clear_pack_notes": True})
    assert not any("Two models" in i for i in builder.get_draft("laser-cutter")["issues"])


def test_toggle_staff_only(docs):
    build(docs)
    builder.set_staff_only("laser-cutter", "material-not-cutting-through.md", 0, True)
    builder.set_staff_only("laser-cutter", "material-not-cutting-through.md", 2, False)
    steps = next(x for x in builder.get_draft("laser-cutter")["docs"]
                 if x["filename"] == "material-not-cutting-through.md")["steps"]
    assert [s["staff_only"] for s in steps] == [True, False, False]
    with pytest.raises(ValueError):
        builder.set_staff_only("laser-cutter", "material-not-cutting-through.md", 9, True)


def test_publish_refuses_an_incomplete_pack(docs):
    build(docs)
    resolve_reviews()
    builder.save_pack("laser-cutter", {"safety_message": ""})
    with pytest.raises(ValueError, match="safety_message"):
        builder.publish("laser-cutter")


@pytest.mark.parametrize("bad", ["../etc", "Laser", "a", "x/y", "_drafts"])
def test_bad_slugs_are_rejected(docs, bad):
    with pytest.raises(ValueError):
        builder.get_draft(bad)


@pytest.mark.parametrize("bad", ["../pack.yaml", "x.txt", "README.md", "a/b.md", ".md"])
def test_bad_doc_names_are_rejected(docs, bad):
    build(docs)
    with pytest.raises(ValueError):
        builder.save_doc("laser-cutter", bad, "hi")


# ---------- reading sources and calling the model ----------

def test_scanned_pdf_is_rejected():
    buf = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(buf, format="PDF")
    with pytest.raises(ValueError, match="scanned"):
        builder.pdf_source("S1", "scan.pdf", buf.getvalue())


def test_too_much_text_is_refused_not_truncated(monkeypatch):
    monkeypatch.setattr(builder, "MAX_SOURCE_CHARS", 100)
    with pytest.raises(ValueError, match="too long"):
        builder.sources_prompt([source()])


def test_sources_prompt_marks_pages():
    text = builder.sources_prompt([source()])
    assert '<source id="S1" label="mc-series-manual.pdf" kind="pdf">' in text
    assert "[Page 1]\nSection I. Safety" in text and "[Page 2]" in text


@pytest.mark.parametrize("url", ["http://localhost/x", "http://127.0.0.1/manual.pdf", "http://10.0.0.5/", "ftp://example.com/a",
                                 "file:///etc/passwd", "http://[::1]/"])
def test_private_or_odd_urls_are_refused(url):
    with pytest.raises(ValueError):
        builder.check_public_url(url)


class FakeStream:
    def __init__(self, message, calls, kwargs):
        self.message = message
        calls.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def fake_client(text, stop_reason="end_turn"):
    calls = []
    message = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])
    client = SimpleNamespace(calls=calls, beta=SimpleNamespace(messages=SimpleNamespace(
        stream=lambda **kw: FakeStream(message, calls, kw))))
    return client


def test_builder_streams_a_strict_schema_on_opus():
    client = fake_client(json.dumps(model_output()))
    data = builder.call_builder([source()], "laser-cutter", client)
    assert data["name"] == "Laser Cutter"
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 64000
    assert kw["output_config"]["effort"] == "high"
    schema = kw["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    step_schema = schema["properties"]["docs"]["items"]["properties"]["steps"]["items"]
    assert set(step_schema["required"]) == {"text", "quote", "staff_only", "staff_reason", "review"}
    assert kw["fallbacks"] == "default"
    assert "Use ONLY the sources" in kw["system"]


@pytest.mark.parametrize("stop, msg", [("refusal", "declined"), ("max_tokens", "too long")])
def test_builder_failures_are_explained(stop, msg):
    with pytest.raises(ValueError, match=msg):
        builder.call_builder([source()], "x1", fake_client("{}", stop))


def test_build_draft_end_to_end_with_a_fake_model(docs, monkeypatch):
    monkeypatch.setattr(builder, "url_source", lambda sid, url, client=None: Source(sid, url, "web", list(MANUAL), "2026-11-12"))
    r = builder.build_draft("laser-cutter", urls=["https://example.com/manual"], client=fake_client(json.dumps(model_output())))
    assert len(r.docs) == 2 and r.review_count > 0
    meta = json.loads((r.folder / "_draft.json").read_text())
    assert meta["sources"][0]["label"] == "https://example.com/manual" and meta["model"] == "claude-opus-5-5"


# ---------- admin API ----------

@pytest.fixture
def staff(docs, monkeypatch):
    monkeypatch.setenv("ADMIN_CODE", ADMIN)
    monkeypatch.setattr(admin, "wrong_admin_codes", RateLimiter(5, 600))
    return TestClient(main.app)


H = {"X-Admin-Code": ADMIN}


def test_admin_is_off_without_a_code(docs, monkeypatch):
    monkeypatch.delenv("ADMIN_CODE", raising=False)
    r = TestClient(main.app).get("/admin/api/drafts", headers=H)
    assert r.status_code == 403 and "ADMIN_CODE" in r.json()["detail"]


def test_admin_needs_the_right_code(staff):
    assert staff.get("/admin/api/drafts").status_code == 401
    assert staff.get("/admin/api/drafts", headers={"X-Admin-Code": "nope"}).status_code == 401
    assert staff.get("/admin/api/drafts", headers=H).json() == []
    assert staff.get("/admin").status_code == 200 and staff.get("/admin/insights").status_code == 200


def test_access_code_is_not_the_admin_code(staff, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", "visitor-code")
    assert staff.get("/admin/api/drafts", headers={"X-Admin-Code": "visitor-code"}).status_code == 401


def test_wrong_admin_codes_lock_out(staff):
    for _ in range(5):
        staff.post("/admin/api/check", headers={"X-Admin-Code": "guess"})
    assert staff.post("/admin/api/check", headers=H).status_code == 429


def test_review_and_publish_through_the_api(staff, docs):
    build(docs)
    d = staff.get("/admin/api/drafts/laser-cutter", headers=H).json()
    assert d["issues"]
    assert staff.post("/admin/api/drafts/laser-cutter/publish", headers=H).status_code == 400
    for doc in d["docs"]:
        clean = "\n".join(line for line in doc["text"].splitlines() if "REVIEW:" not in line)
        assert staff.put(f"/admin/api/drafts/laser-cutter/docs/{doc['filename']}", headers=H, json={"text": clean}).status_code == 200
    r = staff.post("/admin/api/drafts/laser-cutter/docs/emptying-the-crumb-tray.md/steps/0/staff_only", headers=H, json={"value": True})
    assert r.json()["docs"][0]["steps"][0]["staff_only"] is True
    r = staff.put("/admin/api/drafts/laser-cutter/pack", headers=H, json={"escalation": "makerspace staff"})
    assert r.json()["issues"] == []
    r = staff.post("/admin/api/drafts/laser-cutter/publish", headers=H)
    assert r.status_code == 200 and r.json() == {"published": "laser-cutter", "url": "/?pack=laser-cutter"}
    assert "laser-cutter" in [p["id"] for p in staff.get("/packs").json()]


def test_build_endpoint(staff, monkeypatch):
    seen = {}

    def fake_build(slug, files, urls):
        seen.update(slug=slug, files=files, urls=urls)
        return builder.DraftResult(slug, packs.DOCS_DIR / "_drafts" / slug, ["a.md"], 3, 1, 2)

    monkeypatch.setattr(builder, "build_draft", fake_build)
    r = staff.post("/admin/api/build", headers=H, data={"slug": "laser-cutter", "urls": "https://a.example/m.pdf\n\n"},
                   files=[("files", ("manual.pdf", b"%PDF-1.4 fake", "application/pdf"))])
    assert r.status_code == 200 and r.json()["review_count"] == 3
    assert seen == {"slug": "laser-cutter", "files": [("manual.pdf", b"%PDF-1.4 fake")], "urls": ["https://a.example/m.pdf"]}
    assert staff.post("/admin/api/build", data={"slug": "x1"}).status_code == 401
