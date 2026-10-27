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
    env = {**os.environ, "LECTURELAB_DATA": str(data), "LECTURELAB_PORT": str(PORT),
           "LECTURELAB_LOG": str(data / "server.log")}
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
