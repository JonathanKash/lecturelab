"""`lecturelab` command line. Run `lecturelab -h` for the list of commands."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import lecturelab as ll

log = logging.getLogger("lecturelab.cli")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STOP_FLAG = ll.DATA / "supervisor.stop"


class _QuietHealth(logging.Filter):
    """Drop the UI's 15-second /api/health polls from the access log."""

    def filter(self, record):
        return "/api/health" not in record.getMessage()


def cmd_serve(args) -> int:
    import uvicorn
    ll.setup_logging(console=sys.stdout is not None, rotate=True)
    logging.getLogger("uvicorn.access").addFilter(_QuietHealth())
    # log_config=None: uvicorn logs through our root logger. Its default config
    # writes to stderr, which is None under pythonw and would crash the server.
    uvicorn.run(ll.create_app(), host="127.0.0.1", port=ll.PORT, log_config=None)
    return 0


def cmd_supervise(args) -> int:
    """Keep the server running: restart it with backoff whenever it exits."""
    ll.setup_logging(console=sys.stdout is not None)
    lock = ll.FileLock(ll.DATA / "supervisor.lock")
    if not lock.acquire():
        log.info("supervisor already running; exiting")
        return 0
    try:
        STOP_FLAG.unlink(missing_ok=True)
        (ll.DATA / "supervisor.pid").write_text(str(os.getpid()))
        backoff = 1
        while True:
            started = time.time()
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"], cwd=ll.ROOT,
                                     creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # A venv's python.exe is a launcher stub that spawns the real interpreter, so child.pid
            # is the launcher; the server logs its own pid on startup. The launcher exits with it.
            log.info("supervisor %s started server (launcher pid %s)", os.getpid(), child.pid)
            code = child.wait()
            if STOP_FLAG.exists():
                STOP_FLAG.unlink(missing_ok=True)
                log.info("supervisor stopping on request")
                return 0  # clean exit, so Task Scheduler doesn't treat it as a failure and restart
            if time.time() - started > 300:
                backoff = 1  # it ran fine for a while, so this is a fresh crash, not a crash loop
            log.warning("server exited with code %s; restarting in %ss", code, backoff)
            for _ in range(backoff * 4):  # `stop` during the wait: don't start a server it can't see to kill
                if STOP_FLAG.exists():
                    break
                time.sleep(0.25)
            if STOP_FLAG.exists():
                STOP_FLAG.unlink(missing_ok=True)
                log.info("supervisor stopping on request")
                return 0
            backoff = min(backoff * 2, 60)
    except Exception:
        log.exception("supervisor crashed")  # pythonw has no console; make the death visible
        raise
    finally:
        (ll.DATA / "supervisor.pid").unlink(missing_ok=True)
        lock.release()


def _live_server_pid() -> int | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ll.PORT}/api/health", timeout=3) as r:
            return json.load(r)["pid"]
    except Exception:
        return None


def cmd_stop(args) -> int:
    """Stop the supervisor and server. Never trusts a pid file alone (pids get reused)."""
    probe = ll.FileLock(ll.DATA / "supervisor.lock")
    if probe.acquire():
        probe.release()
        pid = _live_server_pid()
        if pid:  # a server started by hand with `serve`, no supervisor
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            print(f"stopped unsupervised server pid {pid}")
            return 0
        print("not running")
        return 0
    STOP_FLAG.write_text("stop")
    pid = _live_server_pid()  # the live server reports its own pid, so this can't be stale
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    for _ in range(40):  # the supervisor sees the flag when its child exits, then quits
        if probe.acquire():
            probe.release()
            print("stopped")
            return 0
        time.sleep(0.25)
    print("supervisor did not stop; see server.log")
    return 1


def cmd_add(args) -> int:
    """Add a recording from a file path or any URL (Zoom, Drive, YouTube, Panopto, ...)."""
    ll.setup_logging(console=True)
    course = ll.find_course(args.course)
    if args.course and not course:
        print(f"unknown course {args.course!r}; known: {', '.join(c['code'] for c in ll.list_courses())}")
        return 2
    if args.date and not ll.valid_date(args.date):  # up front, not after a 20-minute download
        print(f"--date {args.date!r} must be a real day as YYYY-MM-DD")
        return 2
    payload = {"course_id": course and course["id"], "title": args.title, "date": args.date}
    if args.source.lower().startswith(("http://", "https://")):
        payload.update(url=args.source, passcode=args.passcode)
    else:
        path = Path(args.source).expanduser().resolve()
        if not path.is_file():
            print(f"no such file: {path}")
            return 2
        payload.update(path=str(path), kind="file")
    job = ll.run_job_now(ll.enqueue("ingest", payload))
    if job["status"] != "done":
        print(f"FAILED: {job['error']}")
        return 1
    for r in job["result"]["lectures"]:
        lec = ll.get_lecture(r["lecture_id"])
        tag = f"DUPLICATE ({r['how']}) of" if r["duplicate"] else "added"
        print(f"{tag} lecture {lec['id']}: {lec['title']} [{lec['course_code'] or 'no course'}] "
              f"{lec['date']} {lec['duration_sec'] or 0:.0f}s status={lec['status']}")
    return 0


def cmd_import_v1(args) -> int:
    ll.setup_logging(console=True)
    for r in ll.import_v1(Path(args.db)):
        print(r)
    return 0


def cmd_transcribe(args) -> int:
    """Transcribe queued lectures now: one Colab batch (or local CPU with --local)."""
    ll.setup_logging(console=True)
    mode = "local" if args.local else "colab" if args.colab else "auto"
    job = ll.run_job_now(ll.enqueue("transcribe", {"lecture_ids": args.ids or None, "mode": mode}))
    if job["status"] != "done":
        print(f"FAILED: {job['error']}")
        return 1
    print(job["result"])
    return 0


def cmd_slides(args) -> int:
    """Download queued decks, then find, read and align slides for lectures (default: every
    transcribed lecture that isn't aligned yet). Runs here, not in the server."""
    ll.setup_logging(console=True)
    with ll.db() as con:
        pulls = [r[0] for r in con.execute("SELECT id FROM jobs WHERE kind='deck_pull' AND status='queued' ORDER BY id")]
        ids = args.ids or [r[0] for r in con.execute(
            "SELECT l.id FROM lectures l WHERE l.slides_aligned_at IS NULL AND COALESCE(l.deck_how,'') != 'nothing' "
            "AND NOT (l.deck_how='manual' AND l.deck_id IS NULL) AND l.status NOT IN ('duplicate','ingesting') "
            "AND EXISTS (SELECT 1 FROM segments s WHERE s.lecture_id=l.id) ORDER BY l.id")]
    failed = 0
    for jid in pulls:
        job = ll.run_job_now(jid, wait_retry=False)
        if job["status"] == "queued":  # a passing failure (signed out, network): the server retries it
            print(f"deck job {jid}: {job['progress']}, next try after {job['retry_at']} UTC")
            continue
        print(f"deck job {jid}: {job['status']} {job['error'] or job['result']}")
        failed += job["status"] != "done"
    for lid in ids:
        with ll.db() as con:
            busy = con.execute("SELECT 1 FROM jobs WHERE kind='slides' AND lecture_id=? AND status='running'",
                               (lid,)).fetchone()
        if busy:  # the server's worker is on it; running it here too would race on the same files
            print(f"lecture {lid}: skipped, the server is working on it")
            continue
        job = ll.run_job_now(ll.enqueue_slides(lid, ocr=True))
        print(f"lecture {lid}: {job['status']} {job['error'] or job['result']}")
        failed += job["status"] != "done"
    return 1 if failed else 0


def cmd_notes(args) -> int:
    """Write notes, flashcards and a quiz now (default: every transcribed lecture without notes).
    Runs here, not in the server. Every Claude call is cached, so re-running costs nothing."""
    ll.setup_logging(console=True)
    with ll.db() as con:
        ids = args.ids or [r[0] for r in con.execute(
            "SELECT l.id FROM lectures l WHERE l.status NOT IN ('duplicate','ingesting') "
            "AND EXISTS (SELECT 1 FROM segments s WHERE s.lecture_id=l.id) "
            "AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.lecture_id=l.id) ORDER BY l.id")]
    lock = ll.FileLock(ll.DATA / "sync.lock")  # one claude at a time: the laptop is short on memory
    if not lock.acquire():
        print("The hourly sync is running; try again when it's done.")
        return 1
    try:
        return _notes(ids, args)
    finally:
        lock.release()


def _notes(ids: list[int], args) -> int:
    failed = 0
    for lid in ids:
        if not ll.get_lecture(lid):
            print(f"lecture {lid}: no such lecture")
            failed += 1
            continue
        with ll.db() as con:
            busy = con.execute("SELECT 1 FROM jobs WHERE kind='notes' AND lecture_id=? AND status='running'",
                               (lid,)).fetchone()
        if busy:
            print(f"lecture {lid}: skipped, the server is working on it")
            continue
        try:
            jid = ll.enqueue_notes(lid, "offline" if args.offline else "claude", force=args.force)
        except ValueError as e:
            print(f"lecture {lid}: {e} (--force)")
            failed += 1
            continue
        job = ll.run_job_now(jid, wait_retry=False)
        if job["status"] == "queued":  # the usage limit: the server picks it up after the reset
            print(f"lecture {lid}: {job['progress']}")
            break
        print(f"lecture {lid}: {job['status']} {job['error'] or job['result']}")
        failed += job["status"] != "done"
    return 1 if failed else 0


def cmd_sync(args) -> int:
    """The whole cycle, for the hourly task: Canvas, inbox folders, one transcription batch,
    notes, export. Safe to run any time; a second run straight after does nothing."""
    ll.setup_logging(console=not args.quiet)
    out = ll.run_sync(say=(lambda m: None) if args.quiet else (lambda m: print(f"  {m}", flush=True)))
    if not args.quiet:
        print("sync:", out or "nothing new")
    return 0


def cmd_colab_status(args) -> int:
    """Check the Colab CLI in WSL: login, compute units, running sessions."""
    ll.setup_logging(console=False)
    if not ll.COLAB_ENABLED:
        print("Colab is turned off here (LECTURELAB_COLAB=off), so it wasn't checked.")
        return 0
    try:
        print("colab:", ll.colab_bin())
        print(ll.colab("usage", timeout=120).strip())
        print("units parsed:", ll.colab_units())
        print("LectureLab sessions running:", ll.colab_sessions() or "none")
    except Exception as e:
        print(f"FAILED: {e}")
        return 1
    return 0


def cmd_benchmark(args) -> int:
    ll.setup_logging(console=True)
    report = ll.benchmark_local(args.lecture, start=args.start)
    for name, r in report["results"].items():
        print(f"{name:16} WER {r['wer']:.1%}  {r['seconds']:.0f}s for 2 min  (~{r['minutes_per_75min']:.0f} min per 75-min lecture)")
    print("picked:", report["picked"])
    return 0


def cmd_canvas_login(args) -> int:
    """Open the LectureLab browser at Canvas; you sign in with GatorLink + Duo yourself."""
    ll.setup_logging(console=sys.stdout is not None)
    try:
        me = ll.canvas_login()
        print(f"Canvas connected as {me.get('name')}")
        return 0
    except Exception as e:
        print(f"FAILED: {e}")
        return 1


def cmd_canvas_status(args) -> int:
    """Check the Canvas session (one GET /api/v1/users/self). Never signs in on its own."""
    ll.setup_logging(console=False)
    try:
        # Never connected: a signed-out answer is expected, so don't call it an expired session.
        quiet = ll.canvas_state().get("state") == "not_connected"
        with ll.CanvasSession() as cs:
            me = cs.whoami(quiet_auth=quiet)
        print(f"Canvas: connected as {me.get('name')}")
        for c in ll.canvas_courses():
            print(f"  {c['code']:<28} -> {c['course_code'] or '(no LectureLab course)'}   {c['name']}")
        return 0
    except ll.CanvasNeedsSignIn:
        print("Canvas: NEEDS SIGN-IN (run `lecturelab canvas-login` or click Connect Canvas). Inboxes still work.")
        return 3
    except Exception as e:
        print(f"Canvas: FAILED: {e}")
        return 1


TASK_SERVER = "LectureLab Server"
TASK_SYNC = "LectureLab Sync"


def _ps(script: str) -> subprocess.CompletedProcess:
    # NO_WINDOW: from pythonw (the desktop shortcut) a console would flash up otherwise
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, creationflags=NO_WINDOW)


def _q(path) -> str:
    """Quote for a PowerShell single-quoted string."""
    return "'" + str(path).replace("'", "''") + "'"


def server_task_script() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    cli = Path(__file__).resolve()
    arg = f'"{cli}" supervise'
    return f"""
$ErrorActionPreference = 'Stop'
$action  = New-ScheduledTaskAction -Execute {_q(pythonw)} -Argument {_q(arg)} -WorkingDirectory {_q(ll.ROOT)}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable -Priority 4
Register-ScheduledTask -TaskName '{TASK_SERVER}' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'LectureLab v2 server (supervised, restarts on crash)' -Force | Out-Null
'registered {TASK_SERVER}'
"""


def sync_task_script() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    arg = f'"{Path(__file__).resolve()}" sync --quiet'
    return f"""
$ErrorActionPreference = 'Stop'
$action  = New-ScheduledTaskAction -Execute {_q(pythonw)} -Argument {_q(arg)} -WorkingDirectory {_q(ll.ROOT)}
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew -StartWhenAvailable -Priority 7
Register-ScheduledTask -TaskName '{TASK_SYNC}' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'LectureLab v2 hourly sync: Canvas, inboxes, transcription, notes, export' -Force | Out-Null
'registered {TASK_SYNC}'
"""


def cmd_install_tasks(args) -> int:
    """Register the logon task that keeps the server running (hidden, via pythonw) and the
    hourly sync."""
    r = _ps(server_task_script())
    print(r.stdout.strip(), r.stderr.strip())
    if r.returncode == 0:
        r = _ps(sync_task_script())
        print(r.stdout.strip(), r.stderr.strip())
    if r.returncode == 0 and args.start:
        print(_ps(f"Start-ScheduledTask -TaskName '{TASK_SERVER}'; 'started'").stdout.strip())
    return r.returncode


def _app_browsers() -> list[Path]:
    env = os.environ.get
    return [Path(env(base) or "-") / sub for base, sub in (
        ("ProgramFiles", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles(x86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
        ("ProgramFiles(x86)", "Microsoft/Edge/Application/msedge.exe"),
        ("ProgramFiles", "Microsoft/Edge/Application/msedge.exe"))]


def cmd_open(args) -> int:
    """Open LectureLab in a window of its own, starting the server first if it isn't running.
    This is what the desktop shortcut runs."""
    url = f"http://127.0.0.1:{ll.PORT}/"
    if not _live_server_pid():
        if _ps(f"Start-ScheduledTask -TaskName '{TASK_SERVER}'").returncode != 0:  # tasks not installed
            subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), str(Path(__file__).resolve()),
                              "supervise"], cwd=ll.ROOT, creationflags=NO_WINDOW | subprocess.DETACHED_PROCESS)
        end = time.time() + 30
        while time.time() < end and not _live_server_pid():
            time.sleep(0.5)
        # still down: open anyway, the page says the server is unreachable and how to start it
    exe = next((b for b in _app_browsers() if b.is_file()), None)
    if exe:  # --app: no tabs or address bar, so it looks and switches like an app
        subprocess.Popen([str(exe), f"--app={url}"], creationflags=NO_WINDOW)
    else:
        import webbrowser
        webbrowser.open(url)
    return 0


def write_icon(path: Path) -> Path:
    """The favicon's pill (ink with a highlighter end) on a paper tile, so it shows on a dark desktop too."""
    from PIL import Image, ImageDraw  # Pillow comes with python-pptx
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), radius=56, fill="#F6F7F9")
    d.rounded_rectangle((28, 100, 228, 156), radius=28, fill="#16181D")
    d.rounded_rectangle((156, 100, 228, 156), radius=28, fill="#FFE45C")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


def cmd_shortcut(args) -> int:
    """Put a LectureLab shortcut on the desktop."""
    icon = write_icon(ll.DATA / "lecturelab.ico")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    r = _ps(f"""
$ErrorActionPreference = 'Stop'
$path = Join-Path ([Environment]::GetFolderPath('Desktop')) 'LectureLab.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$s.TargetPath = {_q(pythonw)}
$s.Arguments = {_q(f'"{Path(__file__).resolve()}" open')}
$s.WorkingDirectory = {_q(ll.ROOT)}
$s.IconLocation = {_q(icon)}
$s.Description = 'Open LectureLab (starts it if it isn''t running)'
$s.Save()
$path
""")
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode


def _safe_console() -> None:
    """Output redirected to a file or pipe is cp1252 on Windows: a lecture titled with an emoji or
    a ✓ would crash the command after its work is done. Print a ? for those instead."""
    for s in (sys.stdout, sys.stderr):
        if s is not None and hasattr(s, "reconfigure"):
            s.reconfigure(errors="replace")


def main(argv=None) -> int:
    _safe_console()
    p = argparse.ArgumentParser(prog="lecturelab", description="LectureLab v2")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web server in the foreground").set_defaults(fn=cmd_serve)
    sub.add_parser("supervise", help="run the server and restart it if it crashes").set_defaults(fn=cmd_supervise)
    sub.add_parser("stop", help="stop the supervisor and server").set_defaults(fn=cmd_stop)
    a = sub.add_parser("add", help="add a recording from a file or URL")
    a.add_argument("source", help="file path or URL")
    a.add_argument("--course", help="course code or name, e.g. COP4020")
    a.add_argument("--title")
    a.add_argument("--date", help="YYYY-MM-DD (default: from the source's metadata)")
    a.add_argument("--passcode", help="Zoom recording passcode")
    a.set_defaults(fn=cmd_add)
    i = sub.add_parser("import-v1", help="one-time import of v1 recordings and transcripts")
    i.add_argument("db", nargs="?", default=str(Path.home() / "Documents/Projects/lecturelab/lecturelab.db"))
    i.set_defaults(fn=cmd_import_v1)
    tr = sub.add_parser("transcribe", help="transcribe queued lectures now (one Colab batch)")
    tr.add_argument("ids", nargs="*", type=int, help="lecture ids (default: everything queued)")
    g = tr.add_mutually_exclusive_group()
    g.add_argument("--local", action="store_true", help="use the local CPU model instead of Colab")
    g.add_argument("--colab", action="store_true", help="Colab only; fail instead of falling back")
    tr.set_defaults(fn=cmd_transcribe)
    sl = sub.add_parser("slides", help="download queued decks and align slides now")
    sl.add_argument("ids", nargs="*", type=int, help="lecture ids (default: transcribed and not aligned yet)")
    sl.set_defaults(fn=cmd_slides)
    nt = sub.add_parser("notes", help="write notes, flashcards and a quiz now")
    nt.add_argument("ids", nargs="*", type=int, help="lecture ids (default: transcribed lectures without notes)")
    nt.add_argument("--offline", action="store_true", help="no Claude: extractive notes only")
    nt.add_argument("--force", action="store_true", help="with --offline: replace notes Claude wrote")
    nt.set_defaults(fn=cmd_notes)
    sub.add_parser("colab-status", help="check Colab login, compute units and sessions").set_defaults(fn=cmd_colab_status)
    b = sub.add_parser("benchmark", help="pick the local CPU model by speed and accuracy")
    b.add_argument("lecture", type=int, help="a lecture with a Colab transcript to score against")
    b.add_argument("--start", type=float, default=120, help="where the 2-minute slice starts (s)")
    b.set_defaults(fn=cmd_benchmark)
    sub.add_parser("canvas-login", help="open the LectureLab browser to sign in to Canvas").set_defaults(
        fn=cmd_canvas_login)
    sub.add_parser("canvas-status", help="check the Canvas session and list your courses").set_defaults(
        fn=cmd_canvas_status)
    sy = sub.add_parser("sync", help="Canvas, inboxes, one transcription batch, notes, export (hourly task)")
    sy.add_argument("--quiet", action="store_true", help="no console output (the scheduled task)")
    sy.set_defaults(fn=cmd_sync)
    t = sub.add_parser("install-tasks", help="register the logon task and the hourly sync")
    t.add_argument("--start", action="store_true", help="also start it now")
    t.set_defaults(fn=cmd_install_tasks)
    sub.add_parser("open", help="open the app in its own window, starting the server if needed").set_defaults(
        fn=cmd_open)
    sub.add_parser("shortcut", help="put a LectureLab shortcut on the desktop").set_defaults(fn=cmd_shortcut)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
