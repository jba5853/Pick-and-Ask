"""Live check of the point-at-anything flow against the real API (costs a few cents).

    python tests/run_anything.py path/to/photo.jpg [action] ["question"]

Runs identify -> start the task -> check a step with the same photo -> one question, and prints timings.
"""

import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from app import anything as A  # noqa: E402
from app.images import prepare_image  # noqa: E402


def timed(label, fn, *a):
    t = time.time()
    out = fn(*a)
    print(f"\n=== {label} ({time.time() - t:.1f}s)")
    return out


def main():
    photo = Path(sys.argv[1])
    action = sys.argv[2] if len(sys.argv) > 2 else ""
    question = sys.argv[3] if len(sys.argv) > 3 else "What should I be careful about?"
    jpeg = prepare_image(photo.read_bytes())
    s = A.store.new()
    timed("identify", A.identify, s, jpeg, "")
    o = s.obj
    print(f"{o.get('object_name')} | {o.get('brand')} {o.get('model')} | {o.get('confidence')} | hazard={o.get('hazard')}")
    print("seen:", o.get("what_i_see"))
    print("spoken:", o.get("spoken"))
    print("actions:", [a["label"] for a in s.actions], "| stage:", s.stage)
    if s.stage not in ("menu", "need_info") or not s.actions:
        return
    pick = action or s.actions[0]["id"]
    timed(f"plan ({pick})", A.start_task, s, pick)
    print("stage:", s.stage, "| title:", s.task.get("title"), "| grounded:", s.task.get("grounded"))
    for i, st in enumerate(s.steps, 1):
        print(f"  {i}. [{st.check}] {st.title} — {st.detail}{'  ⚠ ' + st.caution if st.caution else ''}")
    print("sources:", [x["url"] for x in s.sources])
    print("last said:", s.messages[-1]["text"])
    if s.stage == "coaching":
        _, v = timed("verify step 1 with the same photo", A.verify, s, jpeg)
        print(v)
    timed(f"chat: {question}", A.chat, s, question)
    print("reply:", s.messages[-1]["text"], "| spoken:", s.messages[-1].get("spoken"))


if __name__ == "__main__":
    main()
