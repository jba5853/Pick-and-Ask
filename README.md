# Point and Ask

A Phase 1 prototype for Penn State TLT's AI Commons. Upload a photo, ask a question, and get a short answer (at most 3 sentences, under 50 words) drawn only from one knowledge pack. The answer names the doc it came from. If the docs don't cover the question, or the question is about safety or personal data, the app says so and points to the pack's escalation contact. When the docs don't cover a question, the reply also says what the pack can help with, using its `description`. Safety questions get only the pack's fixed `safety_message` (no contact line), never advice written by the model.

Packs that set `mode: diagnose` work differently. They troubleshoot from the photo: the app says what it sees, names the likely problem with a confidence (high, medium or low), and gives numbered fix steps from one doc. If it isn't sure, it asks one clarifying question instead of guessing. The `3d-printing` pack uses this mode.

This is a local demo, not production.

## How it works

- `app/main.py`: FastAPI. Serves the page at `/`, lists packs at `/packs`, and answers at `POST /ask`.
- `app/images.py`: fixes photo rotation, shrinks the long edge to 1568px, converts to JPEG and removes all metadata (including GPS). Accepts JPEG, PNG and HEIC. Photos are processed in memory and never saved.
- `app/assistant.py`: sends the photo, the question and every doc in the pack to `claude-sonnet-5-5`. The reply has to match a JSON schema enforced by the API (structured outputs). The `source_doc` field can only be a real filename from the pack. If the reply can't be parsed, cites no valid doc, or the model declines, the app escalates. Answers longer than 3 sentences are trimmed at a sentence boundary.
- `docs/<pack>/`: one folder per knowledge pack, holding `pack.yaml` and the `.md` docs. A `README.md` in a pack is a note for maintainers and is not sent to the model.

### Diagnose mode

For packs with `mode: diagnose`, the model fills a stricter schema: `observation`, `diagnosis`, `confidence`, `steps`, `source_doc`, `ai_suggestion` and `follow_up_question`. The app then applies these rules in code, whatever the model wrote:

| Model output | What the visitor gets |
|---|---|
| High or medium confidence, steps, a real source doc | Status `answered`: what I see, the likely problem, up to 6 steps, the source |
| Low confidence, or status `needs_more_info` | Status `needs_more_info`: exactly one clarifying question, no steps. The page sends the visitor's reply together with the original question |
| Docs don't cover it | `escalate` / `not_covered`, plus an optional `ai_suggestion` for common, low-risk problems. The page labels it "AI suggestion (not from official guides)" and keeps it separate from the official text |
| Anything about safety | Only the pack's fixed `safety_message`. Every other field is dropped, including any AI suggestion |
| Unparseable reply, or "answered" without valid steps or source | Escalation, with any AI text dropped |

The `answer` field holds the short read-aloud version: the problem, the first step and how many more steps are on screen. Diagnose packs use `effort: medium`, answer packs keep `low`.

## Setup (macOS)

You need Python 3.10 or newer. Check with `python3 --version`. If you need to install it, run `brew install python`.

```bash
cd ~/point-and-ask
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set your API key using **one** of these options:

```bash
# Option A: for this terminal session only
export ANTHROPIC_API_KEY=...

# Option B: in a .env file (git-ignored), loaded automatically
cp .env.example .env    # then edit .env
```

The key is read only from the environment. It is never written to logs or committed.

## Run

```bash
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Pick a pack, choose a photo, type a question and click **Ask**. **Read aloud** uses your browser's built-in voice. For a diagnosis it reads the short version, and any AI suggestion is read with its label.

To get photos from Ray-Ban Meta glasses, import them in the Meta AI app, then AirDrop or save them to your Mac.

## Tests

```bash
# Offline: no key, no cost. Checks packs, image handling, reply parsing and trimming.
pytest

# Live: runs the 14 cases in tests/questions.md against the API (costs a few cents).
python tests/run_questions.py
python tests/run_questions.py 3d-printing   # one pack only
```

The live runner prints PASS/FAIL for each case and the answer it got, then a summary table. Read the answers too: the runner checks status and cited doc, not wording. The 3D printing test photos in `tests/images/3d/` are drawn placeholders. Replace them with real photos, keeping the filenames.

## Adding or updating a knowledge pack

1. Create `docs/<pack-name>/pack.yaml`:
   ```yaml
   name: AI Arcade
   description: One sentence about what this pack covers.
   escalation: a Media Commons staff member or aiarcade@psu.edu
   safety_message: Please stop using it and step away. Tell a staff member right away.
   mode: answer   # optional: "answer" (default) or "diagnose"
   ```
2. Add one `.md` file per topic. Filenames are what the app cites, so use clear ones (for example `a006-postcard-portals.md`). Start each doc with a `> Source: <url> · Retrieved <date>` line.
3. Update `tests/questions.md` to match the docs.

Every doc in a pack is sent with each question. That works for small packs (up to roughly 100 pages). Larger packs would need search added.

## Later: reach it from your iPhone on the same Wi‑Fi

For now the server listens only on `127.0.0.1`, so only your Mac can reach it. To test from your iPhone:

1. Find your Mac's local IP: `ipconfig getifaddr en0` (for example `192.168.1.23`).
2. Start the server on all interfaces:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
3. If macOS asks whether to allow incoming connections for Python, click **Allow**.
4. On the iPhone, open `http://192.168.1.23:8000`.

Keep in mind:

- Anyone on that network can then use the app, and your API credits. Only do this on a trusted network, and stop the server when you're done. There is no login.
- Some campus networks block device-to-device traffic. If the page won't load, try a personal hotspot.
- Plain `http://` works for uploading photos. Camera capture straight from the browser needs HTTPS, which is a Phase 2 task.
