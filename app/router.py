"""Turn free text ("2", "done", "that didn't work", "how hot does it get?") into the right action.

Used by the WhatsApp bridge, where the user talks through the glasses ("Hey Meta, reply done").
"""

from __future__ import annotations

import re

from app import anything as A

NUMBER_WORDS = {"1": 0, "one": 0, "first": 0, "2": 1, "two": 1, "second": 1, "3": 2, "three": 2, "third": 2,
                "4": 3, "four": 3, "fourth": 3}
FILLER = re.compile(r"\b(uh+|um+|hey|ok(ay)?|so|please|yeah|yes|just|i think|alright|cool)\b")

NEXT = re.compile(r"^(done|did it|finished|next|got it|that worked|it worked|ready|complete|yep|yup|go on|continue)\b")
FAILED = re.compile(r"(didn'?t work|did not work|not working|doesn'?t work|still (broken|not|the same)|nope|no luck|won'?t)")
REPEAT = re.compile(r"^(repeat|again|say (that|it) again|what was (that|the step)|come again|pardon)\b")
BACK = re.compile(r"^(back|go back|previous|last step)\b")
SKIP = re.compile(r"^skip\b")
RESET = re.compile(r"^(new|start over|reset|cancel|stop|something else)\b")
STEPS = re.compile(r"^(steps|list|all steps|show steps)\b")

ACTION_WORDS = [
    ("setup", re.compile(r"\b(set (it |this |that )?up|setup|install|assemble|get (it )?started|start it)\b")),
    ("troubleshoot", re.compile(r"\b(fix|troubleshoot|problem|broken|issue|error|wrong)\b")),
    ("learn", re.compile(r"\b(learn|how (do i|to) use|teach|controls|features|explain)\b")),
    ("maintain", re.compile(r"\b(clean|maintain|maintenance|filter|descale)\b")),
    ("safety", re.compile(r"\b(safe|safety)\b")),
]


def normalize(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"[^\w\s']", " ", t)
    t = FILLER.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def menu_text(session: A.Session) -> str:
    opts = " ".join(f"{i + 1}) {a['label']}." for i, a in enumerate(session.actions))
    return f"Reply with a number or just say it: {opts}"


def pick_action(session: A.Session, t: str) -> str | None:
    first = t.split(" ")[0] if t else ""
    if first in NUMBER_WORDS and NUMBER_WORDS[first] < len(session.actions):
        return session.actions[NUMBER_WORDS[first]]["id"]
    for aid, rx in ACTION_WORDS:
        if rx.search(t) and any(a["id"] == aid for a in session.actions):
            return aid
    for a in session.actions:  # the label itself, e.g. "clean the filter"
        if normalize(a["label"]) and normalize(a["label"]) in t:
            return a["id"]
    return None


def last_assistant(session: A.Session, n: int = 1) -> list[str]:
    out = [m.get("spoken") or m["text"] for m in session.messages if m["role"] == "assistant"]
    return out[-n:]


def handle_text(session: A.Session, text: str) -> list[str]:
    """Apply a text reply and return the message(s) to send back."""
    t = normalize(text)
    before = session.msg_seq

    if session.stage == "stopped":
        if RESET.match(t):
            return ["Okay. Send me a new photo whenever you're ready."]
        return [A.STOP_MESSAGE]

    if RESET.match(t):
        session.stage = "menu" if session.actions else "new"
        session.steps, session.current = [], 0
        session.bump()
        return ["Okay, starting fresh. " + (menu_text(session) if session.actions else "Send me a photo.")]

    if session.stage in ("menu", "finished") or (session.stage == "need_info" and not session.task):
        aid = pick_action(session, t)
        if aid:
            A.start_task(session, aid)
            return new_messages(session, before)
        if session.stage == "need_info" and not session.obj.get("object_name"):
            # They're telling us what the thing is.
            session.obj["object_name"] = text.strip()
            session.stage, session.actions = "menu", A.DEFAULT_ACTIONS
            session.bump()
            return [f"Got it, {text.strip()}. " + menu_text(session)]

    if session.stage == "need_info" and session.task:
        # Answer to the plan's clarifying question: replan with the detail.
        A.start_task(session, session.task.get("action", "troubleshoot"), detail=text.strip())
        return new_messages(session, before)

    if session.stage == "coaching":
        if STEPS.match(t):
            return ["Steps: " + " ".join(f"{i + 1}) {s.title}." for i, s in enumerate(session.steps))]
        if REPEAT.match(t):
            A.advance(session, "repeat")
            return new_messages(session, before)
        if BACK.match(t):
            A.advance(session, "back")
            return new_messages(session, before)
        if SKIP.match(t):
            A.advance(session, "skip")
            return new_messages(session, before)
        if FAILED.search(t):
            st = session.step
            A.chat(session, f"Step {session.current + 1} ({st.title if st else ''}) didn't work: {text}")
            return new_messages(session, before)
        if NEXT.match(t) or t == "":
            A.advance(session, "next")
            return new_messages(session, before)

    A.chat(session, text)
    return new_messages(session, before)


def new_messages(session: A.Session, before: int) -> list[str]:
    msgs = [m for m in session.messages if m["n"] > before and m["role"] == "assistant"]
    return [m["text"] for m in msgs] or last_assistant(session)
