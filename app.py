"""LectureLab — local lecture capture, transcription, and AI study notes.

FastAPI backend. Transcription runs locally via faster-whisper; study-note
generation uses the Anthropic API when ANTHROPIC_API_KEY is set, with a
fully offline fallback generator when it is not.
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "lecturelab.db"
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "base")
ANTHROPIC_MODEL = os.environ.get("LECTURELAB_MODEL", "claude-sonnet-4-6")
CHUNK_THRESHOLD = 150_000  # chars; above this, summarize in chunks first
CHUNK_SIZE = 100_000

app = FastAPI(title="LectureLab")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    vocabulary TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lectures (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    audio_file TEXT,
    audio_mime TEXT,
    duration_sec REAL,
    status TEXT NOT NULL DEFAULT 'new',
    transcript TEXT,          -- JSON: [{start, end, text}]
    error TEXT
);
CREATE TABLE IF NOT EXISTS markers (
    id INTEGER PRIMARY KEY,
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,       -- important | confused | examhint
    offset_sec REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    lecture_id INTEGER PRIMARY KEY REFERENCES lectures(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,       -- ai | basic
    content TEXT NOT NULL,    -- JSON blob (outline, notes, key_terms, ...)
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS flashcards (
    id INTEGER PRIMARY KEY,
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    box INTEGER NOT NULL DEFAULT 1,   -- Leitner box; higher = better known
    due_at REAL NOT NULL,
    reviews INTEGER NOT NULL DEFAULT 0,
    lapses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS flashcard_reviews (
    id INTEGER PRIMARY KEY,
    flashcard_id INTEGER NOT NULL REFERENCES flashcards(id) ON DELETE CASCADE,
    result TEXT NOT NULL,     -- got | missed
    reviewed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_results (
    id INTEGER PRIMARY KEY,
    lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,
    total INTEGER NOT NULL,
    taken_at REAL NOT NULL
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


with db() as conn:
    conn.executescript(SCHEMA)


def row_to_dict(row):
    return dict(row) if row is not None else None


def get_lecture_or_404(conn, lecture_id: int):
    row = conn.execute("SELECT * FROM lectures WHERE id=?", (lecture_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Lecture not found")
    return row


# ---------------------------------------------------------------------------
# Whisper transcription (lazy singleton + background jobs)
# ---------------------------------------------------------------------------

_whisper_model = None
_whisper_lock = threading.Lock()
transcribe_jobs: dict[int, dict] = {}  # lecture_id -> {status, progress, message}


def get_whisper():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel(
                WHISPER_MODEL_SIZE, device="cpu", compute_type="int8"
            )
        return _whisper_model


def run_transcription(lecture_id: int, audio_path: str):
    job = transcribe_jobs[lecture_id]
    try:
        job.update(status="loading_model",
                   message=f"Loading Whisper '{WHISPER_MODEL_SIZE}' model "
                           "(first run downloads it)...")
        model = get_whisper()
        job.update(status="transcribing", message="Transcribing audio...", progress=0.0)
        segments_iter, info = model.transcribe(audio_path, beam_size=5, vad_filter=True)
        total = info.duration or 0
        segments = []
        for seg in segments_iter:
            text = seg.text.strip()
            if text:
                segments.append({"start": round(seg.start, 2),
                                 "end": round(seg.end, 2), "text": text})
            if total:
                job["progress"] = min(seg.end / total, 1.0)
        with db() as conn:
            if not segments:
                conn.execute(
                    "UPDATE lectures SET status='error', "
                    "error='No speech detected in the recording.' WHERE id=?",
                    (lecture_id,))
                job.update(status="error",
                           message="No speech detected in the recording.")
                return
            conn.execute(
                "UPDATE lectures SET status='transcribed', transcript=?, "
                "duration_sec=COALESCE(duration_sec, ?), error=NULL WHERE id=?",
                (json.dumps(segments), total or None, lecture_id))
        job.update(status="done", progress=1.0, message="Transcription complete.")
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        with db() as conn:
            conn.execute("UPDATE lectures SET status='error', error=? WHERE id=?",
                         (str(exc), lecture_id))
        job.update(status="error", message=f"Transcription failed: {exc}")


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------

class CourseIn(BaseModel):
    name: str


class VocabIn(BaseModel):
    vocabulary: str


@app.get("/api/config")
def get_config():
    return {"has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "whisper_model": WHISPER_MODEL_SIZE,
            "anthropic_model": ANTHROPIC_MODEL}


@app.get("/api/courses")
def list_courses():
    with db() as conn:
        courses = [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM courses ORDER BY name").fetchall()]
        for c in courses:
            c["lectures"] = [row_to_dict(r) for r in conn.execute(
                "SELECT id, title, created_at, status, duration_sec FROM lectures "
                "WHERE course_id=? ORDER BY created_at DESC", (c["id"],)).fetchall()]
    return courses


@app.post("/api/courses")
def create_course(body: CourseIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Course name is required")
    with db() as conn:
        cur = conn.execute("INSERT INTO courses (name, created_at) VALUES (?, ?)",
                           (name, time.time()))
        return {"id": cur.lastrowid, "name": name}


@app.put("/api/courses/{course_id}")
def rename_course(course_id: int, body: CourseIn):
    with db() as conn:
        cur = conn.execute("UPDATE courses SET name=? WHERE id=?",
                           (body.name.strip(), course_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Course not found")
    return {"ok": True}


@app.delete("/api/courses/{course_id}")
def delete_course(course_id: int):
    with db() as conn:
        files = [r["audio_file"] for r in conn.execute(
            "SELECT audio_file FROM lectures WHERE course_id=? AND audio_file IS NOT NULL",
            (course_id,)).fetchall()]
        cur = conn.execute("DELETE FROM courses WHERE id=?", (course_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Course not found")
    for f in files:
        try:
            (AUDIO_DIR / f).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


@app.get("/api/courses/{course_id}/vocabulary")
def get_vocabulary(course_id: int):
    with db() as conn:
        row = conn.execute("SELECT vocabulary FROM courses WHERE id=?",
                           (course_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Course not found")
        return {"vocabulary": row["vocabulary"]}


@app.put("/api/courses/{course_id}/vocabulary")
def set_vocabulary(course_id: int, body: VocabIn):
    with db() as conn:
        cur = conn.execute("UPDATE courses SET vocabulary=? WHERE id=?",
                           (body.vocabulary, course_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Course not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Lectures
# ---------------------------------------------------------------------------

class LectureIn(BaseModel):
    title: str


class TranscriptIn(BaseModel):
    segments: list[dict]


@app.post("/api/courses/{course_id}/lectures")
def create_lecture(course_id: int, body: LectureIn):
    title = body.title.strip() or "Untitled lecture"
    with db() as conn:
        if conn.execute("SELECT 1 FROM courses WHERE id=?", (course_id,)).fetchone() is None:
            raise HTTPException(404, "Course not found")
        cur = conn.execute(
            "INSERT INTO lectures (course_id, title, created_at) VALUES (?, ?, ?)",
            (course_id, title, time.time()))
        return {"id": cur.lastrowid, "title": title, "course_id": course_id}


@app.get("/api/lectures/{lecture_id}")
def get_lecture(lecture_id: int):
    with db() as conn:
        lec = row_to_dict(get_lecture_or_404(conn, lecture_id))
        lec["transcript"] = json.loads(lec["transcript"]) if lec["transcript"] else None
        lec["markers"] = [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM markers WHERE lecture_id=? ORDER BY offset_sec",
            (lecture_id,)).fetchall()]
        notes = conn.execute("SELECT * FROM notes WHERE lecture_id=?",
                             (lecture_id,)).fetchone()
        lec["notes"] = ({"mode": notes["mode"],
                         "created_at": notes["created_at"],
                         **json.loads(notes["content"])} if notes else None)
        lec["course_name"] = conn.execute(
            "SELECT name FROM courses WHERE id=?", (lec["course_id"],)).fetchone()["name"]
    return lec


@app.put("/api/lectures/{lecture_id}")
def rename_lecture(lecture_id: int, body: LectureIn):
    with db() as conn:
        get_lecture_or_404(conn, lecture_id)
        conn.execute("UPDATE lectures SET title=? WHERE id=?",
                     (body.title.strip(), lecture_id))
    return {"ok": True}


@app.delete("/api/lectures/{lecture_id}")
def delete_lecture(lecture_id: int):
    with db() as conn:
        lec = get_lecture_or_404(conn, lecture_id)
        conn.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
    if lec["audio_file"]:
        try:
            (AUDIO_DIR / lec["audio_file"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


@app.put("/api/lectures/{lecture_id}/transcript")
def edit_transcript(lecture_id: int, body: TranscriptIn):
    segments = []
    for seg in body.segments:
        text = str(seg.get("text", "")).strip()
        if text:
            segments.append({"start": float(seg.get("start", 0)),
                             "end": float(seg.get("end", 0)), "text": text})
    if not segments:
        raise HTTPException(400, "Transcript cannot be empty")
    with db() as conn:
        get_lecture_or_404(conn, lecture_id)
        conn.execute("UPDATE lectures SET transcript=? WHERE id=?",
                     (json.dumps(segments), lecture_id))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Audio upload / serving / transcription
# ---------------------------------------------------------------------------

@app.post("/api/lectures/{lecture_id}/audio")
async def upload_audio(lecture_id: int, audio: UploadFile = File(...),
                       duration_sec: float = Form(0.0), markers: str = Form("[]")):
    with db() as conn:
        prev = get_lecture_or_404(conn, lecture_id)
    data = await audio.read()
    if not data:
        raise HTTPException(400, "Empty audio upload")
    mime = audio.content_type or "audio/webm"
    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
           "audio/mpeg": "mp3", "audio/wav": "wav"}.get(mime.split(";")[0], "webm")
    fname = f"lecture_{lecture_id}_{uuid.uuid4().hex[:8]}.{ext}"
    (AUDIO_DIR / fname).write_bytes(data)

    try:
        marker_list = json.loads(markers)
    except json.JSONDecodeError:
        marker_list = []

    with db() as conn:
        # A new recording replaces the old one: clear the now-mismatched
        # transcript and remove the previous audio file.
        conn.execute(
            "UPDATE lectures SET audio_file=?, audio_mime=?, duration_sec=?, "
            "status='recorded', transcript=NULL, error=NULL WHERE id=?",
            (fname, mime.split(";")[0], duration_sec or None, lecture_id))
        conn.execute("DELETE FROM markers WHERE lecture_id=?", (lecture_id,))
        for m in marker_list:
            kind = m.get("kind")
            if kind in ("important", "confused", "examhint"):
                conn.execute(
                    "INSERT INTO markers (lecture_id, kind, offset_sec) VALUES (?, ?, ?)",
                    (lecture_id, kind, float(m.get("offset_sec", 0))))
    if prev["audio_file"] and prev["audio_file"] != fname:
        try:
            (AUDIO_DIR / prev["audio_file"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "file": fname}


@app.get("/api/lectures/{lecture_id}/audio")
def serve_audio(lecture_id: int, request: Request):
    with db() as conn:
        lec = get_lecture_or_404(conn, lecture_id)
    if not lec["audio_file"]:
        raise HTTPException(404, "No audio for this lecture")
    path = AUDIO_DIR / lec["audio_file"]
    if not path.exists():
        raise HTTPException(404, "Audio file missing on disk")
    media_type = lec["audio_mime"] or "audio/webm"

    # Manual byte-range support so <audio> seeking works reliably.
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if m:
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            if start <= end:
                with open(path, "rb") as f:
                    f.seek(start)
                    chunk = f.read(end - start + 1)
                return Response(chunk, status_code=206, media_type=media_type, headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes"})
    return FileResponse(path, media_type=media_type,
                        headers={"Accept-Ranges": "bytes"})


@app.post("/api/lectures/{lecture_id}/transcribe")
def start_transcription(lecture_id: int):
    with db() as conn:
        lec = get_lecture_or_404(conn, lecture_id)
    if not lec["audio_file"]:
        raise HTTPException(400, "Upload audio before transcribing")
    job = transcribe_jobs.get(lecture_id)
    if job and job["status"] in ("loading_model", "transcribing"):
        return {"ok": True, "already_running": True}
    path = AUDIO_DIR / lec["audio_file"]
    if not path.exists():
        raise HTTPException(404, "Audio file missing on disk")
    with db() as conn:
        conn.execute("UPDATE lectures SET status='transcribing' WHERE id=?",
                     (lecture_id,))
    transcribe_jobs[lecture_id] = {"status": "queued", "progress": 0.0,
                                   "message": "Starting..."}
    threading.Thread(target=run_transcription, args=(lecture_id, str(path)),
                     daemon=True).start()
    return {"ok": True}


@app.get("/api/lectures/{lecture_id}/transcribe/status")
def transcription_status(lecture_id: int):
    job = transcribe_jobs.get(lecture_id)
    if job is None:
        with db() as conn:
            lec = get_lecture_or_404(conn, lecture_id)
        return {"status": lec["status"], "progress": None,
                "message": lec["error"] or ""}
    return job


# ---------------------------------------------------------------------------
# Note generation — shared helpers
# ---------------------------------------------------------------------------

def transcript_as_text(segments, with_timestamps=True):
    lines = []
    for seg in segments:
        if with_timestamps:
            lines.append(f"[{format_ts(seg['start'])}] {seg['text']}")
        else:
            lines.append(seg["text"])
    return "\n".join(lines)


def format_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def markers_as_text(markers) -> str:
    labels = {"important": "Important", "confused": "Confused — revisit",
              "examhint": "Exam hint"}
    if not markers:
        return "(none)"
    return "\n".join(f"- [{format_ts(m['offset_sec'])}] {labels[m['kind']]}"
                     for m in markers)


def segments_around(segments, offset_sec, window=45):
    return [s for s in segments
            if s["start"] >= offset_sec - window and s["start"] <= offset_sec + window]


NOTE_KEYS = ["outline", "notes", "key_terms", "exam_signals", "flashcards",
             "quiz", "summary", "confusions_addressed"]


def save_notes(lecture_id: int, mode: str, content: dict):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO notes (lecture_id, mode, content, "
                     "created_at) VALUES (?, ?, ?, ?)",
                     (lecture_id, mode, json.dumps(content), time.time()))
        # Replace flashcards (review state resets when notes regenerate).
        conn.execute("DELETE FROM flashcards WHERE lecture_id=?", (lecture_id,))
        now = time.time()
        for card in content.get("flashcards", []):
            front = str(card.get("front", "")).strip()
            back = str(card.get("back", "")).strip()
            if front and back:
                conn.execute(
                    "INSERT INTO flashcards (lecture_id, front, back, due_at) "
                    "VALUES (?, ?, ?, ?)", (lecture_id, front, back, now))


# ---------------------------------------------------------------------------
# AI note generation (Anthropic API)
# ---------------------------------------------------------------------------

NOTES_SYSTEM = """You are an expert study-notes assistant for a university engineering student.
You turn raw lecture transcripts into precise, well-organized study material.
Preserve technical accuracy: formulas, units, variable names, and worked examples
must match the lecture. Write formulas in LaTeX using $...$ for inline math and
$$...$$ for display math. Respond ONLY with a single valid JSON object — no
markdown fences, no commentary before or after."""


def notes_user_prompt(transcript_text, marker_text, vocab, course, title):
    vocab_line = (f"\nCustom vocabulary for this course (prefer these exact "
                  f"spellings for technical terms): {vocab}" if vocab.strip() else "")
    return f"""Course: {course}
Lecture: {title}{vocab_line}

Live markers the student tapped while recording (match them to nearby transcript timestamps):
{marker_text}

Timestamped lecture transcript:
<transcript>
{transcript_text}
</transcript>

Produce a JSON object with EXACTLY these keys:

- "outline": nested array of {{"topic": string, "subtopics": [string, ...]}} covering the lecture's structure in order.
- "notes": string of GitHub-flavored Markdown study notes — clear headings (##/###), **bolded definitions**, formulas in LaTeX ($...$ inline, $$...$$ display), worked examples preserved step by step with each step on its own line, and bullet points for supporting detail. Comprehensive enough to study from without the audio.
- "key_terms": array of {{"term": string, "definition": string}} — every technical term a student should know, concise one-to-two-sentence definitions.
- "exam_signals": array of strings — anything the professor emphasized, repeated, said would be tested, or that the student flagged with an "Exam hint" marker (cite the approximate timestamp like "(around 12:34)").
- "flashcards": array of 10-20 {{"front": string, "back": string}} — mix of definitions, formula recall, and conceptual questions. Fronts are questions or prompts, backs are complete answers.
- "quiz": array of 8-12 {{"question": string, "options": [4 strings], "answer": int (0-3 index of correct option), "explanation": string (one sentence)}} — test understanding, not trivia; include plausible distractors.
- "summary": string — exactly 5 plain-language sentences recapping the lecture.
- "confusions_addressed": array of {{"timestamp": string, "explanation": string}} — one entry per "Confused — revisit" marker, explaining clearly what was being discussed around that time and untangling the likely confusion. Empty array if there are no confused markers.

Return ONLY the JSON object."""


CHUNK_SYSTEM = """You summarize portions of a university lecture transcript for later
synthesis into study notes. Be detailed and faithful: keep all definitions, formulas
(in LaTeX), worked examples step by step, and anything the professor emphasized.
Note the timestamp ranges you are summarizing. Respond in plain text."""


def strip_code_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if m:
        return m.group(1)
    # Also handle a fence appearing around just the JSON inside extra prose.
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m and m.group(1).lstrip().startswith("{"):
        return m.group(1)
    return text


def extract_json_object(text: str) -> dict:
    text = strip_code_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Last resort: grab the outermost {...} span.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("Response did not contain a JSON object")


def anthropic_client():
    import anthropic
    return anthropic.Anthropic()


def call_claude(client, system, user_content, max_tokens=32000):
    with client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        response = stream.get_final_message()
    text = "".join(b.text for b in response.content if b.type == "text")
    if not text.strip():
        raise ValueError(f"Model returned no text (stop_reason={response.stop_reason})")
    return text


def generate_ai_notes(lecture, segments, markers, vocab, course_name):
    client = anthropic_client()
    transcript_text = transcript_as_text(segments)
    marker_text = markers_as_text(markers)

    if len(transcript_text) > CHUNK_THRESHOLD:
        # Chunk → summarize each chunk → synthesize notes from the summaries.
        chunks, cur, cur_len = [], [], 0
        for seg in segments:
            line = f"[{format_ts(seg['start'])}] {seg['text']}"
            cur.append(line)
            cur_len += len(line) + 1
            if cur_len >= CHUNK_SIZE:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
        if cur:
            chunks.append("\n".join(cur))
        summaries = []
        for i, chunk in enumerate(chunks):
            summary = call_claude(
                client, CHUNK_SYSTEM,
                f"Part {i + 1} of {len(chunks)} of the transcript:\n\n{chunk}\n\n"
                "Write a detailed, faithful summary of this part.",
                max_tokens=8000)
            summaries.append(f"--- Summary of part {i + 1} ---\n{summary}")
        transcript_text = ("The transcript was too long to include verbatim. Here are "
                           "detailed timestamped summaries of each part:\n\n"
                           + "\n\n".join(summaries))

    raw = call_claude(client, NOTES_SYSTEM,
                      notes_user_prompt(transcript_text, marker_text, vocab,
                                        course_name, lecture["title"]))
    content = extract_json_object(raw)
    missing = [k for k in NOTE_KEYS if k not in content]
    for k in missing:
        content[k] = [] if k != "notes" and k != "summary" else ""
    return content


# ---------------------------------------------------------------------------
# Non-AI fallback note generation
# ---------------------------------------------------------------------------

STOPWORDS = set("""a about above after again all also am an and any are as at be because been
before being below between both but by can could did do does doing down during each few for
from further had has have having he her here hers him his how i if in into is it its itself
just like me more most my no nor not now of off on once only or other our out over own re
really said same she should so some such than that the their them then there these they this
those through to too under until up very was we were what when where which while who whom why
will with would you your yours yeah okay right well gonna going know think kind sort thing
things lot want say see get one two three go going goes just actually basically let us""".split())

DEFINITION_PATTERNS = [
    re.compile(r"^(.{3,60}?)\s+is\s+defined\s+as\s+(.+)$", re.IGNORECASE),
    re.compile(r"^(.{3,60}?)\s+means\s+(.+)$", re.IGNORECASE),
    re.compile(r"^(.{3,60}?)\s+is\s+(?:the|a|an)\s+(.+)$", re.IGNORECASE),
    re.compile(r"^(?:we\s+(?:call|define)\s+)(.{3,60}?)\s+(?:as|to be)\s+(.+)$",
               re.IGNORECASE),
]


def generate_basic_notes(lecture, segments, markers, vocab, course_name):
    marker_by_kind = {"important": [], "confused": [], "examhint": []}
    for m in markers:
        marker_by_kind[m["kind"]].append(m["offset_sec"])

    # --- Sections: split at marker positions and every ~5 minutes.
    boundaries = sorted({0.0, *(m["offset_sec"] for m in markers),
                         *(s["start"] for s in segments if s["start"] % 300 < 5)})
    sections, notes_md = [], []
    notes_md.append(f"# {lecture['title']}\n")
    notes_md.append("*Automatically structured notes (no AI) — generated from the "
                    "transcript and your live markers.*\n")

    # --- Frequent terms as likely key concepts.
    words = Counter()
    full_text = " ".join(s["text"] for s in segments)
    for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", full_text):
        lw = w.lower()
        if lw not in STOPWORDS:
            words[lw] += 1
    top_terms = [t for t, c in words.most_common(15) if c >= 3]

    key_terms = []
    sentences = re.split(r"(?<=[.!?])\s+", full_text)
    flashcards = []
    for sent in sentences:
        sent = sent.strip()
        if not (15 < len(sent) < 400):
            continue
        for pat in DEFINITION_PATTERNS:
            m = pat.match(sent)
            if m:
                term = m.group(1).strip(" ,.;:")
                definition = m.group(2).strip()
                if 2 < len(term) < 60 and len(definition) > 10:
                    key_terms.append({"term": term, "definition": definition})
                    flashcards.append({"front": f"What is {term}?",
                                       "back": definition})
                break
        if len(flashcards) >= 20:
            break

    for t in top_terms:
        if len(flashcards) >= 20:
            break
        if not any(t in k["term"].lower() for k in key_terms):
            flashcards.append({
                "front": f'The term "{t}" came up repeatedly in this lecture. '
                         "What does it refer to?",
                "back": "Review the transcript — this looks like a key concept "
                        f'("{t}" was mentioned {words[t]} times).'})

    # --- Build timestamped sections.
    if segments:
        section_starts = [0.0] + [b for b in boundaries if b > 0]
        for i, start in enumerate(section_starts):
            end = section_starts[i + 1] if i + 1 < len(section_starts) else float("inf")
            secs = [s for s in segments if start <= s["start"] < end]
            if not secs:
                continue
            flags = []
            for kind, label in (("important", "⭐ Important"),
                                ("confused", "❓ Confused — revisit"),
                                ("examhint", "📝 Exam hint")):
                if any(start <= off < end for off in marker_by_kind[kind]):
                    flags.append(label)
            heading = f"## {format_ts(start)}"
            if flags:
                heading += "  —  " + ", ".join(flags)
            notes_md.append(heading + "\n")
            notes_md.append(" ".join(s["text"] for s in secs) + "\n")

    if top_terms:
        notes_md.append("## Likely key concepts (frequently repeated)\n")
        notes_md.append(", ".join(f"**{t}**" for t in top_terms) + "\n")
    if vocab.strip():
        notes_md.append("## Course vocabulary to double-check\n")
        notes_md.append(vocab.strip() + "\n")

    exam_signals = [f"You flagged an exam hint at {format_ts(off)} — see that section."
                    for off in marker_by_kind["examhint"]]
    confusions = []
    for off in marker_by_kind["confused"]:
        near = segments_around(segments, off)
        context = " ".join(s["text"] for s in near) or "(no transcript nearby)"
        confusions.append({"timestamp": format_ts(off),
                           "explanation": "Around this point the lecture said: "
                                          f"“{context[:600]}” — review this "
                                          "passage and the surrounding section."})

    summary_sents = [s.strip() for s in sentences if len(s.strip()) > 40][:5]
    outline = [{"topic": f"{format_ts(b)} — section", "subtopics": []}
               for b in ([0.0] + [x for x in boundaries if x > 0])][:12]

    return {
        "outline": outline,
        "notes": "\n".join(notes_md),
        "key_terms": key_terms[:20],
        "exam_signals": exam_signals,
        "flashcards": flashcards[:20],
        "quiz": [],
        "summary": " ".join(summary_sents),
        "confusions_addressed": confusions,
    }


# ---------------------------------------------------------------------------
# Notes endpoints
# ---------------------------------------------------------------------------

class NotesRequest(BaseModel):
    mode: str = "ai"  # ai | basic


@app.post("/api/lectures/{lecture_id}/notes")
def generate_notes(lecture_id: int, body: NotesRequest):
    with db() as conn:
        lec = row_to_dict(get_lecture_or_404(conn, lecture_id))
        if not lec["transcript"]:
            raise HTTPException(400, "Transcribe the lecture before generating notes")
        segments = json.loads(lec["transcript"])
        markers = [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM markers WHERE lecture_id=? ORDER BY offset_sec",
            (lecture_id,)).fetchall()]
        course = conn.execute("SELECT * FROM courses WHERE id=?",
                              (lec["course_id"],)).fetchone()
        vocab, course_name = course["vocabulary"], course["name"]

    mode = body.mode
    if mode == "ai" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set — use basic mode or "
                                 "set the key and restart the app.")
    try:
        if mode == "ai":
            content = generate_ai_notes(lec, segments, markers, vocab, course_name)
        else:
            content = generate_basic_notes(lec, segments, markers, vocab, course_name)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — API/parse errors go to the retry UI
        detail = str(exc)
        try:
            import anthropic
            if isinstance(exc, anthropic.AuthenticationError):
                detail = "Anthropic API key is invalid."
            elif isinstance(exc, anthropic.RateLimitError):
                detail = "Anthropic API rate limit hit — wait a moment and retry."
            elif isinstance(exc, anthropic.APIConnectionError):
                detail = "Could not reach the Anthropic API — check your connection."
            elif isinstance(exc, (json.JSONDecodeError, ValueError)):
                detail = f"Could not parse the model's response as JSON ({exc})."
        except ImportError:
            pass
        raise HTTPException(502, detail)

    save_notes(lecture_id, mode, content)
    return {"mode": mode, **content}


@app.get("/api/lectures/{lecture_id}/export.md", response_class=PlainTextResponse)
def export_markdown(lecture_id: int):
    with db() as conn:
        lec = row_to_dict(get_lecture_or_404(conn, lecture_id))
        notes = conn.execute("SELECT * FROM notes WHERE lecture_id=?",
                             (lecture_id,)).fetchone()
    if notes is None:
        raise HTTPException(404, "No notes generated yet")
    content = json.loads(notes["content"])
    parts = [content.get("notes", "")]
    if content.get("summary"):
        parts.append("\n## Summary\n\n" + content["summary"])
    if content.get("key_terms"):
        parts.append("\n## Key terms\n")
        parts += [f"- **{t['term']}** — {t['definition']}"
                  for t in content["key_terms"]]
    if content.get("exam_signals"):
        parts.append("\n## Exam signals\n")
        parts += [f"- {s}" for s in content["exam_signals"]]
    md = "\n".join(parts)
    fname = re.sub(r"[^\w\- ]", "", lec["title"]).strip().replace(" ", "_") or "notes"
    return PlainTextResponse(md, media_type="text/markdown", headers={
        "Content-Disposition": f'attachment; filename="{fname}.md"'})


@app.get("/api/lectures/{lecture_id}/anki.txt", response_class=PlainTextResponse)
def export_anki(lecture_id: int):
    with db() as conn:
        lec = row_to_dict(get_lecture_or_404(conn, lecture_id))
        cards = conn.execute("SELECT front, back FROM flashcards WHERE lecture_id=?",
                             (lecture_id,)).fetchall()
    if not cards:
        raise HTTPException(404, "No flashcards for this lecture")
    def clean(s):
        return s.replace("\t", " ").replace("\n", "<br>")
    body = "\n".join(f"{clean(c['front'])}\t{clean(c['back'])}" for c in cards)
    fname = re.sub(r"[^\w\- ]", "", lec["title"]).strip().replace(" ", "_") or "cards"
    return PlainTextResponse(body, media_type="text/plain", headers={
        "Content-Disposition": f'attachment; filename="{fname}_anki.txt"'})


# ---------------------------------------------------------------------------
# Flashcards & quiz results
# ---------------------------------------------------------------------------

class ReviewIn(BaseModel):
    result: str  # got | missed


@app.get("/api/lectures/{lecture_id}/flashcards")
def get_flashcards(lecture_id: int):
    now = time.time()
    with db() as conn:
        get_lecture_or_404(conn, lecture_id)
        cards = [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM flashcards WHERE lecture_id=?", (lecture_id,)).fetchall()]
    # Spaced-repetition queue: due cards first (most overdue first), then the rest.
    due = sorted([c for c in cards if c["due_at"] <= now], key=lambda c: c["due_at"])
    later = sorted([c for c in cards if c["due_at"] > now], key=lambda c: c["due_at"])
    return {"due": due, "later": later}


@app.post("/api/flashcards/{card_id}/review")
def review_flashcard(card_id: int, body: ReviewIn):
    if body.result not in ("got", "missed"):
        raise HTTPException(400, "result must be 'got' or 'missed'")
    now = time.time()
    with db() as conn:
        card = conn.execute("SELECT * FROM flashcards WHERE id=?",
                            (card_id,)).fetchone()
        if card is None:
            raise HTTPException(404, "Flashcard not found")
        if body.result == "got":
            box = min(card["box"] + 1, 6)
            due = now + (2 ** (box - 1)) * 86400  # 1, 2, 4, 8, 16, 32 days
            lapses = card["lapses"]
        else:
            box = 1
            due = now + 600  # missed cards resurface within 10 minutes
            lapses = card["lapses"] + 1
        conn.execute("UPDATE flashcards SET box=?, due_at=?, reviews=reviews+1, "
                     "lapses=? WHERE id=?", (box, due, lapses, card_id))
        conn.execute("INSERT INTO flashcard_reviews (flashcard_id, result, "
                     "reviewed_at) VALUES (?, ?, ?)", (card_id, body.result, now))
    return {"ok": True, "box": box}


class QuizResultIn(BaseModel):
    score: int
    total: int


@app.post("/api/lectures/{lecture_id}/quiz-results")
def save_quiz_result(lecture_id: int, body: QuizResultIn):
    with db() as conn:
        get_lecture_or_404(conn, lecture_id)
        conn.execute("INSERT INTO quiz_results (lecture_id, score, total, taken_at) "
                     "VALUES (?, ?, ?, ?)",
                     (lecture_id, body.score, body.total, time.time()))
    return {"ok": True}


@app.get("/api/courses/{course_id}/quiz-results")
def course_quiz_results(course_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT q.*, l.title AS lecture_title FROM quiz_results q "
            "JOIN lectures l ON l.id = q.lecture_id "
            "WHERE l.course_id=? ORDER BY q.taken_at", (course_id,)).fetchall()
    return [row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/api/search")
def search(q: str = ""):
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    needle = q.lower()
    results = []
    with db() as conn:
        lectures = conn.execute(
            "SELECT l.id, l.title, l.transcript, l.course_id, c.name AS course_name "
            "FROM lectures l JOIN courses c ON c.id = l.course_id").fetchall()
        notes_rows = {r["lecture_id"]: r for r in conn.execute(
            "SELECT lecture_id, content FROM notes").fetchall()}
    for lec in lectures:
        if lec["transcript"]:
            for seg in json.loads(lec["transcript"]):
                if needle in seg["text"].lower():
                    results.append({
                        "type": "transcript", "lecture_id": lec["id"],
                        "lecture_title": lec["title"],
                        "course_name": lec["course_name"],
                        "timestamp": seg["start"],
                        "timestamp_label": format_ts(seg["start"]),
                        "snippet": seg["text"]})
        note = notes_rows.get(lec["id"])
        if note:
            notes_md = json.loads(note["content"]).get("notes", "")
            for line in notes_md.splitlines():
                if needle in line.lower() and line.strip():
                    results.append({
                        "type": "notes", "lecture_id": lec["id"],
                        "lecture_title": lec["title"],
                        "course_name": lec["course_name"],
                        "timestamp": None, "timestamp_label": None,
                        "snippet": line.strip()[:300]})
                    if sum(1 for r in results if r["type"] == "notes"
                           and r["lecture_id"] == lec["id"]) >= 5:
                        break
    return {"results": results[:100]}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    print(f"LectureLab — whisper model: {WHISPER_MODEL_SIZE}, "
          f"AI notes: {'enabled (' + ANTHROPIC_MODEL + ')' if os.environ.get('ANTHROPIC_API_KEY') else 'disabled (no ANTHROPIC_API_KEY — basic notes still work)'}")
    uvicorn.run(app, host="127.0.0.1", port=8000)
