"""Coach mode voice commands (app/static/coach-commands.js), run under Node. Skipped if Node isn't installed."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

RUNNER = """
const { match } = require(process.argv[1]);
const cases = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(([t, ctx]) => match(t, ctx).type)));
"""

CASES = [
    # (transcript, context, expected)
    ("done", "step", "done"),
    ("Okay.", "step", "done"),
    ("um, next", "step", "done"),
    ("Yeah I'm done", "step", "done"),
    ("okay I did it", "step", "done"),
    ("that worked, thanks", "step", "done"),
    ("yes please", "offer", "done"),
    ("Sure, go ahead.", "offer", "done"),
    ("no thanks", "offer", "no"),
    ("nope", "offer", "no"),
    ("nope", "step", "failed"),
    ("didn't work", "step", "failed"),
    ("It didn't work.", "step", "failed"),
    ("uh it's still broken", "step", "failed"),
    ("still stringing", "step", "failed"),
    ("no luck", "step", "failed"),
    ("nothing changed", "step", "failed"),
    ("repeat", "step", "repeat"),
    ("Say that again?", "step", "repeat"),
    ("sorry I didn't catch that", "step", "repeat"),
    ("again", "step", "repeat"),
    ("what?", "step", "explain"),
    ("Explain.", "step", "explain"),
    ("I don't understand", "step", "explain"),
    ("what does that mean", "step", "explain"),
    ("stop", "step", "pause"),
    ("pause", "idle", "pause"),
    ("hold on", "step", "pause"),
    ("get staff", "step", "staff"),
    ("Can you call someone from staff?", "step", "staff"),
    ("I need a person", "idle", "staff"),
    ("help", "idle", "staff"),
    ("check this", "step", "camera"),
    ("take a look", "idle", "camera"),
    ("Can you look at it now?", "step", "camera"),
    ("take a picture", "step", "camera"),
    ("which step am I on", "step", "which_step"),
    ("how many more steps", "step", "which_step"),
    ("how hot is the nozzle supposed to be?", "step", "question"),
    ("why didn't it work, what else could be causing the stringing", "step", "question"),
    ("my print has strings all over it", "idle", "question"),
    ("it's PLA", "question", "question"),
    ("", "step", "none"),
    ("um uh", "step", "none"),
    # While paused, only resume (and get staff) count.
    ("resume", "paused", "resume"),
    ("okay I'm ready", "paused", "resume"),
    ("what's the weather", "paused", "none"),
    ("get staff", "paused", "staff"),
]


def run(cases):
    out = subprocess.run([NODE, "-e", RUNNER, str(ROOT / "app/static/coach-commands.js")],
                         input=json.dumps([[t, c] for t, c, _ in cases]), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_voice_commands():
    got = run(CASES)
    wrong = [(t, ctx, want, g) for (t, ctx, want), g in zip(CASES, got) if g != want]
    assert not wrong, "\n".join(f"{t!r} in {ctx}: wanted {want}, got {g}" for t, ctx, want, g in wrong)


def test_normalize_strips_fillers_and_punctuation():
    script = f"""const {{ normalize }} = require({json.dumps(str(ROOT / "app/static/coach-commands.js"))});
process.stdout.write(JSON.stringify([normalize("Um, so... it DIDN’T work!"), normalize("  Okay so, like, next please ")]));"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == ["it didn't work", "next"]
