"""Fill the insights database with FAKE demo conversations so the dashboard looks alive in a pitch.

Every row is flagged demo=1. The dashboard labels them as demo data and can hide them.

Usage:
  python scripts/seed_insights.py            # add ~180 demo conversations over the last 30 days
  python scripts/seed_insights.py --rows 300
  python scripts/seed_insights.py --remove   # delete all demo rows (real rows are kept)
"""

import argparse
import datetime as dt
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.insights import DEFAULT_DB, InsightsLog, new_id  # noqa: E402

# (diagnosis, weight, chance it gets fixed when walked through)
PRINT_PROBLEMS = [
    ("Stringing", 18, 0.8), ("First layer not sticking", 16, 0.7), ("Warping", 12, 0.6),
    ("Spaghetti (failed print)", 10, 0.5), ("Filament runout", 9, 0.9), ("Clogged nozzle", 8, 0.15),
    ("Under-extrusion", 7, 0.35), ("Layer shifting", 4, 0.3), ("Loading filament", 6, 0.95),
]
PRINT_QUESTIONS = {
    "Stringing": ["What's wrong with my print?", "Why is my print all hairy?", "There are thin strings everywhere"],
    "First layer not sticking": ["The first layer won't stick", "My print keeps coming loose from the bed"],
    "Warping": ["Why are the corners lifting?", "The edges of my print curled up"],
    "Spaghetti (failed print)": ["My print turned into a mess", "Why is there spaghetti on the bed?"],
    "Filament runout": ["The screen says filament runout", "It stopped and says insert new filament"],
    "Clogged nozzle": ["Nothing comes out of the nozzle", "The extruder is clicking loudly"],
    "Under-extrusion": ["My print has gaps in the walls", "The layers look thin and weak"],
    "Layer shifting": ["The top half of my print moved sideways"],
    "Loading filament": ["How do I load new filament?", "How do I change the filament color?"],
}
UNCOVERED = [
    ("How do I print with TPU?", 9), ("Can I bring my own filament?", 7), ("How do I book the printer?", 7),
    ("How do I remove supports cleanly?", 6), ("What's the biggest thing I can print?", 5),
    ("How do I resize my model in the slicer?", 5), ("The bottom of my print bulges out (elephant's foot)", 4),
    ("Can I print two colors at once?", 3), ("How long will my print take?", 3),
]
ARCADE_QUESTIONS = ["What do I do here?", "How do I share my postcard?", "How do I make a song with Suno?",
                    "Give me a tip for a better prompt", "How do I make my video longer?"]
ARCADE_UNCOVERED = [("What time does the AI Arcade close?", 6), ("Can I take the iPad home?", 2),
                    ("How do I play Ribbit Rush?", 3)]
SAFETY = ["It smells like burning plastic", "There's smoke coming from the printer", "I burned my finger on the nozzle"]


def pick(rng, weighted):
    return rng.choices([w[0] for w in weighted], weights=[w[1] for w in weighted])[0]


def stamp(rng, days: int) -> str:
    # More visits on weekdays and in the afternoon, like a real space.
    while True:
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=rng.uniform(0, days))
        if when.weekday() < 5 or rng.random() < 0.3:
            break
    when = when.replace(hour=rng.choice([10, 11, 12, 13, 14, 14, 15, 15, 16, 16, 17, 18]), minute=rng.randrange(60),
                        second=0, microsecond=0)
    return min(when, dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)).isoformat(timespec="minutes")


def fake_conversation(rng, days: int) -> dict:
    started = stamp(rng, days)
    row = {"log_id": new_id(), "started": started, "updated": started, "demo": True}
    roll = rng.random()
    if roll < 0.18:  # AI Arcade
        if rng.random() < 0.2:
            q = pick(rng, ARCADE_UNCOVERED)
            return row | {"pack": "ai-arcade", "outcome": "not_covered", "question": q, "uncovered_question": q}
        return row | {"pack": "ai-arcade", "outcome": "answered", "question": rng.choice(ARCADE_QUESTIONS)}
    if roll < 0.21:
        return row | {"pack": "3d-printing", "outcome": "safety", "question": rng.choice(SAFETY)}
    if roll < 0.36:
        q = pick(rng, UNCOVERED)
        return row | {"pack": "3d-printing", "outcome": "not_covered", "question": q, "uncovered_question": q}
    chosen = pick(rng, PRINT_PROBLEMS)
    diagnosis, _, fix_chance = next(p for p in PRINT_PROBLEMS if p[0] == chosen)
    base = row | {"pack": "3d-printing", "question": rng.choice(PRINT_QUESTIONS[diagnosis]), "diagnosis": diagnosis,
                  "confidence": rng.choices(["high", "medium"], weights=[4, 1])[0]}
    if rng.random() < 0.15:
        return base | {"outcome": "open", "steps_tried": 0}
    if rng.random() < fix_chance:
        return base | {"outcome": "fixed", "steps_tried": rng.choice([1, 1, 2, 2, 3])}
    return base | {"outcome": "escalated", "steps_tried": rng.choice([1, 2, 3, 3])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=180)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--remove", action="store_true", help="delete all demo rows")
    parser.add_argument("--db", default=os.environ.get("INSIGHTS_DB") or str(DEFAULT_DB))
    args = parser.parse_args()

    db = InsightsLog(args.db)
    if args.remove:
        print(f"Removed {db.delete_demo()} demo rows from {args.db}. Real rows were kept.")
        return 0
    rng = random.Random(args.seed)
    for _ in range(args.rows):
        db.record(**fake_conversation(rng, args.days))
    print(f"Added {args.rows} DEMO conversations to {args.db} (flagged demo=1; the dashboard labels them).")
    print("Remove them any time with: python scripts/seed_insights.py --remove")
    return 0


if __name__ == "__main__":
    sys.exit(main())
