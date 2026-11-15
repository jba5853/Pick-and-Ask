"""Offline tests for "point at anything": flow, safety rules, text routing, WhatsApp webhook, API. No model calls."""

import hashlib
import hmac
import io
import json
import os

import pytest
from PIL import Image

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-used")

from fastapi.testclient import TestClient  # noqa: E402

from app import anything as A  # noqa: E402
from app import main, router, whatsapp  # noqa: E402

FAN = {"object_name": "Tower fan", "brand": "Dyson", "model": "Pure Cool TP07", "category": "fan",
       "confidence": "high", "what_i_see": "A tower fan, display off.", "hazard": "none", "hazard_note": "",
       "actions": [{"id": "setup", "label": "Set up the fan", "detail": "From the box"},
                   {"id": "troubleshoot", "label": "Fix a problem", "detail": "Not working?"},
                   {"id": "learn", "label": "Learn the controls", "detail": "Remote and app"}],
       "spoken": "This looks like a Dyson Pure Cool fan. Set it up, fix a problem, or learn the controls?",
       "question_for_user": ""}
PLAN = {"status": "plan", "title": "Set up your Dyson fan", "spoken_intro": "Let's set it up in three steps.",
        "steps": [{"title": "Attach the base", "detail": "Click the loop onto the base.", "spoken": "Click the loop onto the base.", "check": "photo", "caution": ""},
                  {"title": "Plug it in", "detail": "Plug into a wall socket.", "spoken": "Plug it into the wall.", "check": "confirm", "caution": "Mains power."},
                  {"title": "Press power", "detail": "Press the power button on the remote.", "spoken": "Press power on the remote.", "check": "confirm", "caution": ""}],
        "question_for_user": "", "grounded": True, "source_urls": ["https://www.dyson.com/support/tp07"]}
RESULTS = [{"title": "Dyson TP07 support", "url": "https://www.dyson.com/support/tp07"},
           {"title": "Random blog", "url": "https://blog.example.com/fan"}]


def jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 200, 220)).save(out, format="JPEG")
    return out.getvalue()


@pytest.fixture
def fake(monkeypatch):
    """Fake model: identify -> FAN, plan -> PLAN, verify -> done, chat -> a short answer."""
    state = {"identify": dict(FAN), "plan": dict(PLAN), "verify": {"verdict": "done", "feedback": "Base is seated.",
             "spoken": "Looks good.", "hazard": "none"},
             "chat": {"reply": "Check the plug is fully in.", "spoken": "Check the plug.", "next": "retry_step",
                      "hazard": "none", "source_urls": []}, "calls": []}

    def structured(system, content, schema, effort="low", max_tokens=3000):
        kind = "identify" if system is A.IDENTIFY_SYSTEM else "verify"
        state["calls"].append(kind)
        return json.loads(json.dumps(state[kind]))

    def researched(system, content, effort="medium", max_tokens=6000):
        kind = "plan" if system is A.PLAN_SYSTEM else "chat"
        state["calls"].append(kind)
        return json.loads(json.dumps(state[kind])), list(RESULTS)

    monkeypatch.setattr(A, "structured", structured)
    monkeypatch.setattr(A, "researched", researched)
    return state


# ---------------------------------------------------------------- engine

def test_identify_then_menu(fake):
    s = A.identify(A.store.new(), jpeg())
    assert s.stage == "menu" and [a["id"] for a in s.actions] == ["setup", "troubleshoot", "learn"]
    assert s.thumb.startswith("data:image/jpeg;base64,")
    assert s.messages[-1]["kind"] == "identify"


def test_identify_stop_hazard_shows_fixed_message(fake):
    fake["identify"].update(hazard="stop", hazard_note="smoke")
    s = A.identify(A.store.new(), jpeg())
    assert s.stage == "stopped" and s.messages[-1]["text"] == A.STOP_MESSAGE


def test_identify_failure_asks_for_another_photo(fake, monkeypatch):
    monkeypatch.setattr(A, "structured", lambda *a, **k: None)
    s = A.identify(A.store.new(), jpeg())
    assert s.stage == "need_info" and "another photo" in s.messages[-1]["text"]


def test_bad_action_ids_fall_back_to_defaults(fake):
    fake["identify"]["actions"] = [{"id": "hack", "label": "x", "detail": "y"}]
    s = A.identify(A.store.new(), jpeg())
    assert s.actions == A.DEFAULT_ACTIONS


def test_plan_picks_only_used_sources(fake):
    s = A.identify(A.store.new(), jpeg())
    A.start_task(s, "setup")
    assert s.stage == "coaching" and len(s.steps) == 3 and s.current == 0
    assert s.sources == [RESULTS[0]] and s.task["grounded"] is True
    assert s.messages[-1]["kind"] == "step" and s.messages[-1]["text"].startswith("First,")
    assert "photo" in s.messages[-1]["text"]  # step 1 is a photo check


def test_plan_refer_pro_has_no_steps(fake):
    fake["plan"] = {"status": "refer_pro", "steps": [{"title": "open it"}]}
    s = A.identify(A.store.new(), jpeg())
    A.start_task(s, "troubleshoot")
    assert s.stage == "stopped" and s.steps == [] and s.messages[-1]["text"] == A.PRO_MESSAGE


def test_plan_need_info_then_replan(fake):
    fake["plan"] = {"status": "need_info", "steps": [], "question_for_user": "What does it do wrong?"}
    s = A.identify(A.store.new(), jpeg())
    A.start_task(s, "troubleshoot")
    assert s.stage == "need_info" and s.task["question"] == "What does it do wrong?"
    fake["plan"] = dict(PLAN)
    out = router.handle_text(s, "it makes a rattling noise")
    assert s.stage == "coaching" and any("First," in m for m in out)


def test_advance_and_finish(fake):
    s = A.identify(A.store.new(), jpeg())
    A.start_task(s, "setup")
    A.advance(s, "next")
    assert s.current == 1 and s.steps[0].status == "done"
    A.advance(s, "back")
    assert s.current == 0 and s.steps[0].status == "todo"
    for _ in range(3):
        A.advance(s, "next")
    assert s.stage == "finished" and s.current == 3
    A.advance(s, "restart")
    assert s.stage == "coaching" and s.current == 0


def test_verify_done_advances_not_yet_stays(fake):
    s = A.identify(A.store.new(), jpeg())
    A.start_task(s, "setup")
    _, v = A.verify(s, jpeg())
    assert v["verdict"] == "done" and s.current == 1
    fake["verify"].update(verdict="not_yet", feedback="The base isn't clicked in.")
    _, v = A.verify(s, jpeg())
    assert v["verdict"] == "not_yet" and s.current == 1


def test_verify_hazard_stops(fake):
    s = A.identify(A.store.new(), jpeg())
    A.start_task(s, "setup")
    fake["verify"].update(hazard="stop")
    A.verify(s, jpeg())
    assert s.stage == "stopped"


def test_chat_hazard_stops_and_hides_reply(fake):
    s = A.identify(A.store.new(), jpeg())
    fake["chat"].update(hazard="stop", reply="do this risky thing")
    A.chat(s, "it's sparking")
    assert s.stage == "stopped" and s.messages[-1]["text"] == A.STOP_MESSAGE
    assert all("risky" not in m["text"] for m in s.messages)


def test_photos_capped(fake):
    s = A.store.new()
    for _ in range(6):
        s.add_photo(jpeg())
    assert len(s.photos) == A.MAX_PHOTOS


# ---------------------------------------------------------------- text routing (WhatsApp / voice)

@pytest.mark.parametrize("text,action", [("1", "setup"), ("two", "troubleshoot"), ("set it up please", "setup"),
                                         ("uh I want to fix it", "troubleshoot"), ("learn the controls", "learn")])
def test_menu_choice(fake, text, action):
    s = A.identify(A.store.new(), jpeg())
    router.handle_text(s, text)
    assert s.task["action"] == action and s.stage == "coaching"


@pytest.mark.parametrize("text,current", [("done", 1), ("okay done", 1), ("ok", 1), ("yep finished", 1),
                                          ("repeat", 0), ("say that again", 0), ("skip", 1)])
def test_coaching_commands(fake, text, current):
    s = A.identify(A.store.new(), jpeg())
    router.handle_text(s, "1")
    router.handle_text(s, text)
    assert s.current == current


def test_didnt_work_asks_model(fake):
    s = A.identify(A.store.new(), jpeg())
    router.handle_text(s, "1")
    out = router.handle_text(s, "nope that didn't work")
    assert fake["calls"][-1] == "chat" and out == ["Check the plug is fully in."] and s.current == 0


def test_reset_and_stopped(fake):
    s = A.identify(A.store.new(), jpeg())
    router.handle_text(s, "1")
    out = router.handle_text(s, "start over")
    assert s.stage == "menu" and "starting fresh" in out[0]
    s.stage = "stopped"
    assert router.handle_text(s, "do it anyway") == [A.STOP_MESSAGE]


# ---------------------------------------------------------------- WhatsApp webhook

@pytest.fixture
def wa(monkeypatch, fake):
    monkeypatch.setenv("WA_TOKEN", "t")
    monkeypatch.setenv("WA_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WA_VERIFY_TOKEN", "verify-me")
    monkeypatch.delenv("WA_APP_SECRET", raising=False)
    monkeypatch.delenv("WA_ALLOWED_NUMBERS", raising=False)
    monkeypatch.delenv("ACCESS_CODE", raising=False)
    sent = []
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: sent.append((to, body)))
    monkeypatch.setattr(whatsapp, "download_media", lambda mid: jpeg())
    whatsapp._seen.clear()
    whatsapp._by_sender.clear()
    return TestClient(main.app), sent


def wa_payload(*msgs):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": list(msgs)}}]}]}


def img(mid, frm="15550001111"):
    return {"from": frm, "id": mid, "type": "image", "image": {"id": "media1", "mime_type": "image/jpeg"}}


def txt(mid, body, frm="15550001111"):
    return {"from": frm, "id": mid, "type": "text", "text": {"body": body}}


def test_webhook_verify(wa):
    client, _ = wa
    ok = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                   "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                    "hub.challenge": "42"})
    assert bad.status_code == 403


def test_glasses_photo_full_flow(wa):
    client, sent = wa
    client.post("/webhooks/whatsapp", json=wa_payload(img("m1")))
    assert any("Dyson Pure Cool" in b and "1) Set up the fan" in b for _, b in sent)
    client.post("/webhooks/whatsapp", json=wa_payload(txt("m2", "set it up")))
    assert "First," in sent[-1][1]
    client.post("/webhooks/whatsapp", json=wa_payload(img("m3")))  # photo mid-task = check my work
    assert "Base is seated." in sent[-1][1] and "Next," in sent[-1][1]
    # The phone app sees the same session
    events = client.get("/api/inbox").json()["events"]
    sid = events[-1]["session_id"]
    view = client.get(f"/api/session/{sid}").json()
    assert view["channel"] == "whatsapp" and view["current"] == 1


def test_webhook_dedupes_retries(wa):
    client, sent = wa
    client.post("/webhooks/whatsapp", json=wa_payload(img("dup")))
    n = len(sent)
    client.post("/webhooks/whatsapp", json=wa_payload(img("dup")))
    assert len(sent) == n


def test_webhook_allow_list(wa, monkeypatch):
    client, sent = wa
    monkeypatch.setenv("WA_ALLOWED_NUMBERS", "15559999999")
    client.post("/webhooks/whatsapp", json=wa_payload(img("x1", frm="15550001111")))
    assert sent == []


def test_webhook_signature(wa, monkeypatch):
    client, sent = wa
    monkeypatch.setenv("WA_APP_SECRET", "shh")
    body = json.dumps(wa_payload(txt("s1", "hi"))).encode()
    assert client.post("/webhooks/whatsapp", content=body, headers={"content-type": "application/json",
                       "x-hub-signature-256": "sha256=bad"}).status_code == 403
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/whatsapp", content=body, headers={"content-type": "application/json",
                       "x-hub-signature-256": sig}).status_code == 200


def test_text_before_photo_gets_hint(wa):
    client, sent = wa
    client.post("/webhooks/whatsapp", json=wa_payload(txt("h1", "hello", frm="15557770000")))
    assert "Send me a photo" in sent[-1][1]


# ---------------------------------------------------------------- web API

@pytest.fixture
def web(monkeypatch, fake):
    monkeypatch.setenv("ACCESS_CODE", "demo-code-1")
    return TestClient(main.app)


def test_api_requires_code(web):
    r = web.post("/api/identify", files={"photo": ("a.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 401


def test_api_flow(web):
    h = {"x-access-code": "demo-code-1"}
    v = web.post("/api/identify", files={"photo": ("a.jpg", jpeg(), "image/jpeg")}, headers=h).json()
    assert v["stage"] == "menu" and v["object"]["brand"] == "Dyson"
    v = web.post("/api/task", data={"session_id": v["id"], "action": "setup"}, headers=h).json()
    assert v["stage"] == "coaching" and len(v["steps"]) == 3 and v["sources"][0]["url"].startswith("https://www.dyson")
    v = web.post("/api/step", data={"session_id": v["id"], "op": "next"}, headers=h).json()
    assert v["current"] == 1
    r = web.post("/api/verify", data={"session_id": v["id"]}, files={"photo": ("b.jpg", jpeg(), "image/jpeg")},
                 headers=h).json()
    assert r["verdict"]["verdict"] == "done" and r["current"] == 2
    v = web.post("/api/chat", data={"session_id": v["id"], "message": "is it safe to leave on?"}, headers=h).json()
    assert v["messages"][-1]["role"] == "assistant"
    assert web.get("/api/recent", headers=h).json()[0]["name"] == "Tower fan"


def test_api_bad_op_and_missing_session(web):
    h = {"x-access-code": "demo-code-1"}
    assert web.post("/api/step", data={"session_id": "nope", "op": "fly"}, headers=h).status_code == 400
    assert web.post("/api/step", data={"session_id": "nope", "op": "next"}, headers=h).status_code == 404


def test_pages_served(web):
    assert "Glance at it" in web.get("/").text
    assert web.get("/classic").status_code == 200
    assert web.get("/api/status").json()["whatsapp"]["enabled"] in (True, False)


def test_parse_json_block():
    assert A.parse_json_block('blah ```json\n{"a": 1}\n``` more') == {"a": 1}
    assert A.parse_json_block('text {"b": 2} end') == {"b": 2}
    assert A.parse_json_block("no json") is None


def test_correct_renames_and_rebuilds_menu(fake):
    s = A.store.new("web"); A.identify(s, jpeg())
    A.start_task(s, "setup")
    assert s.steps
    fake["identify"].update(object_name="Wrong guess", brand="Other", model="X1")
    A.correct(s, "Space heater", "Vornado", "VH200")
    assert (s.obj["object_name"], s.obj["brand"], s.obj["model"]) == ("Space heater", "Vornado", "VH200")
    assert s.obj["corrected"] and s.stage == "menu" and not s.steps and not s.task
    assert s.messages[-2]["kind"] == "correction"
    assert A.view(s)["corrected"] is True


def test_correct_never_lifts_safety_stop(fake):
    fake["identify"].update(hazard="stop")
    s = A.store.new("web"); A.identify(s, jpeg())
    with pytest.raises(ValueError):
        A.correct(s, "Toaster")
    assert s.stage == "stopped"


def test_correct_can_detect_new_hazard(fake):
    s = A.store.new("web"); A.identify(s, jpeg())
    fake["identify"].update(hazard="stop")
    A.correct(s, "Gas heater")
    assert s.stage == "stopped" and s.messages[-1]["kind"] == "safety"


def test_correct_needs_a_name_and_works_when_model_fails(fake, monkeypatch):
    s = A.store.new("web"); A.identify(s, jpeg())
    with pytest.raises(ValueError):
        A.correct(s, "   ")
    monkeypatch.setattr(A, "structured", lambda *a, **k: None)
    A.correct(s, "Kettle", "Smeg")
    assert s.obj["object_name"] == "Kettle" and s.stage == "menu" and "Kettle" in s.obj["spoken"]


def test_api_correct(web, fake):
    h = {"x-access-code": "demo-code-1"}
    files = {"photo": ("a.jpg", jpeg(), "image/jpeg")}
    v = web.post("/api/identify", files=files, headers=h).json()
    v = web.post("/api/correct", data={"session_id": v["id"], "name": "Air purifier", "brand": "Levoit"}, headers=h).json()
    assert v["object"]["object_name"] == "Air purifier" and v["corrected"] is True
    assert web.post("/api/correct", data={"session_id": v["id"], "name": ""}, headers=h).status_code == 400
