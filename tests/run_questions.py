"""Run the cases in tests/questions.md against the live Claude API.

Usage:  python tests/run_questions.py [pack-name]
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
os.environ["INSIGHTS_DISABLED"] = "1"  # test runs don't belong in the staff insights

from app.assistant import ask, help_line  # noqa: E402
from app.conversation import BUTTON_TEXT, Session, converse  # noqa: E402
from app.images import prepare_image  # noqa: E402
from app.packs import load_pack  # noqa: E402

TESTS = Path(__file__).resolve().parent
EXPECTS = ("answer", "escalate", "needs_more_info", "ai_suggestion")


def load_cases(path: Path = TESTS / "questions.md") -> list[dict]:
    """Read every table row, tagged with the pack from the nearest '## Pack: <name>' heading above it."""
    cases, pack = [], "ai-arcade"
    for line in path.read_text().splitlines():
        heading = re.match(r"##\s*Pack:\s*(\S+)", line)
        if heading:
            pack = heading.group(1)
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not cells[0].isdigit():
            continue  # header, separator or prose
        num, question, photo, expect, target = cells
        cases.append({"num": num, "pack": pack, "question": question, "photo": None if photo == "-" else photo,
                      "expect": expect, "target": target})
    return cases


def load_conversations(path: Path = TESTS / "questions.md") -> list[dict]:
    """Rows of the '## Conversations: <pack>' tables. Turns are separated by ' → '."""
    convs, pack = [], None
    for line in path.read_text().splitlines():
        heading = re.match(r"##\s*(Pack|Conversations):\s*(\S+)", line)
        if heading:
            pack = heading.group(2) if heading.group(1) == "Conversations" else None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if pack is None or len(cells) != 5 or not re.fullmatch(r"C\d+", cells[0]):
            continue
        num, turns, photo, expect, target = cells
        convs.append({"num": num, "pack": pack, "turns": [t.strip() for t in turns.split("→")],
                      "photo": None if photo == "-" else photo, "expect": expect, "target": target})
    return convs


def turn_action(turn: str) -> tuple[str, str]:
    match = re.fullmatch(r"\[(\w+)\]", turn)
    if match and match.group(1) in BUTTON_TEXT:
        return match.group(1), ""
    return "message", turn


def run_conversation(conv: dict, pack) -> tuple[list[tuple[str, object]], Session]:
    session = Session(f"live-{conv['num']}", pack.id, 0.0)
    image = prepare_image((TESTS / "images" / conv["photo"]).read_bytes()) if conv["photo"] else None
    results, finished = [], False
    for i, turn in enumerate(conv["turns"]):
        action, text = turn_action(turn)
        if finished and action != "summary":
            continue  # the conversation already ended
        result = converse(session, pack, text, action, image if i == 0 else None)
        results.append((turn, result))
        if result.status in ("resolved", "escalate") and action != "summary":
            finished = True
    return results, session


def check_conversation(conv: dict, results: list, session: Session, pack) -> str | None:
    staff_texts = {s.text for s in pack.steps.values() if s.staff_only}
    for turn, r in results:
        shown = [r.answer, r.handoff, *(s["text"] for s in r.steps if not s["staff_only"])]
        if r.step:
            shown.append(r.step["text"])
        if any(t in x for t in staff_texts for x in shown):
            return f"turn '{turn}' showed a staff-only step"
    final = next((r for t, r in reversed(results) if t != "[summary]"), None)
    if conv["expect"] == "resolved":
        doc, _, n = conv["target"].partition(", fixed on step ")
        if final.status != "resolved":
            return f"expected resolved, got {final.status} ({final.reason})"
        if doc not in session.docs_used:
            return f"expected the fix to come from {doc}, used {session.docs_used}"
        if n and (len(session.tried) != int(n) or session.tried[-1][1] != "worked"):
            return f"expected fixed on step {n}, tried {session.tried}"
    else:
        reasons = [t.strip() for t in conv["target"].split(" or ")]
        if final.status != "escalate" or final.reason not in reasons:
            return f"expected escalate ({conv['target']}), got {final.status} ({final.reason})"
        if "Tried:" not in final.handoff or not session.tried:
            return "escalation should say what was already tried"
    if conv["turns"][-1] == "[summary]":
        summary = results[-1][1].summary
        if not summary or not (summary["problem"] and summary["outcome"] and summary["sources"]):
            return "summary should give the problem, outcome and sources"
        if len(summary["tried"]) != len(session.tried):
            return "summary should list every step tried"
    return None


def describe_turn(r) -> list[str]:
    if r.status == "summary":
        s = r.summary
        tried = "; ".join(f"{t['step']} ({t['outcome']})" for t in s["tried"]) or "nothing"
        return [f"-> summary | problem: {s['problem']} | tried: {tried} | outcome: {s['outcome']} | "
                f"sources: {', '.join(s['sources'])}"]
    lines = describe(r)
    if r.step:
        lines.append(f"   [step {r.step['number']} of {r.step['total']}] {r.step['text']}")
    if r.handoff:
        lines.append(f"   note for staff: {r.handoff}")
    return lines


def allowed_docs(target: str) -> list[str]:
    return [t.strip() for t in target.split(" or ")]


def check(case: dict, result, pack) -> str | None:
    """Return None on pass, or a short reason for failure."""
    expect, target = case["expect"], case["target"]
    diagnose = pack.mode == "diagnose"
    if expect == "answer":
        if result.status != "answered":
            return f"expected an answer, got {result.status} ({result.reason})"
        if result.source_doc not in allowed_docs(target):
            return f"expected source {target}, got {result.source_doc}"
        if diagnose and not result.steps:
            return "diagnosis has no steps"
        if diagnose and result.confidence not in ("high", "medium"):
            return f"diagnosis confidence should be high or medium, got {result.confidence!r}"
        if diagnose and case["photo"] and not result.observation:
            return "diagnosis with a photo should describe what it sees"
    elif expect == "needs_more_info":
        if result.status != "needs_more_info":
            return f"expected a follow-up question, got {result.status} ({result.source_doc or result.reason})"
        if not result.follow_up_question.endswith("?") or result.follow_up_question.count("?") != 1:
            return "follow-up should be exactly one question"
        if result.steps:
            return "follow-up should not include steps"
    elif expect == "ai_suggestion":
        if (result.status, result.reason) != ("escalate", "not_covered"):
            return f"expected escalate/not_covered, got {result.status}/{result.reason}"
        if not result.ai_suggestion:
            return "expected an AI suggestion"
        if result.source_doc or result.steps:
            return "AI suggestion must not cite a doc or give doc steps"
    else:
        if result.status != "escalate":
            return f"expected escalate, got {result.status} ({result.source_doc})"
        if result.reason != target:
            return f"expected reason {target}, got {result.reason}"
        if target == "safety":
            if result.answer != pack.safety_message:
                return "safety reply should be exactly the pack's safety message"
            if result.ai_suggestion or result.steps or result.observation or result.diagnosis:
                return "safety reply must carry nothing the model wrote"
        if target == "not_covered" and not result.answer.endswith(help_line(pack)):
            return "not_covered reply should end with what the pack can help with"
    return None


def describe(result) -> list[str]:
    lines = [f"-> {result.status} | {result.source_doc or result.reason} | {result.answer}"]
    if result.observation:
        lines.append(f"   saw: {result.observation}")
    if result.diagnosis:
        lines.append(f"   diagnosis: {result.diagnosis} ({result.confidence or '-'})")
    lines += [f"   {i}. {'[staff only] ' if s['staff_only'] else ''}{s['text']}" for i, s in enumerate(result.steps, 1)]
    if result.follow_up_question:
        lines.append(f"   follow-up: {result.follow_up_question}")
    if result.ai_suggestion:
        lines.append(f"   AI suggestion: {result.ai_suggestion}")
    return lines


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. See README.")
        return 2
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [c for c in load_cases() if only is None or c["pack"] == only]
    convs = [c for c in load_conversations() if only is None or c["pack"] == only]
    if not cases and not convs:
        print(f"No cases for pack {only!r}.")
        return 2
    packs, rows = {}, []
    for case in cases:
        pack = packs.setdefault(case["pack"], load_pack(case["pack"]))
        image = prepare_image((TESTS / "images" / case["photo"]).read_bytes()) if case["photo"] else None
        result = ask(pack, case["question"], image)
        problem = check(case, result, pack)
        got = result.source_doc or (result.reason if result.status == "escalate" else "-")
        if result.ai_suggestion:
            got += " + AI suggestion"
        rows.append((case, "PASS" if not problem else "FAIL", f"{result.status} / {got}"))
        print(f"[{rows[-1][1]}] #{case['num']} ({case['pack']}) {case['question']}")
        for line in describe(result):
            print(f"       {line}")
        if problem:
            print(f"       !! {problem}")

    for conv in convs:
        pack = packs.setdefault(conv["pack"], load_pack(conv["pack"]))
        results, session = run_conversation(conv, pack)
        problem = check_conversation(conv, results, session, pack)
        final = next((r for t, r in reversed(results) if t != "[summary]"), results[-1])
        got = f"{final.status} / {final.reason}" + (f" after {len(session.tried)} step(s)" if session.tried else "")
        question = " → ".join(t for t, _ in results)
        target = conv["target"]
        rows.append(({"num": conv["num"], "pack": conv["pack"], "question": question, "expect": conv["expect"],
                      "target": target}, "PASS" if not problem else "FAIL", got))
        print(f"[{rows[-1][1]}] #{conv['num']} ({conv['pack']}) conversation")
        for turn, r in results:
            print(f"     > {turn}")
            for line in describe_turn(r):
                print(f"       {line}")
        reads = [u.get("cache_read", 0) for u in session.usage]
        print(f"       cache reads per model call: {reads}")
        if problem:
            print(f"       !! {problem}")

    print("\n| # | Pack | Question | Expected | Got | Result |")
    print("|---|------|----------|----------|-----|--------|")
    for case, verdict, got in rows:
        print(f"| {case['num']} | {case['pack']} | {case['question']} | {case['expect']} / {case['target']} | {got} | {verdict} |")
    failures = sum(v == "FAIL" for _, v, _ in rows)
    print(f"\n{len(rows) - failures}/{len(rows)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
