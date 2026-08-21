/* LectureLab frontend — vanilla JS, no build step. */
"use strict";

// ---------------------------------------------------------------------------
// State & helpers
// ---------------------------------------------------------------------------

const state = {
  config: { has_api_key: false, whisper_model: "?" },
  courses: [],
  expandedCourses: new Set(JSON.parse(localStorage.getItem("ll_expanded") || "[]")),
  currentLectureId: null,
  currentLecture: null,
  activeTab: "record",
  pendingSeek: null, // seconds to seek audio to when transcript tab opens
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
}

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    delete opts.json;
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer = null;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), isError ? 6000 : 3000);
}

// Simple promise-based modal with a text input or textarea.
function prompt2(title, { placeholder = "", value = "", multiline = false, okLabel = "OK" } = {}) {
  return new Promise(resolve => {
    const root = $("#modal-root");
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = multiline
      ? `<textarea id="modal-input" placeholder="${esc(placeholder)}">${esc(value)}</textarea>`
      : `<input id="modal-input" type="text" placeholder="${esc(placeholder)}" value="${esc(value)}">`;
    $("#modal-ok").textContent = okLabel;
    root.classList.remove("hidden");
    const input = $("#modal-input");
    input.focus();
    if (!multiline) input.select();
    const close = (val) => {
      root.classList.add("hidden");
      $("#modal-ok").onclick = $("#modal-cancel").onclick = input.onkeydown = null;
      resolve(val);
    };
    $("#modal-ok").onclick = () => close(input.value);
    $("#modal-cancel").onclick = () => close(null);
    input.onkeydown = e => {
      if (e.key === "Enter" && !multiline) close(input.value);
      if (e.key === "Escape") close(null);
    };
  });
}

function confirm2(title, message, okLabel = "Delete") {
  return new Promise(resolve => {
    const root = $("#modal-root");
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = `<p>${esc(message)}</p>`;
    $("#modal-ok").textContent = okLabel;
    root.classList.remove("hidden");
    const close = (val) => {
      root.classList.add("hidden");
      $("#modal-ok").onclick = $("#modal-cancel").onclick = null;
      resolve(val);
    };
    $("#modal-ok").onclick = () => close(true);
    $("#modal-cancel").onclick = () => close(false);
  });
}

function renderMath(el) {
  if (window.renderMathInElement) {
    renderMathInElement(el, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true },
      ],
      throwOnError: false,
    });
  }
}

function md(text) {
  return marked.parse(text || "", { breaks: false });
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function applyTheme() {
  const dark = localStorage.getItem("ll_theme") === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("#theme-toggle").textContent = dark ? "☀️" : "🌙";
}
$("#theme-toggle").onclick = () => {
  const dark = localStorage.getItem("ll_theme") === "dark";
  localStorage.setItem("ll_theme", dark ? "light" : "dark");
  applyTheme();
};

// ---------------------------------------------------------------------------
// Sidebar: courses & lectures
// ---------------------------------------------------------------------------

async function loadCourses() {
  state.courses = await api("/api/courses");
  renderSidebar();
}

function renderSidebar() {
  const list = $("#course-list");
  if (!state.courses.length) {
    list.innerHTML = `<div class="subtle" style="padding:8px 10px">No courses yet — create one to get started.</div>`;
    return;
  }
  list.innerHTML = state.courses.map(c => {
    const open = state.expandedCourses.has(c.id);
    const lectures = open ? `
      <div class="lecture-list">
        ${c.lectures.map(l => `
          <div class="lecture-item ${l.id === state.currentLectureId ? "active" : ""}" data-lecture="${l.id}">
            <span>${esc(l.title)}</span>
            <span class="lstatus">${statusLabel(l.status)}</span>
          </div>`).join("")}
        <button class="new-lecture-btn" data-newlecture="${c.id}">+ New lecture</button>
      </div>` : "";
    return `
      <div class="course-item">
        <div class="course-header" data-course="${c.id}">
          <span class="chev">${open ? "▼" : "▶"}</span>
          <span class="cname" title="${esc(c.name)}">${esc(c.name)}</span>
          <button class="icon-btn mini" data-vocab="${c.id}" title="Custom vocabulary">📖</button>
          <button class="icon-btn mini" data-rename="${c.id}" title="Rename">✏️</button>
          <button class="icon-btn mini" data-delcourse="${c.id}" title="Delete course">🗑</button>
        </div>
        ${lectures}
      </div>`;
  }).join("");

  list.onclick = async (e) => {
    const t = e.target.closest("[data-course],[data-lecture],[data-newlecture],[data-rename],[data-delcourse],[data-vocab]");
    if (!t) return;
    if (t.dataset.vocab) { e.stopPropagation(); return editVocabulary(+t.dataset.vocab); }
    if (t.dataset.rename) {
      e.stopPropagation();
      const c = state.courses.find(x => x.id === +t.dataset.rename);
      const name = await prompt2("Rename course", { value: c.name });
      if (name && name.trim()) { await api(`/api/courses/${c.id}`, { method: "PUT", json: { name } }); loadCourses(); }
      return;
    }
    if (t.dataset.delcourse) {
      e.stopPropagation();
      const c = state.courses.find(x => x.id === +t.dataset.delcourse);
      if (await confirm2("Delete course", `Delete “${c.name}” and all its lectures, audio, and notes? This cannot be undone.`)) {
        await api(`/api/courses/${c.id}`, { method: "DELETE" });
        if (state.currentLecture && state.currentLecture.course_id === c.id) showWelcome();
        loadCourses();
      }
      return;
    }
    if (t.dataset.newlecture) {
      const title = await prompt2("New lecture", { placeholder: "e.g. Week 5 — NPV and IRR", okLabel: "Create" });
      if (title === null) return;
      const lec = await api(`/api/courses/${t.dataset.newlecture}/lectures`, { method: "POST", json: { title: title || "Untitled lecture" } });
      await loadCourses();
      openLecture(lec.id, "record");
      return;
    }
    if (t.dataset.lecture) return openLecture(+t.dataset.lecture);
    if (t.dataset.course) {
      const id = +t.dataset.course;
      state.expandedCourses.has(id) ? state.expandedCourses.delete(id) : state.expandedCourses.add(id);
      localStorage.setItem("ll_expanded", JSON.stringify([...state.expandedCourses]));
      renderSidebar();
    }
  };
}

function statusLabel(s) {
  return { new: "", recorded: "🎙", transcribing: "⏳", transcribed: "📄", error: "⚠️" }[s] || "";
}

$("#add-course-btn").onclick = async () => {
  const name = await prompt2("New course", { placeholder: "e.g. EIN 3354 Engineering Economy", okLabel: "Create" });
  if (name && name.trim()) {
    const c = await api("/api/courses", { method: "POST", json: { name } });
    state.expandedCourses.add(c.id);
    localStorage.setItem("ll_expanded", JSON.stringify([...state.expandedCourses]));
    loadCourses();
  }
};

async function editVocabulary(courseId) {
  const { vocabulary } = await api(`/api/courses/${courseId}/vocabulary`);
  const v = await prompt2("Custom vocabulary", {
    value: vocabulary, multiline: true, okLabel: "Save",
    placeholder: "One term per line — technical terms Whisper tends to mis-hear, so you remember to double-check them in transcripts.",
  });
  if (v !== null) {
    await api(`/api/courses/${courseId}/vocabulary`, { method: "PUT", json: { vocabulary: v } });
    toast("Vocabulary saved");
  }
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

function showWelcome() {
  stopRecorderCleanup();
  state.currentLectureId = null;
  state.currentLecture = null;
  $("#content").innerHTML = `
    <div class="empty-state">
      <div class="big">🎓</div>
      <h2>Welcome to LectureLab</h2>
      <p>Create a course in the sidebar, add a lecture, and hit record.<br>
      Transcription runs locally with Whisper; study notes, flashcards, and quizzes are generated from the transcript.</p>
    </div>`;
  renderSidebar();
}

async function openLecture(id, tab) {
  stopRecorderCleanup();
  state.currentLectureId = id;
  try {
    state.currentLecture = await api(`/api/lectures/${id}`);
  } catch (err) {
    toast(err.message, true);
    return showWelcome();
  }
  const lec = state.currentLecture;
  state.activeTab = tab || defaultTab(lec);
  renderLecture();
  renderSidebar();
}

function defaultTab(lec) {
  if (lec.notes) return "notes";
  if (lec.transcript) return "transcript";
  return "record";
}

function renderLecture() {
  const lec = state.currentLecture;
  const tabs = [
    ["record", "🎙 Record"],
    ["transcript", "📄 Transcript"],
    ["notes", "📚 Notes"],
    ["flashcards", "🃏 Flashcards"],
    ["quiz", "✅ Quiz"],
  ];
  $("#content").innerHTML = `
    <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap">
      <h2 id="lec-title" style="cursor:pointer" title="Click to rename">${esc(lec.title)}</h2>
      <span class="subtle">${esc(lec.course_name)}${lec.duration_sec ? " · " + fmtTime(lec.duration_sec) : ""}</span>
      <button class="icon-btn" id="del-lecture" title="Delete lecture" style="margin-left:auto">🗑</button>
    </div>
    <div class="tabs">
      ${tabs.map(([k, label]) => `<button class="tab ${k === state.activeTab ? "active" : ""}" data-tab="${k}">${label}</button>`).join("")}
    </div>
    <div id="tab-body"></div>`;

  $("#lec-title").onclick = async () => {
    const title = await prompt2("Rename lecture", { value: lec.title });
    if (title && title.trim()) {
      await api(`/api/lectures/${lec.id}`, { method: "PUT", json: { title } });
      lec.title = title.trim();
      renderLecture(); loadCourses();
    }
  };
  $("#del-lecture").onclick = async () => {
    if (await confirm2("Delete lecture", `Delete “${lec.title}” including its audio, transcript, and notes?`)) {
      await api(`/api/lectures/${lec.id}`, { method: "DELETE" });
      showWelcome(); loadCourses();
    }
  };
  $$(".tab").forEach(t => t.onclick = () => {
    stopRecorderCleanup();
    state.activeTab = t.dataset.tab;
    $$(".tab").forEach(x => x.classList.toggle("active", x === t));
    renderTab();
  });
  renderTab();
}

function renderTab() {
  const body = $("#tab-body");
  const fn = { record: renderRecordTab, transcript: renderTranscriptTab,
               notes: renderNotesTab, flashcards: renderFlashcardsTab,
               quiz: renderQuizTab }[state.activeTab];
  fn(body);
}

// ---------------------------------------------------------------------------
// Record tab
// ---------------------------------------------------------------------------

const rec = {
  mediaRecorder: null, stream: null, chunks: [], markers: [],
  startedAt: 0, pausedTotal: 0, pauseStart: 0, timerInterval: null,
  audioCtx: null, levelRaf: null, uploading: false,
};

function recElapsed() {
  if (!rec.startedAt) return 0;
  const pausedNow = rec.pauseStart ? Date.now() - rec.pauseStart : 0;
  return (Date.now() - rec.startedAt - rec.pausedTotal - pausedNow) / 1000;
}

function stopRecorderCleanup() {
  if (rec.mediaRecorder && rec.mediaRecorder.state !== "inactive") {
    try { rec.mediaRecorder.stop(); } catch { /* already stopped */ }
  }
  if (rec.stream) rec.stream.getTracks().forEach(t => t.stop());
  if (rec.audioCtx) { try { rec.audioCtx.close(); } catch { /* ok */ } }
  cancelAnimationFrame(rec.levelRaf);
  clearInterval(rec.timerInterval);
  Object.assign(rec, { mediaRecorder: null, stream: null, chunks: [], markers: [],
    startedAt: 0, pausedTotal: 0, pauseStart: 0, audioCtx: null, uploading: false });
}

function renderRecordTab(body) {
  const lec = state.currentLecture;
  const hasAudio = !!lec.audio_file;
  body.innerHTML = `
    <div class="card rec-panel">
      ${hasAudio ? `<div class="banner" style="margin-bottom:16px">This lecture already has a recording${lec.transcript ? " and a transcript" : ""}. Recording again will replace it.</div>` : ""}
      <div class="rec-state" id="rec-state"></div>
      <div class="rec-timer" id="rec-timer">0:00</div>
      <div class="level-meter"><div class="level-fill" id="level-fill"></div></div>
      <div class="rec-controls">
        <button class="record-btn" id="record-btn">Record</button>
        <button class="btn" id="pause-btn" disabled>Pause</button>
      </div>
      <div class="marker-btns">
        <button class="btn" id="mark-important" disabled>⭐ Important</button>
        <button class="btn" id="mark-confused" disabled>❓ Confused — revisit</button>
        <button class="btn" id="mark-examhint" disabled>📝 Exam hint</button>
      </div>
      <div class="marker-log" id="marker-log"></div>
      <div id="upload-status"></div>
    </div>`;

  $("#record-btn").onclick = toggleRecording;
  $("#pause-btn").onclick = togglePause;
  const addMarker = (kind, label) => {
    if (!rec.mediaRecorder || rec.mediaRecorder.state === "inactive") return;
    rec.markers.push({ kind, offset_sec: recElapsed() });
    const log = $("#marker-log");
    log.innerHTML = `<div>[${fmtTime(recElapsed())}] ${label}</div>` + log.innerHTML;
  };
  $("#mark-important").onclick = () => addMarker("important", "⭐ Important");
  $("#mark-confused").onclick = () => addMarker("confused", "❓ Confused — revisit");
  $("#mark-examhint").onclick = () => addMarker("examhint", "📝 Exam hint");
}

async function toggleRecording() {
  const btn = $("#record-btn");
  if (rec.uploading) return;
  if (rec.mediaRecorder && rec.mediaRecorder.state !== "inactive") {
    // Stop: onstop handler uploads.
    btn.disabled = true;
    rec.mediaRecorder.stop();
    return;
  }
  // Start
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    $("#upload-status").innerHTML = `
      <div class="banner error" style="margin-top:16px;text-align:left">
        <b>Microphone access failed.</b> ${esc(err.name === "NotAllowedError"
          ? "Permission was denied. Click the microphone icon in your browser's address bar, allow access, then try again."
          : err.message)}
      </div>`;
    return;
  }
  rec.stream = stream;
  rec.chunks = []; rec.markers = [];
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus"
             : MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "";
  rec.mediaRecorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  rec.mediaRecorder.ondataavailable = e => { if (e.data.size) rec.chunks.push(e.data); };
  rec.mediaRecorder.onstop = onRecordingStopped;
  rec.mediaRecorder.start(1000);
  rec.startedAt = Date.now(); rec.pausedTotal = 0; rec.pauseStart = 0;

  // UI
  btn.textContent = "Stop"; btn.classList.add("recording");
  $("#pause-btn").disabled = false;
  ["#mark-important", "#mark-confused", "#mark-examhint"].forEach(s => $(s).disabled = false);
  $("#rec-state").innerHTML = `<span class="rec-dot"></span> RECORDING`;
  rec.timerInterval = setInterval(() => {
    const el = $("#rec-timer"); if (el) el.textContent = fmtTime(recElapsed());
  }, 250);

  // Level meter
  rec.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const source = rec.audioCtx.createMediaStreamSource(stream);
  const analyser = rec.audioCtx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const data = new Uint8Array(analyser.frequencyBinCount);
  const tick = () => {
    analyser.getByteTimeDomainData(data);
    let peak = 0;
    for (const v of data) peak = Math.max(peak, Math.abs(v - 128));
    const fill = $("#level-fill");
    if (fill) fill.style.width = Math.min(100, (peak / 128) * 160) + "%";
    rec.levelRaf = requestAnimationFrame(tick);
  };
  tick();
}

function togglePause() {
  const mr = rec.mediaRecorder;
  if (!mr) return;
  const btn = $("#pause-btn");
  if (mr.state === "recording") {
    mr.pause(); rec.pauseStart = Date.now();
    btn.textContent = "Resume";
    $("#rec-state").innerHTML = `<span style="color:var(--muted)">⏸ PAUSED</span>`;
  } else if (mr.state === "paused") {
    mr.resume();
    rec.pausedTotal += Date.now() - rec.pauseStart; rec.pauseStart = 0;
    btn.textContent = "Pause";
    $("#rec-state").innerHTML = `<span class="rec-dot"></span> RECORDING`;
  }
}

async function onRecordingStopped() {
  const duration = recElapsed();
  const markers = rec.markers.slice();
  const blob = new Blob(rec.chunks, { type: rec.mediaRecorder.mimeType || "audio/webm" });
  const lecId = state.currentLectureId;

  // Tear down mic/meter but keep UI until upload completes.
  if (rec.stream) rec.stream.getTracks().forEach(t => t.stop());
  if (rec.audioCtx) { try { rec.audioCtx.close(); } catch { /* ok */ } }
  cancelAnimationFrame(rec.levelRaf);
  clearInterval(rec.timerInterval);
  rec.uploading = true;

  const statusEl = $("#upload-status");
  const setStatus = html => { if (statusEl) statusEl.innerHTML = html; };
  $("#rec-state") && ($("#rec-state").innerHTML = "");
  const btn = $("#record-btn");
  if (btn) { btn.classList.remove("recording"); btn.textContent = "Record"; btn.disabled = true; }
  ["#pause-btn", "#mark-important", "#mark-confused", "#mark-examhint"]
    .forEach(s => { const el = $(s); if (el) el.disabled = true; });

  if (!blob.size) {
    setStatus(`<div class="banner error" style="margin-top:14px">Nothing was recorded — the microphone produced no audio.</div>`);
    rec.uploading = false; if (btn) btn.disabled = false;
    return;
  }

  try {
    setStatus(progressHtml("Uploading recording…", null));
    const fd = new FormData();
    fd.append("audio", blob, "lecture.webm");
    fd.append("duration_sec", String(duration));
    fd.append("markers", JSON.stringify(markers));
    await api(`/api/lectures/${lecId}/audio`, { method: "POST", body: fd });
    await api(`/api/lectures/${lecId}/transcribe`, { method: "POST" });
    await pollTranscription(lecId, setStatus);
  } catch (err) {
    setStatus(`<div class="banner error" style="margin-top:14px">Upload failed: ${esc(err.message)}</div>`);
    rec.uploading = false; if (btn) btn.disabled = false;
  }
}

function progressHtml(message, fraction) {
  const pct = fraction == null ? 0 : Math.round(fraction * 100);
  return `
    <div class="progress-wrap">
      <p class="subtle" style="margin-bottom:6px">${esc(message)}</p>
      <div class="progress-bar ${fraction == null ? "indeterminate" : ""}">
        <div class="progress-fill" style="width:${pct}%"></div>
      </div>
      ${fraction != null ? `<p class="subtle">${pct}%</p>` : ""}
    </div>`;
}

async function pollTranscription(lecId, setStatus) {
  for (;;) {
    let st;
    try { st = await api(`/api/lectures/${lecId}/transcribe/status`); }
    catch { await new Promise(r => setTimeout(r, 2000)); continue; }
    if (st.status === "done" || st.status === "transcribed") {
      setStatus("");
      toast("Transcription complete");
      if (state.currentLectureId === lecId) await openLecture(lecId, "transcript");
      loadCourses();
      return;
    }
    if (st.status === "error") {
      setStatus(`<div class="banner error" style="margin-top:14px">${esc(st.message || "Transcription failed.")}
        <button class="btn mini" onclick="retryTranscribe(${lecId})" style="margin-left:8px">Retry</button></div>`);
      loadCourses();
      return;
    }
    setStatus(progressHtml(
      st.status === "loading_model" ? (st.message || "Loading Whisper model…")
        : "Transcribing locally with Whisper — this can take a while for long lectures…",
      st.status === "transcribing" ? st.progress : null));
    await new Promise(r => setTimeout(r, 1500));
  }
}

window.retryTranscribe = async (lecId) => {
  try {
    await api(`/api/lectures/${lecId}/transcribe`, { method: "POST" });
    const statusEl = $("#upload-status") || $("#transcript-status");
    await pollTranscription(lecId, html => { if (statusEl) statusEl.innerHTML = html; });
  } catch (err) { toast(err.message, true); }
};

// ---------------------------------------------------------------------------
// Transcript tab
// ---------------------------------------------------------------------------

function renderTranscriptTab(body) {
  const lec = state.currentLecture;
  if (!lec.transcript) {
    if (lec.status === "transcribing") {
      body.innerHTML = `<div class="card"><div id="transcript-status">${progressHtml("Transcription in progress…", null)}</div></div>`;
      pollTranscription(lec.id, html => { const el = $("#transcript-status"); if (el) el.innerHTML = html; });
      return;
    }
    if (lec.audio_file) {
      body.innerHTML = `
        <div class="card" style="text-align:center">
          <p>Audio is recorded but not transcribed yet${lec.error ? ` — last attempt failed: ${esc(lec.error)}` : ""}.</p>
          <div id="transcript-status"></div>
          <button class="btn primary" onclick="retryTranscribe(${lec.id})">Transcribe now</button>
        </div>`;
      return;
    }
    body.innerHTML = `<div class="empty-state"><div class="big">📄</div><p>No transcript yet — record this lecture first.</p></div>`;
    return;
  }

  const markersBySegment = {};
  for (const m of lec.markers) {
    // Attach each marker to the first segment starting at/after its offset.
    let target = null;
    for (const [i, s] of lec.transcript.entries()) {
      if (s.start >= m.offset_sec - 2) { target = i; break; }
    }
    if (target === null) target = lec.transcript.length - 1;
    (markersBySegment[target] = markersBySegment[target] || []).push(m);
  }
  const markerLabel = { important: "⭐ Important", confused: "❓ Confused — revisit", examhint: "📝 Exam hint" };

  body.innerHTML = `
    <div class="card">
      ${lec.audio_file ? `<audio id="audio-player" controls preload="metadata" src="/api/lectures/${lec.id}/audio"></audio>` : ""}
      <div class="transcript-tools">
        <button class="btn" id="edit-transcript-btn">✏️ Edit transcript</button>
        <span class="subtle" style="align-self:center">Click a timestamp to jump the audio. Fix technical terms before generating notes.</span>
      </div>
      <div id="transcript-list">
        ${lec.transcript.map((s, i) => `
          <div class="seg ${markersBySegment[i] ? "highlight" : ""}" data-i="${i}">
            ${(markersBySegment[i] || []).map(m => `<span class="marker-chip ${m.kind}" title="Marked at ${fmtTime(m.offset_sec)}">${markerLabel[m.kind]}</span>`).join(" ")}
            <span class="ts" data-t="${s.start}">${fmtTime(s.start)}</span>
            <span class="stext">${esc(s.text)}</span>
          </div>`).join("")}
      </div>
    </div>`;

  const player = $("#audio-player");
  $("#transcript-list").onclick = e => {
    const ts = e.target.closest(".ts");
    if (ts && player) { player.currentTime = +ts.dataset.t; player.play(); }
  };
  if (state.pendingSeek != null && player) {
    const t = state.pendingSeek; state.pendingSeek = null;
    const seek = () => { player.currentTime = t; };
    player.readyState >= 1 ? seek() : player.addEventListener("loadedmetadata", seek, { once: true });
    const seg = $$(`#transcript-list .seg`).find(el => {
      const tsEl = $(".ts", el); return tsEl && +tsEl.dataset.t >= t - 1;
    });
    if (seg) seg.scrollIntoView({ block: "center" });
  }

  $("#edit-transcript-btn").onclick = () => renderTranscriptEditor(body);
}

function renderTranscriptEditor(body) {
  const lec = state.currentLecture;
  body.innerHTML = `
    <div class="card">
      <div class="transcript-tools">
        <button class="btn primary" id="save-transcript">Save changes</button>
        <button class="btn ghost" id="cancel-edit">Cancel</button>
        <span class="subtle" style="align-self:center">Edit any segment's text; empty segments are removed.</span>
      </div>
      ${lec.transcript.map((s, i) => `
        <div class="seg">
          <span class="ts">${fmtTime(s.start)}</span>
          <textarea class="seg-edit" data-i="${i}" rows="2">${esc(s.text)}</textarea>
        </div>`).join("")}
    </div>`;
  $("#cancel-edit").onclick = () => renderTranscriptTab(body);
  $("#save-transcript").onclick = async () => {
    const segments = lec.transcript.map((s, i) => ({
      start: s.start, end: s.end,
      text: $(`.seg-edit[data-i="${i}"]`).value.trim(),
    })).filter(s => s.text);
    try {
      await api(`/api/lectures/${lec.id}/transcript`, { method: "PUT", json: { segments } });
      lec.transcript = segments;
      toast("Transcript saved");
      renderTranscriptTab(body);
    } catch (err) { toast(err.message, true); }
  };
}

// ---------------------------------------------------------------------------
// Notes tab
// ---------------------------------------------------------------------------

function renderNotesTab(body) {
  const lec = state.currentLecture;
  if (!lec.transcript) {
    body.innerHTML = `<div class="empty-state"><div class="big">📚</div><p>Record and transcribe this lecture first — then generate study notes here.</p></div>`;
    return;
  }
  if (!lec.notes) {
    body.innerHTML = `
      <div class="card" style="text-align:center">
        <p>No notes yet for this lecture.</p>
        ${generateButtonsHtml()}
        <div id="notes-status"></div>
      </div>`;
    wireGenerateButtons(body);
    return;
  }

  const n = lec.notes;
  body.innerHTML = `
    <div class="pill-row">
      <button class="btn" id="regen-notes">↻ Regenerate</button>
      <button class="btn" onclick="window.open('/api/lectures/${lec.id}/export.md')">⬇ Export Markdown</button>
      <button class="btn" onclick="window.print()">🖨 Print</button>
      <span class="subtle" style="align-self:center">${n.mode === "ai" ? "AI-generated" : "Generated without AI (structured fallback)"} · ${new Date(n.created_at * 1000).toLocaleString()}</span>
    </div>
    <div id="notes-status"></div>

    ${n.summary ? `<div class="card"><h3 style="margin-top:0">Summary</h3><p>${esc(n.summary)}</p></div>` : ""}

    ${n.outline && n.outline.length ? `
    <details class="section card" open><summary>Outline</summary>
      <ul class="outline-list">${n.outline.map(o => `
        <li><b>${esc(o.topic)}</b>${o.subtopics && o.subtopics.length ? `<ul>${o.subtopics.map(s => `<li>${esc(s)}</li>`).join("")}</ul>` : ""}</li>`).join("")}
      </ul></details>` : ""}

    <div class="card"><div class="notes-md" id="notes-md"></div></div>

    ${n.key_terms && n.key_terms.length ? `
    <details class="section card" open><summary>Key terms (${n.key_terms.length})</summary>
      ${n.key_terms.map(t => `<div class="term-item"><b>${esc(t.term)}</b> — <span class="term-def">${esc(t.definition)}</span></div>`).join("")}
    </details>` : ""}

    ${n.exam_signals && n.exam_signals.length ? `
    <details class="section card" open><summary>📝 Exam signals (${n.exam_signals.length})</summary>
      <ul>${n.exam_signals.map(s => `<li>${esc(s)}</li>`).join("")}</ul>
    </details>` : ""}

    ${n.confusions_addressed && n.confusions_addressed.length ? `
    <details class="section card" open><summary>❓ Confusions addressed (${n.confusions_addressed.length})</summary>
      ${n.confusions_addressed.map(c => `<div class="term-item"><b>[${esc(c.timestamp)}]</b> ${esc(c.explanation)}</div>`).join("")}
    </details>` : ""}`;

  $("#notes-md").innerHTML = md(n.notes);
  renderMath($("#notes-md"));
  $$(".term-def").forEach(renderMath);

  $("#regen-notes").onclick = () => {
    const statusEl = $("#notes-status");
    statusEl.innerHTML = `<div class="card" style="text-align:center">${generateButtonsHtml("Regenerate with:")}</div>`;
    wireGenerateButtons(statusEl);
  };
}

function generateButtonsHtml(label = "Generate study notes:") {
  const hasKey = state.config.has_api_key;
  return `
    <p class="subtle">${label}</p>
    <div style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap">
      <button class="btn primary" data-gen="ai" ${hasKey ? "" : "disabled title='Set ANTHROPIC_API_KEY to enable'"}>✨ AI notes (Claude)</button>
      <button class="btn" data-gen="basic">🧱 Structured notes (no AI)</button>
    </div>
    ${hasKey ? "" : `<p class="subtle" style="margin-top:8px">AI notes need <code>ANTHROPIC_API_KEY</code> — the no-AI generator still works.</p>`}`;
}

function wireGenerateButtons(root) {
  $$("[data-gen]", root).forEach(btn => btn.onclick = () => generateNotes(btn.dataset.gen));
}

async function generateNotes(mode) {
  const lec = state.currentLecture;
  const statusEl = $("#notes-status") || $("#tab-body");
  statusEl.innerHTML = progressHtml(mode === "ai"
    ? "Asking Claude to build your study notes — this usually takes under a minute…"
    : "Structuring notes from the transcript…", null);
  $$("[data-gen]").forEach(b => b.disabled = true);
  try {
    const notes = await api(`/api/lectures/${lec.id}/notes`, { method: "POST", json: { mode } });
    lec.notes = { ...notes, created_at: Date.now() / 1000 };
    toast("Notes generated");
    if (state.currentLectureId === lec.id && state.activeTab === "notes") renderTab();
  } catch (err) {
    statusEl.innerHTML = `
      <div class="banner error" style="margin-top:14px">
        ${esc(err.message)}
        <button class="btn mini" id="retry-notes" style="margin-left:10px">Retry</button>
      </div>`;
    $("#retry-notes").onclick = () => generateNotes(mode);
    $$("[data-gen]").forEach(b => b.disabled = b.dataset.gen === "ai" && !state.config.has_api_key);
  }
}

// ---------------------------------------------------------------------------
// Flashcards tab
// ---------------------------------------------------------------------------

const flash = { queue: [], index: 0, flipped: false, done: 0 };

async function renderFlashcardsTab(body) {
  const lec = state.currentLecture;
  let data;
  try { data = await api(`/api/lectures/${lec.id}/flashcards`); }
  catch (err) { body.innerHTML = `<div class="banner error">${esc(err.message)}</div>`; return; }
  const all = [...data.due, ...data.later];
  if (!all.length) {
    body.innerHTML = `<div class="empty-state"><div class="big">🃏</div><p>No flashcards yet — generate notes first and cards will be created automatically.</p></div>`;
    return;
  }
  flash.queue = data.due.length ? data.due : all;
  flash.index = 0; flash.flipped = false; flash.done = 0;
  body.innerHTML = `
    <p class="subtle" style="text-align:center">
      ${data.due.length ? `${data.due.length} card(s) due for review` : `Nothing due — reviewing all ${all.length} cards`}
      · space or click to flip · <b>G</b> got it / <b>M</b> missed it
    </p>
    <div class="flash-stage">
      <div class="flashcard" id="flashcard">
        <div class="flash-face front" id="flash-front"></div>
        <div class="flash-face back" id="flash-back"></div>
      </div>
      <div class="flash-actions">
        <button class="btn" id="flash-missed">✗ Missed it</button>
        <button class="btn primary" id="flash-got">✓ Got it</button>
      </div>
      <div class="flash-meta" id="flash-meta"></div>
    </div>`;
  $("#flashcard").onclick = flipCard;
  $("#flash-got").onclick = () => answerCard("got");
  $("#flash-missed").onclick = () => answerCard("missed");
  showCard();
}

function showCard() {
  const card = flash.queue[flash.index];
  if (!card) {
    $("#tab-body").innerHTML = `
      <div class="empty-state"><div class="big">🎉</div>
      <h3>Review session complete</h3><p>You reviewed ${flash.done} card(s). Missed cards will come back sooner.</p>
      <button class="btn primary" id="flash-again">Review again</button></div>`;
    $("#flash-again").onclick = () => renderFlashcardsTab($("#tab-body"));
    return;
  }
  flash.flipped = false;
  $("#flashcard").classList.remove("flipped");
  $("#flash-front").innerHTML = md(card.front);
  $("#flash-back").innerHTML = md(card.back);
  renderMath($("#flash-front")); renderMath($("#flash-back"));
  $("#flash-meta").textContent = `Card ${flash.index + 1} of ${flash.queue.length} · box ${card.box}`;
}

function flipCard() {
  flash.flipped = !flash.flipped;
  $("#flashcard").classList.toggle("flipped", flash.flipped);
}

async function answerCard(result) {
  const card = flash.queue[flash.index];
  if (!card) return;
  try { await api(`/api/flashcards/${card.id}/review`, { method: "POST", json: { result } }); }
  catch (err) { toast(err.message, true); }
  flash.done++;
  flash.index++;
  showCard();
}

// ---------------------------------------------------------------------------
// Quiz tab
// ---------------------------------------------------------------------------

const quiz = { questions: [], index: 0, score: 0, answered: false };

function renderQuizTab(body) {
  const lec = state.currentLecture;
  const questions = (lec.notes && lec.notes.quiz) || [];
  const valid = questions.filter(q => q && q.question && Array.isArray(q.options)
    && q.options.length >= 2 && Number.isInteger(q.answer));
  if (!valid.length) {
    body.innerHTML = `<div class="empty-state"><div class="big">✅</div>
      <p>No quiz for this lecture yet.${lec.notes && lec.notes.mode === "basic"
        ? " Quizzes need AI-generated notes (the no-AI generator doesn't write questions)."
        : " Generate notes first — a quiz comes with them."}</p></div>`;
    return;
  }
  quiz.questions = valid; quiz.index = 0; quiz.score = 0; quiz.answered = false;
  body.innerHTML = `
    <div class="card" id="quiz-card"></div>
    <div id="quiz-history"></div>`;
  showQuestion();
  loadQuizHistory();
}

function showQuestion() {
  const card = $("#quiz-card");
  const q = quiz.questions[quiz.index];
  if (!q) return showQuizScore();
  quiz.answered = false;
  card.innerHTML = `
    <p class="subtle">Question ${quiz.index + 1} of ${quiz.questions.length} · score ${quiz.score} · press 1–4 to answer</p>
    <div class="quiz-q" id="quiz-q"></div>
    ${q.options.map((opt, i) => `
      <button class="quiz-opt" data-opt="${i}"><span class="keyhint">${i + 1}</span><span class="opt-text">${esc(opt)}</span></button>`).join("")}
    <div id="quiz-feedback"></div>`;
  $("#quiz-q").innerHTML = md(q.question);
  renderMath($("#quiz-q"));
  $$(".quiz-opt").forEach(b => { renderMath($(".opt-text", b)); b.onclick = () => answerQuestion(+b.dataset.opt); });
}

function answerQuestion(choice) {
  if (quiz.answered) return;
  quiz.answered = true;
  const q = quiz.questions[quiz.index];
  const correct = choice === q.answer;
  if (correct) quiz.score++;
  $$(".quiz-opt").forEach((b, i) => {
    b.disabled = true;
    if (i === q.answer) b.classList.add("correct");
    else if (i === choice) b.classList.add("wrong");
  });
  $("#quiz-feedback").innerHTML = `
    <div class="quiz-expl">${correct ? "✅ Correct." : "❌ Not quite."} ${esc(q.explanation || "")}</div>
    <button class="btn primary" id="quiz-next">${quiz.index + 1 < quiz.questions.length ? "Next question" : "See score"}</button>`;
  renderMath($(".quiz-expl"));
  $("#quiz-next").onclick = () => { quiz.index++; showQuestion(); };
  $("#quiz-next").focus();
}

async function showQuizScore() {
  const lec = state.currentLecture;
  const pct = Math.round((quiz.score / quiz.questions.length) * 100);
  try {
    await api(`/api/lectures/${lec.id}/quiz-results`, {
      method: "POST", json: { score: quiz.score, total: quiz.questions.length } });
  } catch (err) { toast(err.message, true); }
  $("#quiz-card").innerHTML = `
    <div class="quiz-score">${pct >= 80 ? "🎉" : pct >= 50 ? "👍" : "📖"} ${quiz.score} / ${quiz.questions.length} (${pct}%)</div>
    <div style="text-align:center"><button class="btn primary" id="quiz-retry">Take again</button></div>`;
  $("#quiz-retry").onclick = () => { quiz.index = 0; quiz.score = 0; showQuestion(); loadQuizHistory(); };
  loadQuizHistory();
}

async function loadQuizHistory() {
  const lec = state.currentLecture;
  let rows;
  try { rows = await api(`/api/courses/${lec.course_id}/quiz-results`); } catch { return; }
  const el = $("#quiz-history");
  if (!el || !rows.length) return;
  el.innerHTML = `
    <details class="section card"><summary>Past results — ${esc(lec.course_name)} (${rows.length})</summary>
      <ul>${rows.slice(-15).reverse().map(r => `
        <li>${esc(r.lecture_title)} — <b>${r.score}/${r.total}</b> · ${new Date(r.taken_at * 1000).toLocaleString()}</li>`).join("")}
      </ul></details>`;
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

let searchTimer = null;
$("#search-input").addEventListener("input", e => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (q.length < 2) return;
  searchTimer = setTimeout(() => runSearch(q), 300);
});
$("#search-input").addEventListener("keydown", e => {
  if (e.key === "Enter") runSearch(e.target.value.trim());
});

async function runSearch(q) {
  if (q.length < 2) return;
  stopRecorderCleanup();
  state.currentLectureId = null;
  renderSidebar();
  let data;
  try { data = await api(`/api/search?q=${encodeURIComponent(q)}`); }
  catch (err) { toast(err.message, true); return; }
  const hl = text => esc(text).replace(new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"), "<mark>$1</mark>");
  $("#content").innerHTML = `
    <h2>Search: “${esc(q)}”</h2>
    <p class="subtle">${data.results.length} match(es) across all courses</p>
    ${data.results.length ? data.results.map((r, i) => `
      <div class="card search-result" data-ri="${i}">
        <div class="where">${r.type === "transcript" ? "📄 Transcript" : "📚 Notes"} · ${esc(r.course_name)} › ${esc(r.lecture_title)}${r.timestamp_label ? ` · ${r.timestamp_label}` : ""}</div>
        <div>${hl(r.snippet)}</div>
      </div>`).join("")
    : `<div class="empty-state"><div class="big">🔍</div><p>No matches.</p></div>`}`;
  $$(".search-result").forEach(el => el.onclick = () => {
    const r = data.results[+el.dataset.ri];
    if (r.type === "transcript") state.pendingSeek = r.timestamp;
    openLecture(r.lecture_id, r.type === "transcript" ? "transcript" : "notes");
  });
}

// ---------------------------------------------------------------------------
// Global keyboard shortcuts
// ---------------------------------------------------------------------------

document.addEventListener("keydown", e => {
  const tag = document.activeElement && document.activeElement.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || !$("#modal-root").classList.contains("hidden")) return;
  if (state.activeTab === "flashcards" && flash.queue[flash.index]) {
    if (e.code === "Space") { e.preventDefault(); flipCard(); }
    else if (e.key === "g" || e.key === "G") answerCard("got");
    else if (e.key === "m" || e.key === "M") answerCard("missed");
  }
  if (state.activeTab === "quiz" && !quiz.answered && quiz.questions[quiz.index]) {
    const n = parseInt(e.key, 10);
    if (n >= 1 && n <= (quiz.questions[quiz.index].options || []).length) answerQuestion(n - 1);
  }
});

window.addEventListener("beforeunload", e => {
  if (rec.mediaRecorder && rec.mediaRecorder.state !== "inactive") {
    e.preventDefault();
    e.returnValue = "";
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

(async function init() {
  applyTheme();
  try { state.config = await api("/api/config"); } catch { /* server config optional */ }
  const banner = $("#banner");
  if (!state.config.has_api_key) {
    banner.classList.remove("hidden");
    banner.innerHTML = `💡 <b>AI note generation is off</b> — set the <code>ANTHROPIC_API_KEY</code> environment variable and restart to enable Claude-powered notes. Recording, transcription, and no-AI structured notes all work without it.`;
  }
  await loadCourses();
  showWelcome();
})();
