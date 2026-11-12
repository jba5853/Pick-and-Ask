# Point and Ask

A Phase 1 prototype for Penn State TLT's AI Commons. Upload a photo, ask a question, and get a short answer (at most 3 sentences, under 50 words) drawn only from one knowledge pack. The answer names the doc it came from. If the docs don't cover the question, or the question is about safety or personal data, the app says so and points to the pack's escalation contact. When the docs don't cover a question, the reply also says what the pack can help with, using its `description`. Safety questions get only the pack's fixed `safety_message` (no contact line), never advice written by the model.

Packs that set `mode: diagnose` work differently. They troubleshoot from the photo: the app says what it sees, names the likely problem with a confidence (high, medium or low), and gives numbered fix steps from one doc. If it isn't sure, it asks one clarifying question instead of guessing. The `3d-printing` pack uses this mode.

Every pack is a conversation: follow-up questions see the earlier turns, and the first photo stays attached. In diagnose packs the visitor can say "walk me through it" to get one step at a time, with Done / Didn't work / Show me again buttons, a progress bar, a hand-off to staff when it can't be fixed, and a Summary button.

This is a local demo, not production.

## How it works

- `app/main.py`: FastAPI. Serves the page at `/`, lists packs at `/packs`, and answers at `POST /ask`.
- `app/images.py`: fixes photo rotation, shrinks the long edge to 1568px, converts to JPEG and removes all metadata (including GPS). Accepts JPEG, PNG and HEIC. Photos are processed in memory and never saved.
- `app/assistant.py`: sends the photo, the question and every doc in the pack to `claude-sonnet-5-5`. The reply has to match a JSON schema enforced by the API (structured outputs). The `source_doc` field can only be a real filename from the pack. If the reply can't be parsed, cites no valid doc, or the model declines, the app escalates. Answers longer than 3 sentences are trimmed at a sentence boundary.
- `app/conversation.py`: sessions, history, the guided fix, staff-only enforcement and summaries (see below).
- `docs/<pack>/`: one folder per knowledge pack, holding `pack.yaml` and the `.md` docs. A `README.md` in a pack is a note for maintainers and is not sent to the model.

### Diagnose mode

For packs with `mode: diagnose`, the model fills a stricter schema: `intent`, `observation`, `diagnosis`, `confidence`, `plan_type`, `step_ids`, `source_doc`, `reply`, `ruled_out`, `next_check`, `ai_suggestion` and `follow_up_question`. Each numbered step in the docs gets an id (for example `stringing.md#3`), and the model refers to steps only by id. The step text visitors see always comes from the doc, never from the model. The app then applies these rules in code, whatever the model wrote:

| Model output | What the visitor gets |
|---|---|
| High or medium confidence, step ids, a real source doc | Status `answered`: what I see, the likely problem, the steps (staff-only ones hidden), the source |
| Low confidence, or status `needs_more_info` | Status `needs_more_info`: exactly one clarifying question, no steps. The page sends the visitor's reply together with the original question |
| Docs don't cover it | `escalate` / `not_covered`, plus an optional `ai_suggestion` for common, low-risk problems. The page labels it "AI suggestion (not from official guides)" and keeps it separate from the official text |
| Anything about safety | Only the pack's fixed `safety_message`. Every other field is dropped, including any AI suggestion |
| Unparseable reply, or "answered" without valid steps or source | Escalation, with any AI text dropped |

### Conversations

- **Sessions.** The page makes a random session id. The server keeps each session's history in memory only, keyed by that id. Nothing is saved to disk, there's no login, and a session is forgotten after 30 minutes idle (at most 500 at once). Switching pack or pressing **New conversation** starts a fresh one. Restarting the server clears them all.
- **History and photos.** The first photo stays attached for the whole conversation, and visitors can add a new photo at any point. The last 10 exchanges are kept, plus the first exchange and the first photo. At most 4 photos are kept, always including the first.
- **Prompt caching.** The system prompt (instructions and all docs) is the cached prefix, and the history goes after it with a second cache breakpoint at its end. The guided-fix state is added only to the newest message, so earlier turns stay byte-identical and keep hitting the cache.

### Guided fix

After a diagnosis, **Walk me through it** starts a guided fix. The model reads messages, re-diagnoses and names causes. The code owns the state:

- One step at a time, with a "Step 2 of 5" progress bar and **Done / Didn't work / Show me again** buttons. Visitors can also type ("that fixed it", "still stringy", or a question). Done, Show me again and Walk me through it are handled without a model call.
- **Didn't work**: the model re-diagnoses, picking the next most likely cause, possibly from another doc, and the app says so: "Okay, that rules out the nozzle temperature. Let's check the slicer presets."
- **Done**: for a fix (each step is a different cause) it means fixed. For a procedure (like loading filament) it moves to the next step.
- **Escalation**: after 3 failed steps, or when no steps a visitor can do are left, the app hands off to the pack's contact with a one-line **note for staff**: the problem, what was tried, and which staff-only steps are left.
- **Summary**: the problem, what was tried and how it went, the outcome and the source docs. It's built from the session state, not written by the model, so it's safe to use as a staff hand-off.

### Staff-only steps

End a numbered doc step with `{staff_only: true}` to mark it staff-only. Do this for anything involving heating the nozzle or bed manually, cold pulls, opening the printer, or tools, and mark it whenever you're unsure. The code makes sure visitors never see the text of a staff-only step: it's hidden in diagnoses, skipped in a fix (with a note), and ends a procedure with a hand-off. A question whose answer relies on a staff-only step gets a hand-off instead. `staff_only_terms` in `pack.yaml` is a second safeguard: any model-written text containing one of those phrases is replaced with a hand-off.

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

# Live: runs the 14 single-turn cases and 3 conversations in tests/questions.md (costs a few cents).
python tests/run_questions.py
python tests/run_questions.py 3d-printing   # one pack only
```

The live runner prints PASS/FAIL for each case and the answer it got, then a summary table. For conversations it prints every turn and the cache reads per model call. Read the answers too: the runner checks status and cited doc, not wording. The 3D printing test photos in `tests/images/3d/` are drawn placeholders. Replace them with real photos, keeping the filenames.

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
