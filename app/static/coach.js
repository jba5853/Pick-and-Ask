/* Coach mode: a hands-free loop for Ray-Ban Meta glasses paired to the phone as a Bluetooth headset.
 *
 * speak (glasses speakers) -> listen (glasses mic) -> act -> speak ...
 *
 * iOS notes, all handled here:
 * - Audio, speech and the mic need a user gesture once: the "Start hands-free" tap.
 * - Safari speech recognition is flaky in continuous mode, so each turn starts a fresh one-shot
 *   recognizer, with a watchdog, restart on end/error, and backoff. After 3 real failures we switch to
 *   big on-screen buttons and keep speaking aloud.
 * - We never listen while speaking, so the app can't hear itself.
 * - The screen is kept awake with the Wake Lock API where supported.
 *
 * TODO(server transcription): if Web Speech is missing or keeps failing, the page could record audio with
 * MediaRecorder and POST it to a /transcribe endpoint backed by an env-configured speech-to-text provider.
 * Not added: the Anthropic API has no speech-to-text, so it needs a second provider and key. The button
 * fallback covers this for now.
 *
 * Depends on globals from index.html's script: $, el, store, sessionId, renderReply, showReply, archiveCurrent,
 * showYou, askForCode, codeRequired, pendingRetry.
 */
(function () {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const match = window.CoachCommands.match;
  const DEBUG = new URLSearchParams(location.search).has("debug");
  const MAX_FAILURES = 3;
  const LISTEN_TIMEOUT_MS = 15000;

  let on = false;
  let mode = "off";        // off | speaking | listening | thinking | paused | camera | buttons
  let context = "idle";    // what a reply means: idle | offer | step | question | paused
  let last = null;         // last server reply
  let stepLines = [];      // how the current step was spoken, for resume
  let lastLines = [];      // last thing said, for "repeat"
  let pausedFor = "";      // "voice" (say resume) or "safety" (tap to resume)
  let rec = null, recTimer = null, failures = 0, buttonsMode = false;
  let audio = null, wakeLock = null, speakToken = 0;

  const ui = {};
  function bind() {
    for (const id of ["coach", "coachStart", "coachLive", "coachStartBtn", "coachExit", "coachState", "coachOrb",
                      "coachCaption", "coachHeard", "coachCard", "coachButtons", "coachPause", "coachCamera",
                      "coachCameraBtn", "coachCameraSkip", "coachCameraInput", "coachDebug", "coachSim", "coachSimSend",
                      "coachLog", "coachNoMic"]) ui[id] = document.getElementById(id);
  }

  // ---------- small helpers ----------

  function log(kind, text) {
    if (!DEBUG) return;
    const line = el("div", "", `${new Date().toLocaleTimeString()} ${kind}: ${text}`);
    ui.coachLog.prepend(line);
  }

  function setMode(next, label) {
    mode = next;
    ui.coachOrb.dataset.state = next;
    ui.coachState.textContent = label || { speaking: "Speaking", listening: "Listening…", thinking: "Thinking…",
      paused: "Paused", camera: "Take a photo", buttons: "Tap a button" }[next] || "";
    ui.coachPause.textContent = next === "paused" ? "Resume" : "Pause";
  }

  function earcon(kind) {
    // A short soft tone: rising when listening starts, falling when it stops.
    if (!audio) return;
    try {
      const t = audio.currentTime, osc = audio.createOscillator(), gain = audio.createGain();
      const [a, b] = kind === "start" ? [660, 880] : [740, 520];
      osc.type = "sine";
      osc.frequency.setValueAtTime(a, t);
      osc.frequency.linearRampToValueAtTime(b, t + 0.12);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.08, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
      osc.connect(gain).connect(audio.destination);
      osc.start(t);
      osc.stop(t + 0.18);
    } catch { /* audio is a nicety */ }
  }

  async function keepAwake() {
    try { if ("wakeLock" in navigator && !wakeLock) wakeLock = await navigator.wakeLock.request("screen"); }
    catch { wakeLock = null; }
    wakeLock?.addEventListener?.("release", () => { wakeLock = null; });
  }
  document.addEventListener("visibilitychange", () => { if (on && document.visibilityState === "visible") keepAwake(); });

  // ---------- speaking ----------

  function speak(lines, then) {
    stopListening();
    const token = ++speakToken;
    lines = (lines || []).filter(Boolean);
    if (lines.length) lastLines = lines;
    setMode("speaking");
    log("say", lines.join(" | "));
    if (!window.speechSynthesis || !lines.length) { ui.coachCaption.textContent = lines.join(" "); setTimeout(() => then?.(), 300); return; }
    speechSynthesis.cancel();
    let i = 0;
    const next = () => {
      if (token !== speakToken) return;  // interrupted by a newer speak()
      if (i >= lines.length) { setTimeout(() => { if (token === speakToken) then?.(); }, 350); return; }
      const line = lines[i++];
      ui.coachCaption.textContent = line;
      const u = new SpeechSynthesisUtterance(line);
      u.rate = 1.0;
      let finished = false;
      const done = () => { if (finished) return; finished = true; clearTimeout(guard); next(); };
      // iOS sometimes never fires onend: move on after a generous estimate.
      const guard = setTimeout(done, 2500 + line.split(" ").length * 450);
      u.onend = done;
      u.onerror = done;
      speechSynthesis.speak(u);
    };
    next();
  }

  // ---------- listening ----------

  function listen() {
    if (!on) return;
    if (buttonsMode) { showButtons(); return; }
    if (mode === "paused" && pausedFor === "safety") return;
    stopListening();
    if (!Recognition) { enterButtons("no-recognition"); return; }
    let heard = "", interim = "";
    const r = new Recognition();
    rec = r;
    r.lang = navigator.language || "en-US";
    r.continuous = false;
    r.interimResults = true;
    r.maxAlternatives = 1;
    r.onresult = (e) => {
      interim = "";
      for (const res of e.results) {
        if (res.isFinal) heard = (heard + " " + res[0].transcript).trim();
        else interim += res[0].transcript;
      }
      ui.coachHeard.textContent = (heard || interim) ? `“${heard || interim}”` : "";
    };
    r.onerror = (e) => {
      log("mic error", e.error);
      if (e.error === "not-allowed" || e.error === "service-not-allowed") { failures = MAX_FAILURES; }
      else if (e.error !== "no-speech" && e.error !== "aborted") failures += 1;
    };
    r.onend = () => {
      if (rec !== r) return;  // replaced or stopped on purpose
      clearTimeout(recTimer);
      rec = null;
      earcon("stop");
      const text = heard || interim;
      if (text) { failures = 0; handle(text); return; }
      if (failures >= MAX_FAILURES) { enterButtons("mic-failed"); return; }
      // Silence or a glitch: listen again, backing off if errors pile up.
      const delay = Math.min(400 * 2 ** failures, 4000);
      setTimeout(() => { if (on && (mode === "listening" || mode === "paused") && !rec) listen(); }, delay);
    };
    try {
      r.start();
      if (mode !== "paused") setMode("listening");
      earcon("start");
      recTimer = setTimeout(() => { try { r.stop(); } catch { /* already stopped */ } }, LISTEN_TIMEOUT_MS);
    } catch (err) {
      rec = null;
      failures += 1;
      log("mic start failed", err.message);
      if (failures >= MAX_FAILURES) enterButtons("mic-failed");
      else setTimeout(listen, Math.min(400 * 2 ** failures, 4000));
    }
  }

  function stopListening() {
    clearTimeout(recTimer);
    const r = rec;
    rec = null;
    if (r) { try { r.abort(); } catch { /* fine */ } }
  }

  function enterButtons(why) {
    log("fallback", why);
    buttonsMode = true;
    stopListening();
    const msg = why === "no-recognition" ? "Voice commands aren't available on this phone. Use the big buttons on screen."
                                         : "I'm having trouble hearing you. Use the big buttons on screen.";
    speak([msg], showButtons);
  }

  function showButtons() {
    ui.coachButtons.hidden = false;
    setMode("buttons");
  }

  // ---------- acting on what was heard ----------

  function handle(text) {
    const cmd = match(text, mode === "paused" ? "paused" : context);
    ui.coachHeard.textContent = `“${text}”`;
    log("heard", `${text} -> ${cmd.type}`);
    if (mode === "paused") {
      if (cmd.type === "resume") resume();
      else if (cmd.type === "staff") ask("staff");
      else listen();
      return;
    }
    switch (cmd.type) {
      case "none": listen(); break;
      case "pause": pause("voice"); break;
      case "repeat": speak(lastLines.length ? lastLines : ["I haven't said anything yet."], listen); break;
      case "which_step": whichStep(); break;
      case "staff": ask("staff"); break;
      case "camera": camera(); break;
      case "explain":
        ask("message", context === "step" ? "I don't understand this step. Can you explain it more simply?"
                                          : "I don't understand. Can you explain that more simply?");
        break;
      case "done":
        if (context === "offer") ask("walkthrough");
        else if (context === "step") ask("done");
        else if (context === "question") ask("message", text);
        else speak(["What would you like help with?"], listen);
        break;
      case "failed":
        if (context === "step") ask("failed");
        else ask("message", text);
        break;
      case "no":
        if (context === "offer") { context = "idle"; speak(["Okay.", "Ask me anything, or say take a look to show me."], listen); }
        else ask("message", text);
        break;
      default: ask("message", text);
    }
  }

  function whichStep() {
    const step = last?.step;
    speak([step ? `You're on step ${step.number} of ${step.total}.` : "We haven't started any steps yet."], listen);
  }

  function contextFor(data) {
    if (data.status === "guided" || (data.step && data.status !== "escalate")) return "step";
    if ((data.actions || []).includes("walkthrough")) return "offer";
    if (data.status === "needs_more_info") return "question";
    return "idle";
  }

  async function ask(action, text = "", file = null) {
    stopListening();
    setMode("thinking");
    ui.coachCaption.textContent = file ? "Looking at your photo…" : "Thinking…";
    const body = new FormData();
    body.set("pack", $("pack").value);
    body.set("session_id", sessionId);
    body.set("action", action);
    body.set("question", text);
    if (file) body.set("photo", file);
    let res, data;
    try {
      res = await fetch("/ask", { method: "POST", body, headers: { "X-Access-Code": store.get("sessionStorage", "pa-code") || "" } });
      data = await res.json().catch(() => ({}));
    } catch {
      speak(["I can't reach the server. Check the connection, then say that again."], listen);
      return;
    }
    if (res.status === 401) {
      stop();
      store.del("sessionStorage", "pa-code");
      askForCode("Enter the access code, then start hands-free again.");
      return;
    }
    if (!res.ok) {
      speak([data.detail || "Something went wrong."], res.status === 429 ? () => pause("safety") : listen);
      return;
    }
    last = data;
    context = contextFor(data);
    rememberStep(data);
    // Keep the normal page in sync underneath, without its own read-aloud.
    archiveCurrent();
    $("welcome").hidden = true;
    showYou(action === "message" ? (text || "(photo)") : ({ walkthrough: "Walk me through it", done: "Done", failed: "Didn't work", staff: "Get staff" }[action] || action),
            file ? URL.createObjectURL(file) : "");
    showReply(data, true, true);
    renderCard(data);
    speak(data.voice || [data.answer], data.voice_pause ? () => pause("safety") : listen);
  }

  function rememberStep(data) {
    if (data.status !== "guided") return;
    const lines = data.voice || [];
    const at = lines.findIndex((x) => /^(First|Next|Last one),/.test(x));
    stepLines = at >= 0 ? lines.slice(at).filter((x) => !/^After each step/.test(x)) : lines;
  }

  function renderCard(data) {
    ui.coachCard.replaceChildren(renderReply(data));
    ui.coachCard.querySelectorAll(".speak").forEach((b) => b.remove());
  }

  // ---------- pause, camera, start/stop ----------

  function pause(why) {
    pausedFor = why;
    stopListening();
    if (why === "voice") {
      speak(["Okay, paused. Say resume when you're ready."], () => { setMode("paused"); listen(); });
    } else {
      setMode("paused", "Paused: tap Resume");
      ui.coachCaption.textContent = "Paused. Tap Resume when you're ready.";
    }
  }

  function resume() {
    pausedFor = "";
    const lines = context === "step" && stepLines.length
      ? ["Okay, back to it."].concat(stepLines.map((x) => x.replace(/^(First|Next|Last one), /, "")).map((x, i) => i ? x : x[0].toUpperCase() + x.slice(1)))
      : ["I'm listening."];
    speak(lines, listen);
  }

  function camera() {
    speak(["Hold up your phone and tap the screen."], () => {
      setMode("camera");
      ui.coachCamera.hidden = false;
    });
  }

  function onPhoto(file) {
    ui.coachCamera.hidden = true;
    if (!file) { listen(); return; }
    ask("message", "Here's what it looks like now. What do you see?", file);
  }

  function opening() {
    const data = window.lastReplyData;
    if (data && (data.status === "guided" || (data.actions || []).includes("walkthrough") || data.step)) {
      last = data;
      context = contextFor(data);
      rememberStep(data);
      renderCard(data);
      return ["I'm here, hands-free."].concat(data.voice || []);
    }
    context = "idle";
    ui.coachCard.replaceChildren();
    return ["Hi, I'm listening.", "Tell me what's wrong, or say take a look to show me."];
  }

  function start() {
    // Everything that needs a user gesture on iOS happens synchronously inside this tap.
    on = true;
    failures = 0;
    buttonsMode = !Recognition;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      audio.resume();
    } catch { audio = null; }
    if (window.speechSynthesis) speechSynthesis.speak(new SpeechSynthesisUtterance(""));  // unlock speech
    if (Recognition && navigator.mediaDevices?.getUserMedia) {
      // Ask for the mic now (during the tap); recognition later reuses the permission.
      navigator.mediaDevices.getUserMedia({ audio: true }).then((s) => s.getTracks().forEach((t) => t.stop())).catch(() => {});
    }
    keepAwake();
    ui.coachStart.hidden = true;
    ui.coachLive.hidden = false;
    ui.coachButtons.hidden = !buttonsMode;
    earcon("start");
    speak(opening(), listen);
  }

  function stop() {
    on = false;
    speakToken++;
    stopListening();
    window.speechSynthesis?.cancel();
    try { wakeLock?.release(); } catch { /* fine */ }
    wakeLock = null;
    setMode("off", "");
    ui.coach.hidden = true;
  }

  function open() {
    ui.coach.hidden = false;
    ui.coachStart.hidden = false;
    ui.coachLive.hidden = true;
    ui.coachCamera.hidden = true;
    ui.coachButtons.hidden = true;
    ui.coachNoMic.hidden = !!Recognition;
    ui.coachDebug.hidden = !DEBUG;
    ui.coachHeard.textContent = "";
    ui.coachCaption.textContent = "";
  }

  function init() {
    bind();
    $("coachOpen").addEventListener("click", () => {
      if (codeRequired && !store.get("sessionStorage", "pa-code")) { pendingRetry = open; askForCode(); return; }
      open();
    });
    ui.coachStartBtn.addEventListener("click", start);
    ui.coachExit.addEventListener("click", stop);
    ui.coachPause.addEventListener("click", () => {
      if (mode === "paused") { audio?.resume(); resume(); } else pause("voice");
    });
    ui.coachCameraBtn.addEventListener("click", () => ui.coachCameraInput.click());
    ui.coachCameraSkip.addEventListener("click", () => onPhoto(null));
    ui.coachCameraInput.addEventListener("change", () => {
      const file = ui.coachCameraInput.files[0];
      ui.coachCameraInput.value = "";
      onPhoto(file || null);
    });
    for (const b of ui.coachButtons.querySelectorAll("button")) {
      b.addEventListener("click", () => { audio?.resume(); handle(b.dataset.say); });
    }
    const simulate = () => {
      const text = ui.coachSim.value.trim();
      if (!text || !on) return;
      ui.coachSim.value = "";
      stopListening();
      window.speechSynthesis?.cancel();
      speakToken++;
      handle(text);
    };
    ui.coachSimSend.addEventListener("click", simulate);
    ui.coachSim.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); simulate(); } });
  }

  // Exposed for debugging in the console.
  window.Coach = { state: () => ({ on, mode, context, failures, buttonsMode, lastLines }), handle: (t) => handle(t) };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
