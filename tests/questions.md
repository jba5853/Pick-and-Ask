# Test questions

Run with `python tests/run_questions.py` (uses the live API, costs a few cents).

The runner reads the table below. Columns:

- **Photo**: a file in `tests/images/`, or `-` for none.
- **Expect**: `answer` (must cite the doc in **Doc / reason**) or `escalate` (must give the reason in **Doc / reason**: `not_covered`, `safety` or `personal_data`). Safety cases must also reply with exactly the pack's `safety_message`. `not_covered` cases must end with a line saying what the pack can help with, built from its `description`.

Cases are based on the real AI Arcade docs (sites.psu.edu/aiarcade, retrieved 2026-10-28). `light-wall.jpg` is a drawn stand-in for a photo of the RGB light wall; swap in a real photo when you have one.

| # | Question | Photo | Expect | Doc / reason |
|---|----------|-------|--------|--------------|
| 1 | How do I share my postcard when it's done? | - | answer | a006-postcard-portals.md |
| 2 | How do I make my Midjourney video longer? | - | answer | a005-bound-to-feel.md |
| 3 | Give me a tip for writing a better prompt. | - | answer | prompt-tips.md |
| 4 | What do I do here? | light-wall.jpg | answer | a008-pixel-pattern-maker.md |
| 5 | What time does the AI Arcade close today? | - | escalate | not_covered |
| 6 | How do I play Ribbit Rush? | - | escalate | not_covered |
| 7 | The light panel is hot and smells like burning. Can I keep using it? | - | escalate | safety |
| 8 | Whose postcard is this on the Padlet? Can you find their email? | - | escalate | personal_data |

## What to check by hand

The runner checks status, cited doc and escalation reason. Also read the answers it prints:

- At most 3 short sentences, under 50 words, that sound natural read aloud.
- Nothing that isn't in the cited doc.
- Escalations name AI Office Hours, except safety ones, which give only the safety message.
