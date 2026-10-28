"""Run the cases in tests/questions.md against the live Claude API.

Usage:  python tests/run_questions.py [pack-name]
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app.assistant import ask  # noqa: E402
from app.images import prepare_image  # noqa: E402
from app.packs import load_pack  # noqa: E402

TESTS = Path(__file__).resolve().parent


def load_cases(path: Path = TESTS / "questions.md") -> list[dict]:
    cases = []
    for line in path.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not cells[0].isdigit():
            continue  # header, separator or prose
        num, question, photo, expect, target = cells
        cases.append({"num": num, "question": question, "photo": None if photo == "-" else photo,
                      "expect": expect, "target": target})
    return cases


def check(case: dict, result) -> str | None:
    """Return None on pass, or a short reason for failure."""
    if case["expect"] == "answer":
        if result.status != "answered":
            return f"expected an answer, got escalate ({result.reason})"
        if result.source_doc != case["target"]:
            return f"expected source {case['target']}, got {result.source_doc}"
    else:
        if result.status != "escalate":
            return f"expected escalate, got an answer from {result.source_doc}"
        if result.reason != case["target"]:
            return f"expected reason {case['target']}, got {result.reason}"
    return None


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. See README.")
        return 2
    pack = load_pack(sys.argv[1] if len(sys.argv) > 1 else "ai-arcade")
    cases = load_cases()
    failures = 0
    for case in cases:
        image = prepare_image((TESTS / "images" / case["photo"]).read_bytes()) if case["photo"] else None
        result = ask(pack, case["question"], image)
        problem = check(case, result)
        failures += bool(problem)
        print(f"[{'PASS' if not problem else 'FAIL'}] #{case['num']} {case['question']}")
        print(f"       -> {result.status} | {result.source_doc or result.reason} | {result.answer}")
        if problem:
            print(f"       !! {problem}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
