# Glance

**Point at anything. Get walked through it, hands-free.**

Look at a device, machine or product with Ray-Ban Meta glasses (or your phone) and send a photo. Glance:

1. **Identifies the exact thing**: brand and model from labels and logos, what state it's in, and any hazard.
2. **Asks what you want**: set it up, fix a problem, learn to use it, maintenance or safety, with options tailored to the object.
3. **Finds the official instructions**: it searches for the manufacturer's manual or support page for that model and turns it into short physical steps, with the sources shown.
4. **Coaches you one step at a time**, out loud through the glasses: "done", "repeat", "didn't work", or ask anything.
5. **Checks your work from a photo** before moving on ("Not yet, the base isn't clicked in").
6. **Stops when it's unsafe**: smoke, sparks or exposed wiring get a fixed stop message, and jobs that need a technician get referred to one. It never identifies people.

The main app is at `/`. The original knowledge-pack app (AI Arcade, 3D printing, staff tools) is still at `/classic`.

## Quick start

```bash
cd ~/point-and-ask
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload        # open http://127.0.0.1:8000
```
On your phone (HTTPS + camera): `scripts/share.sh`, then scan the QR code.

Live check against the API (a few cents): `python tests/run_anything.py tests/images/3d/stringing.jpg`

## Glasses link (WhatsApp)

This is the no-tap path: **"Hey Meta, send a photo to Glance on WhatsApp."** The photo goes to the app, the reply comes back as a WhatsApp message, and the glasses read it aloud. Answer by voice ("Hey Meta, reply: set it up", "reply done") or send another photo to have a step checked. The phone app mirrors everything live.

One-time setup, about 20 minutes:

1. Go to developers.facebook.com, then **My Apps → Create app**, choose the Business type and add the **WhatsApp** product.
2. Under **WhatsApp → API Setup**: copy the **temporary access token** (it lasts 24 hours) and the test number's **Phone number ID**. Under "To", add **your own WhatsApp number** and verify it with the code.
3. Put them in `.env`:
   ```
   WA_TOKEN=...
   WA_PHONE_NUMBER_ID=...
   WA_VERIFY_TOKEN=pick-any-secret-word
   WA_ALLOWED_NUMBERS=1XXXXXXXXXX        # your number, digits only
   ```
4. Run `scripts/share.sh`. It prints a webhook URL ending in `/webhooks/whatsapp`.
5. Under **WhatsApp → Configuration → Webhook**: paste that URL and your `WA_VERIFY_TOKEN`, click **Verify and save**, then **Subscribe** to the `messages` field.
6. On your iPhone, save the test number as a contact named **Glance**. In the Meta AI app, make sure WhatsApp is connected to the glasses.
7. Test: "Hey Meta, send a photo to Glance on WhatsApp."

Notes: the quick-tunnel URL changes every time you run `share.sh`, so paste the new URL into step 5 each time. The temporary token expires after 24 hours, so make a fresh one on demo day.

## How the "anything" flow works

- `app/anything.py`: identify (structured output), plan (Claude with the web search tool, preferring manufacturer sources), photo verification, chat, and the safety rules enforced in code.
- `app/router.py`: turns replies like "2", "okay done" or "nope still broken" into actions (WhatsApp and voice).
- `app/whatsapp.py`: webhook with verify token, optional signature check (`WA_APP_SECRET`), allow-list, duplicate protection, and media download.
- `app/static/app.html`: the light, mobile-first app with live inbox, step coach, photo check, chat and hands-free voice.
- Sessions, photos and the inbox live in memory only and expire after 3 hours idle.

---

## Classic app: knowledge packs (`/classic`)

A Phase 1 prototype for Penn State TLT's AI Commons. Upload a photo, ask a question, and get a short answer (at most 3 sentences, under 50 words) drawn only from one knowledge pack. The answer names the doc it came from. If the docs don't cover the question, or the question is about safety or personal data, the app says so and points to the pack's escalation contact. When the docs don't cover a question, the reply also says what the pack can help with, using its `description`. Safety questions get only the pack's fixed `safety_message` (no contact line), never advice written by the model.

Packs that set `mode: diagnose` work differently. They troubleshoot from the photo: the app says what it sees, names the likely problem with a confidence (high, medium or low), and gives numbered fix steps from one doc. If it isn't sure, it asks one clarifying question instead of guessing. The `3d-printing` pack uses this mode.

Every pack is a conversation: follow-up questions see the earlier turns, and the first photo stays attached. In diagnose packs the visitor can say "walk me through it" to get one step at a time, with Done / Didn't work / Show me again buttons, a progress bar, a hand-off to staff when it can't be fixed, and a Summary button.

This is a local demo, not production.

## How it works

- `app/main.py`: FastAPI. Serves the page at `/`, lists packs at `/packs`, and answers at `POST /ask`.
- `app/images.py`: fixes photo rotation, shrinks the long edge to 1568px, converts to JPEG and removes all metadata (including GPS). Accepts JPEG, PNG and HEIC. Photos are processed in memory and never saved.
- `app/assistant.py`: sends the photo, the question and every doc in the pack to `claude-sonnet-5-5`. The reply has to match a JSON schema enforced by the API (structured outputs). The `source_doc` field can only be a real filename from the pack. If the reply can't be parsed, cites no valid doc, or the model declines, the app escalates. Answers longer than 3 sentences are trimmed at a sentence boundary.
- `app/conversation.py`: sessions, history, the guided fix, staff-only enforcement and summaries (see below).
- `app/builder.py`, `app/admin.py`, `app/insights.py`: the AI pack builder, the staff pages and the anonymous insights log (see "Staff tools").
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

# Offline tests also cover the access code and rate limits (tests/test_api.py).
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

## Coach mode (hands-free with Meta glasses)

Pair Ray-Ban Meta glasses with the iPhone as a Bluetooth headset, open the app over HTTPS (`scripts/share.sh`) and tap **Hands-free**, then **Start hands-free**. That one tap unlocks audio, speech and the mic on iOS. After it, everything runs by voice through the glasses, and the phone can go in a pocket.

**The loop:** the coach says the diagnosis in one short sentence, asks "Want me to walk you through it?", then speaks one step at a time and listens for the reply. A soft rising tone means it's listening, and a falling tone means it stopped. The full text stays on screen.

**Voice commands** (`app/static/coach-commands.js`): matching ignores filler words like "um" and "so", and uses context, so "yes" means "walk me through it" after a diagnosis and "done" during a step.

| Say | What happens |
|---|---|
| done / next / okay / yes / it worked | next step, or fixed |
| didn't work / still broken / no luck | the next most likely cause ("Okay, that rules out X. Let's check Y.") |
| repeat / say that again | says the last thing again (no server call) |
| what? / explain | asks the model to explain the current step more simply |
| take a look / check this | "Hold up your phone and tap the screen", then camera, then re-diagnosis |
| which step am I on? | "You're on step 2 of 5" (step numbers are only spoken when asked) |
| stop / pause | pauses; say "resume" to carry on |
| get staff | hands off to staff straight away, with the note for staff |
| anything else | goes to the model as a question, with the conversation history |

**Spoken style** (`app/voice.py`): at most 20 words per utterance, with "First," "Next," and "Last one," as leads. Menu paths in brackets stay on screen only. Safety and staff hand-off messages are always spoken in full, and then the loop pauses until you tap Resume.

**iOS robustness:**
- Each turn uses a fresh one-shot recognizer with a 15-second watchdog, restarts after silence, and backs off after errors.
- It never listens while it's speaking.
- After 3 real recognition failures, or on browsers without speech recognition, big **Done / Didn't work / Repeat / Take a look / Get staff** buttons take over, and the coach keeps talking.
- The screen stays awake (Wake Lock API) while Coach mode is on.

**Testing without glasses:** add `?debug=1` to the URL. Coach mode then shows a **Simulate voice** box (type a reply as if you'd said it) and a log of everything it said and heard. `window.Coach.handle("didn't work")` works from the console too.

Server-side transcription is not built. There's a TODO at the top of `app/static/coach.js`: it would need a second, speech-to-text provider, since the Anthropic API doesn't transcribe audio. The button fallback covers it for now.

## Staff tools: AI pack builder and insights

Set `ADMIN_CODE` in `.env` (at least 8 characters, different from `ACCESS_CODE`). Then open `/admin` (packs) or `/admin/insights`. Without `ADMIN_CODE` the staff pages are off. Five wrong codes from one IP lock that IP out for 10 minutes.

### AI pack builder (`/admin`)

"Any space can get this by adding its docs":

1. Give the pack an id, upload manual PDFs and/or paste links to official guides (up to 5), and click **Build draft**. Building takes 1 to 4 minutes.
2. `claude-opus-5-5` reads the text (PDF text page by page, with page markers) and drafts short docs: one problem or task per doc, numbered steps, the source and page/section at the top of each, plus a suggested `pack.yaml` (description, mode, safety message) and staff-only markers.
3. The draft is saved to `docs/_drafts/<id>/`, which visitors can't see. On the review screen you edit the text, tick or untick staff-only per step, and set the pack settings.
4. **Publish** moves it to `docs/<id>/` and it's live straight away.

What keeps it honest (`app/builder.py`):

- **Steps must come from the source.** Every step comes with a short verbatim quote, and the code checks that quote is really in the source text. If it isn't, the step gets a `REVIEW:` note.
- **Anything uncertain gets a `REVIEW:` note**, and so does the contact person. Publishing is blocked until every `REVIEW:` note is gone.
- **Risky steps are suggested as staff-only**, by the model and by a keyword backstop (`STAFF_RULES`: optics, electrical, panels, tools, heat, blades, chemicals, calibration…). When in doubt, it marks the step staff-only.
- **Sources are read safely.** Too much text is refused, never silently cut. Links must be public http(s) addresses, and scanned PDFs without text are rejected.

`docs/_drafts/example/` is a real draft built from Full Spectrum Laser's MC Series CO2 laser cutter manual (`https://fslaser.com/static/files/resources/manuals/mc-series-co2-flatbed-manual.pdf`): 16 docs, 55 staff-only steps and 39 REVIEW notes, with every step's quote found in the manual. Review it at `/admin`.

### Insights (`/admin/insights`)

Each conversation is logged anonymously to `data/insights.sqlite3`, which is git-ignored. One row per conversation holds:

- The pack.
- The first question, with emails, phone numbers, handles, links and long numbers removed.
- The diagnosis and confidence.
- The outcome: fixed, escalated, safety, not covered, answered or unfinished.
- The latest question the docs didn't cover.
- How many steps were tried.
- Minute-level timestamps.

It never stores photos, IPs, session ids, the rest of the conversation, or questions the model flagged as personal data.

The dashboard shows:

- Totals, the fix rate (fixed ÷ (fixed + escalated)), escalations and safety triggers.
- The most common problems, with each one's fix rate, and how conversations ended.
- **Questions the docs don't cover**, which is the list of docs to write next.

Demo data for a pitch:

```bash
python scripts/seed_insights.py            # adds ~180 FAKE conversations, flagged demo=1
python scripts/seed_insights.py --remove   # deletes only the demo rows
```

The dashboard labels demo rows and can hide them. The test runner and pytest never write to the real database. Set `INSIGHTS_DISABLED=1` to turn logging off, or `INSIGHTS_DB=path` to use another file.

## Demo on your phone

`scripts/share.sh` gives your iPhone an HTTPS link to the app running on your Mac, using a free Cloudflare quick tunnel (no account needed). HTTPS lets the phone use the camera, the mic and read aloud.

One-time setup:

```bash
brew install cloudflared
echo 'ACCESS_CODE=pick-something-memorable' >> .env   # at least 6 characters
source .venv/bin/activate && pip install -r requirements.txt
```

Each demo:

```bash
scripts/share.sh                 # opens on the default pack (AI Arcade)
scripts/share.sh 3d-printing     # opens straight into the 3D printing pack
```

The script starts the server, opens the tunnel, and prints a `https://<random-words>.trycloudflare.com` link and a QR code. Scan the QR code with the iPhone camera, enter the access code once, and you're in. Press Ctrl+C to stop both. The link stops working then, and you get a new link each time.

On the phone:

- **Take photo** opens the camera. **Library** picks a photo, including Meta glasses photos that have synced to the Camera Roll through the Meta AI app.
- The **mic** button fills the message box with what you said, so you can fix it before sending. It's hidden on browsers without speech recognition. On iPhone it needs Siri & Dictation turned on.
- **Auto-read** speaks each reply. Long steps are read as their first sentence, and the full step stays on screen.
- `?pack=3d-printing` in the link picks the pack, so a QR code stuck on a printer can open straight into the right pack.

Safety:

- The link is public while the script runs, so `/ask` requires the access code (`X-Access-Code`, checked in constant time). Ten wrong codes from one IP lock it out for 10 minutes.
- Rate limits: 20 asks per 10 minutes per conversation, and 60 per 10 minutes per IP.
- The limits and sessions are in memory and reset when the server restarts.
- Don't share the link and code beyond the people in the demo. Every question uses your API credits.

If the QR link doesn't load at first, wait about 10 seconds for the tunnel to come up, then refresh. If cloudflared complains about a config file, move `~/.cloudflared/config.yml` aside, since quick tunnels can't run with one.

## Alternative: same Wi‑Fi, no tunnel


By default the server listens only on `127.0.0.1`, so only your Mac can reach it. To test from your iPhone without a tunnel:

1. Find your Mac's local IP: `ipconfig getifaddr en0` (for example `192.168.1.23`).
2. Start the server on all interfaces:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
3. If macOS asks whether to allow incoming connections for Python, click **Allow**.
4. On the iPhone, open `http://192.168.1.23:8000`.

Keep in mind:

- Anyone on that network can then use the app, and your API credits, unless `ACCESS_CODE` is set. Only do this on a trusted network, and stop the server when you're done.
- Some campus networks block device-to-device traffic. If the page won't load, try a personal hotspot, or use `scripts/share.sh`.
- Over plain `http://`, photos still work, but the mic needs HTTPS. Use `scripts/share.sh` for the full demo.
