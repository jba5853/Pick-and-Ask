# Point and Ask

A Phase 1 prototype for Penn State TLT's AI Commons. Upload a photo, ask a question, and get a short answer (at most 3 sentences, under 50 words) drawn only from one knowledge pack. The answer names the doc it came from. If the docs don't cover the question, or the question is about safety or personal data, the app says so and points to the pack's escalation contact. Safety questions always get the pack's fixed `safety_message`, never advice written by the model.

This is a local demo, not production.

## How it works

- `app/main.py`: FastAPI. Serves the page at `/`, lists packs at `/packs`, and answers at `POST /ask`.
- `app/images.py`: fixes photo rotation, shrinks the long edge to 1568px, converts to JPEG and removes all metadata (including GPS). Accepts JPEG, PNG and HEIC. Photos are processed in memory and never saved.
- `app/assistant.py`: sends the photo, the question and every doc in the pack to `claude-sonnet-5-5`. The reply has to match a JSON schema enforced by the API (structured outputs). The `source_doc` field can only be a real filename from the pack. If the reply can't be parsed, cites no valid doc, or the model declines, the app escalates. Answers longer than 3 sentences are trimmed at a sentence boundary.
- `docs/<pack>/`: one folder per knowledge pack, holding `pack.yaml` and the `.md` docs.

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

Open http://127.0.0.1:8000. Pick a pack, choose a photo, type a question and click **Ask**. **Read aloud** uses your browser's built-in voice.

To get photos from Ray-Ban Meta glasses, import them in the Meta AI app, then AirDrop or save them to your Mac.

## Tests

```bash
# Offline: no key, no cost. Checks packs, image handling, reply parsing and trimming.
pytest

# Live: runs the 8 cases in tests/questions.md against the API (costs a few cents).
python tests/run_questions.py
```

The live runner prints PASS/FAIL for each case and the answer it got. Read the answers too: the runner checks status and cited doc, not wording.

## Adding or updating a knowledge pack

1. Create `docs/<pack-name>/pack.yaml`:
   ```yaml
   name: AI Arcade
   description: One sentence about what this pack covers.
   escalation: AI Office Hours
   safety_message: Please stop using it and step away. Tell a staff member right away.
   ```
2. Add one `.md` file per topic. Filenames are what the app cites, so use clear ones (for example `station-1-image-generator.md`).
3. Delete the `PLACEHOLDER-*.md` files once the real docs are in, and update `tests/questions.md` to match.

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
