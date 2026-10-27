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
            time.sleep(backoff)
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


TASK_SERVER = "LectureLab Server"


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True)


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


def cmd_install_tasks(args) -> int:
    """Register the logon task that keeps the server running (hidden, via pythonw)."""
    r = _ps(server_task_script())
    print(r.stdout.strip(), r.stderr.strip())
    if r.returncode == 0 and args.start:
        print(_ps(f"Start-ScheduledTask -TaskName '{TASK_SERVER}'; 'started'").stdout.strip())
    return r.returncode


def main(argv=None) -> int:
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
    t = sub.add_parser("install-tasks", help="register the Windows logon task")
    t.add_argument("--start", action="store_true", help="also start it now")
    t.set_defaults(fn=cmd_install_tasks)
    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
