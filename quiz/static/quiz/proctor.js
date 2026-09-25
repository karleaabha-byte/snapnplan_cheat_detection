/* Screen proctor for SnapNPlan exam mode.
 *
 * Captures the student's screen, classifies a frame every few seconds with a
 * TensorFlow.js model, and escalates: detect -> warn -> flag.
 *
 * Design notes that matter:
 *
 *  - NEVER stores frames. Only {t, type, seconds} records leave this file.
 *    A two-hour exam is a few kilobytes of JSON, and there is no footage
 *    anywhere to leak.
 *  - Requires the WHOLE monitor. Sharing one tab would defeat the point, so
 *    displaySurface is checked and anything else is refused.
 *  - Debounces before acting. One frame means nothing; a notification popup
 *    or a dropdown would otherwise fire the alarm constantly.
 *  - Warns before flagging, so an accidental window can be closed with no
 *    consequence. Only sustained presence is recorded.
 *
 * MODES (set by the invigilator, see attachControl)
 *
 *    "monitoring"  normal. Detections escalate to warnings and flags.
 *
 *    "permitted"   an allowed lookup period, e.g. an open-book section.
 *                  Classification CONTINUES and windows are still logged, but
 *                  as `permitted_window_visible` - no alarm, no flag. This is
 *                  deliberate: the audit trail stays unbroken, and a window
 *                  left open after the period ends is caught immediately,
 *                  because the mode change is itself a logged event.
 *
 *    "paused"      hard stop. No classification at all, for when a student
 *                  must open something private. Leaves a `paused` gap in the
 *                  record that is visible on the report, so nobody can
 *                  pretend the exam was monitored throughout.
 *
 * Usage
 *   const p = new Proctor({
 *     modelUrl: "/static/quiz/screen_model/model.json",
 *     onState: (s) => {},      // "ok"|"warning"|"flagged"|"permitted"|"paused"|"stopped"
 *     onEvent: (e) => {},      // every recorded event
 *   });
 *   await p.start();
 *   p.attachControl("/exam/state/", 5000);   // poll for invigilator mode
 *   const report = p.stop();                 // {events, stats} -> POST
 */

const DEFAULTS = {
  sampleMs: 4000,        // how often to classify a frame
  warnAfterMs: 8000,     // sustained detection before the student is warned
  flagAfterMs: 18000,    // sustained detection before it is recorded

  // Measured on 37 REAL screenshots (19 clean, 18 with a window open),
  // not on the generated training data:
  //
  //   threshold   clean left alone   windows caught
  //     0.50           84.2%              100%
  //     0.58-0.72      100%               100%   <- the plateau
  //     0.74           100%              88.9%
  //
  // 0.65 sits in the middle of that plateau, so an unusual screen has
  // room to move in either direction before accuracy drops. The model's
  // own config.json says 0.50, which was tuned on synthetic data where
  // separation was perfect - do not use that number here.
  threshold: 0.65,

  classes: ["clean", "ai_chat", "document", "messaging", "search_web"],
  allow: [],             // e.g. ["document"] for an open-book paper
  alarm: true,           // audible tone on warning
  requireMonitor: true,  // refuse tab-only or window-only sharing
};

export class Proctor {
  constructor(opts) {
    this.o = { ...DEFAULTS, ...opts };
    this.model = null;
    this.stream = null;
    this.video = null;
    this.canvas = null;
    this.timer = null;
    this.control = null;
    this.state = "stopped";
    this.mode = "monitoring";
    this.detectedSince = null;
    this.warned = false;
    this.events = [];
    this.frames = 0;
    this.detections = 0;
    this.permittedMs = 0;
    this.pausedMs = 0;
    this._modeSince = null;
    this.startedAt = null;
  }

  /* ---------------------------------------------------------------- start */
  async start() {
    if (!navigator.mediaDevices?.getDisplayMedia) {
      throw new Error("This browser cannot share the screen.");
    }

    this.stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: 1 },
      audio: false,
    });

    const track = this.stream.getVideoTracks()[0];
    const surface = track.getSettings().displaySurface;
    if (this.o.requireMonitor && surface && surface !== "monitor") {
      this._stopStream();
      throw new Error(
        `You shared a ${surface}. Please share your ENTIRE SCREEN instead.`);
    }

    // Stopping the share mid-exam is itself an event - the obvious first
    // thing a student would try.
    track.addEventListener("ended", () => {
      if (this.state !== "stopped") {
        this._record("sharing_stopped", 0);
        this._setState("flagged");
      }
    });

    this.video = document.createElement("video");
    this.video.srcObject = this.stream;
    this.video.muted = true;
    await this.video.play();

    this.canvas = document.createElement("canvas");
    this.canvas.width = 224;
    this.canvas.height = 224;

    if (!this.model) await this._loadModel();

    this.startedAt = Date.now();
    this._modeSince = this.startedAt;
    this._setState("ok");
    this.timer = setInterval(() => this._tick(), this.o.sampleMs);
    return true;
  }

  /** Load the model, whichever way it was exported.
   *
   *  tensorflowjs often cannot convert a Keras 3 .keras file to a LAYERS
   *  model, so export_tfjs.py falls back to SavedModel -> GRAPH model. The two
   *  need different loaders but both expose .predict(), so everything after
   *  this point is identical. Trying layers first and falling back means the
   *  export can take either path without this file changing.
   *
   *  The warm-up predict is not optional: the first call compiles WebGL
   *  shaders and takes 1-3 seconds. Doing it here, before the exam starts,
   *  keeps the first real frame from being slow enough to miss. */
  async _loadModel() {
    try {
      this.model = await tf.loadLayersModel(this.o.modelUrl);
      this.modelKind = "layers";
    } catch (e) {
      this.model = await tf.loadGraphModel(this.o.modelUrl);
      this.modelKind = "graph";
    }
    // Warm up (compiles WebGL shaders, 1-3s) and sanity-check in one go.
    //
    // A model fed doubly-normalised input sees an almost flat image whatever
    // the screen shows, so it returns nearly the same probabilities every
    // frame - and nothing about that looks like an error. Pushing a black
    // frame and a white frame through catches it immediately: if the two
    // answers barely differ, the input scaling is wrong.
    // Build the probes the SAME way a real frame is built - fill the canvas,
    // then tf.browser.fromPixels. Constructing them directly with tf.fill
    // takes a shortcut around the actual code path and does not give the same
    // answers, so a guard built that way tests something other than what runs.
    const probe = (colour) => {
      const ctx = this.canvas.getContext("2d");
      ctx.fillStyle = colour;
      ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
      return tf.tidy(() => {
        const x = tf.browser.fromPixels(this.canvas).toFloat().expandDims(0);
        let out = this.model.predict(x);
        if (Array.isArray(out)) out = out[0];
        else if (out && !out.dataSync) out = Object.values(out)[0];
        return Array.from(out.dataSync());
      });
    };

    const black = probe("#000000"), white = probe("#ffffff");
    const spread = Math.max(...black.map((v, i) => Math.abs(v - white[i])));
    this.probeSpread = spread;

    if (spread < 0.01) {
      console.warn(
        `[proctor] Model gives near-identical output for a black and a white ` +
        `frame (max difference ${spread.toFixed(5)}). The input scaling is ` +
        `almost certainly wrong - this model expects RAW 0-255 pixels ` +
        `because it normalises internally. See the comment in _classify().`);
    }
  }

  /* ------------------------------------------------------ invigilator mode */

  /** Poll the server for the mode the invigilator has set.
   *  Endpoint should return {"mode": "monitoring"|"permitted"|"paused",
   *                          "reason": "optional text"}
   *  Polling rather than WebSockets on purpose: PythonAnywhere's free tier
   *  does not support persistent sockets, and a 5s poll is ample here. */
  attachControl(url, intervalMs = 5000) {
    const poll = async () => {
      try {
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) return;
        const { mode, reason } = await r.json();
        if (mode && mode !== this.mode) this.setMode(mode, reason);
      } catch (_) { /* offline or server blip: keep the current mode */ }
    };
    poll();
    this.control = setInterval(poll, intervalMs);
  }

  /** Change mode. Every transition is recorded, with how long the previous
   *  mode lasted, so the report can prove what was monitored and when. */
  setMode(mode, reason = "") {
    if (!["monitoring", "permitted", "paused"].includes(mode)) return;
    if (mode === this.mode) return;

    const now = Date.now();
    const heldS = Math.round((now - this._modeSince) / 1000);
    if (this.mode === "permitted") this.permittedMs += now - this._modeSince;
    if (this.mode === "paused") this.pausedMs += now - this._modeSince;

    this._record(`mode_${mode}`, heldS, reason);
    this.mode = mode;
    this._modeSince = now;

    // Leaving a relaxed mode starts everyone clean, so a window that was
    // legitimately open must be closed now or it is flagged from scratch.
    this.detectedSince = null;
    this.warned = false;

    this._setState(mode === "monitoring" ? "ok" : mode);
  }

  /* ----------------------------------------------------------- inspection */
  async _tick() {
    if (this.mode === "paused") return;              // hard stop, no capture
    if (!this.video || this.video.readyState < 2) return;

    const { p, label } = await this._classify();
    this.frames += 1;
    const detected = p >= this.o.threshold;
    if (detected) {
      this.detections += 1;
      this.lastLabel = label;
    }

    const now = Date.now();

    // An allowlist lets an open-book paper permit `document` while still
    // flagging `ai_chat`. Empty means nothing is allowed.
    if (detected && this.o.allow?.includes(label)) {
      if (this.detectedSince) { this.detectedSince = null; this.warned = false; }
      if (this.state !== "flagged") this._setState("ok");
      return;
    }

    // Permitted period: log, but never warn or flag.
    if (this.mode === "permitted") {
      if (detected && !this.detectedSince) {
        this.detectedSince = now;
      } else if (!detected && this.detectedSince) {
        this._record("permitted_window_visible",
          Math.round((now - this.detectedSince) / 1000), this.lastLabel);
        this.detectedSince = null;
      }
      return;
    }

    if (!detected) {
      if (this.detectedSince && this.warned) {
        this._record("warning_cleared",
          Math.round((now - this.detectedSince) / 1000));
      }
      this.detectedSince = null;
      this.warned = false;
      if (this.state !== "flagged") this._setState("ok");
      return;
    }

    if (!this.detectedSince) this.detectedSince = now;
    const heldMs = now - this.detectedSince;

    if (heldMs >= this.o.flagAfterMs) {
      this._record("other_window_visible", Math.round(heldMs / 1000),
        this.lastLabel);
      this._setState("flagged");
      this.detectedSince = now;          // restart, so long runs log repeatedly
      this.warned = false;
    } else if (heldMs >= this.o.warnAfterMs && !this.warned) {
      this.warned = true;
      this._setState("warning");
      if (this.o.alarm) this._beep();
    }
  }

  /** Returns {p, label}: p = probability that ANYTHING is visible (1 - P(clean)),
   *  label = which category won, e.g. "ai_chat". With a binary model the label
   *  is simply "flagged". */
  async _classify() {
    const ctx = this.canvas.getContext("2d");
    // SQUASH the whole screen into 224x224, matching training exactly
    // (tf.image.resize with no aspect preservation). Do NOT centre-crop:
    // a 16:9 screen cropped to a centre square loses the left and right
    // thirds, which is precisely where a side-by-side window sits.
    ctx.drawImage(this.video, 0, 0, 224, 224);

    const probs = tf.tidy(() => {
      // RAW 0-255 PIXELS. Do not normalise here.
      //
      // The V4 model has the mobilenet_v2 preprocessing baked into its graph
      // as two op layers, x/127.5 then x-1.0, sitting between the
      // augmentation block and the backbone. Dividing again here would map
      // every pixel into roughly [-1, -0.992] - a flat grey rectangle - and
      // the model would return the same class on every frame while appearing
      // to work fine. eval_real.py feeds raw pixels, which is why it measured
      // 100%/100%; this has to match it exactly or the numbers mean nothing.
      //
      // Check before changing this: open the .keras file (it is a zip) and
      // look in config.json for TrueDivide / Subtract layers. If they are
      // absent, the model wants normalised input and this line needs
      // .div(127.5).sub(1) restored.
      const x = tf.browser.fromPixels(this.canvas).toFloat().expandDims(0);
      let out = this.model.predict(x);
      // A graph model can hand back an array or a named map instead of a
      // single tensor, depending on how the SavedModel was signed.
      if (Array.isArray(out)) out = out[0];
      else if (out && !out.dataSync) out = Object.values(out)[0];
      return Array.from(out.dataSync());
    });

    // Class 0 is always "clean" (train_screen.py guarantees the ordering).
    let best = 1, bestP = -1;
    for (let i = 1; i < probs.length; i++) {
      if (probs[i] > bestP) { bestP = probs[i]; best = i; }
    }
    return { p: 1 - probs[0], label: this.o.classes?.[best] ?? "flagged" };
  }

  /* --------------------------------------------------------------- output */
  _record(type, seconds, note = "") {
    const e = {
      type,
      seconds,
      note,
      mode: this.mode,
      at: new Date().toISOString(),
      elapsed: Math.round((Date.now() - this.startedAt) / 1000),
    };
    this.events.push(e);
    this.o.onEvent?.(e);
  }

  _setState(s) {
    if (this.state === s) return;
    this.state = s;
    this.o.onState?.(s);
  }

  _beep() {
    try {
      const ac = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ac.createOscillator(), gain = ac.createGain();
      osc.frequency.value = 660;
      gain.gain.setValueAtTime(0.0001, ac.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.25, ac.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + 0.5);
      osc.connect(gain).connect(ac.destination);
      osc.start();
      osc.stop(ac.currentTime + 0.5);
    } catch (_) { /* audio blocked; the visual warning still shows */ }
  }

  stop() {
    clearInterval(this.timer);
    clearInterval(this.control);
    this.timer = this.control = null;

    const now = Date.now();
    if (this.mode === "permitted") this.permittedMs += now - this._modeSince;
    if (this.mode === "paused") this.pausedMs += now - this._modeSince;

    this._stopStream();
    this._setState("stopped");

    const durationS = Math.round((now - this.startedAt) / 1000);
    const flagged = this.events.filter(
      (e) => e.type === "other_window_visible");
    return {
      events: this.events,
      stats: {
        duration_s: durationS,
        monitored_s: durationS
          - Math.round((this.permittedMs + this.pausedMs) / 1000),
        permitted_s: Math.round(this.permittedMs / 1000),
        paused_s: Math.round(this.pausedMs / 1000),
        frames_checked: this.frames,
        frames_detected: this.detections,
        confirmed_events: flagged.length,
        flagged_seconds: flagged.reduce((a, e) => a + e.seconds, 0),
      },
    };
  }

  _stopStream() {
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.video) { this.video.srcObject = null; this.video = null; }
  }
}
