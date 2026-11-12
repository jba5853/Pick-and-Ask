"""Multi-turn conversations: in-memory sessions, guided fixes, staff-only enforcement and summaries.

Who decides what:
- The model reads the visitor's message, diagnoses, re-diagnoses after a failed step, and names causes.
- This module owns the guided-fix state. It picks which step is shown, counts failures, escalates,
  and never shows a staff-only step, whatever the model says.
"""

import copy
import logging
import threading
import time
from dataclasses import dataclass, field

from app.assistant import (
    Parsed, Result, call_model, diagnosis_result, escalation, first_sentence, interpret, parse_diagnose,
    plan_ids, trim_sentences, user_content,
)
from app.packs import Pack

log = logging.getLogger("point_and_ask")

SESSION_TTL = 30 * 60      # seconds idle before a session is forgotten
MAX_SESSIONS = 500         # oldest sessions are dropped beyond this
MAX_HISTORY = 20           # messages kept (10 exchanges); the first exchange and first photo always stay
MAX_PHOTOS = 4             # photos kept in history; the first photo always stays
MAX_FAILURES = 3           # failed steps before handing off to staff
GUIDED_ACTIONS = ["done", "failed", "repeat"]
BUTTON_TEXT = {            # what a quick-reply button "says", for the history
    "walkthrough": "Walk me through it.",
    "done": "Done.",
    "failed": "That didn't work.",
    "repeat": "Show me that step again.",
    "summary": "Can I get a summary?",
}
PHOTO_REMOVED = "[An earlier photo was here; it was removed to save space.]"


@dataclass
class Session:
    id: str
    pack_id: str
    last_seen: float
    messages: list[dict] = field(default_factory=list)   # API-format history, user/assistant alternating
    diagnosis: str = ""
    confidence: str = ""
    source_doc: str = ""
    plan_type: str = "fix"
    plan: list[str] = field(default_factory=list)        # step ids still to try, in order
    current: str | None = None                           # step id the visitor is on
    tried: list[tuple[str, str]] = field(default_factory=list)  # (step id, "failed" | "worked" | "done")
    staff_needed: list[str] = field(default_factory=list)       # staff-only step ids held back
    failures: int = 0
    phase: str = "chat"        # chat | diagnosed | guided | resolved | escalated
    end_reason: str = ""       # why the session escalated
    docs_used: list[str] = field(default_factory=list)
    usage: list[dict] = field(default_factory=list)      # token usage per model call (cache hits etc.)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def tried_ids(self) -> set[str]:
        return {sid for sid, _ in self.tried}

    def use_doc(self, doc: str) -> None:
        if doc and doc not in self.docs_used:
            self.docs_used.append(doc)


class SessionStore:
    """Sessions live only in memory. Nothing is written to disk."""

    def __init__(self, ttl: float = SESSION_TTL, max_sessions: int = MAX_SESSIONS, clock=time.monotonic):
        self.ttl, self.max_sessions, self.clock = ttl, max_sessions, clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str, pack_id: str) -> Session:
        """The live session for this id, or a fresh one (also when the pack changed)."""
        now = self.clock()
        with self._lock:
            self._purge(now)
            session = self._sessions.get(session_id)
            if session is None or session.pack_id != pack_id:
                session = Session(session_id, pack_id, now)
                self._sessions[session_id] = session
                while len(self._sessions) > self.max_sessions:
                    oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                    del self._sessions[oldest.id]
            session.last_seen = now
            return session

    def peek(self, session_id: str) -> Session | None:
        with self._lock:
            self._purge(self.clock())
            return self._sessions.get(session_id)

    def _purge(self, now: float) -> None:
        for sid in [sid for sid, s in self._sessions.items() if now - s.last_seen > self.ttl]:
            del self._sessions[sid]

    def __len__(self) -> int:
        return len(self._sessions)


# ---------- history ----------

def has_image(message: dict) -> bool:
    return isinstance(message["content"], list) and any(b.get("type") == "image" for b in message["content"])


def trim_history(messages: list[dict]) -> list[dict]:
    """Keep the first exchange, the exchange with the first photo, and the most recent exchanges."""
    if len(messages) <= MAX_HISTORY:
        return messages
    pairs = [messages[i:i + 2] for i in range(0, len(messages), 2)]
    keep = {0}
    first_photo = next((i for i, p in enumerate(pairs) if has_image(p[0])), None)
    if first_photo is not None:
        keep.add(first_photo)
    room = MAX_HISTORY // 2 - len(keep)
    keep.update(range(len(pairs) - room, len(pairs)))
    return [m for i in sorted(keep) for m in pairs[i]]


def cap_photos(messages: list[dict]) -> None:
    """Drop the oldest photos beyond MAX_PHOTOS, never the first one."""
    with_photos = [m for m in messages if m["role"] == "user" and has_image(m)]
    for message in with_photos[1:len(with_photos) - MAX_PHOTOS + 1]:
        message["content"] = [b if b.get("type") != "image" else {"type": "text", "text": PHOTO_REMOVED}
                              for b in message["content"]]


def record(session: Session, user_blocks: list[dict], assistant_text: str) -> None:
    session.messages.append({"role": "user", "content": user_blocks})
    session.messages.append({"role": "assistant", "content": [{"type": "text", "text": assistant_text}]})
    cap_photos(session.messages)
    session.messages = trim_history(session.messages)


def build_messages(session: Session, user_blocks: list[dict], state: str = "") -> list[dict]:
    """History + the new message. The session state is added to the new message only, so earlier turns
    stay byte-identical and the cached prefix (system prompt, then history) keeps matching."""
    messages = copy.deepcopy(session.messages)
    if messages:
        # Second cache breakpoint at the end of the history, after the cached system prompt.
        messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    blocks = list(user_blocks)
    if state:
        blocks.append({"type": "text", "text": state})
    messages.append({"role": "user", "content": blocks})
    return messages


def state_block(session: Session, pack: Pack) -> str:
    if not session.diagnosis:
        return ""
    lines = [f"Diagnosis: {session.diagnosis} ({session.confidence}), from {session.source_doc}, "
             f"plan type {session.plan_type}", f"Phase: {session.phase}"]
    if session.current:
        lines.append(f"Current step: [{session.current}] {pack.steps[session.current].text}")
    if session.tried:
        lines.append("Already tried: " + ", ".join(f"[{sid}] {outcome}" for sid, outcome in session.tried))
    if session.plan:
        lines.append("Remaining plan: " + ", ".join(f"[{sid}]" for sid in session.plan))
    if session.staff_needed:
        lines.append("Held for staff: " + ", ".join(f"[{sid}]" for sid in session.staff_needed))
    lines.append(f"Failed attempts: {session.failures} of {MAX_FAILURES}")
    return "<session_state>\n" + "\n".join(lines) + "\n</session_state>"


def history_text(r: Result) -> str:
    """What the visitor saw, as plain text, for the conversation history."""
    parts = []
    if r.observation:
        parts.append(f"I see: {r.observation}")
    if r.diagnosis and r.status == "answered" and r.steps:
        parts.append(f"Likely problem: {r.diagnosis} ({r.confidence} confidence), from {r.source_doc}.")
        parts += [f"{i}. {s['text']}" for i, s in enumerate(r.steps, 1)]
    elif r.step:
        parts.append(r.answer)
    else:
        parts.append(r.answer)
    if r.ai_suggestion:
        parts.append(f"AI suggestion (not from official guides): {r.ai_suggestion}")
    if r.handoff:
        parts.append(f"Note for staff: {r.handoff}")
    return "\n".join(parts)


# ---------- guided fix ----------

def short(text: str, words: int = 10) -> str:
    sentence = first_sentence(text).rstrip(".")
    parts = sentence.split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def adopt(session: Session, pack: Pack, p: Parsed) -> None:
    """Start fresh with a new diagnosis."""
    session.diagnosis, session.confidence, session.source_doc = p.diagnosis, p.confidence, p.source_doc
    session.plan_type = p.plan_type if p.plan_type in ("fix", "procedure") else "fix"
    session.plan = plan_ids(pack, p)
    session.current, session.tried, session.staff_needed = None, [], []
    session.failures, session.phase, session.end_reason = 0, "diagnosed", ""
    session.use_doc(p.source_doc)


def advance(session: Session, pack: Pack) -> tuple[str | None, list[str], bool]:
    """Move to the next step a visitor may do. Staff-only steps are held for staff, never shown.

    Returns (next step id or None, staff-only ids skipped, blocked). blocked is True when a procedure hits
    a staff-only step: its steps must be done in order, so it can't skip ahead.
    """
    skipped, tried = [], session.tried_ids()
    session.current = None
    while session.plan:
        sid = session.plan.pop(0)
        step = pack.steps.get(sid)
        if step is None or sid in tried:
            continue
        if step.staff_only:
            if sid not in session.staff_needed:
                session.staff_needed.append(sid)
            if session.plan_type == "procedure":
                return None, skipped + [sid], True
            skipped.append(sid)
            continue
        session.current = sid
        return sid, skipped, False
    return None, skipped, False


def progress(session: Session, pack: Pack) -> tuple[int, int]:
    tried = session.tried_ids()
    remaining = [sid for sid in session.plan
                 if sid in pack.steps and not pack.steps[sid].staff_only and sid not in tried]
    number = len(session.tried) + 1
    return number, number + len(remaining)


def step_info(session: Session, pack: Pack) -> dict | None:
    if not session.current:
        return None
    number, total = progress(session, pack)
    step = pack.steps[session.current]
    return {"number": number, "total": total, "text": step.text, "doc": step.doc}


def speakable(text: str) -> str:
    """Short read-aloud version of a doc step: its first sentence. The full step stays on screen."""
    first = first_sentence(text)
    return first if first == " ".join(text.split()) else f"{first} The rest is on screen."


def present(session: Session, pack: Pack, intro: str, skipped: list[str] = ()) -> Result:
    session.phase = "guided"
    info = step_info(session, pack)
    if skipped:
        intro += f" One check needs {pack.escalation}, so I've noted it for them."
    spoken = f"{intro} Step {info['number']} of {info['total']}: {speakable(info['text'])}".strip()
    return Result("guided", spoken, info["doc"], "none", diagnosis=session.diagnosis,
                  confidence=session.confidence, step=info, actions=GUIDED_ACTIONS)


def handoff_line(session: Session, pack: Pack) -> str:
    parts = [f"Problem: {session.diagnosis or 'not diagnosed'}"
             + (f" ({', '.join(session.docs_used)})" if session.docs_used else "") + "."]
    outcome = {"failed": "didn't work", "worked": "fixed it", "done": "done"}
    if session.tried:
        parts.append("Tried: " + "; ".join(f"{short(pack.steps[sid].text)} ({outcome[o]})"
                                           for sid, o in session.tried) + ".")
    else:
        parts.append("Nothing tried yet.")
    if session.staff_needed:
        order = list(pack.steps)
        staff = sorted(session.staff_needed, key=order.index)
        parts.append("Needs staff: " + "; ".join(pack.steps[sid].label for sid in staff) + ".")
    return " ".join(parts)


def escalate(session: Session, pack: Pack, reason: str) -> Result:
    """Hand off to staff with a one-line note of what was already tried."""
    session.phase, session.end_reason, session.current = "escalated", reason, None
    if reason == "staff_only":
        lead = f"The next step needs {pack.escalation}, so I won't walk you through it."
    else:
        n = len(session.tried)
        lead = f"We've tried {n} step{'s' if n != 1 else ''} and the problem is still there, so it's time to get help."
    return Result("escalate", f"{lead} Please check with {pack.escalation} and show them the note below.",
                  None, reason, diagnosis=session.diagnosis, confidence=session.confidence,
                  handoff=handoff_line(session, pack), actions=["summary"])


def next_or_escalate(session: Session, pack: Pack, intro: str) -> Result:
    nxt, skipped, blocked = advance(session, pack)
    if nxt is None:
        return escalate(session, pack, "staff_only" if (blocked or session.staff_needed) else "not_fixed")
    return present(session, pack, intro, skipped)


def resolved(session: Session) -> Result:
    session.phase, session.current = "resolved", None
    return Result("resolved", "Great, glad that's sorted. If it comes back, just ask me again.", None, "none",
                  diagnosis=session.diagnosis, confidence=session.confidence, actions=["summary"])


def step_done(session: Session, pack: Pack) -> Result:
    if session.plan_type == "procedure":
        session.tried.append((session.current, "done"))
        nxt, skipped, blocked = advance(session, pack)
        if nxt:
            return present(session, pack, "Nice.", skipped)
        if blocked:
            return escalate(session, pack, "staff_only")
        return resolved(session)
    session.tried.append((session.current, "worked"))
    return resolved(session)


def clean_phrase(pack: Pack, text: str) -> str:
    """A short cause name from the model, or "" if it's long or touches staff-only territory."""
    text = " ".join(text.split()).rstrip(".")
    if not text or len(text.split()) > 8 or pack.mentions_staff_only(text):
        return ""
    return text[0].lower() + text[1:] if text[:2].istitle() else text


def step_failed(session: Session, pack: Pack, p: Parsed | None) -> Result:
    session.tried.append((session.current, "failed"))
    session.failures += 1
    session.current = None
    if session.failures >= MAX_FAILURES:
        return escalate(session, pack, "not_fixed")

    # Re-diagnose: take the model's new order of remaining steps (possibly from another doc).
    model_next = None
    if p is not None and p.step_ids:
        tried = session.tried_ids()
        new_plan = [sid for sid in p.step_ids if sid not in tried]
        if new_plan:
            session.plan, model_next = new_plan, new_plan[0]
            if p.source_doc in pack.docs and p.diagnosis and p.source_doc != session.source_doc:
                session.diagnosis, session.source_doc = p.diagnosis, p.source_doc
                session.use_doc(p.source_doc)

    nxt, skipped, blocked = advance(session, pack)
    if nxt is None:
        return escalate(session, pack, "staff_only" if (blocked or session.staff_needed) else "not_fixed")

    ruled_out = clean_phrase(pack, p.ruled_out) if p else ""
    next_check = clean_phrase(pack, p.next_check) if p and model_next == nxt else ""
    if ruled_out and next_check:
        intro = f"Okay, that rules out {ruled_out}. Let's check {next_check}."
    elif ruled_out:
        intro = f"Okay, that rules out {ruled_out}."
    else:
        intro = "Okay, that didn't fix it. Let's try the next thing."
    return present(session, pack, intro, skipped)


def start_guided(session: Session, pack: Pack) -> Result:
    return next_or_escalate(session, pack, "Okay, let's go one step at a time.")


def keep_step(session: Session, pack: Pack, r: Result) -> Result:
    """While a guided fix is running, keep the current step and its buttons on screen."""
    if session.current:
        r.step, r.actions = step_info(session, pack), GUIDED_ACTIONS
    return r


def answer_question(session: Session, pack: Pack, p: Parsed) -> Result:
    cited = [pack.steps[sid] for sid in p.step_ids]
    if any(s.staff_only for s in cited) or pack.mentions_staff_only(p.reply):
        r = Result("escalate", f"That part needs {pack.escalation}, so I won't walk you through it. "
                               f"Please check with {pack.escalation}.", None, "staff_only")
    elif p.status == "answered" and p.reply and p.source_doc in pack.docs:
        session.use_doc(p.source_doc)
        r = Result("answered", trim_sentences(p.reply), p.source_doc, "none")
    else:
        r = escalation(pack, "not_covered")
    r.diagnosis, r.confidence = session.diagnosis, session.confidence
    return keep_step(session, pack, r)


def summary(session: Session, pack: Pack) -> Result:
    outcome_text = {"failed": "didn't work", "worked": "fixed it", "done": "done"}
    tried = [{"step": short(pack.steps[sid].text, 14), "doc": pack.steps[sid].doc, "outcome": outcome_text[o]}
             for sid, o in session.tried]
    if session.phase == "resolved":
        fixed = next((t["step"] for t in reversed(tried) if t["outcome"] == "fixed it"), "")
        outcome = f"Fixed{': ' + fixed if fixed else ''}"
    elif session.phase == "escalated":
        outcome = {"staff_only": "Handed to staff: the next step needs staff",
                   "not_fixed": f"Not fixed after {len(session.tried)} step(s); handed to staff",
                   "safety": "Stopped for a safety concern; staff told"}.get(session.end_reason, "Handed to staff")
    elif session.phase == "guided":
        number, total = progress(session, pack)
        outcome = f"In progress (step {number} of {total})"
    elif session.phase == "diagnosed":
        outcome = "Diagnosed; no steps tried yet"
    else:
        outcome = "Not diagnosed yet"
    data = {
        "problem": session.diagnosis or "Not diagnosed yet",
        "confidence": session.confidence,
        "tried": tried,
        "needs_staff": [pack.steps[sid].label for sid in session.staff_needed],
        "outcome": outcome,
        "sources": list(session.docs_used),
    }
    n = len(tried)
    spoken = f"Summary. Problem: {data['problem']}. Tried {n} step{'s' if n != 1 else ''}. Outcome: {outcome}."
    r = Result("summary", spoken, None, "none", diagnosis=session.diagnosis, confidence=session.confidence,
               summary=data, handoff=handoff_line(session, pack))
    return keep_step(session, pack, r)


# ---------- one turn ----------

def safety_stop(session: Session, pack: Pack) -> Result:
    """Only the pack's fixed safety message; the guided fix stops."""
    session.phase, session.end_reason, session.current = "escalated", "safety", None
    return escalation(pack, "safety")


def dispatch(session: Session, pack: Pack, p: Parsed | None, forced: str | None) -> Result:
    if p is None:
        return keep_step(session, pack, escalation(pack, "error"))
    if p.reason == "safety":
        return safety_stop(session, pack)
    if p.reason == "personal_data":
        return keep_step(session, pack, escalation(pack, "personal_data"))

    intent = forced or p.intent
    if intent == "summary":
        return summary(session, pack)
    if intent == "repeat" and session.current:
        return present(session, pack, "Here it is again.")
    if intent == "done" and session.current:
        return step_done(session, pack)
    if intent == "failed":
        if session.current:
            return step_failed(session, pack, p)
        if session.phase == "resolved" and session.tried and session.tried[-1][1] == "worked":
            session.current = session.tried.pop()[0]  # "it came back": reopen the step that seemed to work
            return step_failed(session, pack, p)
    if intent == "walkthrough":
        fresh = diagnosis_result(pack, p)
        if fresh.status == "answered" and p.source_doc != session.source_doc:
            adopt(session, pack, p)
        if session.current:
            return present(session, pack, "Here's where we are.")
        if session.plan:
            return start_guided(session, pack)
    if intent == "question" and p.reply:
        return answer_question(session, pack, p)

    r = diagnosis_result(pack, p)
    if r.status == "answered":
        adopt(session, pack, p)
    return keep_step(session, pack, r)


def converse(session: Session, pack: Pack, text: str, action: str = "message",
             image_jpeg: bytes | None = None) -> Result:
    """Handle one visitor turn: a typed message or a quick-reply button."""
    with session.lock:
        result = _turn(session, pack, text.strip(), action, image_jpeg)
        result.session_id = session.id
        log.info("session=%s pack=%s action=%s status=%s reason=%s phase=%s failures=%d", session.id[:8],
                 pack.id, action, result.status, result.reason, session.phase, session.failures)
        return result


def _turn(session: Session, pack: Pack, text: str, action: str, image_jpeg: bytes | None) -> Result:
    text = text or BUTTON_TEXT.get(action, "")
    user_blocks = user_content(text, image_jpeg)

    if pack.mode != "diagnose":
        raw, error, usage = call_model(pack, build_messages(session, user_blocks))
        session.usage.append(usage)
        r = escalation(pack, error) if error else interpret(pack, raw)
        record(session, user_blocks, history_text(r))
        return r

    # Buttons the app can answer without the model. Summary isn't added to the history.
    # A new photo always goes to the model, since it may change the picture.
    if action == "summary":
        return summary(session, pack)
    handled = None
    if not image_jpeg:
        if action == "repeat" and session.current:
            handled = present(session, pack, "Here it is again.")
        elif action == "done" and session.current:
            handled = step_done(session, pack)
        elif action == "walkthrough" and session.phase in ("diagnosed", "guided") and (session.plan or session.current):
            handled = present(session, pack, "Here's where we are.") if session.current else start_guided(session, pack)
    if handled:
        record(session, user_blocks, history_text(handled))
        return handled

    forced = "failed" if action == "failed" and session.current else None
    raw, error, usage = call_model(pack, build_messages(session, user_blocks, state_block(session, pack)))
    session.usage.append(usage)
    if error == "safety":
        r = safety_stop(session, pack)
    elif error:
        r = keep_step(session, pack, escalation(pack, error))
    else:
        r = dispatch(session, pack, parse_diagnose(pack, raw), forced)
    record(session, user_blocks, history_text(r))
    return r
