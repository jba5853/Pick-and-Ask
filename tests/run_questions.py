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

from app.assistant import ask, help_line  # noqa: E402
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
    lines += [f"   {i}. {s}" for i, s in enumerate(result.steps, 1)]
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
    if not cases:
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

    print("\n| # | Pack | Question | Expected | Got | Result |")
    print("|---|------|----------|----------|-----|--------|")
    for case, verdict, got in rows:
        print(f"| {case['num']} | {case['pack']} | {case['question']} | {case['expect']} / {case['target']} | {got} | {verdict} |")
    failures = sum(v == "FAIL" for _, v, _ in rows)
    print(f"\n{len(rows) - failures}/{len(rows)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
