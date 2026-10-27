"""LectureLab v2: turns lecture recordings into study material.

One module on purpose. Sections, top to bottom:
  config / logging / lock / tools  ->  database  ->  courses  ->  jobs  ->  ingest
  ->  inbox watcher  ->  v1 import  ->  web API
"""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import date as Date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
# FastAPI names are module-level on purpose: with `from __future__ import annotations` every
# annotation is a string that FastAPI resolves against module globals, not create_app() locals.
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA = Path(os.environ.get("LECTURELAB_DATA", ROOT / "data")).resolve()
DB_PATH = DATA / "lecturelab.db"
PORT = int(os.environ.get("LECTURELAB_PORT", "8765"))
LOG_PATH = Path(os.environ.get("LECTURELAB_LOG", ROOT / "server.log"))
VERSION = "2.0.0"
HOME = Path.home()
LOCAL_INBOX = Path(os.environ.get("LECTURELAB_INBOX", HOME / "Documents" / "LectureLab Inbox"))
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

log = logging.getLogger("lecturelab")


class AppendCloseHandler(logging.FileHandler):
    """Opens server.log only for the moment of each write.

    Several processes (supervisor, server, sync) log to the same file, but only the server
    rotates it. On Windows a rename fails while another process holds the file open, so
    the non-rotating writers must not keep a handle.
    """

    def __init__(self, path):
        super().__init__(path, encoding="utf-8", delay=True)

    def emit(self, record):
        super().emit(record)
        self.close()


def setup_logging(console: bool = False, rotate: bool = False) -> None:
    """server.log for every process. Only the server passes rotate=True (5 MB x 3)."""
    if getattr(setup_logging, "_done", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(message)s")
    if rotate:
        fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    else:
        fh = AppendCloseHandler(LOG_PATH)
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    setup_logging._done = True


class FileLock:
    """OS-level exclusive lock (msvcrt on Windows, fcntl elsewhere).

    The OS drops the lock when the process dies, so a crash never leaves a stale lock.
    A threading.Lock covers threads inside the same process.
    """

    _thread_locks: dict[str, threading.Lock] = {}

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None
        self._tl = self._thread_locks.setdefault(str(self.path), threading.Lock())

    def acquire(self) -> bool:
        if not self._tl.acquire(blocking=False):
            return False
        fh = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if fh:
                fh.close()
            self._tl.release()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
            self._tl.release()

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"lock busy: {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is running. (os.kill(pid, 0) would KILL it on Windows.)"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


# ---------------------------------------------------------------- external tools

def find_tool(name: str) -> str:
    """ffmpeg/ffprobe/soffice. winget adds them to PATH only for new shells, and the logon
    task may have an old PATH, so also look where winget and LibreOffice install them."""
    hit = shutil.which(name)
    if hit:
        return hit
    globs = {
        "ffmpeg": [HOME / "AppData/Local/Microsoft/WinGet/Packages", "Gyan.FFmpeg*/*/bin/ffmpeg.exe"],
        "ffprobe": [HOME / "AppData/Local/Microsoft/WinGet/Packages", "Gyan.FFmpeg*/*/bin/ffprobe.exe"],
        "soffice": [Path("C:/Program Files/LibreOffice/program"), "soffice.exe"],
    }
    if name in globs:
        base, pattern = globs[name]
        found = sorted(base.glob(pattern)) if base.exists() else []
        if found:
            return str(found[-1])
    raise FileNotFoundError(f"{name} not found; install it (see README)")


def run(cmd: list, timeout: float = 2 * 3600, **kw) -> subprocess.CompletedProcess:
    """subprocess.run with no console window, no stdin, captured output and a deadline (so one
    corrupt file can't hang the worker forever); raises with stderr on failure."""
    kw.setdefault("capture_output", True)
    r = subprocess.run([str(c) for c in cmd], creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
                       timeout=timeout, **kw)
    if r.returncode != 0:
        dec = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")  # noqa: E731
        # Some tools (the colab CLI) print their errors on stdout, so include text stdout too. Binary
        # stdout (ffmpeg piping PCM) is data, not a message: leave it out. The colab CLI's error
        # URLs carry short-lived runtime tokens: never let those reach server.log or the UI.
        out = r.stdout[-4000:] if isinstance(r.stdout, str) else ""
        err = (out.strip() + "\n" + dec(r.stderr).strip()).strip()  # stderr last: it survives the cut below
        err = re.sub(r"(?i)\b((?:token|authuser|key|code|state)=)[^&\s'\"]+", r"\1<redacted>", err)
        err = re.sub(r"(?i)(\"(?:\w*token|key|secret)\"\s*:\s*\")[^\"]+", r"\1<redacted>", err)
        # wsl.exe -d <distro> -e <tool> ...: name the tool, not "wsl.exe" or "-d"
        tool = cmd[4] if Path(str(cmd[0])).name == "wsl.exe" and len(cmd) > 4 else cmd[0]
        raise RuntimeError(f"{Path(str(tool)).name} failed ({r.returncode}): {err[-1500:]}")
    return r


def ffprobe(path: Path) -> dict:
    out = run([find_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
              timeout=120).stdout
    info = json.loads(out)
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    tags = {k.lower(): v for k, v in (info.get("format", {}).get("tags") or {}).items()}
    dur = info.get("format", {}).get("duration")
    return {"duration": float(dur) if dur not in (None, "N/A") else None, "has_video": bool(video),
            "has_audio": bool(audio), "creation_time": tags.get("creation_time"), "title": tags.get("title")}


# ---------------------------------------------------------------- database

# Append-only: migration N runs once when PRAGMA user_version < N. Never edit a shipped one.
MIGRATIONS = [
    # IF NOT EXISTS only here: databases from before versioning already have these tables.
    """
    CREATE TABLE IF NOT EXISTS courses (
        id          INTEGER PRIMARY KEY,
        code        TEXT NOT NULL UNIQUE,      -- folder name in inboxes, e.g. COP4020
        name        TEXT NOT NULL,
        vocabulary  TEXT NOT NULL DEFAULT '',  -- one term per line; fed to Whisper and the note prompts
        canvas_id   INTEGER,
        auto_pull   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """,
    """
    CREATE TABLE lectures (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reuse an id: media/<id>/ may linger
        course_id     INTEGER REFERENCES courses(id) ON DELETE SET NULL,
        title         TEXT NOT NULL,
        date          TEXT,                    -- YYYY-MM-DD, from source metadata when available
        duration_sec  REAL,
        audio_path    TEXT,                    -- relative to DATA: media/<id>/audio.flac (16 kHz mono)
        video_path    TEXT,                    -- relative to DATA: media/<id>/original.<ext>, when there is video
        status        TEXT NOT NULL,           -- see STATUSES; 'ingesting' until its files are in place
        error         TEXT,
        sha_source    TEXT,                    -- sha256 of the file as received
        sha_pcm       TEXT,                    -- sha256 of the normalized PCM: same audio, different container
        fingerprint   BLOB,                    -- per-second loudness envelope (float16): same lecture, re-encoded
        dup_of        INTEGER REFERENCES lectures(id) ON DELETE SET NULL,
        transcript_source TEXT,                -- colab-large-v3 | local-<model> | v1
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX lectures_sha_source ON lectures(sha_source);
    CREATE INDEX lectures_sha_pcm ON lectures(sha_pcm);
    CREATE TABLE sources (
        id          INTEGER PRIMARY KEY,
        lecture_id  INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,             -- file | inbox | drive | zoom | canvas | url | v1
        url         TEXT,
        added_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(lecture_id, url)
    );
    CREATE TABLE segments (
        id          INTEGER PRIMARY KEY,
        lecture_id  INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
        idx         INTEGER NOT NULL,
        start       REAL NOT NULL,
        end         REAL NOT NULL,
        text        TEXT NOT NULL,
        words       TEXT,                      -- JSON [[start, end, word], ...]
        slide_no    INTEGER,
        UNIQUE(lecture_id, idx)
    );
    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,             -- ingest | ...
        lecture_id  INTEGER REFERENCES lectures(id) ON DELETE CASCADE,
        status      TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | error
        payload     TEXT NOT NULL DEFAULT '{}',
        result      TEXT,
        progress    TEXT,
        error       TEXT,
        owner_pid   INTEGER,
        attempts    INTEGER NOT NULL DEFAULT 0,
        retry_at    TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX jobs_status ON jobs(status, kind);
    CREATE TABLE seen_files (
        path        TEXT PRIMARY KEY,
        size        INTEGER NOT NULL,
        mtime       REAL NOT NULL,
        job_id      INTEGER,
        tries       INTEGER NOT NULL DEFAULT 1,  -- failed ingests are retried up to 3 times
        seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    CREATE TABLE canvas_items (
        key         TEXT NOT NULL,             -- e.g. file:123, kaltura:1_abcd, panopto:<guid>
        course_id   INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,             -- file | kaltura | panopto | mediasite | youtube | zoom | media_object
        title       TEXT,
        url         TEXT NOT NULL,             -- what we hand to the downloader
        page_url    TEXT,                      -- where it was found, for the user
        created_at  TEXT,
        status      TEXT NOT NULL DEFAULT 'new',  -- new | queued | pulled | ignored | error
        lecture_id  INTEGER REFERENCES lectures(id) ON DELETE SET NULL,
        error       TEXT,
        found_at    TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (course_id, key)           -- the same embed can appear in two courses
    );
    """,
    """
    CREATE TABLE transcribe_batches (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        mode          TEXT NOT NULL,           -- colab | local
        gpu           TEXT,                    -- T4 / L4 on Colab, the model name locally
        lecture_ids   TEXT NOT NULL,           -- JSON list
        done_ids      TEXT NOT NULL DEFAULT '[]',
        audio_sec     REAL,                    -- total audio in the batch
        units_before  REAL,
        units_after   REAL,
        wall_sec      REAL,
        ok            INTEGER,
        error         TEXT,
        started_at    TEXT NOT NULL DEFAULT (datetime('now')),
        ended_at      TEXT
    );
    """,
    """
    -- billing rate while the session ran (0 on the free tier)
    ALTER TABLE transcribe_batches ADD COLUMN units_per_hour REAL;
    """,
    """
    -- the Canvas page an embed was found on; opened in the LectureLab browser before a
    -- Kaltura/Panopto download so the player's own login cookies get set
    ALTER TABLE canvas_items ADD COLUMN page_link TEXT;
    """,
]

# Lecture lifecycle. Stages add work for each step; the UI shows these as chips.
STATUSES = ["queued_transcribe", "transcribing", "transcribed", "aligned", "notes_queued",
            "notes_running", "notes_done", "exported", "duplicate", "error", "waiting_quota"]

# Starter vocab so Whisper and the note prompts know the jargon on day one. Edit in the UI.
SEED_COURSES = [
    ("COP4020", "Programming Language Concepts", """BNF
EBNF
context-free grammar
parse tree
abstract syntax tree
lexer
recursive descent
ambiguity
precedence
associativity
lambda calculus
beta reduction
alpha conversion
currying
closure
higher-order function
pattern matching
type inference
Hindley-Milner
polymorphism
static scoping
dynamic scoping
activation record
garbage collection
lazy evaluation
Haskell
OCaml
Racket
Prolog
unification"""),
    ("COP4533", "Algorithm Abstraction and Design", """asymptotic notation
Big-O
Big-Theta
Big-Omega
recurrence
Master theorem
divide and conquer
dynamic programming
memoization
greedy algorithm
Dijkstra
Bellman-Ford
Floyd-Warshall
Kruskal
Prim
minimum spanning tree
BFS
DFS
topological sort
Ford-Fulkerson
max flow
NP-complete
reduction
amortized analysis"""),
    ("CNT4007", "Computer Network Fundamentals", """TCP
UDP
IP
HTTP
DNS
DHCP
ARP
Ethernet
MAC address
subnet
CIDR
NAT
OSPF
BGP
congestion control
flow control
sliding window
Go-Back-N
selective repeat
checksum
socket
three-way handshake
Wireshark
throughput
bandwidth-delay product"""),
    ("CIS4930", "Special Topics", ""),
    ("PHI3681", "Ethics, Data and Technology", """utilitarianism
consequentialism
deontology
Kant
categorical imperative
virtue ethics
Aristotle
Mill
informed consent
privacy
surveillance
algorithmic bias
fairness
autonomy
GDPR"""),
    ("MML", "Math for Machine Learning", """eigenvalue
eigenvector
SVD
PCA
gradient
Jacobian
Hessian
chain rule
convex
Lagrange multiplier
gradient descent
determinant
rank
null space
orthogonal projection
inner product
norm
Bayes' theorem
expectation
variance
Gaussian
maximum likelihood
KL divergence"""),
]

_db_init_lock = threading.Lock()
_db_ready = False


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def db():
    """Short-lived connection per unit of work; commits on success."""
    init_db()
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    global _db_ready
    if _db_ready:
        return
    with _db_init_lock:
        if _db_ready:
            return
        DATA.mkdir(parents=True, exist_ok=True)
        # The server and a CLI command can start together; only one may migrate at a time,
        # and the version is read after taking the lock so the second sees the first's work.
        mlock = FileLock(DATA / "migrate.lock")
        for _ in range(600):
            if mlock.acquire():
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("timed out waiting for another process to migrate the database")
        con = connect()
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            for n, sql in enumerate(MIGRATIONS, start=1):
                if n <= version:
                    continue
                con.executescript(f"BEGIN; {sql}; PRAGMA user_version={n}; COMMIT;")
                log.info("db migrated to version %s", n)
                if n == 1:  # seed once, so a course you delete later stays deleted
                    con.executemany("INSERT OR IGNORE INTO courses(code, name, vocabulary) VALUES (?,?,?)", SEED_COURSES)
                    con.commit()
        finally:
            con.close()
            mlock.release()
        _db_ready = True


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- courses

def list_courses() -> list[dict]:
    with db() as con:
        return rows(con.execute("SELECT * FROM courses ORDER BY code"))


def update_course(course_id: int, name: str | None = None, vocabulary: str | None = None) -> dict:
    with db() as con:
        cur = con.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if not cur:
            raise KeyError(course_id)
        if name is not None:
            if not name.strip():
                raise ValueError("course name can't be empty")
            con.execute("UPDATE courses SET name=? WHERE id=?", (name.strip(), course_id))
        if vocabulary is not None:
            terms = [t.strip() for t in vocabulary.splitlines() if t.strip()]
            con.execute("UPDATE courses SET vocabulary=? WHERE id=?", ("\n".join(terms), course_id))
        return dict(con.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone())


def find_course(key: str | int | None) -> dict | None:
    """Course by id (int), or by code or name (str, case-insensitive). Inbox folder names go
    through here as strings, so a folder called '2024' is never mistaken for course id 2024."""
    if key in (None, ""):
        return None
    with db() as con:
        if isinstance(key, int):
            r = con.execute("SELECT * FROM courses WHERE id=?", (int(key),)).fetchone()
            if r:
                return dict(r)
        k = str(key).strip().lower()
        for c in rows(con.execute("SELECT * FROM courses")):
            if k in (c["code"].lower(), c["name"].lower()) or k.startswith(c["code"].lower() + " "):
                return c
    return None


def guess_course(*texts: str) -> dict | None:
    """A course code mentioned in a title/filename/URL, e.g. 'COP4020 Lecture 7.mp4'."""
    blob = " ".join(t for t in texts if t).upper().replace(" ", "")
    for c in list_courses():
        if c["code"].upper() in blob:
            return c
    return None


# ---------------------------------------------------------------- jobs

def enqueue(kind: str, payload: dict, lecture_id: int | None = None) -> int:
    with db() as con:
        return con.execute("INSERT INTO jobs(kind, payload, lecture_id) VALUES (?,?,?)",
                           (kind, json.dumps(payload), lecture_id)).lastrowid


def job_update(job_id: int, **fields) -> None:
    if not fields:
        return
    for k in ("result", "payload"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=datetime('now')"
    with db() as con:
        con.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: int) -> dict | None:
    with db() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return None
    j = dict(r)
    for k in ("payload", "result"):
        j[k] = json.loads(j[k]) if j[k] else None
    return j


def claim_job(kinds: list[str]) -> dict | None:
    """Atomically take the oldest queued job of these kinds (safe across processes)."""
    marks = ",".join("?" * len(kinds))
    with db() as con:
        r = con.execute(
            f"""UPDATE jobs SET status='running', owner_pid=?, attempts=attempts+1, updated_at=datetime('now')
                WHERE id=(SELECT id FROM jobs WHERE status='queued' AND kind IN ({marks})
                          AND (retry_at IS NULL OR retry_at<=datetime('now')) ORDER BY id LIMIT 1)
                RETURNING id""", (os.getpid(), *kinds)).fetchone()
    return get_job(r["id"]) if r else None


def run_job_now(job_id: int) -> dict:
    """Claim one specific queued job and run it here (CLI use). If the server's worker got to it
    first, wait for that run to finish instead."""
    with db() as con:
        ok = con.execute("UPDATE jobs SET status='running', owner_pid=?, attempts=attempts+1 "
                         "WHERE id=? AND status='queued' RETURNING id", (os.getpid(), job_id)).fetchone()
    if ok:
        run_job(get_job(job_id))
    while (j := get_job(job_id))["status"] in ("queued", "running"):
        time.sleep(1)
    return j


MAX_ATTEMPTS = 3


def requeue_orphaned_jobs() -> int:
    """Jobs left 'running' by a process that died go back to the queue, unless they've already
    been tried MAX_ATTEMPTS times (a job that kills its process would otherwise loop forever)."""
    with db() as con:
        stuck = rows(con.execute("SELECT id, owner_pid, attempts FROM jobs WHERE status='running'"))
        dead = [j for j in stuck if not j["owner_pid"] or not pid_alive(j["owner_pid"])]
        for j in dead:
            if j["attempts"] >= MAX_ATTEMPTS:
                con.execute("UPDATE jobs SET status='error', owner_pid=NULL, error=? WHERE id=?",
                            (f"gave up after {j['attempts']} attempts (the process running it died each time)", j["id"]))
            else:
                con.execute("UPDATE jobs SET status='queued', owner_pid=NULL WHERE id=?", (j["id"],))
    if dead:
        log.warning("recovered %s orphaned job(s): %s", len(dead), [j["id"] for j in dead])
    return len(dead)


JOB_HANDLERS: dict[str, callable] = {}  # kind -> fn(job) -> result dict


SECRET_KEYS = ("passcode",)  # scrubbed from a job's stored payload once it has run


def run_job(job: dict) -> None:
    scrubbed = {k: v for k, v in job["payload"].items() if k not in SECRET_KEYS}
    try:
        result = JOB_HANDLERS[job["kind"]](job)
        job_update(job["id"], status="done", result=result or {}, error=None, progress=None, payload=scrubbed)
        log.info("job %s %s done: %s", job["id"], job["kind"], result)
    except RetryLater as e:  # the job re-queued itself with a retry time; leave that in place
        log.info("job %s %s deferred: %s", job["id"], job["kind"], e)
    except Exception as e:
        log.exception("job %s %s failed", job["id"], job["kind"])
        job_update(job["id"], status="error", error=str(e)[:4000], progress=None, payload=scrubbed)


class Worker(threading.Thread):
    """Background thread in the server: runs queued jobs of the given kinds, one at a time."""

    def __init__(self, kinds: list[str], name: str):
        super().__init__(daemon=True, name=name)
        self.kinds, self.wake = kinds, threading.Event()

    def run(self):
        while True:
            try:
                job = claim_job(self.kinds)
                if job:
                    run_job(job)
                    continue
            except Exception:
                log.exception("worker %s loop error", self.name)
            self.wake.wait(5)
            self.wake.clear()


# ---------------------------------------------------------------- ingest

class Duplicate(Exception):
    def __init__(self, lecture_id: int, how: str):
        super().__init__(f"duplicate of lecture {lecture_id} ({how})")
        self.lecture_id, self.how = lecture_id, how


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_audio(src: Path, dst: Path) -> None:
    """16 kHz mono FLAC: lossless, about half the size of WAV, plays in Chrome, decodes everywhere."""
    tmp = dst.with_suffix(".tmp.flac")
    run([find_tool("ffmpeg"), "-v", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
         "-sample_fmt", "s16", "-c:a", "flac", tmp])
    tmp.replace(dst)


def pcm_signature(flac: Path) -> tuple[str, "bytes", float]:
    """One pass over the decoded audio: sha256 of the PCM, per-second loudness envelope, duration.
    Raises if ffmpeg fails, so a truncated decode is never stored as a valid signature."""
    import numpy as np
    proc = subprocess.Popen([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-i", str(flac), "-f", "s16le",
                             "-ac", "1", "-ar", "16000", "-"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    h, env, total = hashlib.sha256(), [], 0
    try:
        while True:
            chunk = proc.stdout.read(32000)  # one second of 16 kHz s16
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
            x = np.frombuffer(chunk[: len(chunk) // 2 * 2], np.int16).astype(np.float32) / 32768
            env.append(float(np.sqrt(np.mean(x * x))) if x.size else 0.0)
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait(timeout=60) != 0:
            raise RuntimeError(f"ffmpeg could not decode {flac.name}: {err.strip()[-500:]}")
    finally:
        if proc.poll() is None:
            proc.kill()  # never leave ffmpeg holding the file open (Windows can't delete it then)
        proc.stdout.close()
        proc.stderr.close()
    loud = np.log10(np.array(env, np.float32) + 1e-4).astype(np.float16)
    return h.hexdigest(), loud.tobytes(), total / 32000


MAX_LAG = 30  # seconds: the same lecture from Zoom and Canvas is often trimmed differently


def envelope_match(a: bytes, b: bytes) -> float:
    """Best correlation of two per-second loudness envelopes over lags of up to +-MAX_LAG seconds."""
    import numpy as np
    x, y = np.frombuffer(a, np.float16).astype(np.float32), np.frombuffer(b, np.float16).astype(np.float32)
    best = 0.0
    for lag in range(-MAX_LAG, MAX_LAG + 1):
        xs, ys = (x[lag:], y) if lag >= 0 else (x, y[-lag:])
        n = min(len(xs), len(ys))
        if n < 60:
            continue
        xs, ys = xs[:n], ys[:n]
        if xs.std() < 1e-3 or ys.std() < 1e-3:
            continue  # silence matches everything; never call that a duplicate
        best = max(best, float(np.corrcoef(xs, ys)[0, 1]))
    return best


def find_duplicate(con, sha_source=None, sha_pcm=None, fingerprint=None, duration=None):
    """Exact matches consider every finished lecture, parked duplicates included (re-adding a
    parked file must not park it again). The fuzzy envelope match only considers real lectures."""
    done = "audio_path IS NOT NULL AND status != 'ingesting'"
    if sha_source:
        r = con.execute(f"SELECT id FROM lectures WHERE sha_source=? AND {done}", (sha_source,)).fetchone()
        if r:
            return r["id"], "identical file"
    if sha_pcm:
        r = con.execute(f"SELECT id FROM lectures WHERE sha_pcm=? AND {done}", (sha_pcm,)).fetchone()
        if r:
            return r["id"], "identical audio"
    if fingerprint and duration:
        tol = 2 * MAX_LAG + duration * 0.01
        for r in con.execute(f"SELECT id, fingerprint FROM lectures WHERE fingerprint IS NOT NULL AND {done}"
                             " AND status != 'duplicate' AND ABS(duration_sec - ?) <= ?", (duration, tol)):
            score = envelope_match(fingerprint, r["fingerprint"])
            if score >= 0.95:
                return r["id"], f"same recording, re-encoded (envelope r={score:.3f})"
    return None


def clean_source_url(url: str | None) -> str | None:
    """Drop secrets from a URL before storing or showing it (Zoom puts the passcode in ?pwd=)."""
    if not url or not url.startswith("http"):
        return url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k.lower() not in ("pwd", "passcode", "token")]
    return urlunsplit(u._replace(query=urlencode(q)))


def add_source(con, lecture_id: int, kind: str, url: str | None) -> None:
    con.execute("INSERT OR IGNORE INTO sources(lecture_id, kind, url) VALUES (?,?,?)",
                (lecture_id, kind, clean_source_url(url)))


def parse_date(*candidates) -> str | None:
    """First usable date among yt-dlp upload_date (YYYYMMDD), ISO timestamps, epoch seconds and
    dates embedded in names such as Zoom's '2026-04-08 19.47.47 ...' folders.
    Full timestamps (ffprobe creation_time, Canvas created_at) are UTC: an evening class must
    land on its local date, not tomorrow's, so those are converted to local time first."""
    for c in candidates:
        if c in (None, ""):
            continue
        if isinstance(c, (int, float)):
            return datetime.fromtimestamp(c, timezone.utc).astimezone().date().isoformat()
        s = str(c).strip()
        if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:?\d\d)?", s):
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone().date().isoformat()
            except ValueError:
                pass
        m = re.search(r"(20\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])", s)
        if m:
            try:
                return Date(int(m[1]), int(m[2]), int(m[3])).isoformat()
            except ValueError:
                continue
    return None


def _move_into_place(stage: Path, final: Path) -> None:
    """Rename the staging dir to media/<id>. Windows briefly locks fresh files (Defender, the
    indexer), so retry; and clear a leftover dir from a lecture whose delete couldn't finish."""
    if final.exists():
        log.warning("removing leftover %s before reuse", final)
        shutil.rmtree(final)
    for attempt in range(10):
        try:
            stage.rename(final)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.5 * (attempt + 1))


def ingest_file(path: Path, *, kind: str, url: str | None = None, course_id: int | None = None,
                title: str | None = None, date: str | None = None, meta: dict | None = None,
                keep_original: bool = True) -> dict:
    """Store one recording: dedupe, normalize to 16 kHz mono FLAC, keep the original video.

    Returns {"lecture_id", "duplicate": bool, "how", "parked_dup_of"}. Raises on unreadable media.
    The row is 'ingesting' until its files are in place and is removed if anything fails, so a
    failure never leaves a half-made lecture that blocks the retry.
    """
    path = Path(path)
    meta = meta or {}
    url = url or str(path)
    if date and not re.fullmatch(r"\d{4}-\d\d-\d\d", date):
        raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
    if course_id is not None and not find_course(int(course_id)):
        raise ValueError(f"no course with id {course_id}")
    sha = sha256_file(path)
    with db() as con:
        hit = find_duplicate(con, sha_source=sha)
        if hit:
            add_source(con, hit[0], kind, url)
            log.info("ingest %s: %s of lecture %s", path.name, hit[1], hit[0])
            return {"lecture_id": hit[0], "duplicate": True, "how": hit[1]}

    probe = ffprobe(path)
    if not probe["has_audio"]:
        raise ValueError(f"{path.name} has no audio stream")
    stage = DATA / "media" / f"incoming-{uuid.uuid4().hex[:8]}"
    stage.mkdir(parents=True)
    lid = None
    try:
        normalize_audio(path, stage / "audio.flac")
        sha_pcm, fingerprint, duration = pcm_signature(stage / "audio.flac")
        title = (title or meta.get("title") or probe["title"] or path.stem).strip()
        date = date or parse_date(meta.get("upload_date"), meta.get("timestamp"), probe["creation_time"],
                                  path.name, path.parent.name) \
            or datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
        if course_id is None:
            c = guess_course(title, path.name, url)
            course_id = c["id"] if c else None
        with db() as con:
            # IMMEDIATE: the check and the insert are one step, so two processes ingesting the
            # same lecture at once can't both decide it's new.
            con.execute("BEGIN IMMEDIATE")
            hit = find_duplicate(con, sha_source=sha, sha_pcm=sha_pcm, fingerprint=fingerprint, duration=duration)
            if hit and hit[1] in ("identical file", "identical audio"):
                add_source(con, hit[0], kind, url)
                con.execute("UPDATE lectures SET sha_source=COALESCE(sha_source, ?) WHERE id=?", (sha, hit[0]))
                log.info("ingest %s: %s of lecture %s", path.name, hit[1], hit[0])
                return {"lecture_id": hit[0], "duplicate": True, "how": hit[1]}
            # A near match (re-encoded) still gets its own row, parked as 'duplicate', so a
            # false positive can be released with "Not a duplicate" without re-downloading.
            lid = con.execute(
                """INSERT INTO lectures(course_id, title, date, duration_sec, status, sha_source, sha_pcm,
                   fingerprint, dup_of) VALUES (?,?,?,?,'ingesting',?,?,?,?)""",
                (course_id, title, date, duration, sha, sha_pcm, fingerprint, hit[0] if hit else None)).lastrowid
            add_source(con, lid, kind, url)
        final = DATA / "media" / str(lid)
        _move_into_place(stage, final)
        video_rel = None
        if probe["has_video"] and keep_original:
            dst = final / f"original{path.suffix.lower()}"
            shutil.copy2(path, dst)
            video_rel = dst.relative_to(DATA).as_posix()
        with db() as con:
            con.execute("UPDATE lectures SET audio_path=?, video_path=?, status=?, updated_at=datetime('now') "
                        "WHERE id=?", ((final / "audio.flac").relative_to(DATA).as_posix(), video_rel,
                                       "duplicate" if hit else "queued_transcribe", lid))
        log.info("ingested lecture %s %r (%s, %.0fs, video=%s)%s", lid, title, kind, duration, bool(video_rel),
                 f" flagged duplicate of {hit[0]}: {hit[1]}" if hit else "")
        return {"lecture_id": lid, "duplicate": bool(hit), "how": hit[1] if hit else None,
                "parked_dup_of": hit[0] if hit else None}
    except BaseException:
        if lid is not None:  # roll back the half-made lecture so a retry starts clean
            with db() as con:
                con.execute("DELETE FROM lectures WHERE id=? AND status='ingesting'", (lid,))
            shutil.rmtree(DATA / "media" / str(lid), ignore_errors=True)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def classify_url(url: str) -> str:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith(("drive.google.com", "docs.google.com")):
        return "drive"
    if host.endswith(("zoom.us", "zoom.com")):
        return "zoom"
    if host.endswith("instructure.com"):
        return "canvas"
    return "url"


_DRIVE_FILE_ID = re.compile(r"(?:/file/(?:u/\d+/)?d/|[?&]id=)([\w-]{20,})")


def download_url(url: str, dest: Path, passcode: str | None = None, progress=None,
                 cookiefile: Path | None = None) -> list[tuple[Path, dict]]:
    """Fetch a URL into dest. Returns [(file, metadata)]. Errors carry the tool's own message."""
    dest.mkdir(parents=True, exist_ok=True)
    if classify_url(url) == "drive":
        import gdown
        if "/folders/" in url:
            files = gdown.download_folder(url, output=str(dest), quiet=True) or []
            got = [Path(f) for f in files if Path(f).suffix.lower() in MEDIA_EXTS]
        else:
            m = _DRIVE_FILE_ID.search(url)
            if not m:
                raise RuntimeError(f"Google Drive: can't find a file id in {url}")
            f = gdown.download(id=m[1], output=str(dest) + os.sep, quiet=True)
            got = [Path(f)] if f else []
        if not got:
            raise RuntimeError("Google Drive: nothing downloadable at that link. Is it shared as "
                               "'Anyone with the link'? Private files sync through the Drive inbox folder instead.")
        return [(f, {"title": f.stem}) for f in got]

    import yt_dlp
    opts = {
        "outtmpl": str(dest / "%(title).80B [%(id)s].%(ext)s"),
        # Up to 720p keeps slides legible for OCR without multi-GB downloads.
        "format": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        "merge_output_format": "mp4",
        "ffmpeg_location": str(Path(find_tool("ffmpeg")).parent),
        # One link, one recording: a watch?v=X&list=Y link must not pull the whole playlist.
        "noplaylist": True, "playlistend": 1, "quiet": True, "no_warnings": True, "noprogress": True,
        "js_runtimes": {"node": {}},  # YouTube needs a JS runtime; Node is installed
        "retries": 5, "fragment_retries": 10,
    }
    if passcode:
        opts["videopassword"] = passcode
    if cookiefile:
        opts["cookiefile"] = str(cookiefile)
    if progress:
        last = [0.0]

        def hook(d):
            if d.get("status") != "downloading" or time.time() - last[0] < 2:
                return
            last[0] = time.time()
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            progress(f"downloading {d.get('downloaded_bytes', 0) / total:.0%}" if total else "downloading")
        opts["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            msg = f"yt-dlp: {str(e).removeprefix('ERROR: ')}"
            if cookiefile and re.search(r"(?i)\b(401|403|login|sign in|private|permission)\b", msg):
                msg += ("\nThe player didn't accept the LectureLab browser's UF session. If Canvas shows"
                        " 'needs sign-in', click Connect Canvas; otherwise open the video once in that browser.")
            elif classify_url(url) == "zoom":
                msg += ("\nFor Zoom this usually means the link expired or was deleted, the passcode is missing or"
                        " wrong, or the host limited it to signed-in UF users. Open the link in a browser: if it"
                        " only plays after you sign in, download it there and drop the file in the inbox.")
            raise RuntimeError(msg) from None
    entries = info.get("entries") or [info]
    out = []
    for e in entries:
        if not e:
            continue
        files = [Path(r["filepath"]) for r in e.get("requested_downloads") or [] if r.get("filepath")]
        f = next((p for p in files if p.exists()), None)
        if f:
            out.append((f, {"title": e.get("title"), "upload_date": e.get("upload_date"),
                            "timestamp": e.get("timestamp"), "url": e.get("webpage_url") or url}))
    if not out:
        raise RuntimeError("yt-dlp finished but produced no media file")
    return out


_LOGIN_HOSTS = ("kaltura.com", "panopto.com", "instructure.com", "ufl.edu")


def needs_login_cookies(url: str) -> bool:
    """Links that may sit behind UF login: matched on the host, not anywhere in the URL."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _LOGIN_HOSTS) or "/mediasite/" in u.path.lower()


def job_cookie_file(job_id: int) -> Path:
    """Per-job cookie file for yt-dlp; the job deletes it when it's done."""
    return DATA / "secrets" / f"cookies-job{job_id}.txt"


def _login_cookies_for(url: str, path: Path) -> Path | None:
    """A pasted Kaltura/Panopto/Mediasite link may need the UF session too, if Canvas is connected."""
    if not needs_login_cookies(url) or canvas_state().get("state") != "connected":
        return None
    try:
        with CanvasSession() as cs:
            return cs.export_cookies(path)
    except Exception as e:
        log.warning("could not export Canvas cookies: %s", e)
        return None


def job_ingest(job: dict) -> dict:
    """payload: {path | url, kind?, course_id?, title?, date?, passcode?, delete_after?}"""
    p = job["payload"]
    say = lambda msg: job_update(job["id"], progress=msg)  # noqa: E731
    results = []
    if p.get("url"):
        kind = p.get("kind") or classify_url(p["url"])
        tmp = DATA / "downloads" / f"job{job['id']}"
        cookies = job_cookie_file(job["id"])
        try:
            say("downloading")
            files = download_url(p["url"], tmp, passcode=p.get("passcode"), progress=say,
                                 cookiefile=_login_cookies_for(p["url"], cookies))
            for i, (f, meta) in enumerate(files, 1):
                say(f"processing {i}/{len(files)}: {f.name}")
                title = p.get("title") if len(files) == 1 else None
                results.append(ingest_file(f, kind=kind, url=meta.get("url") or p["url"], course_id=p.get("course_id"),
                                           title=title, date=p.get("date"), meta=meta))
        finally:
            cookies.unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        say("processing")
        path = Path(p["path"])
        try:
            results.append(ingest_file(path, kind=p.get("kind", "file"), url=p.get("source_url"),
                                       course_id=p.get("course_id"), title=p.get("title"), date=p.get("date")))
        finally:
            if p.get("delete_after"):
                path.unlink(missing_ok=True)
    first = results[0] if results else {}
    if first.get("lecture_id"):
        with db() as con:
            con.execute("UPDATE jobs SET lecture_id=? WHERE id=?", (first["lecture_id"], job["id"]))
    return {"lectures": results}


JOB_HANDLERS["ingest"] = job_ingest


# ---------------------------------------------------------------- lectures

def list_lectures(course_id: int | None = None) -> list[dict]:
    q = """SELECT l.id, l.course_id, l.title, l.date, l.duration_sec, l.status, l.error, l.dup_of,
                  l.video_path IS NOT NULL AS has_video, l.transcript_source, c.code AS course_code
           FROM lectures l LEFT JOIN courses c ON c.id=l.course_id"""
    with db() as con:
        if course_id:
            return rows(con.execute(q + " WHERE l.course_id=? ORDER BY l.date DESC, l.id DESC", (course_id,)))
        return rows(con.execute(q + " ORDER BY l.date DESC, l.id DESC"))


def get_lecture(lecture_id: int) -> dict | None:
    with db() as con:
        r = con.execute("""SELECT l.*, c.code AS course_code, c.name AS course_name FROM lectures l
                           LEFT JOIN courses c ON c.id=l.course_id WHERE l.id=?""", (lecture_id,)).fetchone()
        if not r:
            return None
        lec = dict(r)
        lec.pop("fingerprint", None)
        lec["sources"] = rows(con.execute("SELECT kind, url, added_at FROM sources WHERE lecture_id=? ORDER BY id",
                                          (lecture_id,)))
        lec["segment_count"] = con.execute("SELECT COUNT(*) FROM segments WHERE lecture_id=?",
                                           (lecture_id,)).fetchone()[0]
    return lec


def update_lecture(lecture_id: int, **fields) -> dict:
    allowed = {k: v for k, v in fields.items() if k in ("title", "date", "course_id") and v is not None}
    if "title" in allowed and not str(allowed["title"]).strip():
        raise ValueError("title can't be empty")
    if "date" in allowed and allowed["date"] and not re.fullmatch(r"\d{4}-\d\d-\d\d", allowed["date"]):
        raise ValueError("date must be YYYY-MM-DD")
    with db() as con:
        if not con.execute("SELECT 1 FROM lectures WHERE id=?", (lecture_id,)).fetchone():
            raise KeyError(lecture_id)
        for k, v in allowed.items():
            con.execute(f"UPDATE lectures SET {k}=?, updated_at=datetime('now') WHERE id=?", (v, lecture_id))
    return get_lecture(lecture_id)


def release_duplicate(lecture_id: int) -> dict:
    """'Not a duplicate': a near-match flagged by the envelope check goes into the pipeline."""
    with db() as con:
        con.execute("UPDATE lectures SET status='queued_transcribe', dup_of=NULL, updated_at=datetime('now') "
                    "WHERE id=? AND status='duplicate'", (lecture_id,))
    return get_lecture(lecture_id)


def delete_lecture(lecture_id: int) -> None:
    with db() as con:
        con.execute("DELETE FROM lectures WHERE id=?", (lecture_id,))
    shutil.rmtree(DATA / "media" / str(lecture_id), ignore_errors=True)
    log.info("deleted lecture %s", lecture_id)


# ---------------------------------------------------------------- inboxes

_drive_cache: tuple[float, Path | None] = (0.0, None)


def drive_inbox() -> Path | None:
    """My Drive/LectureLab/Inbox on whatever letter Drive for Desktop mounted.
    LECTURELAB_DRIVE_INBOX overrides it (a path, or "off"; tests use this).
    Cached for 5 minutes: probing dead drive letters can block for seconds."""
    global _drive_cache
    override = os.environ.get("LECTURELAB_DRIVE_INBOX")
    if override:
        return None if override == "off" else Path(override)
    if time.time() - _drive_cache[0] < 300:
        return _drive_cache[1]
    found = None
    for letter in "GHIJKLMNOPQRSTUVWXYZ":
        p = Path(f"{letter}:/My Drive")
        try:
            if p.is_dir():
                found = p / "LectureLab" / "Inbox"
                break
        except OSError:
            continue
    _drive_cache = (time.time(), found)
    return found


def inbox_roots() -> list[tuple[Path, str]]:
    roots = [(LOCAL_INBOX, "inbox")]
    d = drive_inbox()
    if d:
        roots.append((d, "drive"))
    return roots


_pending_sizes: dict[str, tuple[int, float]] = {}


def _still_being_written(f: Path) -> bool:
    """Windows refuses to open a file for writing while another process (a copy, Drive sync)
    still has it open for writing; size+mtime alone miss a copy that stalls between scans."""
    try:
        with open(f, "ab"):
            return False
    except OSError:
        return True


def scan_inboxes(require_stable: bool = True) -> list[int]:
    """Queue new media in <inbox>/<Course>/. Files are never moved (Drive would fight that).

    A file is queued once its size is unchanged between two scans and nothing is still writing
    it. A file whose ingest failed is retried on later scans, up to MAX_ATTEMPTS times.
    Returns the new job ids.
    """
    jobs = []
    with db() as con:
        seen = {r["path"]: dict(r) for r in con.execute(
            "SELECT s.*, j.status AS job_status FROM seen_files s LEFT JOIN jobs j ON j.id=s.job_id")}
    for root, kind in inbox_roots():
        try:
            root.mkdir(parents=True, exist_ok=True)
            for c in list_courses():  # a folder per course, so there's an obvious place to drop files
                (root / c["code"]).mkdir(exist_ok=True)
            files = [f for f in root.rglob("*") if f.is_file() and f.suffix.lower() in MEDIA_EXTS]
        except OSError as e:
            log.warning("inbox %s unreadable: %s", root, e)
            continue
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            key = str(f.resolve())
            prev = seen.get(key)
            unchanged = prev and prev["size"] == st.st_size and abs(prev["mtime"] - st.st_mtime) < 1
            if unchanged and not (prev["job_status"] == "error" and prev["tries"] < MAX_ATTEMPTS):
                continue
            if require_stable and not unchanged and _pending_sizes.get(key) != (st.st_size, st.st_mtime):
                _pending_sizes[key] = (st.st_size, st.st_mtime)
                continue
            if _still_being_written(f):
                continue
            _pending_sizes.pop(key, None)
            rel = f.relative_to(root).parts
            course = find_course(rel[0]) if len(rel) > 1 else None
            jid = enqueue("ingest", {"path": key, "kind": kind, "course_id": course["id"] if course else None})
            tries = prev["tries"] + 1 if unchanged else 1
            with db() as con:
                con.execute("INSERT OR REPLACE INTO seen_files(path, size, mtime, job_id, tries) VALUES (?,?,?,?,?)",
                            (key, st.st_size, st.st_mtime, jid, tries))
            log.info("inbox: queued %s (course %s, job %s, try %s)", f, course and course["code"], jid, tries)
            jobs.append(jid)
    return jobs


class InboxWatcher(threading.Thread):
    def __init__(self, interval: float = 30, on_new=None):
        super().__init__(daemon=True, name="inbox")
        self.interval, self.on_new = interval, on_new

    def run(self):
        while True:
            try:
                if scan_inboxes() and self.on_new:
                    self.on_new()
            except Exception:
                log.exception("inbox scan failed")
            time.sleep(self.interval)


# ---------------------------------------------------------------- Canvas

class CanvasError(RuntimeError):
    pass


class CanvasNeedsSignIn(CanvasError):
    pass


# UF students can't make API tokens, so LectureLab keeps its own browser profile: you sign in
# there once with GatorLink + Duo, and LectureLab reads the Canvas API with that session the way
# Canvas's own web pages do (GET only). No password is ever asked for or stored.
CANVAS_BASE = os.environ.get("CANVAS_BASE_URL", "https://ufl.instructure.com").rstrip("/")
PROFILE_DIR = DATA / "browser-profile"
# Canvas and UF login use session-only cookies, which Chromium drops when the browser closes.
# They're kept here between launches so one sign-in lasts until Canvas itself expires it.
SESSION_COOKIES = DATA / "secrets" / "canvas-session.json"
_canvas_local = threading.local()


def canvas_state() -> dict:
    return meta_get("canvas_state", {"state": "not_connected"})


def set_canvas_state(state: str, **info) -> None:
    prev = canvas_state().get("state")
    meta_set("canvas_state", {"state": state, "checked_at": datetime.now().isoformat(timespec="seconds"), **info})
    if state == "needs_signin" and prev != "needs_signin":
        log.warning("Canvas session expired: needs sign-in (inbox folders keep working)")
        notify("LectureLab: Canvas needs sign-in",
               "Your Canvas session expired. Open LectureLab and click Connect Canvas. Inbox folders still work.")


def notify(title: str, body: str) -> None:
    """Best-effort Windows toast. Off in tests (LECTURELAB_NOTIFY=off)."""
    if os.environ.get("LECTURELAB_NOTIFY") == "off" or os.name != "nt":
        return
    esc_ = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace("'", "''")  # noqa: E731
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{esc_(title)}</text><text>{esc_(body)}</text></binding></visual></toast>')
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         creationflags=NO_WINDOW, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning("notification failed: %s", e)


def _prepare_profile() -> None:
    """Turn off the profile's password manager so it never offers to save the UF password."""
    prefs = PROFILE_DIR / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(prefs.read_text(encoding="utf-8")) if prefs.exists() else {}
    except ValueError:
        data = {}
    data["credentials_enable_service"] = False
    data.setdefault("profile", {})["password_manager_enabled"] = False
    prefs.write_text(json.dumps(data), encoding="utf-8")


class CanvasSession:
    """The LectureLab browser profile, open headless, used to read Canvas. One at a time
    (a profile can't be opened twice); nested `with` blocks in a thread reuse the open one."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.lock = FileLock(DATA / "browser.lock")
        self.nested = False

    def __enter__(self):
        if getattr(_canvas_local, "session", None):
            self.nested = True
            return _canvas_local.session
        for _ in range(240):
            if self.lock.acquire():
                break
            time.sleep(0.5)
        else:
            raise CanvasError("the LectureLab browser is busy (a sign-in window may be open)")
        self.pw = self.ctx = None
        try:
            from playwright.sync_api import sync_playwright
            _prepare_profile()
            self.pw = sync_playwright().start()
            self.ctx = self.pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=self.headless, accept_downloads=False,
                args=["--disable-features=PasswordManagerOnboarding,AutofillServerCommunication"])
            self.restore_session_cookies()
        except Exception:
            self._shutdown(save=False)  # a half-open browser would keep the profile locked
            raise
        _canvas_local.session = self
        return self

    def __exit__(self, *exc):
        if self.nested:
            return
        _canvas_local.session = None
        self._shutdown(save=True)

    def _shutdown(self, save: bool) -> None:
        """Close the browser and Playwright and free the lock, each step even if an earlier one fails."""
        try:
            if save and self.ctx:
                try:
                    self.save_session_cookies()
                except Exception as e:
                    log.warning("could not save the Canvas session cookies: %s", e)
            for step in (self.ctx and self.ctx.close, self.pw and self.pw.stop):
                if step:
                    try:
                        step()
                    except Exception as e:
                        log.warning("closing the LectureLab browser: %s", e)
        finally:
            self.lock.release()

    def get(self, path: str, params: dict | None = None, paginate: bool = True, quiet_auth: bool = False):
        """GET /api/v1/<path> with the session, following Link: rel=next pagination.
        quiet_auth: a signed-out answer raises without marking Canvas 'needs sign-in' (used
        while you're signing in, so it doesn't notify you mid-login)."""
        from urllib.parse import urlencode
        if path.startswith("http"):
            if not path.startswith(CANVAS_BASE + "/api/v1/"):
                raise CanvasError(f"refusing a non-API Canvas URL: {path}")
            url = path
        else:
            url = f"{CANVAS_BASE}/api/v1/{path.lstrip('/')}?" + urlencode({"per_page": 100, **(params or {})}, doseq=True)
        out = []
        while url:
            for attempt in range(5):
                r = self.ctx.request.get(url, headers={"Accept": "application/json+canvas-string-ids, application/json"},
                                         max_redirects=0, timeout=30_000)
                # Canvas also answers 403 when throttling; that must not look like "no items".
                if r.status == 403 and "rate limit" in r.text().lower():
                    time.sleep(2 ** attempt)
                    continue
                break
            else:
                raise CanvasError("Canvas kept rate-limiting; try again in a few minutes.")
            if r.status in (403, 404):
                return [] if paginate else None  # tab hidden by the instructor, or no access: not an error
            # Signed out: the API answers 401, or bounces to the SSO login page. Anything else that
            # isn't JSON (a 5xx, a maintenance page) is an outage, not an expired session: it must
            # not flip the sticky "needs sign-in" state.
            html = "json" not in (r.headers.get("content-type") or "")
            if r.status == 401 or 300 <= r.status < 400 or (
                    r.ok and html and re.search(r"(?i)log\s*in|sign\s*in|/login|login\.ufl\.edu", r.text())):
                if not quiet_auth:
                    set_canvas_state("needs_signin", reason=f"HTTP {r.status} from {url.split('?')[0]}")
                raise CanvasNeedsSignIn("Canvas needs sign-in: click Connect Canvas in LectureLab")
            if not r.ok or html:
                raise CanvasError(f"Canvas API answered {r.status}{' (not JSON)' if html else ''} for "
                                  f"{url.split('?')[0]}; Canvas may be down, try again later")
            data = r.json()
            if not paginate or not isinstance(data, list):
                return data
            out.extend(data)
            url = _next_link(r.headers.get("link"))
        return out

    def whoami(self, quiet_auth: bool = False) -> dict:
        me = self.get("users/self", paginate=False, quiet_auth=quiet_auth) or {}
        set_canvas_state("connected", user=me.get("name"))
        return me

    def warm(self, url: str) -> None:
        """Open a Canvas page so embedded players (Kaltura, Panopto) run their LTI sign-in and
        set their own cookies in this profile."""
        page = self.ctx.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(3000)
        except Exception as e:
            log.warning("warming %s: %s", url, e)
        finally:
            page.close()

    def save_session_cookies(self, path: Path = SESSION_COOKIES) -> None:
        keep = [c for c in self.ctx.cookies() if c.get("expires", -1) < 0]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep), encoding="utf-8")
        os.replace(tmp, path)

    def restore_session_cookies(self, path: Path = SESSION_COOKIES) -> int:
        """Add back saved session cookies the profile lost on close (never overwrites newer ones)."""
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        have = {(c["name"], c["domain"], c["path"]) for c in self.ctx.cookies()}
        add = [c for c in saved if (c["name"], c["domain"], c["path"]) not in have]
        if add:
            self.ctx.add_cookies(add)
        return len(add)

    def export_cookies(self, path: Path) -> Path:
        """Write the profile's cookies as a Netscape cookies.txt for yt-dlp --cookies. Callers use a
        per-job path (job_cookie_file) and delete it afterwards: no long-lived cookie file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(netscape_cookies(self.ctx.cookies()), encoding="utf-8")
        return path

    def httpx_cookies(self):
        import httpx
        jar = httpx.Cookies()
        for c in self.ctx.cookies():
            jar.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        return jar


def _next_link(header: str | None) -> str | None:
    for part in (header or "").split(","):
        m = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if m:
            return m[1]
    return None


def netscape_cookies(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Written by LectureLab from its browser profile. Do not share."]
    for c in cookies:
        domain = c["domain"]
        lines.append("\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE", c.get("path") or "/",
                                "TRUE" if c.get("secure") else "FALSE",
                                str(int(c["expires"])) if c.get("expires", -1) and c.get("expires", -1) > 0 else "0",
                                c["name"], c["value"]]))
    return "\n".join(lines) + "\n"


def canvas_get(path: str, params: dict | None = None, paginate: bool = True):
    """GET a Canvas API path with the LectureLab profile's session (reuses an open one)."""
    if not getattr(_canvas_local, "session", None):
        canvas_ready()  # not connected or needs sign-in: say so without opening a browser
    with CanvasSession() as cs:
        return cs.get(path, params, paginate)


def canvas_login(timeout_min: float = 10) -> dict:
    """Open the LectureLab browser so you can sign in with GatorLink + Duo yourself. Returns once
    the Canvas API answers as you, or after timeout_min. Never fills in anything."""
    with CanvasSession(headless=False) as cs:
        page = cs.ctx.pages[0] if cs.ctx.pages else cs.ctx.new_page()
        page.goto(CANVAS_BASE + "/", wait_until="domcontentloaded")
        end = time.time() + timeout_min * 60
        while time.time() < end:
            try:
                me = cs.get("users/self", paginate=False, quiet_auth=True)
                if me and me.get("id"):
                    set_canvas_state("connected", user=me.get("name"))
                    log.info("Canvas connected as %s", me.get("name"))
                    page.wait_for_timeout(1500)
                    return me
            except CanvasNeedsSignIn:
                pass
            if not cs.ctx.pages:
                break  # you closed the window
            time.sleep(2)
        set_canvas_state("needs_signin" if canvas_state().get("state") != "not_connected" else "not_connected",
                         reason="sign-in window closed or timed out")
        raise CanvasError("Canvas sign-in didn't finish (window closed or timed out)")


def canvas_courses() -> list[dict]:
    """Current Canvas courses, each matched to a LectureLab course by course code."""
    mine = list_courses()
    out = []
    for c in canvas_get("courses", {"enrollment_state": "active", "include[]": "term"}):
        if not c.get("name"):
            continue
        blob = f"{c.get('course_code', '')} {c.get('name', '')}".upper().replace(" ", "")
        match = next((m for m in mine if m["code"].upper() in blob), None)
        out.append({"canvas_id": c["id"], "name": c["name"], "code": c.get("course_code"),
                    "term": (c.get("term") or {}).get("name"), "course_id": match and match["id"],
                    "course_code": match and match["code"]})
        if match and match["canvas_id"] != c["id"]:
            with db() as con:
                con.execute("UPDATE courses SET canvas_id=? WHERE id=?", (c["id"], match["id"]))
    return out


# Embedded players we know how to fetch. Each regex captures enough to build a stable key.
_EMBEDS = [
    ("kaltura", re.compile(r"(?:entry_?id[/=]|entryid%2F|/entryId/)(\d_[a-z0-9]{8})", re.I)),
    ("panopto", re.compile(r"https?://[\w.-]*panopto\.com/Panopto/Pages/(?:Viewer|Embed)\.aspx\?(?:[^\"'<> ]*&)?id=([0-9a-f-]{36})", re.I)),
    ("mediasite", re.compile(r"https?://[\w.-]+/Mediasite/Play/([0-9a-f]{32,34})", re.I)),
    ("youtube", re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=)|youtu\.be/)([\w-]{11})", re.I)),
    ("zoom", re.compile(r"https?://[\w.-]*zoom\.us/rec/(?:share|play)/[\w.\-~%]+", re.I)),
    ("media_object", re.compile(r"media_objects(?:_iframe)?/(m-[\w]+)", re.I)),
    # Some courses keep recordings in a shared Google Drive folder linked from a module.
    ("drive_folder", re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]{20,})", re.I)),
    ("drive", re.compile(r"drive\.google\.com/(?:file/(?:u/\d+/)?d/|open\?id=|uc\?(?:[^\"'<> ]*&)?id=)([\w-]{20,})", re.I)),
]
_KALTURA_PARTNER = re.compile(r"(\d{6,8})\.kaf\.kaltura\.com|/p/(\d{6,8})/|partner_?id[/=](\d{6,8})", re.I)


def scan_html_for_media(html: str, page_url: str) -> list[dict]:
    from html import unescape
    from urllib.parse import unquote
    text = unquote(unescape(html or ""))
    found = []
    for kind, rx in _EMBEDS:
        for m in rx.finditer(text):
            if kind == "kaltura":
                pm = _KALTURA_PARTNER.search(text)
                partner = next((g for g in pm.groups() if g), None) if pm else None
                # yt-dlp's Kaltura extractor takes kaltura:<partner>:<entry> directly. Without a
                # partner id there's no reliable URL, so mark it rather than guess a host.
                url = f"kaltura:{partner}:{m[1]}" if partner else f"kaltura-unresolved:{m[1]}"
                found.append({"key": f"kaltura:{m[1]}", "kind": kind, "url": url})
            elif kind == "panopto":
                found.append({"key": f"panopto:{m[1]}", "kind": kind, "url": m[0].replace("Embed.aspx", "Viewer.aspx")})
            elif kind == "youtube":
                found.append({"key": f"youtube:{m[1]}", "kind": kind, "url": f"https://www.youtube.com/watch?v={m[1]}"})
            elif kind == "media_object":
                found.append({"key": f"media_object:{m[1]}", "kind": kind, "url": f"media_object:{m[1]}"})
            elif kind == "drive_folder":
                found.append({"key": f"drive_folder:{m[1]}", "kind": kind,
                              "url": f"https://drive.google.com/drive/folders/{m[1]}"})
            elif kind == "drive":
                found.append({"key": f"drive:{m[1]}", "kind": kind, "url": f"https://drive.google.com/file/d/{m[1]}/view"})
            else:
                found.append({"key": f"{kind}:{m[0] if kind == 'zoom' else m[1]}", "kind": kind, "url": m[0]})
    for f in found:
        f["page_url"] = page_url
    return found


def drive_list_folder(folder_id: str) -> list[dict]:
    """The files in a public Drive folder (recursively), listed without downloading anything."""
    import gdown
    with tempfile.TemporaryDirectory() as tmp:
        files = gdown.download_folder(id=folder_id, output=tmp, skip_download=True, quiet=True) or []
    return [{"id": f.id, "path": f.path} for f in files]


def expand_drive_folders(items: dict[str, dict]) -> None:
    """Replace each linked Drive folder with one item per recording in it, so recordings are pulled
    one at a time (a semester folder is many GB). A folder that can't be listed stays as it is."""
    for key, it in list(items.items()):
        if it["kind"] != "drive_folder":
            continue
        try:
            listing = drive_list_folder(key.split(":", 1)[1])
        except Exception as e:
            log.warning("couldn't list the Drive folder linked at %s: %s", it.get("page_url"), e)
            continue
        del items[key]
        for f in listing:
            if Path(f["path"]).suffix.lower() in MEDIA_EXTS:
                items.setdefault(f"drive:{f['id']}", {
                    "key": f"drive:{f['id']}", "kind": "drive", "title": Path(f["path"]).stem,
                    "url": f"https://drive.google.com/file/d/{f['id']}/view",
                    "page_url": f"{it.get('page_url')} > {f['path']}", "page_link": it.get("page_link")})


def canvas_scan_course(course: dict, progress=None) -> dict:
    """Find recordings in a course's Files, Modules, Pages and Announcements. Records new ones."""
    canvas_ready()  # not connected or needs sign-in: no browser, no "expired" toast
    with CanvasSession():
        return _canvas_scan_course(course, progress)


def _canvas_scan_course(course: dict, progress=None) -> dict:
    from urllib.parse import quote
    cid = course["canvas_id"]
    say = progress or (lambda m: None)
    items: dict[str, dict] = {}

    def add(it):
        items.setdefault(it["key"], it)

    def add_file(f, where):
        mime = f.get("content-type") or f.get("mime_class") or ""
        if mime.startswith(("video/", "audio/")) or mime in ("video", "audio") or \
                Path(f.get("display_name", "")).suffix.lower() in MEDIA_EXTS:
            add({"key": f"file:{f['id']}", "kind": "file", "title": Path(f.get("display_name", "")).stem,
                 "url": f"canvas-file:{f['id']}", "page_url": where, "created_at": f.get("created_at")})

    say("files")
    for f in canvas_get(f"courses/{cid}/files", {"content_types[]": ["video", "audio"]}):
        add_file(f, "course files")
    say("modules")
    pages_to_read = set()
    for mod in canvas_get(f"courses/{cid}/modules", {"include[]": "items"}):
        for it in mod.get("items") or []:
            where = f"module: {mod.get('name')} > {it.get('title')}"
            if it.get("type") == "File" and it.get("content_id"):
                f = canvas_get(f"files/{it['content_id']}", paginate=False)
                if f:
                    add_file(f, where)
            elif it.get("type") in ("ExternalUrl", "ExternalTool"):
                for m in scan_html_for_media(it.get("external_url") or "", where):
                    m.setdefault("title", it.get("title"))
                    m["page_link"] = it.get("html_url")
                    add(m)
            elif it.get("type") == "Page" and it.get("page_url"):
                pages_to_read.add(it["page_url"])
    say("pages")
    for p in canvas_get(f"courses/{cid}/pages"):
        pages_to_read.add(p["url"])
    for slug in sorted(pages_to_read):
        p = canvas_get(f"courses/{cid}/pages/{quote(slug, safe='')}", paginate=False)  # a slug must not escape the path
        if p:
            for m in scan_html_for_media(p.get("body") or "", f"page: {p.get('title')}"):
                m.setdefault("title", p.get("title"))
                m.setdefault("created_at", p.get("created_at"))
                m["page_link"] = p.get("html_url") or f"{CANVAS_BASE}/courses/{cid}/pages/{slug}"
                add(m)
    say("announcements")
    for a in canvas_get(f"courses/{cid}/discussion_topics", {"only_announcements": "true"}):
        for m in scan_html_for_media(a.get("message") or "", f"announcement: {a.get('title')}"):
            m.setdefault("title", a.get("title"))
            m.setdefault("created_at", a.get("posted_at"))
            m["page_link"] = a.get("html_url")
            add(m)
    say("drive folders")
    expand_drive_folders(items)
    new = 0
    with db() as con:
        for it in items.values():
            cur = con.execute("""INSERT OR IGNORE INTO canvas_items(key, course_id, kind, title, url, page_url,
                                 created_at, page_link) VALUES (?,?,?,?,?,?,?,?)""",
                              (it["key"], course["id"], it["kind"], it.get("title"), it["url"], it.get("page_url"),
                               it.get("created_at"), it.get("page_link")))
            new += cur.rowcount
    log.info("canvas scan %s: %s items (%s new)", course["code"], len(items), new)
    return {"found": len(items), "new": new}


def canvas_file_source(file_id: int) -> tuple[str, str, dict]:
    """(download URL, file name, metadata) for a Canvas File. Needs the open session."""
    f = canvas_get(f"files/{file_id}", paginate=False)
    if not f:
        raise CanvasError(f"Canvas file {file_id} is not accessible")
    return f["url"], Path(f["display_name"]).name, {"title": Path(f["display_name"]).stem,
                                                    "upload_date": f.get("created_at")}


def canvas_media_source(mid: str) -> tuple[str, str, dict]:
    """Canvas-native media (Canvas Studio/media comments): the API lists direct source URLs."""
    mo = canvas_get(f"media_objects/{mid}", paginate=False) or {}
    sources = sorted(mo.get("media_sources") or [], key=lambda s: int(s.get("bitrate") or 0))
    if not sources:
        raise CanvasError(f"Canvas media {mid}: no downloadable sources")
    src = sources[-1]
    return src["url"], f"{mid}.{src.get('fileExt') or 'mp4'}", {"title": mo.get("title") or mid,
                                                              "upload_date": mo.get("created_at")}


def stream_download(url: str, out: Path, jar=None) -> Path:
    import httpx
    out.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120, cookies=jar) as r:
        r.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    return out


def canvas_source_url(it: dict) -> str:
    """A real link for the lecture's source, not the internal downloader handle."""
    base = CANVAS_BASE
    course = find_course(it["course_id"]) or {}
    if it["kind"] == "file":
        return f"{base}/courses/{course.get('canvas_id')}/files/{it['url'].split(':', 1)[1]}"
    if it["kind"] == "media_object":
        return f"{base}/media_objects/{it['url'].split(':', 1)[1]}"
    return it["url"]


def job_canvas_pull(job: dict) -> dict:
    """payload: {course_id, key}. Download one Canvas item and ingest it."""
    cid, key = job["payload"]["course_id"], job["payload"]["key"]
    with db() as con:
        it = dict(con.execute("SELECT * FROM canvas_items WHERE course_id=? AND key=?", (cid, key)).fetchone())
    tmp = DATA / "downloads" / f"job{job['id']}"
    cookie_file = job_cookie_file(job["id"])
    try:
        job_update(job["id"], progress=f"downloading {it['title'] or key}")
        if it["url"].startswith("kaltura-unresolved:"):
            raise CanvasError("This Kaltura embed doesn't say which Kaltura account it belongs to, so it can't be "
                              f"fetched automatically. Open it from {it['page_url']}, copy the video's share link "
                              "and add that under Add recording > Link.")
        # Only Canvas-hosted items need the browser, and only to look things up and copy its
        # cookies: it's closed again before the (possibly multi-GB) download, so Connect Canvas
        # and scans aren't blocked meanwhile.
        source = cookies = None
        if it["kind"] in ("file", "media_object", "kaltura", "panopto", "mediasite"):
            canvas_ready()
            with CanvasSession() as cs:
                ref = it["url"].split(":", 1)[1]
                if it["kind"] == "file":
                    source = (*canvas_file_source(int(ref)), cs.httpx_cookies())
                elif it["kind"] == "media_object":
                    source = (*canvas_media_source(ref), cs.httpx_cookies())
                else:
                    # Videos behind UF login: let the player sign in inside our profile, then hand
                    # yt-dlp that profile's cookies (Chrome's own cookies can't be read on Windows).
                    if it.get("page_link"):
                        job_update(job["id"], progress="opening the Canvas page to sign in to the player")
                        cs.warm(it["page_link"])
                    cookies = cs.export_cookies(cookie_file)
        if source:
            url, name, meta, jar = source
            files = [(stream_download(url, tmp / Path(name).name, jar), meta)]
        else:
            files = download_url(it["url"], tmp, cookiefile=cookies,
                                 progress=lambda m: job_update(job["id"], progress=m))
        results = []
        for f, meta in files:
            job_update(job["id"], progress=f"processing {f.name}")
            meta["upload_date"] = meta.get("upload_date") or it["created_at"]
            results.append(ingest_file(f, kind="canvas", url=canvas_source_url(it), course_id=it["course_id"],
                                       title=it["title"] or meta.get("title"), meta=meta))
        with db() as con:
            con.execute("UPDATE canvas_items SET status='pulled', lecture_id=?, error=NULL WHERE course_id=? AND key=?",
                        (results[0]["lecture_id"], cid, key))
        return {"lectures": results}
    except Exception as e:
        with db() as con:
            con.execute("UPDATE canvas_items SET status='error', error=? WHERE course_id=? AND key=?",
                        (str(e)[:2000], cid, key))
        raise
    finally:
        cookie_file.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)


def canvas_pull(course_id: int, keys: list[str]) -> list[int]:
    jobs = []
    for k in keys:
        with db() as con:
            n = con.execute("UPDATE canvas_items SET status='queued' WHERE course_id=? AND key=? "
                            "AND status IN ('new','error','ignored')", (course_id, k)).rowcount
            title = con.execute("SELECT title FROM canvas_items WHERE course_id=? AND key=?", (course_id, k)).fetchone()
        if n:
            # The title is only for the jobs list; the job reads the item itself.
            jobs.append(enqueue("canvas_pull", {"course_id": course_id, "key": k, "title": title and title[0]}))
    return jobs


def canvas_auto_pull() -> list[int]:
    """For courses with auto_pull on: scan, then pull everything new. Used by sync."""
    jobs = []
    for c in list_courses():
        if c["auto_pull"] and c["canvas_id"]:
            canvas_scan_course(c)
            with db() as con:
                keys = [r["key"] for r in con.execute("SELECT key FROM canvas_items WHERE course_id=? AND status='new'",
                                                      (c["id"],))]
            jobs += canvas_pull(c["id"], keys)
    return jobs


def job_canvas_scan(job: dict) -> dict:
    course = find_course(job["payload"]["course_id"])
    if not course["canvas_id"]:
        canvas_courses()  # links LectureLab courses to Canvas ids by course code
        course = find_course(course["id"])
    if not course["canvas_id"]:
        raise CanvasError(f"No current Canvas course matches {course['code']}")
    return canvas_scan_course(course, progress=lambda m: job_update(job["id"], progress=f"scanning {m}"))


def canvas_ready() -> None:
    """Raise a clear error unless the LectureLab browser has a Canvas session."""
    st = canvas_state().get("state")
    if st == "not_connected":
        raise CanvasError("Canvas isn't connected yet: click Connect Canvas and sign in with GatorLink.")
    if st == "needs_signin":
        raise CanvasNeedsSignIn("Canvas needs sign-in: click Connect Canvas. Inbox folders keep working meanwhile.")


JOB_HANDLERS["canvas_pull"] = job_canvas_pull
JOB_HANDLERS["canvas_scan"] = job_canvas_scan


# ---------------------------------------------------------------- transcription

WSL_DISTRO = os.environ.get("LECTURELAB_WSL_DISTRO", "Ubuntu")  # the default distro is docker-desktop
COLAB_SCRIPT = ROOT / "colab" / "transcribe.py"
COLAB_REMOTE = "/content/lecturelab"
# Runs colab/transcribe.py as a child process on the VM. A native crash (e.g. CTranslate2 not
# finding cuDNN) would otherwise kill the kernel silently; this way its stderr and exit code
# come back in the exec output. The pip cuDNN/cuBLAS libraries go on LD_LIBRARY_PATH first.
COLAB_LAUNCHER = f"""
import glob, os, subprocess, sys
env = dict(os.environ)
libs = sorted({{d for site in sys.path for pat in ("nvidia/cudnn/lib", "nvidia/cublas/lib")
               for d in glob.glob(os.path.join(site, pat))}})
env["LD_LIBRARY_PATH"] = ":".join(libs + [env.get("LD_LIBRARY_PATH", "")])
p = subprocess.Popen([sys.executable, "-u", "transcribe.py"], cwd={COLAB_REMOTE!r}, env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for line in p.stdout:
    print(line, end="", flush=True)
print(f"[worker exit {{p.wait()}}]", flush=True)
"""
COLAB_GPUS = ["T4", "L4"]  # T4 is cheapest; L4 when no T4 is free
MIN_UNITS = float(os.environ.get("LECTURELAB_MIN_UNITS", 20))
SESSION_PREFIX = "lecturelab-"
LOCAL_MODELS = ["large-v3-turbo", "distil-large-v3"]
# Tests run with LECTURELAB_COLAB=off: a test server must never touch the real Colab account
# (its orphan-session sweep would stop a real batch).
COLAB_ENABLED = os.environ.get("LECTURELAB_COLAB", "on") != "off"


class LowUnits(Exception):
    pass


def meta_get(key: str, default=None):
    with db() as con:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def meta_set(key: str, value) -> None:
    with db() as con:
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))


def initial_prompt(lecture: dict) -> str:
    """Whisper's initial_prompt: course name, title, the course vocabulary and the matched slide
    text, so jargon, names and function names are spelled right. Whisper only reads the last
    ~224 tokens of a prompt, so it's capped at ~800 characters, vocabulary first."""
    course = find_course(lecture["course_id"]) if lecture.get("course_id") else None
    parts = [f"{course['code']} {course['name']} lecture: {lecture['title']}." if course
             else f"Lecture: {lecture['title']}."]
    vocab = [t for t in (course or {}).get("vocabulary", "").splitlines() if t.strip()]
    if vocab:
        parts.append("Terms: " + ", ".join(vocab) + ".")
    slides = lecture_slide_terms(lecture["id"])
    if slides:
        parts.append("Slides: " + slides)
    text = " ".join(parts)
    return text[:800].rsplit(" ", 1)[0] if len(text) > 800 else text


def lecture_slide_terms(lecture_id: int) -> str:
    """Hook for Stage 3: key terms from the slides matched to this lecture."""
    return ""


def store_transcript(lecture_id: int, result: dict, source: str) -> int:
    """Save a transcript (Colab or local JSON shape) and mark the lecture transcribed."""
    path = DATA / "transcripts" / f"{lecture_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")
    segs = result["segments"]
    with db() as con:
        con.execute("DELETE FROM segments WHERE lecture_id=?", (lecture_id,))
        con.executemany("INSERT INTO segments(lecture_id, idx, start, end, text, words) VALUES (?,?,?,?,?,?)",
                        [(lecture_id, i, s["start"], s["end"], s["text"], json.dumps(s.get("words") or []))
                         for i, s in enumerate(segs)])
        con.execute("UPDATE lectures SET status='transcribed', transcript_source=?, error=NULL, "
                    "updated_at=datetime('now') WHERE id=?", (source, lecture_id))
    log.info("lecture %s transcribed by %s: %s segments", lecture_id, source, len(segs))
    return len(segs)


def load_audio_array(path: Path):
    """16 kHz float32 via ffmpeg (not PyAV: some PyAV releases break faster-whisper's decoder)."""
    import numpy as np
    raw = run([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1",
               "-ar", "16000", "-"]).stdout
    return np.frombuffer(raw, np.float32)


_local_models: dict = {}


def transcribe_local(lecture: dict, progress=None, model_name: str | None = None) -> dict:
    """CPU fallback: faster-whisper int8 with the model the benchmark picked."""
    from faster_whisper import WhisperModel
    name = model_name or os.environ.get("LECTURELAB_LOCAL_MODEL") or meta_get("local_model", "large-v3-turbo")
    if name not in _local_models:
        _local_models.clear()  # one model in RAM at a time; they're ~1.5 GB each
        _local_models[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count())
    model = _local_models[name]
    audio = load_audio_array(DATA / lecture["audio_path"])
    t = time.time()
    segments, info = model.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 500}, word_timestamps=True,
                                      initial_prompt=initial_prompt(lecture) or None)
    segs, last = [], 0.0
    for s in segments:
        segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
                     "words": [[round(w.start, 2), round(w.end, 2), w.word, round(w.probability, 3)]
                               for w in (s.words or [])]})
        if progress and time.time() - last > 3:
            last = time.time()
            progress(f"transcribing locally ({name}): {s.end / max(info.duration, 1):.0%}")
    return {"lecture_id": lecture["id"], "model": name, "device": "cpu-int8", "language": info.language,
            "duration": round(info.duration, 2), "elapsed_sec": round(time.time() - t, 1), "segments": segs}


# --- Colab through the official CLI, inside WSL

def wsl(*args, timeout: float = 600) -> subprocess.CompletedProcess:
    return run(["wsl.exe", "-d", WSL_DISTRO, "-e", *args], timeout=timeout, text=True, encoding="utf-8",
               errors="replace")


_colab_bin: str | None = None


def colab_bin() -> str:
    global _colab_bin
    if not _colab_bin:
        out = wsl("sh", "-c", 'command -v colab || echo "$HOME/.local/bin/colab"', timeout=60).stdout.strip()
        _colab_bin = out.splitlines()[-1]
    return _colab_bin


def colab(*args, timeout: float = 900) -> str:
    """Run one colab CLI command in WSL. stdin is closed, so if the CLI was never logged in it
    fails fast with its OAuth prompt in the error instead of hanging."""
    return wsl(colab_bin(), *args, timeout=timeout).stdout


UPLOAD_CHUNK_MB = int(os.environ.get("LECTURELAB_UPLOAD_CHUNK_MB", 8))


def colab_exec_code(session: str, code: str, workdir: Path, name: str, timeout: float = 600) -> str:
    """Run a small generated script on the VM (`colab exec -f`)."""
    f = workdir / name
    f.write_text(code, encoding="utf-8")
    return colab_exec(session, wslpath(f), timeout)


def colab_exec(session: str, remote_file: str, timeout: float) -> str:
    """`colab exec` gives up after 30 s by default and still exits 0, so always pass --timeout
    (a little under ours, so the CLI returns its own output before we kill it)."""
    return colab("exec", "-s", session, "-f", remote_file, "--timeout", str(int(max(timeout - 60, 30))),
                 timeout=timeout)


def colab_upload(session: str, local: Path, remote: str, workdir: Path) -> None:
    """Upload through the CLI. Its contents API rejects big files (an 86 MB FLAC fails with an SSL
    EOF), so anything over UPLOAD_CHUNK_MB goes up in parts and is joined and checksummed on the VM."""
    size, chunk = local.stat().st_size, UPLOAD_CHUNK_MB << 20
    if size <= chunk:
        colab("upload", "-s", session, wslpath(local), remote, timeout=900)
        return
    parts_dir = workdir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    digest, parts = hashlib.sha256(), []
    try:
        with open(local, "rb") as fh:
            while block := fh.read(chunk):
                digest.update(block)
                part = parts_dir / f"{local.parent.name}-{len(parts):03d}.part"
                parts.append(part)
                part.write_bytes(block)
        for i, part in enumerate(parts):
            for attempt in (1, 2):  # one retry: the contents API sometimes drops a connection (SSL EOF)
                try:
                    colab("upload", "-s", session, wslpath(part), f"{remote}.{i:03d}.part", timeout=900)
                    break
                except RuntimeError as e:
                    if attempt == 2:
                        raise
                    log.warning("upload of part %s of %s failed, retrying: %s", i, local.name, str(e)[-200:])
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
    out = colab_exec_code(session, f"""
import hashlib, os
remote, n, want = {remote!r}, {len(parts)}, {digest.hexdigest()!r}
h = hashlib.sha256()
with open(remote, "wb") as out:
    for i in range(n):
        p = f"{{remote}}.{{i:03d}}.part"
        with open(p, "rb") as f:
            b = f.read()
        h.update(b); out.write(b); os.remove(p)
print("JOINED_OK" if h.hexdigest() == want else "JOINED_BAD " + h.hexdigest())
""", workdir, f"join-{local.parent.name}.py")
    if "JOINED_OK" not in out:
        raise RuntimeError(f"upload of {local.name} was corrupted on the way to Colab: {out.strip()[-300:]}")


def wslpath(p: Path) -> str:
    return wsl("wslpath", "-a", str(Path(p).resolve()), timeout=60).stdout.strip()


def parse_colab_units(out: str) -> float | None:
    """`colab usage` prints "Current balance: 0.00 compute units". Anything else is None (unknown),
    never a guess: a wrong number here could spend paid units."""
    m = re.search(r"(?im)^\s*current balance:\s*([\d,]+(?:\.\d+)?)\s*compute units\s*$", out)
    return float(m[1].replace(",", "")) if m else None


def parse_colab_rate(out: str) -> float | None:
    m = re.search(r"(?im)^\s*usage rate:\s*([\d.]+)\s*/\s*hr\s*$", out)
    return float(m[1]) if m else None


def colab_units() -> float | None:
    """Remaining compute units from `colab usage`, or None if the output can't be read."""
    out = colab("usage", timeout=120)
    meta_set("colab_usage_raw", out[-2000:])
    return parse_colab_units(out)


def colab_rate() -> float | None:
    """Units per hour the running session is billed at ("Usage rate: 1.07/hr")."""
    return parse_colab_rate(colab("usage", timeout=120))


def colab_sessions() -> list[str]:
    out = colab("sessions", timeout=120)
    return sorted(set(re.findall(rf"\b({re.escape(SESSION_PREFIX)}\d+)\b", out)))


def stop_orphan_sessions(keep: str | None = None) -> list[str]:
    """An idle runtime keeps burning compute units. If a batch process died before its finally
    ran, its session is still up: stop every LectureLab session that isn't the current one."""
    stopped = []
    try:
        for s in colab_sessions():
            if s != keep:
                try:
                    colab("stop", "-s", s, timeout=180)
                    stopped.append(s)
                except Exception as e:
                    log.warning("could not stop orphan Colab session %s: %s", s, e)
    except Exception as e:
        log.warning("could not list Colab sessions: %s", e)
    if stopped:
        log.warning("stopped orphaned Colab session(s): %s", stopped)
    return stopped


def sweep_orphans_if_idle() -> list[str]:
    """Stop leftover sessions only when no batch is running anywhere (we can take the transcribe
    lock). Otherwise a server restart would kill the session another process is using."""
    lk = FileLock(DATA / "transcribe.lock")
    if not lk.acquire():
        return []
    try:
        return stop_orphan_sessions()
    finally:
        lk.release()


def colab_new_session(name: str) -> str:
    errors = []
    for gpu in COLAB_GPUS:
        try:
            colab("new", "-s", name, "--gpu", gpu, timeout=900)
            return gpu
        except Exception as e:
            errors.append(f"{gpu}: {e}")
            log.warning("colab new --gpu %s failed: %s", gpu, e)
    raise RuntimeError("no Colab GPU available: " + " | ".join(errors))


def transcribe_batch_colab(lectures: list[dict], batch_id: int, progress=None, force: bool = False) -> list[int]:
    """One Colab session for the whole batch: new -> upload -> exec -> download -> stop (always).
    Each finished transcript is stored as soon as it's downloaded, so an interrupted batch keeps
    its finished lectures and the rest stay queued. force: you asked for Colab explicitly, so an
    unreadable balance doesn't stop it (a known low balance still does)."""
    say = progress or (lambda m: None)
    session = f"{SESSION_PREFIX}{batch_id}"
    stop_orphan_sessions()
    units_before = colab_units()
    # 0 units means the free tier: there's nothing to burn, so a free T4 is fine. A small paid
    # balance is protected instead: below MIN_UNITS the batch goes to the local CPU.
    if units_before is None and not force:
        raise LowUnits("couldn't read the Colab compute-unit balance from `colab usage`; not risking paid units")
    if units_before is not None and 0 < units_before < MIN_UNITS:
        raise LowUnits(f"only {units_before:g} Colab compute units left (minimum {MIN_UNITS:g})")
    with db() as con:
        con.execute("UPDATE transcribe_batches SET units_before=? WHERE id=?", (units_before, batch_id))
    manifest = {"model": "large-v3", "items": [
        {"lecture_id": l["id"], "audio": f"{l['id']}.flac", "initial_prompt": initial_prompt(l)} for l in lectures]}
    mpath = DATA / "colab_batches" / str(batch_id) / "manifest.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    done: list[int] = []
    gpu = None

    def fetch_results(budget: float = 900):
        # A transcript is a few MB at most. The budget bounds how long a hung WSL/CLI can keep
        # the (billed) session alive before the stop in the finally below.
        end = time.time() + budget
        for l in lectures:
            if l["id"] in done:
                continue
            left = end - time.time()
            if left < 10:
                log.warning("batch %s: out of time downloading results; the rest stay queued", batch_id)
                return
            local = mpath.parent / f"{l['id']}.json"
            try:
                colab("download", "-s", session, f"{COLAB_REMOTE}/out/{l['id']}.json", wslpath(local),
                      timeout=min(180, left))
            except Exception:
                continue  # not finished; it stays queued
            if local.exists():
                try:
                    store_transcript(l["id"], json.loads(local.read_text(encoding="utf-8")), f"colab-large-v3 ({gpu})")
                except Exception as e:  # one bad file mustn't cost the others (or mask the real error)
                    log.error("batch %s: couldn't store the transcript of lecture %s: %s", batch_id, l["id"], e)
                    continue
                done.append(l["id"])
                with db() as con:
                    con.execute("UPDATE transcribe_batches SET done_ids=? WHERE id=?", (json.dumps(done), batch_id))

    try:
        say("starting a Colab GPU")
        gpu = colab_new_session(session)
        with db() as con:
            con.execute("UPDATE transcribe_batches SET gpu=? WHERE id=?", (gpu, batch_id))
        try:
            rate = colab_rate()
            log.info("Colab %s session %s: billed at %s units/hr", gpu, session, rate)
            with db() as con:
                con.execute("UPDATE transcribe_batches SET units_per_hour=? WHERE id=?", (rate, batch_id))
        except Exception as e:
            log.warning("could not read the Colab usage rate: %s", e)
        say(f"uploading to Colab ({gpu})")
        # The contents API won't create folders (a 500 if the parent is missing), so make them first.
        colab_exec_code(session, f"import os; os.makedirs({COLAB_REMOTE + '/out'!r}, exist_ok=True)",
                        mpath.parent, "prep.py")
        colab("upload", "-s", session, wslpath(mpath), f"{COLAB_REMOTE}/manifest.json", timeout=600)
        colab("upload", "-s", session, wslpath(COLAB_SCRIPT), f"{COLAB_REMOTE}/transcribe.py", timeout=600)
        t_up = time.time()
        for i, l in enumerate(lectures, 1):
            say(f"uploading audio {i}/{len(lectures)} to Colab ({gpu})")
            colab_upload(session, DATA / l["audio_path"], f"{COLAB_REMOTE}/{l['id']}.flac", mpath.parent)
        log.info("batch %s: uploaded %s file(s) in %.0fs", batch_id, len(lectures), time.time() - t_up)
        say(f"transcribing {len(lectures)} lecture(s) on Colab {gpu}")
        out = colab_exec_code(session, COLAB_LAUNCHER, mpath.parent, "run.py", timeout=4 * 3600)
        (mpath.parent / "exec.log").write_text(out, encoding="utf-8")
        log.info("colab exec output (batch %s):\n%s", batch_id, out[-3000:])
        say("downloading transcripts")
        fetch_results()
        if len(done) < len(lectures):
            # exec can end early without an error (timeout, kernel crash): that's a failed batch.
            tail = out.strip().splitlines()[-3:] or ["(no output)"]
            raise RuntimeError(f"Colab returned {len(done)} of {len(lectures)} transcript(s); "
                               f"last output: {' | '.join(tail)}")
    finally:
        try:
            if gpu:  # a session exists: keep whatever finished before a failure
                fetch_results(budget=300)
        finally:
            for attempt in (1, 2):
                try:
                    colab("stop", "-s", session, timeout=300)
                    break
                except Exception as e:
                    log.error("colab stop -s %s failed (attempt %s): %s%s", session, attempt, e,
                              " (will retry before the next batch)" if attempt == 2 else "")
    return done


def finish_batch(batch_id: int, t0: float, ok: bool, error: str | None = None, units_after: float | None = None):
    with db() as con:
        con.execute("UPDATE transcribe_batches SET ok=?, error=?, units_after=?, wall_sec=?, ended_at=datetime('now') "
                    "WHERE id=?", (int(ok), error, units_after, round(time.time() - t0, 1), batch_id))
        b = dict(con.execute("SELECT * FROM transcribe_batches WHERE id=?", (batch_id,)).fetchone())
    used = (b["units_before"] - units_after) if (b["units_before"] is not None and units_after is not None) else None
    per75 = used / (b["audio_sec"] / 4500) if used is not None and b["audio_sec"] else None
    log.info("transcribe batch %s (%s %s): ok=%s, %s lecture(s), %.1f min audio, %.1f min wall, units %s -> %s "
             "(used %s, %s per 75-min lecture)%s", batch_id, b["mode"], b["gpu"], ok, len(json.loads(b["done_ids"])),
             (b["audio_sec"] or 0) / 60, (time.time() - t0) / 60, b["units_before"], units_after,
             f"{used:.2f}" if used is not None else "?", f"{per75:.2f}" if per75 is not None else "?",
             f" error: {error}" if error else "")


def job_transcribe(job: dict) -> dict:
    """payload: {lecture_ids?: [...], mode: auto|colab|local}. With no ids, takes every queued lecture.
    auto = Colab, unless units are low or Colab failed twice in a row; then local CPU."""
    mode = job["payload"].get("mode", "auto")
    say = lambda m: job_update(job["id"], progress=m)  # noqa: E731
    lock = FileLock(DATA / "transcribe.lock")
    if not lock.acquire():
        job_update(job["id"], status="queued", retry_at=_sqlnow(120), owner_pid=None)
        raise RetryLater("another transcription batch is running")
    try:
        with db() as con:
            con.execute("BEGIN IMMEDIATE")
            ids = job["payload"].get("lecture_ids")
            if ids is not None:
                # Lectures you picked: a failed one ('error') may be retried on purpose.
                lectures = rows(con.execute(
                    "SELECT * FROM lectures WHERE status IN ('queued_transcribe','error') AND audio_path IS NOT NULL "
                    f"AND id IN ({','.join('?' * len(ids)) or 'NULL'}) ORDER BY id", ids))
            else:
                # "Everything waiting": only the queue, never lectures that already failed.
                lectures = rows(con.execute("SELECT * FROM lectures WHERE status='queued_transcribe' "
                                            "AND audio_path IS NOT NULL ORDER BY id"))
            missing = [l for l in lectures if not (DATA / l["audio_path"]).exists()]
            for l in missing:  # a local problem: don't send it anywhere, and don't blame Colab for it
                con.execute("UPDATE lectures SET status='error', error=? WHERE id=?",
                            (f"audio file missing: {l['audio_path']}", l["id"]))
            lectures = [l for l in lectures if l not in missing]
            if not lectures:
                return {"transcribed": [], "note": "nothing waiting"}
            for l in lectures:
                con.execute("UPDATE lectures SET status='transcribing', error=NULL WHERE id=?", (l["id"],))
            audio_sec = sum(l["duration_sec"] or 0 for l in lectures)
        use_local = mode == "local"
        reason = "requested" if use_local else None
        if mode == "auto" and not COLAB_ENABLED:
            use_local, reason = True, "Colab disabled (LECTURELAB_COLAB=off)"
        if mode == "auto" and colab_backed_off():
            use_local, reason = True, f"Colab failed twice in a row; trying it again after {COLAB_BACKOFF_H:g} h"
        done, t0 = [], time.time()
        batch_id = None
        try:
            if not use_local:
                with db() as con:
                    batch_id = con.execute("INSERT INTO transcribe_batches(mode, lecture_ids, audio_sec) VALUES "
                                           "('colab',?,?)", (json.dumps([l["id"] for l in lectures]), audio_sec)).lastrowid
                try:
                    done = transcribe_batch_colab(lectures, batch_id, progress=say, force=mode == "colab")
                    meta_set("colab_failures", 0)
                    finish_batch(batch_id, t0, ok=True, units_after=_safe_units())
                except LowUnits as e:
                    finish_batch(batch_id, t0, ok=False, error=str(e))
                    if mode == "colab":
                        raise
                    use_local, reason = True, str(e)
                except Exception as e:
                    meta_set("colab_failures", meta_get("colab_failures", 0) + 1)
                    meta_set("colab_failed_at", time.time())
                    finish_batch(batch_id, t0, ok=False, error=str(e)[:2000], units_after=_safe_units())
                    raise
            if use_local:
                todo = [l for l in lectures if l["id"] not in done]
                model = os.environ.get("LECTURELAB_LOCAL_MODEL") or meta_get("local_model", "large-v3-turbo")
                with db() as con:
                    batch_id = con.execute("INSERT INTO transcribe_batches(mode, gpu, lecture_ids, audio_sec) VALUES "
                                           "('local',?,?,?)", (model, json.dumps([l["id"] for l in todo]),
                                                               sum(l["duration_sec"] or 0 for l in todo))).lastrowid
                log.info("transcribing %s lecture(s) locally (%s): %s", len(todo), model, reason)
                t0 = time.time()
                try:
                    for l in todo:
                        res = transcribe_local(l, progress=say)
                        store_transcript(l["id"], res, f"local-{res['model']}")
                        done.append(l["id"])
                except Exception as e:
                    finish_batch(batch_id, t0, ok=False, error=str(e)[:2000])  # never leave it looking busy
                    raise
                finish_batch(batch_id, t0, ok=True)
            return {"transcribed": done, "mode": "local" if use_local else "colab", "reason": reason}
        finally:
            # Anything not finished goes back to the queue for the next run; nothing is redone.
            with db() as con:
                for l in lectures:
                    if l["id"] not in done:
                        con.execute("UPDATE lectures SET status='queued_transcribe' WHERE id=? AND status='transcribing'",
                                    (l["id"],))
    finally:
        lock.release()


COLAB_BACKOFF_H = float(os.environ.get("LECTURELAB_COLAB_BACKOFF_H", "6"))


def colab_backed_off() -> bool:
    """After 2 Colab failures in a row, auto mode uses the CPU for a while, then tries Colab again
    (a success resets the count). Without the time limit it would never try Colab again."""
    if meta_get("colab_failures", 0) < 2:
        return False
    return time.time() - meta_get("colab_failed_at", 0) < COLAB_BACKOFF_H * 3600


def _safe_units():
    try:
        return colab_units()
    except Exception as e:
        log.warning("colab usage failed: %s", e)
        return None


def _sqlnow(plus_sec: int = 0) -> str:
    return datetime.fromtimestamp(time.time() + plus_sec, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class RetryLater(Exception):
    """Raised by a job that re-queued itself (it set its own status and retry_at)."""


def reset_stuck_transcribing() -> int:
    """Lectures left 'transcribing' by a process that died go back to the queue, and that
    process's batch row is closed (an open row makes the UI think a batch is still running)."""
    lk = FileLock(DATA / "transcribe.lock")
    if not lk.acquire():
        return 0  # a batch is genuinely running
    try:
        with db() as con:
            n = con.execute("UPDATE lectures SET status='queued_transcribe' WHERE status='transcribing'").rowcount
            con.execute("UPDATE transcribe_batches SET ok=0, error='interrupted (the process stopped mid-batch)', "
                        "ended_at=datetime('now') WHERE ended_at IS NULL")
    finally:
        lk.release()
    if n:
        log.warning("requeued %s lecture(s) left mid-transcription", n)
    return n


JOB_HANDLERS["transcribe"] = job_transcribe


def normalize_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower().replace("-", " "))


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate: word-level edit distance / reference length."""
    r, h = normalize_words(reference), normalize_words(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1] / len(r)


def benchmark_local(lecture_id: int, start: float = 120, length: float = 120) -> dict:
    """Pick the CPU fallback model: transcribe the same 2-minute slice with each candidate and
    score it against that lecture's Colab large-v3 transcript (the best transcript we have).
    The most accurate model wins unless it's more than 2x slower than the fastest."""
    lec = get_lecture(lecture_id)
    if not (lec.get("transcript_source") or "").startswith("colab"):
        raise ValueError(f"lecture {lecture_id} needs a Colab large-v3 transcript to score against")
    with db() as con:
        segs = rows(con.execute("SELECT start, end, text, words FROM segments WHERE lecture_id=? ORDER BY idx",
                                (lecture_id,)))
    ref_words = [w for s in segs for w in json.loads(s["words"] or "[]") if start <= w[0] < start + length]
    reference = " ".join(w[2] for w in ref_words)
    raw = run([find_tool("ffmpeg"), "-nostdin", "-v", "error", "-ss", start, "-t", length, "-i", DATA / lec["audio_path"],
               "-f", "f32le", "-ac", "1", "-ar", "16000", "-"]).stdout
    import numpy as np
    from faster_whisper import WhisperModel
    audio = np.frombuffer(raw, np.float32)
    results = {}
    for name in LOCAL_MODELS:
        model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count())
        t = time.time()
        sgs, _ = model.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                  initial_prompt=initial_prompt(lec) or None)
        text = " ".join(x.text.strip() for x in sgs)
        took = time.time() - t
        results[name] = {"seconds": round(took, 1), "rtf": round(took / length, 3), "wer": round(wer(reference, text), 4),
                         "minutes_per_75min": round(took / length * 75, 1)}
        log.info("benchmark %s: %s", name, results[name])
        del model
    fastest = min(r["seconds"] for r in results.values())
    eligible = {n: r for n, r in results.items() if r["seconds"] <= 2 * fastest}
    pick = min(eligible, key=lambda n: (eligible[n]["wer"], eligible[n]["seconds"]))
    report = {"lecture_id": lecture_id, "slice": [start, start + length], "reference_words": len(ref_words),
              "results": results, "picked": pick, "cpu": os.cpu_count(), "at": datetime.now().isoformat(timespec="seconds")}
    meta_set("benchmark", report)
    meta_set("local_model", pick)
    return report


# ---------------------------------------------------------------- v1 import

V1_COURSE_MAP = {"ethics": "PHI3681", "programming language concepts": "COP4020",
                 "math for machine learning": "MML"}


def import_v1(v1_db: Path) -> list[dict]:
    """One-time: bring v1 recordings and transcripts in. Safe to re-run (dedupe by content hash).

    v1 transcripts come from Whisper 'base'; they're kept as transcript_source='v1' so the
    lecture is usable right away, and the lecture stays queued for a large-v3 re-transcription.
    """
    v1_db = Path(v1_db)
    audio_dir = v1_db.parent / "audio"
    src = sqlite3.connect(f"file:{v1_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        return _import_v1_rows(src, audio_dir)
    finally:
        src.close()


def _import_v1_rows(src, audio_dir: Path) -> list[dict]:
    out = []
    for lec in src.execute("""SELECT l.*, c.name AS course_name FROM lectures l
                              LEFT JOIN courses c ON c.id=l.course_id ORDER BY l.id"""):
        audio = audio_dir / lec["audio_file"] if lec["audio_file"] else None
        if not audio or not audio.exists():
            # v1 sometimes lost the link while the file stayed on disk: lecture_<id>_<hash>.webm
            matches = sorted(audio_dir.glob(f"lecture_{lec['id']}_*"))
            audio = matches[0] if matches else None
        if not audio:
            out.append({"v1_id": lec["id"], "title": lec["title"], "skipped": "no audio file"})
            continue
        course = find_course(V1_COURSE_MAP.get((lec["course_name"] or "").lower(), lec["course_name"]))
        res = ingest_file(audio, kind="v1", url=f"v1://lecture/{lec['id']}", course_id=course and course["id"],
                          title=lec["title"], date=parse_date(lec["created_at"]))  # v1 stored epoch seconds
        segs = json.loads(lec["transcript"] or "[]")
        with db() as con:
            has = con.execute("SELECT COUNT(*) FROM segments WHERE lecture_id=?", (res["lecture_id"],)).fetchone()[0]
            if segs and not has:
                con.executemany("INSERT INTO segments(lecture_id, idx, start, end, text) VALUES (?,?,?,?,?)",
                                [(res["lecture_id"], i, s["start"], s["end"], s["text"].strip())
                                 for i, s in enumerate(segs)])
                con.execute("UPDATE lectures SET transcript_source='v1' WHERE id=?", (res["lecture_id"],))
        out.append({"v1_id": lec["id"], "title": lec["title"], "lecture_id": res["lecture_id"],
                    "duplicate": res["duplicate"], "segments": len(segs), "audio": audio.name})
    return out


# ---------------------------------------------------------------- web API

# Request models live at module level for the same reason as the FastAPI imports above.
class CoursePatch(BaseModel):
    name: str | None = None
    vocabulary: str | None = None


class RecordingIn(BaseModel):
    url: str
    course_id: int | None = None
    title: str | None = None
    date: str | None = None
    passcode: str | None = None


class LecturePatch(BaseModel):
    title: str | None = None
    date: str | None = None
    course_id: int | None = None


class CanvasPull(BaseModel):
    course_id: int
    keys: list[str]


class AutoPull(BaseModel):
    auto_pull: bool


class TranscribeIn(BaseModel):
    lecture_ids: list[int] | None = None
    mode: str = "auto"  # auto | colab | local


def check_ingest_fields(course_id: int | None, date: str | None) -> None:
    """Reject bad fields up front, not after a 20-minute download."""
    if course_id is not None and not find_course(course_id):
        raise HTTPException(400, f"no course with id {course_id}")
    if date and not re.fullmatch(r"\d{4}-\d\d-\d\d", date):
        raise HTTPException(400, "date must be YYYY-MM-DD")


WORKERS: dict[str, Worker] = {}


def wake_workers() -> None:
    for w in WORKERS.values():
        if isinstance(w, Worker):
            w.wake.set()


def start_background(watch_inboxes: bool = True) -> None:
    requeue_orphaned_jobs()
    reset_stuck_transcribing()
    if COLAB_ENABLED:  # off the startup path: WSL can take a few seconds to wake
        threading.Thread(target=sweep_orphans_if_idle, daemon=True, name="colab-sweep").start()
    if "transcribe" not in WORKERS:
        WORKERS["transcribe"] = Worker(["transcribe"], "transcribe-worker")
        WORKERS["transcribe"].start()
    if "ingest" not in WORKERS:
        WORKERS["ingest"] = Worker(["ingest"], "ingest-worker")
        WORKERS["ingest"].start()
        WORKERS["canvas"] = Worker(["canvas_scan", "canvas_pull"], "canvas-worker")
        WORKERS["canvas"].start()
    if watch_inboxes and "inbox" not in WORKERS:
        WORKERS["inbox"] = InboxWatcher(interval=float(os.environ.get("LECTURELAB_INBOX_INTERVAL", 30)),
                                        on_new=wake_workers)
        WORKERS["inbox"].start()


def create_app():
    @asynccontextmanager
    async def lifespan(_app):
        init_db()
        start_background()
        log.info("server up on 127.0.0.1:%s pid %s (data=%s)", PORT, os.getpid(), DATA)
        yield
        log.info("server shutting down")

    app = FastAPI(title="LectureLab", version=VERSION, lifespan=lifespan)
    allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Bound to 127.0.0.1, but a web page could still reach us through DNS rebinding
        # (wrong Host header) or a cross-site form post (foreign Origin). Refuse both.
        if request.headers.get("host") not in allowed_hosts:
            return PlainTextResponse("bad host", status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and origin.split("//", 1)[-1] not in allowed_hosts:
            return PlainTextResponse("bad origin", status_code=403)
        return await call_next(request)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": VERSION, "pid": os.getpid(), "time": time.time()}

    @app.get("/api/courses")
    def api_courses():
        return list_courses()

    @app.put("/api/courses/{course_id}")
    def api_update_course(course_id: int, body: CoursePatch):
        try:
            return update_course(course_id, body.name, body.vocabulary)
        except KeyError:
            raise HTTPException(404, "course not found")
        except ValueError as e:
            raise HTTPException(400, str(e))

    # --- recordings / lectures
    @app.post("/api/recordings")
    def api_add_url(body: RecordingIn):
        url = body.url.strip()
        if not re.match(r"https?://", url):
            raise HTTPException(400, "that doesn't look like a link (expected http:// or https://)")
        check_ingest_fields(body.course_id, body.date)
        jid = enqueue("ingest", {"url": url, "course_id": body.course_id, "title": body.title or None,
                                 "date": body.date or None, "passcode": body.passcode or None})
        wake_workers()
        return {"job_id": jid}

    @app.post("/api/recordings/upload")
    def api_upload(file: UploadFile = File(...), course_id: int | None = Form(None),
                   title: str | None = Form(None), date: str | None = Form(None)):
        # Plain def: FastAPI runs it in a thread, so the copy and DB calls don't block the event loop.
        name = Path(file.filename or "upload").name
        if Path(name).suffix.lower() not in MEDIA_EXTS:
            raise HTTPException(400, f"unsupported file type; use one of {', '.join(sorted(MEDIA_EXTS))}")
        check_ingest_fields(course_id, date)
        # The stored name is ours (uuid + extension); the user's filename only travels as metadata,
        # so names like CON.mp4 or ../../x.mp4 never reach the filesystem.
        dest = DATA / "uploads" / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(dest, "wb") as out:
                shutil.copyfileobj(file.file, out, 1 << 20)
        except BaseException:
            dest.unlink(missing_ok=True)  # an aborted upload leaves nothing behind
            raise
        jid = enqueue("ingest", {"path": str(dest), "kind": "file", "source_url": f"upload:{name}",
                                 "course_id": course_id, "title": title or Path(name).stem, "date": date or None,
                                 "delete_after": True})
        wake_workers()
        return {"job_id": jid}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int):
        j = get_job(job_id)
        if not j:
            raise HTTPException(404, "job not found")
        j.pop("payload", None)
        return j

    @app.get("/api/jobs")
    def api_jobs(active: bool = True):
        with db() as con:
            q = "SELECT id, kind, status, progress, error, lecture_id, payload, updated_at FROM jobs"
            q += " WHERE status IN ('queued','running') ORDER BY id" if active else " ORDER BY id DESC LIMIT 50"
            out = rows(con.execute(q))
        for j in out:
            p = json.loads(j.pop("payload") or "{}")
            if j["kind"] == "transcribe":
                ids = p.get("lecture_ids")
                j["label"] = f"{'lecture ' + ', '.join(map(str, ids)) if ids else 'everything waiting'} ({p.get('mode', 'auto')})"
            else:
                j["label"] = (p.get("url") or p.get("title") or p.get("key") or Path(p.get("path", "")).name
                              or (find_course(p.get("course_id")) or {}).get("code", ""))
        return out

    @app.get("/api/lectures")
    def api_lectures(course_id: int | None = None):
        return list_lectures(course_id)

    @app.get("/api/lectures/{lecture_id}")
    def api_lecture(lecture_id: int):
        lec = get_lecture(lecture_id)
        if not lec:
            raise HTTPException(404, "lecture not found")
        return lec

    @app.patch("/api/lectures/{lecture_id}")
    def api_patch_lecture(lecture_id: int, body: LecturePatch):
        if body.course_id is not None and not find_course(body.course_id):
            raise HTTPException(400, f"no course with id {body.course_id}")
        try:
            return update_lecture(lecture_id, **body.model_dump())
        except KeyError:
            raise HTTPException(404, "lecture not found")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/lectures/{lecture_id}/not-duplicate")
    def api_not_duplicate(lecture_id: int):
        return release_duplicate(lecture_id)

    @app.delete("/api/lectures/{lecture_id}")
    def api_delete_lecture(lecture_id: int):
        delete_lecture(lecture_id)
        return {"ok": True}

    @app.get("/api/lectures/{lecture_id}/media/{which}")
    def api_media(lecture_id: int, which: str):
        lec = get_lecture(lecture_id)
        rel = lec and lec.get({"audio": "audio_path", "video": "video_path"}.get(which, ""))
        if not rel or not (DATA / rel).is_file():
            raise HTTPException(404, "no such media")
        return FileResponse(DATA / rel)  # supports Range requests, so the player can seek

    # --- Canvas (through LectureLab's own browser profile; you sign in there yourself)
    @app.get("/api/canvas/status")
    def api_canvas_status():
        st = canvas_state()
        lk = FileLock(DATA / "browser.lock")
        busy = not lk.acquire()
        if not busy:
            lk.release()
        return {**st, "browser_busy": busy, "base": CANVAS_BASE}

    @app.post("/api/canvas/connect")
    def api_canvas_connect():
        lk = FileLock(DATA / "browser.lock")
        if not lk.acquire():
            raise HTTPException(409, "The LectureLab browser is already open; finish signing in there.")
        lk.release()
        # Its own process: the sign-in window must be a normal visible window, and Playwright's
        # browser shouldn't live inside the server process.
        subprocess.Popen([sys.executable, str(ROOT / "cli.py"), "canvas-login"], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("opened the LectureLab browser for Canvas sign-in")
        return {"opened": True}

    @app.get("/api/canvas/courses")
    def api_canvas_courses():
        try:
            return canvas_courses()
        except CanvasError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/canvas/scan/{course_id}")
    def api_canvas_scan(course_id: int):
        if not find_course(course_id):
            raise HTTPException(404, "course not found")
        try:
            canvas_ready()
        except CanvasError as e:
            raise HTTPException(400, str(e))
        jid = enqueue("canvas_scan", {"course_id": course_id})
        wake_workers()
        return {"job_id": jid}

    @app.get("/api/canvas/items")
    def api_canvas_items(course_id: int):
        with db() as con:
            return rows(con.execute("SELECT * FROM canvas_items WHERE course_id=? ORDER BY created_at DESC, key",
                                    (course_id,)))

    @app.post("/api/canvas/pull")
    def api_canvas_pull(body: CanvasPull):
        jobs = canvas_pull(body.course_id, body.keys)
        wake_workers()
        return {"job_ids": jobs}

    @app.post("/api/canvas/ignore")
    def api_canvas_ignore(body: CanvasPull):
        with db() as con:
            con.executemany("UPDATE canvas_items SET status='ignored' WHERE course_id=? AND key=?",
                            [(body.course_id, k) for k in body.keys])
        return {"ok": True}

    @app.put("/api/courses/{course_id}/auto-pull")
    def api_auto_pull(course_id: int, body: AutoPull):
        if not find_course(course_id):
            raise HTTPException(404, "course not found")
        with db() as con:
            con.execute("UPDATE courses SET auto_pull=? WHERE id=?", (int(body.auto_pull), course_id))
        return find_course(course_id)

    # --- transcription
    @app.post("/api/transcribe")
    def api_transcribe(body: TranscribeIn):
        if body.mode not in ("auto", "colab", "local"):
            raise HTTPException(400, "mode must be auto, colab or local")
        with db() as con:
            busy = con.execute("SELECT id FROM jobs WHERE kind='transcribe' AND status IN ('queued','running')").fetchone()
        if busy and not body.lecture_ids:
            return {"job_id": busy["id"], "note": "a batch is already queued"}
        jid = enqueue("transcribe", {"lecture_ids": body.lecture_ids, "mode": body.mode})
        wake_workers()
        return {"job_id": jid}

    @app.get("/api/transcribe/status")
    def api_transcribe_status():
        with db() as con:
            batches = rows(con.execute("SELECT * FROM transcribe_batches ORDER BY id DESC LIMIT 10"))
            queued = con.execute("SELECT COUNT(*) FROM lectures WHERE status='queued_transcribe'").fetchone()[0]
        return {"colab_enabled": COLAB_ENABLED, "colab_failures": meta_get("colab_failures", 0),
                "local_model": os.environ.get("LECTURELAB_LOCAL_MODEL") or meta_get("local_model", "large-v3-turbo"),
                "benchmark": meta_get("benchmark"), "min_units": MIN_UNITS, "queued": queued, "batches": batches}

    @app.get("/api/lectures/{lecture_id}/segments")
    def api_segments(lecture_id: int):
        with db() as con:
            segs = rows(con.execute("SELECT idx, start, end, text, words, slide_no FROM segments "
                                    "WHERE lecture_id=? ORDER BY idx", (lecture_id,)))
        for sg in segs:
            sg["words"] = json.loads(sg["words"]) if sg["words"] else []
        return segs

    @app.get("/api/inboxes")
    def api_inboxes():
        return [{"path": str(p), "kind": k, "exists": p.exists()} for p, k in inbox_roots()]

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
