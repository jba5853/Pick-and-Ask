/* Coach mode voice commands: turn a (messy) speech transcript into an action.
 *
 * Pure functions, no DOM: used by coach.js in the browser and by tests/test_coach_commands.py under Node.
 *
 * match(transcript, context) -> { type, text }
 *   context: "idle" | "offer" (just asked "walk you through it?") | "step" (on a guided step)
 *            | "question" (the assistant asked a clarifying question) | "paused"
 *   type:    "done" | "failed" | "repeat" | "explain" | "pause" | "resume" | "staff" | "camera"
 *            | "which_step" | "no" | "question" | "none"
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CoachCommands = api;
})(typeof self !== "undefined" ? self : this, function () {
  const FILLERS = /\b(um+|uh+|uhh+|erm+|er|hmm+|mm+|ah+|oh|well|so|like|you know|i mean|actually|basically|just|please|hey|okay so|ok so|alright so|coach|assistant)\b/g;

  function normalize(text) {
    return String(text || "")
      .toLowerCase()
      .replace(/[’‘]/g, "'")
      .replace(/[^a-z0-9' ]+/g, " ")
      .replace(FILLERS, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  const words = (t) => (t ? t.split(" ").length : 0);
  const any = (t, patterns) => patterns.some((p) => p.test(t));

  const STAFF = [
    /\b(get|call|find|fetch|need|want|talk to|speak to|speak with|talk with)\b.*\b(staff|a person|person|human|someone|somebody|an adult|attendant|technician|tech)\b/,
    /^(staff|get staff|help|i need help|get help|need help|call for help)$/,
  ];
  const PAUSE = /^(stop|pause|wait|hold on|hang on|stop listening|be quiet|quiet|shush|stop talking|give me a (sec|second|minute)|one (sec|second|moment))( for now)?$/;
  const RESUME = /^(resume|continue|start|start again|unpause|i'm back|im back|go|go on|carry on|let's continue|lets continue|keep going|i'm ready|im ready|ready)$/;
  const CAMERA = [
    /\b(check|look at|take a look at|have a look at|see|look)\b.*\b(this|that|it|now|here)\b/,
    /^(take a look|have a look|look|take a picture|take a photo|photo|picture|camera|show you)$/,
    /\btake (a )?(photo|picture|pic)\b/,
    /\b(let me|can i|i'll|i will|i want to) show you\b/,
  ];
  const WHICH_STEP = /\b(which|what) step\b|\bhow many (steps|more)\b|\bwhere am i\b|\bhow far\b/;
  const FAILED = [
    /\b(did ?n'?t|did not|does ?n'?t|does not|is ?n'?t|is not|has ?n'?t|has not) (work|working|help|helped|fix|fixed|change|changed)\b/,
    /\bnot (working|fixed|better|helping)\b/,
    /\bstill (broken|there|bad|happening|stringing|warping|clicking|stuck|the same|not|wrong|doing it)\b/,
    /\b(no luck|same (problem|thing|issue)|nothing changed|no change|no difference|it's broken|its broken|that failed|it failed|worse|no good)\b/,
  ];
  const REPEAT_SHORT = /^(again|say again|again please|come again|repeat|repeat that|repeat it|pardon|sorry|what was that|one more time)$/;
  const REPEAT = /\b(repeat|say (that|it) again|say again|didn'?t (catch|hear)|did not (catch|hear)|one more time|read (that|it) again)\b/;
  const EXPLAIN_SHORT = /^(what|huh|eh|explain|explain that|explain it|what do you mean|meaning|how|how do i do that|how do i do it|what does that mean|i don'?t understand|i do not understand|i don'?t get it|i'm confused|im confused|more detail|tell me more)$/;
  const EXPLAIN = /\b(explain|don'?t understand|do not understand|what does that mean|what do you mean|confused)\b/;
  const NO = /^(no|nah|nope|no thanks|no thank you|not now|not yet|no need|i'm good|im good)$/;
  const DONE = /^(done|i'm done|im done|i'm finished|finished|next|next step|next one|ok|okay|alright|all right|got it|did it|i did it|i've done it|ive done it|yes|yeah|yep|yup|yes please|sure|go ahead|please do|do it|let's do it|lets do it|let's go|lets go|sounds good|continue|carry on|keep going|move on|ready|it worked|that worked|worked|fixed|it's fixed|its fixed|that fixed it|works now|it works|perfect|great|cool|good)( thanks| thank you)?$/;

  function match(transcript, context = "idle") {
    const t = normalize(transcript);
    const n = words(t);
    const out = (type) => ({ type, text: String(transcript || "").trim() });
    if (!t) return out("none");
    // "okay I did it", "sure, go ahead": also try without a leading acknowledgment.
    const core = t.replace(/^(okay|ok|alright|all right|yeah|yes|sure|right|great|cool|good)\b\s*/, "") || t;

    if (any(t, STAFF)) return out("staff");
    if (context === "paused") return [t, core].some((x) => RESUME.test(x) || DONE.test(x)) ? out("resume") : out("none");
    if (PAUSE.test(t)) return out("pause");
    if (n <= 9 && any(t, CAMERA) && !/^(how|what|why|when|is|does|should)\b/.test(t)) return out("camera");
    if (WHICH_STEP.test(t)) return out("which_step");
    if (REPEAT_SHORT.test(t) || (n <= 7 && REPEAT.test(t))) return out("repeat");
    if (context === "offer" && NO.test(t)) return out("no");
    // "why didn't it work, what else could it be" is a question; "it still didn't work" is a failure.
    const asks = /^(why|how|what|when|where|which|is|are|does|do|can|could|should|will|would)\b/.test(t);
    if (any(t, FAILED) && !(asks && n > 6)) return out("failed");
    if (NO.test(t)) return out(context === "step" ? "failed" : "no");
    if (EXPLAIN_SHORT.test(t) || (n <= 8 && EXPLAIN.test(t))) return out("explain");
    if (DONE.test(t) || DONE.test(core) || (n <= 4 && /\b(done|next|worked|fixed|finished)\b/.test(t))) return out("done");
    return out("question");
  }

  return { match, normalize };
});
