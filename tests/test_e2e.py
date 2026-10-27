"""End-to-end tests: a real supervised server and a real browser.

The server runs on its own port with a throwaway data dir, so tests never touch the real library.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("LECTURELAB_TEST_PORT", "8766"))
BASE = f"http://127.0.0.1:{PORT}"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")


def health(timeout=0.5):
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def wait_health(predicate=lambda h: True, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        h = health()
        if h and predicate(h):
            return h
        time.sleep(0.25)
    raise AssertionError("server did not become healthy")


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    data = tmp_path_factory.mktemp("lldata")
    # Inboxes point at temp folders: the real Drive inbox must never be touched by tests.
    env = {**os.environ, "LECTURELAB_DATA": str(data), "LECTURELAB_PORT": str(PORT),
           "LECTURELAB_LOG": str(data / "server.log"), "LECTURELAB_INBOX": str(data / "inbox"),
           "LECTURELAB_DRIVE_INBOX": str(data / "drive_inbox"), "LECTURELAB_INBOX_INTERVAL": "1",
           # Never touch the real Colab account from tests; transcribe with the small cached model.
           "LECTURELAB_COLAB": "off", "LECTURELAB_LOCAL_MODEL": "base", "LECTURELAB_NOTIFY": "off"}
    assert health() is None, f"port {PORT} already in use"
    # pythonw + supervise is exactly how the logon task runs it: hidden, restarting on crash.
    sup = subprocess.Popen([str(PYTHONW), str(ROOT / "cli.py"), "supervise"], cwd=ROOT, env=env)
    wait_health()
    yield {"data": data, "supervisor": sup}
    subprocess.run(["taskkill", "/PID", str(sup.pid), "/T", "/F"], capture_output=True)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")  # the installed Chrome; no browser download
        yield b
        b.close()


@pytest.fixture()
def page(browser, server):
    ctx = browser.new_context()
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    yield pg
    ctx.close()
    assert not errors, f"JS errors: {errors}"


FIXTURE = ROOT / "data" / "fixtures" / "plc_lecture1_8min.webm"  # real class recording, gitignored
needs_fixture = pytest.mark.skipif(not FIXTURE.exists(), reason="real lecture clip not present in data/fixtures")


def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def wait_for(fn, seconds=120, every=0.5, what="condition"):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}")


def test_ui_lists_courses_and_vocab_persists(page):
    page.goto(BASE + "/#/courses")
    for code in ["COP4020", "COP4533", "CNT4007", "CIS4930", "PHI3681", "MML"]:
        expect(page.locator("#course-nav")).to_contain_text(code)
    card = page.locator("[data-course]", has_text="COP4020")
    vocab = card.locator(".course-vocab")
    vocab.fill(vocab.input_value() + "\nmonad\nfoldr")
    card.locator(".save-course").click()
    expect(page.locator("#toast")).to_have_text("Saved")
    page.reload()
    vocab = page.locator("[data-course]", has_text="COP4020").locator(".course-vocab")
    assert vocab.input_value().splitlines()[-2:] == ["monad", "foldr"]
    expect(page.locator("#health")).to_contain_text("ok")


def test_course_name_is_rendered_as_text_not_html(page):
    page.goto(BASE + "/#/courses")
    card = page.locator("[data-course]", has_text="CIS4930")
    card.locator(".course-name").fill('<img src=x onerror="window.pwned=1">Topics')
    card.locator(".save-course").click()
    expect(page.locator("#toast")).to_have_text("Saved")
    page.reload()
    expect(page.locator("#course-nav")).to_contain_text('<img src=x onerror="window.pwned=1">Topics')
    assert page.evaluate("window.pwned") is None


def test_api_rejects_foreign_host_and_unknown_course(server):
    req = urllib.request.Request(f"{BASE}/api/health", headers={"Host": "evil.example:8766"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    req = urllib.request.Request(f"{BASE}/api/courses/999", method="PUT", data=b'{"name":"x"}',
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 404


@needs_fixture
def test_upload_real_lecture_then_play_and_edit(page):
    page.goto(BASE + "/")
    page.click("#add-btn")
    page.set_input_files("#add-file", str(FIXTURE))
    page.select_option("#add-course", label="COP4020 · Programming Language Concepts")
    page.fill("#add-title", "PLC Lecture 1 (first 8 min)")
    page.fill("#add-date", "2026-08-21")
    page.click("#add-submit")
    expect(page.locator("#toast")).to_contain_text("Ready: lecture", timeout=120_000)
    row = page.locator(".lec", has_text="PLC Lecture 1 (first 8 min)")
    expect(row).to_contain_text("COP4020")
    expect(row.locator("[data-status]")).to_have_attribute("data-status", "queued_transcribe")
    row.click()
    # The view renders after a fetch, so wait for the player to exist before polling it.
    expect(page.locator("#player")).to_be_attached()
    # The normalized 16 kHz FLAC is served with Range support: duration is right and seeking works.
    page.wait_for_function("document.querySelector('#player')?.readyState >= 1")
    assert abs(page.evaluate("document.querySelector('#player').duration") - 480) < 2
    page.evaluate("document.querySelector('#player').currentTime = 300")
    page.wait_for_function("Math.abs(document.querySelector('#player').currentTime - 300) < 1")
    expect(page.locator("[data-source]")).to_have_count(1)
    page.fill("#m-title", "PLC Lecture 1 · intro clip")
    page.click("#m-save")
    expect(page.locator("#toast")).to_have_text("Saved")
    page.reload()
    expect(page.locator("#m-title")).to_have_value("PLC Lecture 1 · intro clip")
    expect(page.locator("#m-date")).to_have_value("2026-08-21")


@needs_fixture
def test_transcribe_locally_then_click_to_seek(page):
    lec = next(l for l in api("/api/lectures") if l["title"].startswith("PLC Lecture 1"))
    page.goto(f"{BASE}/#/lecture/{lec['id']}")
    page.click("#tr-local")
    expect(page.locator("#tr-source")).to_contain_text("local-base", timeout=600_000)
    segs = page.locator(".seg")
    n = segs.count()
    # Whisper picks its own segment lengths (the base model gave 20-30 for this clip), so the
    # word count is the real check on 8 minutes of speech.
    assert n >= 10, n
    words = " ".join(segs.all_inner_texts()).split()
    assert len(words) > 600, len(words)  # people say ~120-160 words a minute
    expect(page.locator("[data-status]").first).to_have_attribute("data-status", "transcribed")
    target = segs.nth(n // 2)
    t = float(target.get_attribute("data-start"))
    target.locator("[data-seek]").click()
    page.wait_for_function(f"Math.abs(document.querySelector('#player').currentTime - {t}) < 1.5")
    expect(page.locator(".seg.now")).to_have_count(1, timeout=10_000)
    with_words = api(f"/api/lectures/{lec['id']}/segments")
    assert all(s["words"] for s in with_words[:10])  # word-level timestamps stored


@needs_fixture
def test_inbox_drop_of_same_lecture_is_deduped(server, page):
    before = api("/api/lectures")
    target = server["data"] / "drive_inbox" / "COP4020" / "copy from drive.webm"
    wait_for(lambda: target.parent.exists(), 30, what="inbox course folders")  # created by the watcher
    import shutil
    shutil.copy(FIXTURE, target)
    lec_id = before[0]["id"]
    wait_for(lambda: any(s["kind"] == "drive" for s in api(f"/api/lectures/{lec_id}")["sources"]), 60,
             what="drive inbox file to be ingested")
    assert len(api("/api/lectures")) == len(before)  # same lecture, no new row
    page.goto(f"{BASE}/#/lecture/{lec_id}")
    expect(page.locator("[data-source=drive]")).to_contain_text("copy from drive.webm")


@needs_fixture
def test_reencoded_copy_flagged_and_released(server, page, tmp_path):
    from lecturelab import find_tool
    mp3 = tmp_path / "PLC lecture (zoom export).mp3"
    subprocess.run([find_tool("ffmpeg"), "-v", "error", "-i", str(FIXTURE), "-ss", "4", "-c:a", "libmp3lame",
                    "-b:a", "48k", str(mp3)], check=True)  # re-encoded AND trimmed 4 s, like another source would be
    page.goto(BASE + "/")
    page.click("#add-btn")
    page.set_input_files("#add-file", str(mp3))
    page.click("#add-submit")
    expect(page.locator("#toast")).to_contain_text("Possible duplicate of lecture", timeout=120_000)
    row = page.locator(".lec", has_text="PLC lecture (zoom export)")
    expect(row.locator("[data-status]")).to_have_attribute("data-status", "duplicate")
    row.click()
    expect(page.locator("#dup-banner")).to_contain_text("same recording as")
    page.click("#not-dup")
    expect(page.locator("[data-status]").first).to_have_attribute("data-status", "queued_transcribe")


def test_canvas_tab_when_not_connected(page):
    page.goto(BASE + "/")
    page.click("#add-btn")
    page.click("[data-tab=canvas]")
    expect(page.locator("[data-canvas-state]")).to_have_attribute("data-canvas-state", "not_connected")
    expect(page.locator("#cv-connect")).to_have_text("Connect Canvas")
    expect(page.locator("#cv-status")).to_contain_text("GatorLink")
    page.select_option("#cv-course", label="COP4020 · Programming Language Concepts")
    page.click("#cv-scan")
    expect(page.locator("#toast")).to_contain_text("isn't connected yet")
    expect(page.locator("#canvas-badge")).to_be_hidden()  # the warning badge is only for an expired session


def test_bad_link_reports_the_real_error(page):
    page.goto(BASE + "/")
    page.click("#add-btn")
    page.click("[data-tab=link]")
    page.fill("#add-url", "https://zoom.us/rec/share/this-recording-does-not-exist")
    expect(page.locator("#passcode-field")).to_be_visible()
    page.fill("#add-passcode", "abc123")
    page.click("#add-submit")
    expect(page.locator("#toast")).to_contain_text("Failed: yt-dlp:", timeout=90_000)


def test_supervisor_restarts_crashed_server(server, page):
    before = wait_health()["pid"]
    subprocess.run(["taskkill", "/PID", str(before), "/F"], capture_output=True)
    after = wait_health(lambda h: h["pid"] != before, seconds=30)["pid"]
    assert after != before
    log = (server["data"] / "server.log").read_text(encoding="utf-8")
    assert "server exited with code" in log
    assert f"pid {after} (data=" in log
    page.goto(BASE + "/")
    expect(page.locator("#health")).to_contain_text("ok")


def test_stop_command_shuts_down_cleanly(server):
    """Runs last: `lecturelab stop` ends server and supervisor with a clean exit code."""
    env = {**os.environ, "LECTURELAB_DATA": str(server["data"]), "LECTURELAB_PORT": str(PORT),
           "LECTURELAB_LOG": str(server["data"] / "server.log")}
    r = subprocess.run([sys.executable, str(ROOT / "cli.py"), "stop"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert server["supervisor"].wait(timeout=15) == 0  # clean exit: Task Scheduler won't "restart on failure"
    assert health() is None
    assert "supervisor stopping on request" in (server["data"] / "server.log").read_text(encoding="utf-8")
