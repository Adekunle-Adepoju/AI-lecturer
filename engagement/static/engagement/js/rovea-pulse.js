/* Rovea engagement pulse — first-party and tiny.
   Measures: ACTIVE seconds (tab visible + recent interaction), TTS playback
   seconds, TTS plays/finishes, and lecture parts delivered. */
(function () {
  "use strict";
  var el = document.currentScript;
  var ENDPOINT = el && el.dataset && el.dataset.endpoint;
  if (!ENDPOINT) return;

  var FLUSH_MS = 30000, DEFAULT_IDLE_MS = 60000, MAX_TTS_STEP_MS = 65000;
  var QKEY = "rovea:pulse-q", OPENKEY = "rovea:open";

  function lagosDay() {
    try { return new Date().toLocaleDateString("en-CA", { timeZone: "Africa/Lagos" }); }
    catch (e) { return new Date().toISOString().slice(0, 10); }
  }
  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    if (m && m.content) return m.content;
    var c = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return c ? decodeURIComponent(c[1]) : "";
  }
  function pageKey() {
    return (location.pathname.split("/")[1] || "home").toLowerCase()
      .replace(/[^a-z0-9_-]/g, "").slice(0, 30) || "home";
  }

  var now = function () { return performance.now(); };
  var sid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
          : String(Date.now()) + Math.random().toString(16).slice(2);
  var seq = 0, ctx = null, idleMs = DEFAULT_IDLE_MS;
  var lastTick = now(), lastActivity = lastTick;
  var activeMs = 0, ttsMs = 0, parts = 0, ttsPlays = 0, ttsDone = 0;
  var ttsToken = 0, ttsCurrent = null, ttsMark = 0;
  var openPending = true;
  try { openPending = localStorage.getItem(OPENKEY) !== lagosDay(); } catch (e) {}

  // ── activity + active-time accounting ─────────────────────────────
  function markActivity() { lastActivity = now(); }
  ["pointerdown", "keydown", "scroll", "touchstart", "wheel", "click"].forEach(function (evt) {
    window.addEventListener(evt, markActivity, { capture: true, passive: true });
  });

  function tick() {
    var t = now(), dt = t - lastTick; lastTick = t;
    if (document.visibilityState !== "visible") return;
    if (dt > 5000) return;                        // sleep/throttle gap: don't credit it
    if (ttsCurrent !== null || t - lastActivity <= idleMs) activeMs += dt;
  }
  setInterval(tick, 1000);

  // ── TTS accounting (speechSynthesis has no timeupdate) ────────────
  function accrueTts() {
    if (ttsCurrent === null) return;
    var t = now();
    ttsMs += Math.min(t - ttsMark, MAX_TTS_STEP_MS);
    ttsMark = t;
    // Some mobile browsers never fire onend; the engine state is the truth.
    if (window.speechSynthesis && !window.speechSynthesis.speaking) ttsCurrent = null;
  }
  function ttsStart() { accrueTts(); ttsCurrent = ++ttsToken; ttsMark = now(); ttsPlays++; return ttsCurrent; }
  function ttsStop(token, completed) {
    if (token !== ttsCurrent) return;             // stale event from a cancelled utterance
    ttsMs += Math.min(now() - ttsMark, MAX_TTS_STEP_MS);
    ttsCurrent = null;
    if (completed) ttsDone++;
    flush("tts");
  }

  // ── transport: beacon on unload, keepalive fetch otherwise, tiny retry queue ──
  function readQ() { try { return JSON.parse(localStorage.getItem(QKEY) || "[]"); } catch (e) { return []; } }
  function writeQ(q) { try { localStorage.setItem(QKEY, JSON.stringify(q.slice(-30))); } catch (e) {} }
  function enqueue(payload) { var q = readQ(); q.push(payload); writeQ(q); }

  function post(payload, unloading) {
    var fd = new FormData();
    fd.append("csrfmiddlewaretoken", csrf());
    fd.append("payload", JSON.stringify(payload));
    if (unloading && navigator.sendBeacon && navigator.sendBeacon(ENDPOINT, fd)) return;
    fetch(ENDPOINT, { method: "POST", body: fd, keepalive: true, credentials: "same-origin" })
      .then(function (r) { if (r.status >= 500) throw new Error("5xx"); })
      .catch(function () { enqueue(payload); });   // offline / server hiccup: retry later
  }
  function drainQueue() {
    var q = readQ(); if (!q.length) return;
    writeQ([]);
    q.forEach(function (p) { post(p, false); });
  }

  function flush(reason, unloading) {
    tick(); accrueTts();
    var a = Math.floor(activeMs / 1000), t = Math.floor(ttsMs / 1000);
    if (!a && !t && !parts && !ttsPlays && !ttsDone && !openPending) return;
    var payload = {
      v: 1, sid: sid, n: ++seq, page: pageKey(),
      ts: ctx ? ctx.topicSessionId : null,
      active: a, tts: t, plays: ttsPlays, done: ttsDone, parts: parts
    };
    activeMs -= a * 1000; ttsMs -= t * 1000; parts = ttsPlays = ttsDone = 0;
    if (openPending) {
      openPending = false;
      try { localStorage.setItem(OPENKEY, lagosDay()); } catch (e) {}
    }
    post(payload, !!unloading);
    if (!unloading) drainQueue();
  }

  setInterval(function () { flush("interval"); }, FLUSH_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flush("hidden", true);
    else { lastTick = now(); markActivity(); }
  });
  window.addEventListener("pagehide", function () { flush("pagehide", true); });
  window.addEventListener("pageshow", function (e) { if (e.persisted) { lastTick = now(); markActivity(); } });

  window.RoveaTrack = {
    setContext: function (c) {
      var id = c && c.topicSessionId ? Number(c.topicSessionId) : null;
      if (ctx && ctx.topicSessionId !== id) flush("ctx");
      ctx = id ? { topicSessionId: id } : null;
      idleMs = (c && c.idleMs) || DEFAULT_IDLE_MS;
    },
    part: function () { parts++; },
    ttsStart: ttsStart,
    ttsStop: ttsStop,
    flush: flush
  };
  drainQueue();
})();