"""Spoken lines for Coach mode (hands-free, through the glasses speakers).

Every reply gets `voice`: a list of short utterances, at most MAX_WORDS each, in natural phrasing. The full
text stays on screen. Rules:
- No "step 3 of 7" unless the visitor asks (the page answers that itself).
- Doc steps are spoken in full, split into short utterances at sentence and clause boundaries.
- Safety and staff hand-off messages are always spoken in full, and `voice_pause` tells the page to stop
  the hands-free loop after them.
"""

import re

from app.assistant import Result
from app.packs import Pack

MAX_WORDS = 20
OFFER = "Want me to walk you through it?"
FIRST_HINT = "After each step, say done, or didn't work."


def for_speech(text: str) -> str:
    """Small fixes so text-to-speech reads doc text naturally."""
    text = " ".join(text.split())
    text = re.sub(r"(\d)\s*[–-]\s*(\d)", r"\1 to \2", text)            # 5–10 -> 5 to 10
    text = re.sub(r"\s*°\s*C\b", " degrees C", text)
    text = re.sub(r"\s*°\s*F\b", " degrees F", text)
    text = re.sub(r"(\d)\s*%", r"\1 percent", text)
    text = re.sub(r"\s*\([^)]*\)", "", text)                         # menu paths etc. stay on screen only
    text = re.sub(r"\s+([,;:.!?])", r"\1", text)
    return text.replace('"', "").strip()


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text) if s.strip()]


def _clauses(sentence: str) -> list[str]:
    """Split a long sentence at commas, semicolons, colons and dashes, then hard-wrap whatever is left."""
    parts = [p.strip() for p in re.split(r"(?<=[,;:])\s+|\s+[–—-]\s+", sentence) if p.strip()]
    out, current = [], []
    for part in parts:
        words = part.split()
        if current and len(current) + len(words) > MAX_WORDS:
            out.append(" ".join(current))
            current = []
        current += words
        while len(current) > MAX_WORDS:
            out.append(" ".join(current[:MAX_WORDS]))
            current = current[MAX_WORDS:]
    if current:
        out.append(" ".join(current))
    return out


def utterances(text: str) -> list[str]:
    """Split text into utterances of at most MAX_WORDS words, keeping every word (asides in parentheses
    are left to the screen). Only the last piece of a sentence ends with a full stop."""
    out = []
    for sentence in _sentences(for_speech(text)):
        pieces = [sentence] if len(sentence.split()) <= MAX_WORDS else _clauses(sentence)
        pieces = [x for x in pieces if x.strip(" ,;:")]
        for i, piece in enumerate(pieces):
            if i == len(pieces) - 1:
                piece = piece.rstrip(",;:") + ("" if piece.rstrip(",;:")[-1:] in ".!?" else ".")
            out.append(piece)
    return out


def lower_first(text: str) -> str:
    return text[0].lower() + text[1:] if text[:2].istitle() else text


def step_lines(r: Result) -> list[str]:
    number, total = r.step["number"], r.step["total"]
    lead = "First," if number == 1 else "Last one," if number == total else "Next,"
    lines = utterances(r.step["text"])
    if lines:
        candidate = f"{lead} {lower_first(lines[0])}"
        lines[0:1] = utterances(candidate) if len(candidate.split()) > MAX_WORDS else [candidate]
    return lines


def voice_lines(r: Result, pack: Pack) -> tuple[list[str], bool]:
    """(utterances, pause_after) for a reply."""
    if r.status == "escalate":
        if r.reason == "safety":
            return utterances(pack.safety_message), True
        if r.reason in ("staff_only", "not_fixed", "requested"):
            spoken = r.answer.replace("show them the note below", "show them the note on your screen")
            return utterances(spoken), True
        lines = utterances(r.answer)
        if r.ai_suggestion:
            lines += ["Here's an AI suggestion, not from the official guides."] + utterances(r.ai_suggestion)
        return lines, False

    if r.status == "answered" and r.steps:  # a new diagnosis
        diagnosis = r.diagnosis.rstrip(".")
        lines = utterances(f"Looks like {lower_first(diagnosis)}.")
        if "walkthrough" in r.actions:
            return lines + [OFFER], False
        return lines + utterances(f"The fix needs {pack.escalation}. Please ask them for help."), True

    if r.status == "guided" and r.step:
        intro = r.answer.split(f" Step {r.step['number']} of {r.step['total']}:")[0].strip()
        lines = []
        if intro and not intro.startswith("Here it is again"):
            lines += utterances(intro)
        lines += step_lines(r)
        if r.step["number"] == 1 and intro.startswith("Okay, let's go one step at a time"):
            lines.append(FIRST_HINT)
        return lines, False

    if r.status == "needs_more_info":
        return utterances(r.follow_up_question or r.answer), False

    if r.status == "resolved":
        return ["Great, glad that's fixed.", "Ask me anything else, or say stop."], False

    lines = utterances(r.answer)
    if r.step and r.status in ("answered", "summary"):
        lines.append("Say done when you're ready, or didn't work.")
    return lines, False
