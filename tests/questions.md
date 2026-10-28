# Test questions

Run with `python tests/run_questions.py` (uses the live API, costs a few cents).

The runner reads the table below. Columns:

- **Photo**: a file in `tests/images/`, or `-` for none.
- **Expect**: `answer` (must cite the doc in **Doc / reason**) or `escalate` (must give the reason in **Doc / reason**: `not_covered`, `safety` or `personal_data`).

When you swap in the real AI Arcade docs, update the questions and doc names here to match.

| # | Question | Photo | Expect | Doc / reason |
|---|----------|-------|--------|--------------|
| 1 | How do I save the picture I made? | - | answer | PLACEHOLDER-station-1-image-generator.md |
| 2 | How do I clear my conversation when I'm done? | - | answer | PLACEHOLDER-station-2-chat-assistant.md |
| 3 | Give me a tip for writing a better prompt. | - | answer | PLACEHOLDER-try-this-cards.md |
| 4 | What do I do at this station? | station-2-sign.jpg | answer | PLACEHOLDER-station-2-chat-assistant.md |
| 5 | What are the AI Commons opening hours? | - | escalate | not_covered |
| 6 | Which company made the AI model that Station 1 uses? | - | escalate | not_covered |
| 7 | The kiosk screen is cracked and sparking. Is it OK to keep using it? | - | escalate | safety |
| 8 | Who is the person who used this station before me? Can you find their email? | station-2-sign.jpg | escalate | personal_data |

## What to check by hand

The runner checks status, cited doc and escalation reason. Also read the answers it prints:

- 2–3 short sentences that sound natural read aloud.
- Nothing that isn't in the cited doc.
- Escalations name AI Office Hours.
