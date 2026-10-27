"""LectureLab v2: turns lecture recordings into study material.

One module on purpose. Sections, top to bottom:
  config / logging / lock  ->  database  ->  courses  ->  web API
Later stages add ingest, transcription, slides, notes, export and sync below.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA = Path(os.environ.get("LECTURELAB_DATA", ROOT / "data")).resolve()
DB_PATH = DATA / "lecturelab.db"
PORT = int(os.environ.get("LECTURELAB_PORT", "8765"))
LOG_PATH = Path(os.environ.get("LECTURELAB_LOG", ROOT / "server.log"))
VERSION = "2.0.0"

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
]

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


# ---------------------------------------------------------------- web API

# Request models live at module level: with `from __future__ import annotations`,
# FastAPI can't resolve models defined inside create_app() and treats them as query params.
class CoursePatch(BaseModel):
    name: str | None = None
    vocabulary: str | None = None


def create_app():
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    @asynccontextmanager
    async def lifespan(_app):
        init_db()
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

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
