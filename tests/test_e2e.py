"""End-to-end tests: a real supervised server and a real browser.

The server runs on its own port with a throwaway data dir, so tests never touch the real library.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


DECK = ROOT / "data" / "fixtures" / "plc_lecture1_slides.pdf"  # COP4020's real "M0L1 Course Overview" deck, gitignored


@needs_fixture
def test_attach_slides_then_fix_the_match(page):
    lec = next(l for l in api("/api/lectures") if l["title"].startswith("PLC Lecture 1"))
    try:
        api(f"/api/lectures/{lec['id']}/slides")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            pytest.skip("this server has no slides endpoint yet (Stage 3 not merged)")
        raise
    if not DECK.exists():
        pytest.skip("slide deck fixture not present in data/fixtures")
    page.set_viewport_size({"width": 1440, "height": 900})  # wide enough for the slide panel beside the transcript
    page.goto(f"{BASE}/#/lecture/{lec['id']}")
    expect(page.locator("#slides-sec")).to_have_attribute("data-state", "none")
    page.set_input_files("#sl-file", str(DECK))
    # Stage 4's server only checks the file opens before answering, under a second; Stage 3's reads all 22 pages first (20 s).
    expect(page.locator("#toast")).to_contain_text("Attached", timeout=30_000)
    expect(page.locator("#slides-sec")).to_have_attribute("data-state", "aligned", timeout=180_000)
    sl = api(f"/api/lectures/{lec['id']}/slides")
    assert sl["deck"]["pages"] == 22
    assert [s["title"] for s in sl["slides"][:2]] == ["Course Overview", "Welcome!"]
    shown = [s for s in sl["slides"] if s["first"] is not None]
    assert shown, "alignment put no slide on screen"
    marks = page.locator(".slide-mark")
    assert marks.count() >= len(shown), (marks.count(), len(shown))  # every slide that came up is marked; revisits add more
    expect(page.locator("#slide-panel figcaption")).to_contain_text("of 22")
    page.wait_for_function("document.querySelector('#slide-panel img')?.naturalWidth > 0")  # the thumbnail really loads
    # Fix the match: no slides, reload, then pick the deck back from the course's candidates. Both must persist.
    page.select_option("#sl-deck", "none")
    page.click("#sl-use")
    page.click("#confirm-ok")
    expect(page.locator("#toast")).to_have_text("Slides removed from this lecture")
    expect(marks).to_have_count(0)
    page.reload()
    expect(page.locator("#slides-sec")).to_have_attribute("data-state", "none")
    expect(page.locator("#sl-deck")).to_have_value("none")
    page.select_option("#sl-deck", str(sl["deck"]["id"]))
    page.click("#sl-use")
    expect(page.locator("#slides-sec")).to_have_attribute("data-state", "aligned", timeout=180_000)
    page.reload()
    expect(page.locator("#sl-deck")).to_have_value(str(sl["deck"]["id"]))
    expect(marks.first).to_be_visible()



SLIDE_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180"><rect width="320" height="180" fill="#fff"/>'
             '<text x="16" y="40" font-size="20">slide</text></svg>')


@needs_fixture
def test_slides_ui_against_stubbed_answers(page):
    # The slides UI against canned answers in the shape agreed with Stage 3, so it's covered before that server lands:
    # a "not checked yet" lecture, an upload the server rejects, aligning, aligned with a demo stretch that has no slide,
    # a thumbnail that 404s, the viewer, and a server with no slides endpoint at all.
    lid = next(l for l in api("/api/lectures") if l["title"].startswith("PLC Lecture 1"))["id"]
    segs = api(f"/api/lectures/{lid}/segments")
    assert segs, "needs the transcript from test_transcribe_locally_then_click_to_seek"
    end = max(s["end"] for s in segs)
    q = end / 4
    slide_at = lambda t: 1 if t < q else 2 if t < 2 * q else None if t < 3 * q else 3  # third quarter: a live demo
    # half a second into a line at least a second long, so the line before it (matched up to end + 0.3) can't claim it
    at = lambda frac: next(s["start"] for s in segs if s["start"] >= frac * end and s["end"] - s["start"] >= 1) + 0.5
    titles = ["Course Overview", "Welcome!", "Grading"]

    def slide(no, ranges):
        return {"no": no, "title": titles[no - 1], "text": "", "first": ranges[0][0] if ranges else None, "ranges": ranges,
                "thumb": f"/media/slides/7/{no}-320.png?v=1", "image": f"/media/slides/7/{no}-1280.png?v=1"}
    deck = {"id": 7, "title": "M0L1 Course Overview.pdf", "source": "canvas", "how": "auto", "score": 0.8, "pages": 3,
            "thumb_w": 320, "thumb_h": 180}
    st = {"state": "waiting", "reason": "not checked yet", "progress": None, "error": None, "deck": deck,
          "slides": [slide(n, []) for n in (1, 2, 3)], "candidates": []}

    def slides_api(route):
        req = route.request
        if req.method == "POST" and req.url.endswith("/align"):
            st.update(state="aligning", reason=None, progress="extracting page 2 of 3")
        elif req.method == "POST":
            return route.fulfill(status=422, json={"detail": "This PDF couldn't be read. Export an unlocked copy and attach that."})
        route.fulfill(json=st)

    def segments(route):
        rows = route.fetch().json()
        route.fulfill(json=[{**s, "slide_no": slide_at(s["start"])} for s in rows] if st["state"] == "aligned" else rows)
    page.route(re.compile(rf"/api/lectures/{lid}/slides(/align)?$"), slides_api)
    page.route(f"**/api/lectures/{lid}/segments", segments)
    page.route("**/media/slides/**", lambda r: r.fulfill(status=404, body="gone") if "/2-320.png" in r.request.url
               else r.fulfill(content_type="image/svg+xml", body=SLIDE_SVG))
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{BASE}/#/lecture/{lid}")
    sec = page.locator("#slides-sec")
    expect(sec).to_have_attribute("data-state", "waiting")
    expect(page.locator("#sl-status")).to_contain_text("No deck is matched yet")
    expect(page.locator("#sl-check")).to_have_text("Look for slides")

    page.click("#sl-check")
    expect(sec).to_have_attribute("data-state", "aligning")
    expect(page.locator("#slides-sec .job-detail")).to_have_text("extracting page 2 of 3")
    expect(page.locator("#sl-status")).to_be_focused()

    # the server rejects an upload while aligning: the reason shows under the button, which works again, and polling goes on
    page.set_input_files("#sl-file", files=[{"name": "broken.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4"}])
    expect(page.locator("#sl-err")).to_contain_text("couldn't be read")
    expect(page.locator("#sl-attach")).to_be_enabled()
    expect(page.locator("#sl-attach")).to_be_focused()
    st.update(state="aligned", progress=None, slides=[slide(1, [[0, q]]), slide(2, [[q, 2 * q]]), slide(3, [[3 * q, end]])])
    expect(sec).to_have_attribute("data-state", "aligned", timeout=10_000)  # the 3 s poll picks it up
    expect(page.locator(".slide-mark")).to_have_count(4)  # slides 1, 2, the demo stretch, 3
    expect(page.locator(".slide-mark.gap")).to_have_count(1)
    # the re-render kept the reader down at the Slides section, past the last line; back at the top the panel shows slide 1
    expect(page.locator("#slides-sec")).to_be_in_viewport()
    page.evaluate("scrollTo(0, 0)")
    expect(page.locator("#slide-panel figcaption")).to_have_text("Slide 1 of 3")

    # the panel follows the player: a 404 thumbnail becomes a labelled placeholder; the demo shows no slide
    page.wait_for_function("document.querySelector('#player').readyState >= 1")
    def seek_to(t):  # then let the transcript's smooth follow-scroll settle: the panel must still match the played line
        page.evaluate("t => { document.querySelector('#player').currentTime = t; }", t)
        page.wait_for_timeout(1500)
    seek_to(at(0.3))
    expect(page.locator("#slide-panel .slide-img.missing")).to_contain_text("Slide 2, Welcome!: image not available yet")
    seek_to(at(0.6))
    expect(page.locator("#slide-panel")).to_have_text("No slide on screen here")
    seek_to(at(0.05))
    expect(page.locator("#slide-panel figcaption")).to_have_text("Slide 1 of 3")

    # the viewer: starts on Play, arrows step through, Play from jumps there and closes it
    page.click("#slide-panel button.slide-img")
    expect(page.locator("#slide-dlg-h")).to_have_text("Course Overview")
    expect(page.locator("#slide-play")).to_be_focused()
    expect(page.locator("#slide-prev")).to_be_disabled()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#slide-dlg-h")).to_have_text("Welcome!")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#slide-dlg-h")).to_have_text("Grading")
    expect(page.locator("#slide-next")).to_be_disabled()
    page.click("#slide-play")
    expect(page.locator("#slide-dlg")).to_be_hidden()
    page.wait_for_function(f"document.querySelector('#player').currentTime >= {3 * q - 0.5}")
    page.evaluate("document.querySelector('#player').pause()")

    # a link to a slide opens the viewer on it; closing takes the slide out of the URL
    page.goto(f"{BASE}/?slide=2#/lecture/{lid}")
    expect(page.locator("#slide-dlg-h")).to_have_text("Welcome!")
    page.click("#slide-close")
    expect(page).not_to_have_url(re.compile(r"slide="))

    # choosing in the deck list only selects; removing the slides asks first, and Cancel changes nothing
    page.select_option("#sl-deck", "none")
    expect(page.locator("#sl-use")).to_have_text("Remove slides")
    page.click("#sl-use")
    expect(page.locator("#confirm-dlg")).to_be_visible()
    page.click("#confirm-dlg button[value=cancel]")
    expect(sec).to_have_attribute("data-state", "aligned")
    expect(page.locator(".slide-mark")).to_have_count(4)

    # a server without the slides endpoint: the page is exactly what it was before slides existed
    page.unroute(re.compile(rf"/api/lectures/{lid}/slides(/align)?$"))
    page.route(f"**/api/lectures/{lid}/slides", lambda r: r.fulfill(status=404, json={"detail": "Not Found"}))
    page.reload()
    expect(page.locator("#transcript .seg").first).to_be_visible()
    expect(page.locator("#slides-sec")).to_have_count(0)
    expect(page.locator(".slide-mark")).to_have_count(0)


def fake_notes(end):
    # Made-up notes in the Stage 4 shape, using every Markdown convention the server writes (no class content in the repo).
    t1, t2, t3 = 0.0, round(end / 3, 2), round(2 * end / 3, 2)
    jump = lambda t: f"[▶ {int(t) // 60}:{int(t) % 60:02d}](#t={t})"
    return {"state": "done", "progress": None, "error": None, "retry_at": None, "generator": "claude",
            "created_at": "2026-10-28 15:59:02", "cards": 12, "quiz": 5, "stale": False, "stale_reason": None,
            "removed": 1, "flagged": 1, "open_questions": ["Why is `<b>` *not* bold here?"],
            "summary": "Scoping, in **two** parts.\n\n- A function $f$ maps inputs to $\\hat{y}$.\n- Shadowing is legal (Slide 2).",
            "issues": [{"where": "section 2", "verdict": "removed", "claim": "Scopes are global.", "reason": "Never said."},
                       {"where": "card 2", "verdict": "flagged", "claim": "A guess.", "reason": "Unclear audio."}],
            "sections": [
                {"title": "Grades are out", "start": t1, "end": t2, "kind": "logistics", "slides": [], "gist": "Admin.",
                 "markdown": f"{jump(t1)}: Grades are posted."},
                {"title": "Nested scopes", "start": t2, "end": t3, "kind": "content", "slides": [1, 2, 3], "gist": "How scopes nest.",
                 "markdown": f"### Inner and outer\n\n{jump(t2 + 1)}: A **scope** can nest (Slides 1 to 3).\n\n"
                             "```java\nif (true) { var x = \"<script>\"; }\n```\n\n```c#\nvar y = 1;\n```\n\n"
                             f"> [!exam] {jump(t2 + 2)} Know this one.\n\n> [!unclear]\n> - the audio cut out\n\n"
                             "> [!check] Verify the default value."},
                {"title": "Static vs dynamic", "start": t3, "end": end, "kind": "content", "slides": [3], "gist": "",
                 "markdown": f"{jump(t3)}: Static scoping looks at the code, `dynamic` at the call stack."}]}


@needs_fixture
def test_notes_ui_against_stubbed_answers(page):
    # The notes UI against canned answers in the shape agreed with Stage 4: write, queued, writing, the finished notes
    # (tabs, sections, jumps, callouts, code, the check's changes), deep links, offline over Claude's notes, and no endpoint.
    lid = next(l for l in api("/api/lectures") if l["title"].startswith("PLC Lecture 1"))["id"]
    segs = api(f"/api/lectures/{lid}/segments")
    assert segs, "needs the transcript from test_transcribe_locally_then_click_to_seek"
    done = fake_notes(max(s["end"] for s in segs))
    n = {"state": "none"}
    posts = []

    def notes_api(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            posts.append(body)
            if body["mode"] == "offline" and not body.get("force"):
                return route.fulfill(status=409, json={"detail": "These notes were written by Claude. Offline notes would replace them."})
            n.clear()
            n.update(state="queued")
        route.fulfill(json=n)
    page.route(f"**/api/lectures/{lid}/notes", notes_api)
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{BASE}/#/lecture/{lid}")
    # no notes yet: the transcript is the open tab, and the notes tab offers to write them
    expect(page.locator("#tab-transcript")).to_have_attribute("aria-selected", "true")
    page.click("#tab-notes")
    expect(page).to_have_url(re.compile(r"tab=notes"))
    expect(page.locator("#pane-notes")).to_contain_text("No notes yet")
    page.click("#nt-write")
    expect(page.locator("#pane-notes .job-what")).to_have_text("Waiting to write notes")
    assert posts[-1] == {"mode": "claude"}
    n.update(state="writing", progress="writing section 2 of 3")
    expect(page.locator("#pane-notes .job-detail")).to_have_text("writing section 2 of 3", timeout=10_000)
    n.clear()
    n.update(done)
    expect(page.locator(".note-sec[id]")).to_have_count(3, timeout=10_000)  # swapped in place by the 4 s poll

    # the finished notes: sections, the tape's notches, callouts, code, and nothing from the Markdown run as HTML
    expect(page.locator(".note-sec h4")).to_have_text(["Grades are out", "Nested scopes", "Static vs dynamic", "Questions the lecture left open"])
    expect(page.locator(".notch")).to_have_count(2)
    expect(page.locator(".callout-label")).to_have_text(["On the exam", "Unclear in the recording", "Check this"])
    expect(page.locator(".code-block .lang")).to_have_text(["Java", "c#"])  # an odd fence still renders (it used to hang the tab)
    expect(page.locator(".code-block code").first).to_have_text('if (true) { var x = "<script>"; }')
    expect(page.locator("#pane-notes script")).to_have_count(0)
    expect(page.locator(".note-sec:not([id]) li code")).to_have_text("<b>")  # server text is shown, never run as HTML
    expect(page.locator("#pane-notes .math")).to_have_count(2)
    expect(page.locator("#note-sec-1 .note-meta")).to_have_text("Class logistics")
    expect(page.locator("#note-sec-2 .note-meta")).to_have_text("Slides 1 to 3")  # no slides lined up: plain text, not a button
    expect(page.locator(".note-issues summary")).to_have_text("What the check changed: removed 1 claim, flagged 1")
    expect(page.locator(".note-issues li").first).to_contain_text("Removed from Nested scopes")
    expect(page.locator(".note-issues li").nth(1)).to_contain_text("Flagged in flashcard 2")

    # a jump plays from that moment; the contents move to a section and focus its heading
    page.wait_for_function("document.querySelector('#player').readyState >= 1")
    page.locator("#note-sec-3 .np .ts").click()
    page.evaluate("document.querySelector('#player').pause()")
    page.wait_for_function(f"Math.abs(document.querySelector('#player').currentTime - {done['sections'][2]['start']}) < 1.5")
    page.click('.notes-toc a[data-section="2"]')
    expect(page.locator("#note-h-2")).to_be_focused()
    expect(page.locator('.notes-toc a[data-section="2"]')).to_have_attribute("aria-current", "location")

    # the Add dialog's tabs leave the lecture's alone
    page.click("#add-btn")
    page.click("#add-cancel")
    expect(page.locator("#tab-notes")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#tab-notes")).to_have_attribute("tabindex", "0")

    # tabs: arrow keys, the URL, and the tab doesn't follow you out of the lecture
    page.focus("#tab-notes")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#tab-transcript")).to_be_focused()
    expect(page.locator("#transcript-card")).to_be_visible()
    expect(page.locator("#pane-notes")).to_be_hidden()
    expect(page).to_have_url(re.compile(r"tab=transcript"))
    page.click('aside a[href="#/"]')
    expect(page.locator(".lec").first).to_be_visible()
    expect(page).not_to_have_url(re.compile(r"tab="))

    # links: ?section opens the notes there; ?t opens the transcript at that moment
    page.goto(f"{BASE}/?section=3#/lecture/{lid}")
    expect(page.locator("#note-h-3")).to_be_focused()
    expect(page).not_to_have_url(re.compile(r"section="))
    page.goto(f"{BASE}/?t=5#/lecture/{lid}")
    expect(page.locator("#tab-transcript")).to_have_attribute("aria-selected", "true")

    # offline over Claude's notes: the server says no, the page asks, and "Replace anyway" forces it
    n.update(stale=True, stale_reason="The lecture was transcribed again after these notes were written.")
    page.goto(f"{BASE}/?tab=notes#/lecture/{lid}")
    expect(page.locator("#pane-notes .banner")).to_contain_text("transcribed again")
    n.clear()
    n.update(state="error", error="Claude returned no usable outline twice.")
    page.reload()
    expect(page.locator("#pane-notes .banner.bad")).to_contain_text("no usable outline")
    page.click("#nt-offline")
    expect(page.locator("#confirm-dlg")).to_be_visible()
    page.click("#confirm-ok")
    expect(page.locator("#pane-notes .job-what")).to_have_text("Waiting to write notes")
    assert posts[-2:] == [{"mode": "offline"}, {"mode": "offline", "force": True}]

    # a server without the notes endpoint: no tabs, the transcript as before
    page.unroute(f"**/api/lectures/{lid}/notes")
    page.route(f"**/api/lectures/{lid}/notes", lambda r: r.fulfill(status=404, json={"detail": "Not Found"}))
    page.goto(f"{BASE}/#/lecture/{lid}")
    expect(page.locator("#transcript .seg").first).to_be_visible()
    expect(page.locator("#lec-tabs")).to_have_count(0)


def test_review_ui_against_stubbed_answers(page):
    # Flashcard review against canned answers in the Stage 4 shape: cloze and Q&A cards, keys, undo, a failed save, done.
    meta = {"lecture_id": 1, "lecture_title": "Scoping", "lecture_date": "2026-10-16", "course_code": "COP4020",
            "t": 75.0, "reviews": 0, "lapses": 0}
    cards = [{**meta, "id": 1, "kind": "cloze", "front": "A name can be {{c1::shadowed}} in an inner `scope`.", "back": "Said twice.",
              "box": 1, "due": None},
             {**meta, "id": 2, "kind": "qa", "front": "What is <b>arity</b>?", "back": "The number of parameters.",
              "box": 2, "due": "2026-10-28"}]
    review = {"cards": cards, "total": 2, "new": 1, "due": 1, "boxes": [3, 1, 0, 0, 0]}
    posts, fail, held, asked = [], {"on": False}, [], []
    hold = {"undo": False}

    def card_api(route):
        url, body = route.request.url, route.request.post_data_json
        posts.append((url.rsplit("/", 1)[-1], body))
        if fail["on"]:
            return route.fulfill(status=503, json={"detail": "database is locked"})
        cid = int(re.search(r"/cards/(\d+)/", url).group(1))
        if url.endswith("/undo"):
            if hold["undo"]:
                return held.append(route)  # answered later, from the test
            return route.fulfill(json={"id": cid, "box": 1, "due": None})
        route.fulfill(json={"card": {"id": cid, "box": 2 if body["correct"] else 1, "due": "2026-10-30"},
                            "before": {"id": cid, "box": 1, "due": None}})
    def review_api(route):
        asked.append(parse_qs(urlparse(route.request.url).query))
        route.fulfill(json=review)
    page.route(re.compile(r"/api/review(\?|$)"), review_api)
    page.route(re.compile(r"/api/cards/\d+/(review|undo)$"), card_api)
    page.goto(f"{BASE}/#/review")
    expect(page.locator('#nav a[data-route="review"]')).to_be_visible()  # the rail shows it once the server has flashcards
    expect(page.locator("#due-count")).to_have_text("2")
    expect(page.locator(".boxes b")).to_have_text(["3", "1", "0", "0", "0"])
    expect(page.locator("#rv-sub")).to_have_text("1 due and 1 new")
    # a cloze card hides its answer until asked, and server text is never HTML
    expect(page.locator("#rv-q")).to_contain_text("A name can be")
    expect(page.locator("#rv-q .cloze")).to_have_text("…")
    expect(page.locator("#rv-q code")).to_have_text("scope")
    page.keyboard.press("Space")
    expect(page.locator("#rv-q .cloze.on")).to_have_text("shadowed")
    expect(page.locator("#rv-a a")).to_have_attribute("href", "?t=75#/lecture/1")
    page.keyboard.press("2")
    expect(page.locator("#rv-undo-row")).to_contain_text("Got it. It comes back on")
    assert posts[-1] == ("review", {"correct": True})
    expect(page.locator("#rv-q")).to_have_text("What is <b>arity</b>?")
    expect(page.locator(".boxes b")).to_have_text(["3", "2", "0", "0", "0"])  # a new card went into box 2
    expect(page.locator("#rv-sub")).to_have_text("1 due and 0 new")
    expect(page.locator(".boxes .on")).to_contain_text("this card’s box")
    # undo brings the card back, once: a second U while it's saving does nothing
    hold["undo"] = True
    page.keyboard.press("u")
    expect(page.locator("#rv-undo-row")).to_have_text("Undoing…")
    page.keyboard.press("Space")
    page.keyboard.press("u")
    assert [p for p in posts if p[0] == "undo"] == [("undo", {"before": {"id": 1, "box": 1, "due": None}})]
    hold["undo"] = False
    held.pop().fulfill(json={"id": 1, "box": 1, "due": None})
    expect(page.locator("#rv-undo-row")).to_contain_text("Undone")
    expect(page.locator("#rv-q")).to_contain_text("A name can be")
    expect(page.locator(".boxes b")).to_have_text(["3", "1", "0", "0", "0"])
    expect(page.locator("#rv-sub")).to_have_text("1 due and 1 new")
    assert sum(p[0] == "undo" for p in posts) == 1
    # a save that fails puts the card back with the reason, and the next try works
    page.keyboard.press("Space")
    fail["on"] = True
    page.click("#rv-miss")
    expect(page.locator("#rv-undo-row")).to_contain_text("didn’t save: database is locked")
    expect(page.locator("#rv-q")).to_contain_text("A name can be")
    expect(page.locator("#rv-q")).to_be_focused()
    fail["on"] = False
    page.keyboard.press("Space")
    page.keyboard.press("1")
    expect(page.locator("#rv-undo-row")).to_contain_text("Missed it. It’s back in the first box and comes back on")
    page.keyboard.press("Space")
    page.keyboard.press("2")
    expect(page.locator(".rv-done h3")).to_have_text("That’s everything due")
    expect(page.locator(".rv-done")).to_contain_text("You answered 2 cards")
    expect(page.locator(".rv-export a")).to_have_attribute("href", "/api/anki.tsv")
    # the scope lives in the URL, asks the server for that course, survives Back, and leaves with the page
    cop = str(course_id("COP4020"))
    page.select_option("#rv-scope", cop)
    expect(page).to_have_url(re.compile(rf"course={cop}"))
    expect(page.locator(".rv-export a")).to_have_attribute("href", f"/api/anki.tsv?course_id={cop}")
    assert asked[-1].get("course_id") == [cop], asked[-1]
    page.click(".flash-meta a")
    expect(page).to_have_url(re.compile(r"#/lecture/1$"))
    expect(page).not_to_have_url(re.compile(r"course="))
    page.go_back()
    expect(page).to_have_url(re.compile(rf"course={cop}#/review$"))
    expect(page.locator("#rv-scope")).to_have_value(cop)
    page.click('aside a[href="#/"]')
    expect(page).not_to_have_url(re.compile(r"course="))


def test_quiz_ui_against_stubbed_answers(page):
    # Check without saving, mark your own written answers, save one attempt. MCQ answers go as the choice's text.
    lid = 987654  # a stubbed lecture, so this runs on its own
    qs = [{"id": 1, "idx": 0, "kind": "mcq", "question": "Which scope rule does `bash` use?", "choices": ["Static", "Dynamic", "<b>Both</b>"], "t": 30.0},
          {"id": 2, "idx": 1, "kind": "short", "question": "What is shadowing?", "choices": [], "t": 60.0},
          {"id": 3, "idx": 2, "kind": "short", "question": "Name a language with static scope.", "choices": [], "t": 90.0}]
    posts, held, mode = [], [], {"fail": False, "hold": False}

    def answer(route):
        body = route.request.post_data_json
        got = {a["id"]: a for a in body["answers"]}
        results = [{"id": 1, "kind": "mcq", "your_answer": got[1]["answer"], "correct": got[1]["answer"] == "Dynamic", "answer": "Dynamic",
                    "explanation": "Said with the `bash` demo.", "t": 30.0}]
        for q in qs[1:]:
            a = got[q["id"]]
            results.append({"id": q["id"], "kind": "short", "your_answer": a["answer"], "correct": a.get("correct") if body.get("check") else bool(a.get("correct")),
                            "answer": "An inner name hiding an outer one.", "explanation": "", "t": q["t"]})
        route.fulfill(json={"results": results, "score": sum(r["correct"] is True for r in results), "total": 3,
                            "unmarked": sum(r["correct"] is None for r in results), "saved": not body.get("check")})

    def quiz_api(route):
        if route.request.method == "GET":
            return route.fulfill(json={"questions": qs, "last_attempt": {"score": 1, "total": 3, "at": "2026-10-27 22:10:00"}})
        posts.append(route.request.post_data_json)
        if mode["fail"]:
            return route.fulfill(status=503, json={"detail": "database is locked"})
        if mode["hold"]:
            return held.append(route)
        answer(route)
    page.route(re.compile(rf"/api/lectures/{lid}$"), lambda r: r.fulfill(json={"id": lid, "title": "Scoping", "course_id": None}))
    page.route(re.compile(rf"/api/lectures/{lid}/quiz$"), quiz_api)
    page.goto(f"{BASE}/#/quiz/{lid}")
    expect(page.locator(".qz-q")).to_have_count(3)
    expect(page.locator(".page-head .sub")).to_contain_text("Last time 1 of 3")
    expect(page.locator(".qz-choice").nth(2)).to_contain_text("<b>Both</b>")  # server text is never HTML
    expect(page.locator("#qz-q-1 code")).to_have_text("bash")
    page.locator(".qz-choice").nth(1).click()
    page.fill("#qz-a-2", "  An inner name hides an outer one.  ")
    expect(page.locator("#qz-count")).to_have_text("2 of 3 answered")
    # typed answers are guarded: cancel stays put, nothing re-renders
    dialogs = []
    page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.click('aside a[href="#/"]')
    expect(page).to_have_url(re.compile(rf"#/quiz/{lid}$"))
    expect(page.locator("#qz-a-2")).to_have_value("  An inner name hides an outer one.  ")
    assert dialogs and "unsaved" in dialogs[0]
    # a check that fails keeps the answers and can be tried again
    mode["fail"] = True
    page.click("#qz-check")
    expect(page.locator("#qz-err")).to_contain_text("weren’t checked: database is locked")
    expect(page.locator("#qz-check")).to_be_enabled()
    expect(page.locator("#qz-a-2")).to_have_value("An inner name hides an outer one.")
    mode["fail"] = False
    page.click("#qz-check")
    # checked, not saved
    expect(page.locator("#qz-sum")).to_contain_text("1 of 3 right so far")
    expect(page.locator("#qz-sum")).to_be_focused()
    assert posts[-1] == {"check": True, "answers": [{"id": 1, "answer": "Dynamic"}, {"id": 2, "answer": "An inner name hides an outer one."},
                                                     {"id": 3, "answer": ""}]}
    expect(page.locator(".qz-choice.is-right .qz-tag")).to_have_text("Your answer, and the right one")
    expect(page.locator("#qz-a-2")).to_be_disabled()
    expect(page.locator("#qz-q-1 .qz-res a")).to_have_attribute("target", "_blank")  # checked work isn't lost to a page load
    # a blank written answer is simply missed: nothing to mark
    expect(page.locator("#qz-q-3 .qz-verdict")).to_have_text("Not answered.")
    expect(page.locator("#qz-q-3 [data-mark]")).to_have_count(0)
    expect(page.locator(".qz-actions .hint")).to_contain_text("1 written answer isn’t marked yet")
    # mark, unmark, mark again
    page.click('#qz-q-2 [data-mark="1"]')
    expect(page.locator('#qz-q-2 [data-mark="1"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('#qz-q-2 [data-mark="1"]')).to_be_focused()
    expect(page.locator("#qz-q-2 .qz-res")).to_have_attribute("data-verdict", "right")
    expect(page.locator("#qz-sum")).to_contain_text("2 of 3 right so far")
    page.click('#qz-q-2 [data-mark="1"]')
    expect(page.locator('#qz-q-2 [data-mark="1"]')).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".qz-actions .hint")).to_have_count(1)
    page.click('#qz-q-2 [data-mark="1"]')
    expect(page.locator(".qz-actions .hint")).to_have_count(0)
    # "Change my answers" keeps them; the marks go
    page.click("#qz-again")
    expect(page.locator("#qz-a-2")).to_be_enabled()
    expect(page.locator("#qz-a-2")).to_have_value("An inner name hides an outer one.")
    expect(page.locator(".qz-choice input").nth(1)).to_be_checked()
    page.click("#qz-check")
    expect(page.locator("#qz-sum")).to_contain_text("1 of 3 right so far")
    page.click('#qz-q-2 [data-mark="1"]')
    # while a save is out, nothing can start a second one
    mode["hold"] = True
    n = len(posts)
    page.click("#qz-save")
    expect(page.locator("#qz-save")).to_be_disabled()
    expect(page.locator("#qz-save")).to_have_text("Saving…")
    expect(page.locator('#qz-q-2 [data-mark="0"]')).to_be_disabled()
    expect(page.locator("#qz-again")).to_be_disabled()
    page.keyboard.press("Enter")
    assert len(posts) == n + 1
    mode["hold"] = False
    answer(held.pop())
    expect(page.locator("#qz-sum")).to_contain_text("Saved: 2 of 3")
    assert "check" not in posts[-1]
    assert posts[-1]["answers"][1:] == [{"id": 2, "answer": "An inner name hides an outer one.", "correct": True},
                                        {"id": 3, "answer": "", "correct": False}]
    expect(page.locator('#qz-q-2 [data-mark="0"]')).to_be_disabled()  # marks are locked once saved
    # saved, so leaving doesn't ask; "Take it again" is a clean quiz
    page.click("#qz-again")
    expect(page.locator("#qz-check")).to_be_visible()
    expect(page.locator("#qz-a-2")).to_have_value("")
    page.click('aside a[href="#/"]')
    expect(page).to_have_url(re.compile(r"#/$"))
    # an older server, and a lecture without a quiz yet
    page.unroute(re.compile(rf"/api/lectures/{lid}/quiz$"))
    page.route(re.compile(rf"/api/lectures/{lid}/quiz$"), lambda r: r.fulfill(status=404, json={"detail": "Not Found"}))
    page.goto(f"{BASE}/#/quiz/{lid}")
    expect(page.locator(".empty h3")).to_have_text("Quizzes aren’t on this server yet")
    page.unroute(re.compile(rf"/api/lectures/{lid}/quiz$"))
    page.route(re.compile(rf"/api/lectures/{lid}/quiz$"), lambda r: r.fulfill(json={"questions": [], "last_attempt": None}))
    page.goto(f"{BASE}/?v=2#/quiz/{lid}")
    expect(page.locator(".empty h3")).to_have_text("This lecture has no quiz yet")
    expect(page.locator(".empty a")).to_have_attribute("href", f"?tab=notes#/lecture/{lid}")


def test_search_ui_against_stubbed_answers(page):
    # Search as you type, grouped by lecture. Only the server's <mark> is HTML; anything else in a snippet is dropped.
    hit = {"lecture_id": 1, "title": "Scoping", "date": "2026-10-16", "code": "COP4020"}
    rows = [{**hit, "kind": "transcript", "t": 638, "snippet_html": "that's <mark>dynamic</mark> <mark>scoping</mark> &lt;b&gt;x&lt;/b&gt; ..."},
            {**hit, "kind": "notes", "t": 410, "snippet_html": "<mark>dynamic</mark> in the notes<img src=x onerror=alert(1)>"},
            {**hit, "lecture_id": 2, "title": "Names", "kind": "slide", "t": None, "snippet_html": "Slide 4: <mark>dynamic</mark>"},
            {**hit, "lecture_id": 2, "title": "Names", "kind": "transcript", "t": 40,
             "snippet_html": "<mark onclick=alert(1)>dynamic</mark> then <mark>unclosed <script>alert(1)</script>"}]
    asked, held = [], []

    def search(route):
        q = parse_qs(urlparse(route.request.url).query)
        asked.append(q)
        if q["q"][0] == "slow":
            return held.append(route)
        if q["q"][0] == "boom":
            return route.fulfill(status=500, json={"detail": "index is rebuilding"})
        route.fulfill(json=[] if q["q"][0] == "zzz" else rows)
    page.route(re.compile(r"/api/search\?"), search)
    page.route(re.compile(r"/api/review(\?|$)"), lambda r: r.fulfill(json={"cards": [], "total": 0, "new": 0, "due": 0, "boxes": [0] * 5}))
    page.goto(f"{BASE}/#/")
    expect(page.locator('#nav a[data-route="search"]')).to_be_visible()
    page.keyboard.press("/")
    expect(page.locator("#sr-q")).to_be_focused()
    page.keyboard.type("dynamic scop", delay=20)
    expect(page.locator(".sr-hit")).to_have_count(4)
    assert len(asked) <= 2, asked  # typing waits for a pause before asking
    expect(page).to_have_url(re.compile(r"q=dynamic\+scop"))
    expect(page.locator(".sr-lec h3")).to_have_text(["Scoping", "Names"])
    # inside a lecture, hits read in time order; matched words next to each other are one match; "..." becomes "…"
    expect(page.locator(".sr-hit").nth(1).locator("mark")).to_have_text("dynamic scoping")
    expect(page.locator(".sr-hit").nth(1)).to_contain_text("<b>x</b> …")
    expect(page.locator("#sr-results img, #sr-results script, #sr-results [onclick]")).to_have_count(0)
    expect(page.locator(".sr-hit").nth(3).locator("mark")).to_have_count(0)  # out-of-order marks are dropped, not left open
    expect(page.locator(".sr-hit a").first).to_have_attribute("href", "?tab=notes&t=410#/lecture/1")
    expect(page.locator(".sr-hit a").nth(1)).to_have_attribute("href", "?t=638#/lecture/1")
    expect(page.locator(".sr-hit a").nth(2)).to_have_attribute("href", "#/lecture/2")  # no time: open the lecture, don't jump to 0:00
    # Back from a result's lecture returns to the same search
    page.click(".sr-lec h3 a >> nth=0")
    expect(page).to_have_url(re.compile(r"/#/lecture/1$"))
    page.go_back()
    expect(page).to_have_url(re.compile(r"q=dynamic\+scop#/search$"))
    expect(page.locator(".sr-hit")).to_have_count(4)
    expect(page.locator("#sr-q")).to_have_value("dynamic scop")
    page.select_option("#sr-course", str(course_id("COP4020")))
    expect(page.locator(".sr-hit")).to_have_count(4)
    assert asked[-1]["course_id"] == [str(course_id("COP4020"))]
    # clearing the box while a search is out shows the tip at once, not dimmed
    page.fill("#sr-q", "slow")
    expect(page.locator("#sr-results")).to_have_attribute("aria-busy", "true")
    page.fill("#sr-q", "")
    expect(page.locator("#sr-results .sr-tip")).to_be_visible()
    expect(page.locator("#sr-results")).not_to_have_attribute("aria-busy", "true")
    for r in held:
        try:
            r.fulfill(json=rows)
        except Exception:
            pass  # the page already gave up on it
    expect(page.locator(".sr-hit")).to_have_count(0)
    page.fill("#sr-q", "zzz")
    expect(page.locator("#sr-results .empty h3")).to_have_text("Nothing matches “zzz”")
    page.fill("#sr-q", "boom")
    expect(page.locator("#sr-results .err")).to_contain_text("index is rebuilding")
    # a link to the results opens them, and the query leaves with the page
    page.goto(f"{BASE}/?q=dynamic#/search")
    expect(page.locator(".sr-hit")).to_have_count(4)
    page.click('aside a[href="#/"]')
    expect(page).not_to_have_url(re.compile(r"q="))


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


def course_id(code):
    return next(c["id"] for c in api("/api/courses") if c["code"] == code)


def test_add_dialog_tab_is_in_the_url(page):
    page.goto(BASE + "/?add=canvas#/")
    expect(page.locator("#add-dlg")).to_be_visible()
    expect(page.locator("[data-tab=canvas]")).to_have_attribute("aria-selected", "true")
    expect(page.locator("[data-tab=canvas]")).to_be_focused()
    page.click("[data-tab=link]")
    expect(page).to_have_url(re.compile(r"[?&]add=link"))
    page.click("#add-cancel")
    expect(page.locator("#add-dlg")).to_be_hidden()
    expect(page).not_to_have_url(re.compile(r"add="))


def test_canvas_tab_follows_the_course_page(page):
    for code in ["COP4020", "COP4533"]:  # the second open must not keep the first one's course
        cid = course_id(code)
        page.goto(f"{BASE}/#/course/{cid}")
        expect(page.locator(".sub .code")).to_have_text(code)
        page.click("#add-btn")
        page.click("[data-tab=canvas]")
        expect(page.locator("#cv-course")).to_have_value(str(cid))
        page.click("#add-cancel")


def test_canvas_pull_keeps_unsaved_vocabulary(page):
    # Canvas isn't connected in tests, so its item list and the pull are stubbed: what's under test is the page.
    cid, pulls = course_id("COP4533"), []
    item = {"key": "kaltura:1_test", "kind": "kaltura", "title": "Week 3 recording", "status": "new",
            "page_url": "", "created_at": "2026-09-14"}
    page.route(re.compile(r"/api/canvas/items\?course_id=\d+"), lambda r: r.fulfill(json=[item]))

    def pull(route):
        pulls.append(route.request.post_data_json)
        route.fulfill(json={"job_ids": []})
    page.route("**/api/canvas/pull", pull)
    page.goto(BASE + "/#/courses")
    vocab = page.locator("[data-course]", has_text="COP4533").locator(".course-vocab")
    vocab.fill("dynamic programming\nunsaved term")
    page.click("#add-btn")
    page.click("[data-tab=canvas]")
    page.select_option("#cv-course", str(cid))
    page.check("[data-cv='kaltura:1_test'] input")
    page.click("#add-submit")
    expect(page.locator("#toast")).to_contain_text("Pulling 0 recording(s) from Canvas")
    expect(page.locator("#add-dlg")).to_be_hidden()
    assert pulls == [{"course_id": cid, "keys": ["kaltura:1_test"]}]
    expect(vocab).to_have_value("dynamic programming\nunsaved term")


def test_waiting_jobs_stay_open_while_jobs_poll(page):
    job = {"error": None, "lecture_id": None, "updated_at": "2026-10-27 03:00:00", "kind": "canvas_pull"}
    jobs = [{**job, "id": 900, "status": "running", "progress": "downloading M2L1 Lexer Introduction", "label": "M2L1 Lexer"},
            *[{**job, "id": 901 + i, "status": "queued", "progress": None, "label": f"M2L{i + 2} Parsing, part {i + 1}"}
              for i in range(3)]]
    page.route("**/api/jobs", lambda r: r.fulfill(json=jobs))
    page.goto(BASE + "/")
    summary = page.locator(".job-queue summary")
    expect(summary).to_contain_text("3 more waiting to be pulled from Canvas")
    summary.click()
    expect(page.locator(".job-queue li")).to_have_count(3)
    jobs[0] = {**jobs[0], "progress": "processing M2L1 Lexer Introduction.mkv"}  # the next poll has news
    for _ in range(2):  # wait inside Playwright: a plain sleep would leave the stubbed route unanswered
        with page.expect_response("**/api/jobs", timeout=15_000):
            pass
    expect(page.locator(".job-detail")).to_have_text("processing M2L1 Lexer Introduction.mkv")
    expect(page.locator(".job-queue")).to_have_attribute("open", "")
    expect(summary).to_be_focused()  # patched in place: the control the user clicked is still there


def test_sweep_links_quiz_marks_and_canvas_status(page):
    # A query typed inside the hash still opens the page, with the query moved before the hash.
    page.goto(f"{BASE}/#/course/987654?tab=notes")
    expect(page.locator("[data-error] h3")).to_have_text("There’s no course 987654")
    expect(page).to_have_url(f"{BASE}/?tab=notes#/course/987654")
    # The right choice is marked the way the server marks it: case and spacing don't matter.
    lid = 987653
    qs = [{"id": 1, "idx": 0, "kind": "mcq", "question": "Which scope rule does bash use?", "choices": ["Static", "Dynamic"], "t": 30.0}]
    graded = {"results": [{"id": 1, "kind": "mcq", "your_answer": "Static", "correct": False, "answer": "  dynamic ",
                           "explanation": "", "t": 30.0}], "score": 0, "total": 1, "unmarked": 0, "saved": False}
    page.route(re.compile(rf"/api/lectures/{lid}$"), lambda r: r.fulfill(json={"id": lid, "title": "Scoping", "course_id": None}))
    page.route(re.compile(rf"/api/lectures/{lid}/quiz$"), lambda r: r.fulfill(
        json={"questions": qs, "last_attempt": None} if r.request.method == "GET" else graded))
    page.goto(f"{BASE}/#/quiz/{lid}")
    page.locator(".qz-choice").first.click()
    page.click("#qz-check")
    expect(page.locator(".qz-choice.is-right")).to_have_text(re.compile("Dynamic"))
    expect(page.locator(".qz-choice.is-wrong")).to_have_text(re.compile("Static"))
    # A Canvas status that fails isn't the server being down.
    page.route("**/api/canvas/status", lambda r: r.fulfill(status=500, json={"detail": "boom"}))
    page.goto(f"{BASE}/#/")
    expect(page.locator("#health")).to_have_text(re.compile("^Server ok"))


@needs_fixture
def test_sweep_text_only_deck_and_offline_notes_button(page):
    lid = next(l for l in api("/api/lectures") if l["title"].startswith("PLC Lecture 1"))["id"]
    # A deck read as text only has no images: no request for /null, and the viewer says why.
    deck = {"id": 7, "title": "Overview.pptx", "source": "upload", "how": "manual", "score": None, "pages": 2, "thumb_w": None, "thumb_h": None}
    slides = [{"no": n, "title": f"Slide title {n}", "text": "words", "first": 0.0, "ranges": [[0.0, 5.0]], "thumb": None, "image": None}
              for n in (1, 2)]
    page.route(f"**/api/lectures/{lid}/slides", lambda r: r.fulfill(json={"state": "aligned", "reason": None, "progress": None,
               "error": None, "deck": deck, "slides": slides, "candidates": []}))
    page.route(f"**/api/lectures/{lid}/segments", lambda r: r.fulfill(json=[{**s, "slide_no": 1 if s["idx"] % 2 else 2}
                                                                            for s in r.fetch().json()]))
    nulls = []
    page.on("request", lambda r: r.url.endswith("/null") and nulls.append(r.url))
    page.goto(f"{BASE}/?tab=transcript&slide=2#/lecture/{lid}")
    expect(page.locator(".slide-mark .missing").first).to_contain_text("text only, no image")
    expect(page.locator("#slide-dlg")).to_be_visible()
    expect(page.locator("#slide-dlg-missing")).to_contain_text("text only")
    expect(page.locator("#slide-dlg-img")).to_be_hidden()
    page.keyboard.press("Escape")
    assert not nulls

    # Replacing Claude's notes with offline ones puts the button's label back once it's done.
    def notes_api(route):
        if route.request.method == "POST" and not route.request.post_data_json.get("force"):
            return route.fulfill(status=409, json={"detail": "These notes were written by Claude."})
        route.fulfill(json={"state": "none"})
    page.route(f"**/api/lectures/{lid}/notes", notes_api)
    page.goto(f"{BASE}/?tab=notes#/lecture/{lid}")
    page.click("#nt-offline")
    page.click("#confirm-ok")
    expect(page.locator("#toast")).to_contain_text("Writing offline notes")
    expect(page.locator("#nt-offline")).to_have_text("Write offline notes")
    expect(page.locator("#nt-offline")).to_be_enabled()


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
